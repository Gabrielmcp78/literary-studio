"""Read-only Git worktree snapshotting for project-scoped manuscript imports."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from studio_db import is_repository_manuscript_path, _natural_path_key


class SnapshotError(ValueError):
    pass


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "").strip() or str(exc)
        raise SnapshotError(f"Git worktree inspection failed: {detail}") from exc
    return result.stdout.strip()


def _repo_root(source_root: str) -> Path:
    candidate = Path(source_root).expanduser().resolve()
    if not candidate.is_dir():
        raise SnapshotError("The source checkout path is not a directory.")
    try:
        root = Path(_git(candidate, "rev-parse", "--show-toplevel")).resolve()
    except SnapshotError as exc:
        raise SnapshotError("The selected folder is not inside a Git worktree.") from exc
    if not root.is_dir():
        raise SnapshotError("Git reported an invalid worktree root.")
    return root


def _scope_prefix(root: Path, source_subpath: str | None) -> str:
    if not source_subpath or not source_subpath.strip():
        return ""
    raw = Path(source_subpath.strip()).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    scope = candidate.resolve()
    if scope != root and root not in scope.parents:
        raise SnapshotError("The manuscript subdirectory must be inside the Git worktree.")
    if not scope.is_dir():
        raise SnapshotError("The selected manuscript subdirectory is not a directory.")
    return scope.relative_to(root).as_posix()


def _candidate_paths(root: Path, scope_prefix: str = "") -> list[str]:
    raw = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "*.md",
        "*.markdown",
    )
    paths = [
        path
        for path in raw.split("\0")
        if path
        and is_repository_manuscript_path(path)
        and (not scope_prefix or path == scope_prefix or path.startswith(f"{scope_prefix}/"))
    ]
    return sorted(set(paths), key=_natural_path_key)


def build_snapshot(
    project_id: str,
    title: str,
    source_root: str,
    source_subpath: str | None = None,
) -> dict[str, Any]:
    root = _repo_root(source_root)
    scope_prefix = _scope_prefix(root, source_subpath)
    branch = _git(root, "branch", "--show-current") or "(detached HEAD)"
    commit_sha = _git(root, "rev-parse", "HEAD")
    dirty = bool(_git(root, "status", "--porcelain"))
    paths = _candidate_paths(root, scope_prefix)
    if not paths:
        raise SnapshotError("No Act/Chapter Markdown files were found in the worktree.")

    digest = hashlib.sha256()
    files: list[dict[str, str | int]] = []
    total_bytes = 0
    snapshot_sections: list[str] = []
    for relative_path in paths:
        file_path = (root / relative_path).resolve()
        if root not in file_path.parents:
            raise SnapshotError(f"Refusing a path outside the worktree: {relative_path}")
        try:
            content_bytes = file_path.read_bytes()
        except OSError as exc:
            raise SnapshotError(f"Unable to read {relative_path}: {exc}") from exc
        if len(content_bytes) > MAX_FILE_BYTES:
            raise SnapshotError(f"{relative_path} exceeds the 10 MB chapter limit.")
        total_bytes += len(content_bytes)
        if total_bytes > MAX_TOTAL_BYTES:
            raise SnapshotError("The manuscript snapshot exceeds the 100 MB total limit.")
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_bytes)
        content = content_bytes.decode("utf-8", errors="replace").rstrip()
        snapshot_sections.append(f"<!-- source: {relative_path} -->\n\n{content}\n")
        files.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(content_bytes).hexdigest(),
                "bytes": len(content_bytes),
            }
        )

    source_digest = digest.hexdigest()
    manifest = {
        "project_id": project_id,
        "title": title,
        "source_root": str(root),
        "source_subpath": scope_prefix or None,
        "branch": branch,
        "commit_sha": commit_sha,
        "working_tree_dirty": dirty,
        "source_digest": source_digest,
        "files": files,
    }
    frontmatter = "\n".join(
        [
            "---",
            f"project_id: {project_id}",
            f"title: {title}",
            f"source_subpath: {scope_prefix or None}",
            f"branch: {branch}",
            f"commit_sha: {commit_sha}",
            f"working_tree_dirty: {str(dirty).lower()}",
            f"source_digest: {source_digest}",
            "---",
            "",
        ]
    )
    return {
        "manifest": manifest,
        "markdown": frontmatter + "\n".join(snapshot_sections),
        "files": [
            {"path": item["path"], "text": (root / str(item["path"])).read_text(encoding="utf-8", errors="replace")}
            for item in files
        ],
    }


def write_snapshot(snapshot: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "manuscript.snapshot.md"
    manifest_path = output_dir / "manuscript.manifest.json"
    snapshot_path.write_text(snapshot["markdown"], encoding="utf-8")
    manifest_path.write_text(json.dumps(snapshot["manifest"], indent=2) + "\n", encoding="utf-8")
    return snapshot_path, manifest_path
