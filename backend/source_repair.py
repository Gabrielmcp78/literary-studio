"""Source-pinned Markdown repair for one manuscript file at a time.

The repair surface is deliberately separate from the normal scene editor. It
lets the author classify the complete source file with the same semantic
styles used by the editor, then re-imports only that file after an exact
content-hash check. No other manuscript file is rewritten.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from compiler_engine import html_to_markdown
from governance import GovernanceError, _atomic_write, repo_root
from studio_db import (
    StudioDB,
    _markdown_scenes,
    assign_block_ids,
    is_project_local_scratch_path,
    is_repository_manuscript_path,
)


class SourceRepairError(ValueError):
    """A source repair request cannot be applied safely."""

    def __init__(self, message: str, code: str = "source_repair_error") -> None:
        super().__init__(message)
        self.code = code


def _source_scope(db: StudioDB) -> tuple[dict[str, Any], Path, Path]:
    project = db.project_info()
    try:
        root = repo_root(project.get("source_root"))
    except GovernanceError as exc:
        raise SourceRepairError(str(exc), getattr(exc, "code", "no_source")) from exc
    scope = (root / str(project.get("source_subpath") or "")).resolve()
    if root not in scope.parents and scope != root:
        raise SourceRepairError("The configured manuscript scope is outside its Git worktree.", "invalid_scope")
    if not scope.is_dir():
        raise SourceRepairError("The configured manuscript scope is not available.", "invalid_scope")
    return project, root, scope


def _safe_source_path(db: StudioDB, relative_path: str) -> tuple[dict[str, Any], Path, str]:
    project, root, scope = _source_scope(db)
    relative = str(relative_path or "").replace("\\", "/").strip("/")
    if not relative or Path(relative).suffix.casefold() not in {".md", ".markdown"}:
        raise SourceRepairError("Choose a Markdown manuscript file.", "invalid_path")
    if is_project_local_scratch_path(relative):
        raise SourceRepairError("Scratch material is Studio-owned and cannot be opened as Git source.", "scratch_not_source")
    candidate = (root / relative).resolve()
    if root not in candidate.parents or scope not in candidate.parents or not candidate.is_file():
        raise SourceRepairError("That file is outside the linked manuscript scope.", "invalid_path")
    return project, candidate, candidate.relative_to(root).as_posix()


def _frontmatter_and_body(text: str) -> tuple[str, str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"^---\s*\n.*?\n---\s*\n", normalized, flags=re.DOTALL)
    if not match:
        return "", normalized
    return match.group(0), normalized[match.end() :]


def _render_source_body(text: str) -> str:
    from studio_db import _render_markdown_body

    return assign_block_ids(_render_markdown_body(text))


def list_source_repair_files(db: StudioDB) -> list[dict[str, Any]]:
    project, root, scope = _source_scope(db)
    files: list[dict[str, Any]] = []
    known: dict[str, list[dict[str, Any]]] = {}
    for scene in db.list_scenes():
        path = str(scene.get("source_path") or "").replace("\\", "/").strip("/")
        project_prefix = f"{project['project_id']}/"
        if path.startswith(project_prefix):
            path = path[len(project_prefix) :]
        known.setdefault(path, []).append(scene)
    for path in sorted(scope.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in {".md", ".markdown"}:
            continue
        relative = path.relative_to(root).as_posix()
        if not is_repository_manuscript_path(relative):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        scenes = sorted(known.get(relative, []), key=lambda item: (item.get("scene_number_in_chapter") or 999999, item["scene_number"]))
        files.append({
            "path": relative,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "scene_count": len(scenes),
            "scene_numbers": [scene["scene_number"] for scene in scenes],
            "chapter_number": scenes[0].get("chapter_number") if scenes else None,
            "chapter_title": scenes[0].get("chapter_title") if scenes else None,
        })
    return files


def read_source_repair_file(db: StudioDB, relative_path: str) -> dict[str, Any]:
    _project, path, relative = _safe_source_path(db, relative_path)
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    frontmatter, body = _frontmatter_and_body(text)
    return {
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "raw_text": text,
        "frontmatter": frontmatter,
        "content_html": _render_source_body(body),
    }


def recompile_source_repair_file(
    db: StudioDB,
    *,
    relative_path: str,
    expected_sha256: str,
    content_html: str,
    frontmatter: str = "",
) -> dict[str, Any]:
    project, path, relative = _safe_source_path(db, relative_path)
    current_bytes = path.read_bytes()
    actual_sha256 = hashlib.sha256(current_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise SourceRepairError(
            "The source file changed after it was opened. Reload it before recompiling.",
            "source_changed",
        )

    body = html_to_markdown(content_html or "").strip()
    if not body:
        raise SourceRepairError("The repaired file cannot be empty.", "empty_source")
    prefix = str(frontmatter or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    updated_text = f"{prefix}\n\n{body}\n" if prefix else f"{body}\n"
    sections = _markdown_scenes(relative, updated_text)
    if not sections:
        raise SourceRepairError("The repaired file contains no importable manuscript text.", "no_sections")

    updated_bytes = updated_text.encode("utf-8")
    _atomic_write(path, updated_bytes)
    try:
        scenes, removed_scene_numbers = db.replace_imported_file(relative, sections)
    except Exception:
        # Restore the exact source bytes if the project index cannot be updated.
        _atomic_write(path, current_bytes)
        raise

    return {
        "project_id": project["project_id"],
        "path": relative,
        "sha256_before": actual_sha256,
        "sha256_after": hashlib.sha256(updated_bytes).hexdigest(),
        "scenes": scenes,
        "removed_scene_numbers": removed_scene_numbers,
        "source_root": str(path.parent),
    }
