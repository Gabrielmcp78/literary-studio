"""Author-controlled full-desk synchronization to a linked Git worktree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from compiler_engine import html_to_markdown
from governance import GovernanceError, _atomic_write, _git, _sha256, repo_root, source_file
from manual_sync import _unsafe_scene_deletion, scene_source_span
from studio_db import StudioDB, utc_now


def build_desk_push_plan(
    db: StudioDB,
    *,
    allow_unsafe: bool = False,
    override_dirty: bool = False,
) -> dict[str, Any]:
    """Build a complete source-file plan from the current Studio scene store.

    The plan is intentionally source-shaped: Markdown frontmatter, chapter
    headings, scene headings, and unrelated files remain untouched. Only the
    body span belonging to a saved Studio scene is replaced.
    """
    project = db.project_info()
    root = repo_root(project.get("source_root"))
    branch = _git(root, "branch", "--show-current") or None
    dirty = _git(root, "status", "--porcelain")
    dirty_files = [line[3:] if len(line) >= 4 else line for line in dirty.splitlines() if line]
    if dirty_files and not override_dirty:
        raise GovernanceError(
            "The linked source checkout is already dirty. Review or commit those files before a full desk push: "
            + ", ".join(dirty_files[:8]),
            "source_dirty",
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for scene in db.list_scenes():
        located = source_file(project, scene)
        if located is None:
            continue
        path, relative = located
        grouped.setdefault(relative, []).append({"scene": scene, "path": path})

    files: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for relative, entries in sorted(grouped.items()):
        path = root / relative
        original_bytes = path.read_bytes()
        original_text = original_bytes.decode("utf-8", errors="replace")
        replacements: list[tuple[int, int, str, dict[str, Any]]] = []
        for entry in sorted(
            entries,
            key=lambda item: (
                item["scene"].get("scene_number_in_chapter") or 999999,
                item["scene"]["scene_number"],
            ),
        ):
            scene = entry["scene"]
            span = scene_source_span(original_text, scene)
            if span is None:
                blocked.append({
                    "scene_number": scene["scene_number"],
                    "source_path": relative,
                    "reason": "The imported scene boundary could not be located safely in the live Markdown file.",
                })
                continue
            start, end, source_body = span
            studio_body = html_to_markdown(scene["content_html"]).strip()
            if source_body == studio_body:
                continue
            hunks = [{"operation": "replace", "original": source_body, "replacement": studio_body}]
            safety_errors = _unsafe_scene_deletion(source_body, studio_body, hunks)
            if safety_errors and not allow_unsafe:
                blocked.append({
                    "scene_number": scene["scene_number"],
                    "source_path": relative,
                    "reason": " ".join(safety_errors),
                    "source_words": len(source_body.split()),
                    "studio_words": len(studio_body.split()),
                })
                continue
            replacements.append((start, end, studio_body, scene))

        if not replacements:
            continue
        replacements.sort(key=lambda item: item[0])
        if any(replacements[index][1] > replacements[index + 1][0] for index in range(len(replacements) - 1)):
            raise GovernanceError(
                f"Scene boundaries overlap in {relative}; no source file was changed.",
                "scene_boundary_overlap",
            )
        updated_text = original_text
        for start, end, replacement, _scene in reversed(replacements):
            updated_text = updated_text[:start] + replacement + updated_text[end:]
        updated_bytes = updated_text.encode("utf-8")
        if updated_bytes == original_bytes:
            continue
        files.append({
            "path": relative,
            "absolute_path": str(path),
            "sha_before": _sha256(original_bytes),
            "sha_after": _sha256(updated_bytes),
            "previous_bytes": original_bytes,
            "updated_bytes": updated_bytes,
            "scene_numbers": [scene["scene_number"] for *_rest, scene in replacements],
            "scene_count": len(replacements),
        })

    return {
        "project_id": project["project_id"],
        "source_root": str(root),
        "branch": branch,
        "files": files,
        "blocked": blocked,
        "allow_unsafe": allow_unsafe,
        "override_dirty": override_dirty,
        "dirty_files": dirty_files,
        "built_at": utc_now(),
    }


def push_desk_to_git(
    db: StudioDB,
    *,
    allow_unsafe: bool = False,
    override_dirty: bool = False,
) -> dict[str, Any]:
    """Write the safe plan, commit it atomically, and return audit metadata."""
    plan = build_desk_push_plan(
        db,
        allow_unsafe=allow_unsafe,
        override_dirty=override_dirty,
    )
    if plan["blocked"] and not plan["files"]:
        return {
            "status": "blocked",
            "committed": False,
            "project_id": plan["project_id"],
            "branch": plan["branch"],
            "files": [],
            "blocked": plan["blocked"],
            "override_dirty": override_dirty,
            "dirty_files": plan["dirty_files"],
        }
    if not plan["files"]:
        return {
            "status": "clean",
            "committed": False,
            "project_id": plan["project_id"],
            "branch": plan["branch"],
            "files": [],
            "blocked": plan["blocked"],
            "message": "The saved Studio desk already matches the linked Markdown source.",
            "override_dirty": override_dirty,
            "dirty_files": plan["dirty_files"],
        }

    root = Path(plan["source_root"])
    written: list[dict[str, Any]] = []
    try:
        for item in plan["files"]:
            _atomic_write(Path(item["absolute_path"]), item["updated_bytes"])
            written.append(item)
        message = (
            f"Literary Studio: full desk sync ({sum(item['scene_count'] for item in written)} scenes, "
            f"{len(written)} Markdown file{'s' if len(written) != 1 else ''})"
        )
        _git(root, "commit", "--only", "-m", message, "--", *[item["path"] for item in written])
        commit_sha = _git(root, "rev-parse", "HEAD")
    except Exception:
        for item in reversed(written):
            _atomic_write(Path(item["absolute_path"]), item["previous_bytes"])
        raise

    return {
        "status": "committed",
        "committed": True,
        "project_id": plan["project_id"],
        "branch": plan["branch"],
        "commit_sha": commit_sha,
        "files": [
            {
                "path": item["path"],
                "scene_numbers": item["scene_numbers"],
                "scene_count": item["scene_count"],
                "sha_before": item["sha_before"],
                "sha_after": item["sha_after"],
            }
            for item in written
        ],
        "blocked": plan["blocked"],
        "allow_unsafe": allow_unsafe,
        "override_dirty": override_dirty,
        "dirty_files": plan["dirty_files"],
        "warning": (
            "Dirty-tree override was used. Only saved Studio desk files were included in this commit; "
            "other dirty files remain untouched."
            if override_dirty and plan["dirty_files"]
            else None
        ),
    }
