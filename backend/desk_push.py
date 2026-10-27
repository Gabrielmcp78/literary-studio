"""Author-controlled full-desk synchronization to a linked Git worktree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from compiler_engine import html_to_markdown
from governance import GovernanceError, _atomic_write, _git, _sha256, repo_root, source_file
from manual_sync import _unsafe_scene_deletion, scene_source_span
from source_snapshot import _candidate_paths, _scope_prefix
from studio_db import StudioDB, _markdown_scenes, utc_now


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
    grouped: dict[str, list[dict[str, Any]]] = {}
    for scene in db.list_scenes():
        located = source_file(project, scene)
        if located is None:
            continue
        path, relative = located
        grouped.setdefault(relative, []).append({"scene": scene, "path": path})

    # Scope dirty check strictly to manuscript files, ignoring build artifacts, scratch notes, and OS junk
    manuscript_files = list(grouped.keys())
    subpath = (project.get("source_subpath") or "").strip()
    status_args = ["status", "--porcelain"]
    if subpath:
        status_args.extend(["--", subpath])
    elif manuscript_files:
        status_args.extend(["--", *manuscript_files])
    dirty = _git(root, *status_args)
    dirty_files = [
        line[3:].strip()
        for line in dirty.splitlines()
        if len(line) >= 4
        and not line[3:].strip().endswith(".DS_Store")
        and not any(x in line for x in ("build_artifacts", "scratch/", ".runtime"))
    ]
    if dirty_files and not override_dirty:
        raise GovernanceError(
            "The linked source checkout has uncommitted edits in manuscript files. Review or commit those files before a full desk push: "
            + ", ".join(dirty_files[:8]),
            "source_dirty",
        )

    # Git HEAD advance detection: prevent silent overwriting of external commits
    last_synced_sha = project.get("commit_sha")
    current_sha = _git(root, "rev-parse", "HEAD")
    if last_synced_sha and current_sha != last_synced_sha and not override_dirty and manuscript_files:
        diff = _git(root, "diff", "--name-only", f"{last_synced_sha}..{current_sha}", "--", *manuscript_files)
        if diff.strip():
            diff_lines = [f for f in diff.splitlines() if f.strip()]
            raise GovernanceError(
                f"The linked Git repository has newer commits ({current_sha[:8]}) touching manuscript files ({', '.join(diff_lines[:3])}) that are not synced to Literary Studio. Pull changes from Git before pushing.",
                "git_advanced",
            )

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


def commit_scene_to_git(
    db: StudioDB,
    scene_number: int,
    *,
    message: str | None = None,
    override_dirty: bool = False,
) -> dict[str, Any]:
    """Author direct commit of a single scene directly to Git without proposal batching."""
    project = db.project_info()
    root = repo_root(project.get("source_root"))
    scene = db.get_scene(scene_number)
    if not scene:
        raise GovernanceError(f"Scene {scene_number} not found.", "scene_not_found")

    located = source_file(project, scene)
    if located is None:
        raise GovernanceError(
            f"Scene {scene_number} has no linked Markdown file in the repository.",
            "no_source_file",
        )
    path, relative = located

    # Dirty check for other manuscript files if not override_dirty
    subpath = (project.get("source_subpath") or "").strip()
    status_args = ["status", "--porcelain"]
    if subpath:
        status_args.extend(["--", subpath])
    dirty = _git(root, *status_args)
    dirty_files = [
        line[3:].strip()
        for line in dirty.splitlines()
        if len(line) >= 4
        and not line[3:].strip().endswith(".DS_Store")
        and not any(x in line for x in ("build_artifacts", "scratch/", ".runtime"))
        and line[3:].strip() != relative
    ]
    if dirty_files and not override_dirty:
        raise GovernanceError(
            "The linked source checkout has uncommitted edits in other manuscript files. "
            "Review or commit those files before direct scene commit: " + ", ".join(dirty_files[:8]),
            "source_dirty",
        )

    # Git HEAD advance detection: prevent silent overwriting of external commits
    last_synced_sha = project.get("commit_sha")
    current_sha = _git(root, "rev-parse", "HEAD")
    if last_synced_sha and current_sha != last_synced_sha and not override_dirty:
        diff = _git(root, "diff", "--name-only", f"{last_synced_sha}..{current_sha}", "--", relative)
        if diff.strip():
            raise GovernanceError(
                f"The linked Git repository has newer commits touching {relative}. Pull changes from Git before committing.",
                "git_advanced",
            )

    original_bytes = path.read_bytes()
    original_text = original_bytes.decode("utf-8", errors="replace")
    span = scene_source_span(original_text, scene)
    if span is None:
        raise GovernanceError(
            f"The imported boundary for scene {scene_number} could not be located safely in {relative}.",
            "boundary_not_found",
        )
    start, end, source_body = span
    studio_body = html_to_markdown(scene["content_html"]).strip()

    if source_body == studio_body and not _git(root, "status", "--porcelain", "--", relative).strip():
        return {
            "status": "clean",
            "committed": False,
            "project_id": project["project_id"],
            "scene_number": scene_number,
            "path": relative,
            "commit_sha": current_sha,
            "branch": _git(root, "branch", "--show-current") or None,
            "message": "Scene is already committed and matches Git.",
        }

    updated_text = original_text[:start] + studio_body + original_text[end:]
    updated_bytes = updated_text.encode("utf-8")

    commit_msg = message or f"Literary Studio: author direct commit for scene {scene_number} ({scene.get('title') or 'Untitled'})"
    _atomic_write(path, updated_bytes)
    try:
        _git(root, "commit", "--only", "-m", commit_msg, "--", relative)
        commit_sha = _git(root, "rev-parse", "HEAD")
    except Exception as exc:
        _atomic_write(path, original_bytes)
        raise GovernanceError(f"Git commit failed: {exc}", "commit_failed") from exc

    branch = _git(root, "branch", "--show-current") or None
    return {
        "status": "committed",
        "committed": True,
        "project_id": project["project_id"],
        "scene_number": scene_number,
        "path": relative,
        "commit_sha": commit_sha,
        "branch": branch,
    }


def pull_desk_from_git(
    db: StudioDB,
    *,
    pull_remote: bool = False,
    override_dirty: bool = False,
) -> dict[str, Any]:
    """Pull source markdown changes from Git into StudioDB non-destructively."""
    project = db.project_info()
    root = repo_root(project.get("source_root"))

    # Scope dirty check strictly to manuscript files
    subpath = (project.get("source_subpath") or "").strip()
    status_args = ["status", "--porcelain"]
    if subpath:
        status_args.extend(["--", subpath])
    dirty = _git(root, *status_args)
    dirty_files = [
        line[3:].strip()
        for line in dirty.splitlines()
        if len(line) >= 4
        and not line[3:].strip().endswith(".DS_Store")
        and not any(x in line for x in ("build_artifacts", "scratch/", ".runtime"))
    ]
    if dirty_files and not override_dirty:
        raise GovernanceError(
            "Cannot pull from Git: the linked source checkout has uncommitted edits in manuscript files. "
            "Review, commit, or stash these files before pulling: " + ", ".join(dirty_files[:8]),
            "source_dirty",
        )

    if pull_remote:
        remotes = _git(root, "remote").split()
        if remotes:
            _git(root, "pull")

    scope_prefix = _scope_prefix(root, project.get("source_subpath"))
    paths = _candidate_paths(root, scope_prefix)

    updated_files: list[dict[str, Any]] = []
    total_scenes = 0
    for rel_path in paths:
        full_path = root / rel_path
        if not full_path.exists() or not full_path.is_file():
            continue
        text = full_path.read_text(encoding="utf-8", errors="replace")
        sections = _markdown_scenes(rel_path, text)
        if sections:
            updated, _ = db.replace_imported_file(
                rel_path, sections, source="git_pull", summary=f"Synced from Git: {rel_path}"
            )
            updated_files.append({"path": rel_path, "scene_count": len(updated)})
            total_scenes += len(updated)

    current_sha = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current") or None

    return {
        "status": "pulled",
        "project_id": project["project_id"],
        "branch": branch,
        "commit_sha": current_sha,
        "files_updated": updated_files,
        "total_scenes": total_scenes,
    }
