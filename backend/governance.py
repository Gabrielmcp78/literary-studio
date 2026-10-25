"""Proposal governance: validation, per-passage conflict checks, and Git write-back.

Three guarantees live here:

1. A proposal reaches the Approval Desk only if it can be applied exactly:
   its original text appears once, within one paragraph, outside inline
   formatting; its replacement keeps quotes and brackets balanced; and it
   introduces no prose-law errors (FictionForce's deterministic checker).
2. Staleness is judged per passage, not per scene or per manuscript. Accepting
   one proposal no longer blocks its siblings, and editing chapter 20 no longer
   blocks a scene-4 batch. A proposal is stale only when its own original text
   is gone or ambiguous, or the branch changed.
3. Accepting writes the change back to the Markdown source on the pinned
   branch. Each batch becomes one Git commit when its last proposal is
   reviewed. Source files carrying the author's own uncommitted edits are
   never touched, so the commit never sweeps up work-in-progress.

Imports
- hashlib: SHA-256 file fingerprints. https://docs.python.org/3/library/hashlib.html
- sys: register the path-loaded prose_law module (dataclasses require it).
  https://docs.python.org/3/library/sys.html#sys.modules
- importlib.util: load FictionForce's prose_law module by path, so both tools
  enforce the same rules from one source.
  https://docs.python.org/3/library/importlib.html#importlib.util.spec_from_file_location
- json: commit-message trailers and ledger encoding. https://docs.python.org/3/library/json.html
- os: environment overrides and atomic file replacement (os.replace).
  https://docs.python.org/3/library/os.html#os.replace
- shutil.copymode: keep the source file's permissions on atomic rewrite.
  https://docs.python.org/3/library/shutil.html#shutil.copymode
- subprocess: run git (status, commit, rev-parse) without a shell.
  https://docs.python.org/3/library/subprocess.html
- tempfile: same-directory temp file for atomic writes.
  https://docs.python.org/3/library/tempfile.html
- pathlib.Path: path containment checks. https://docs.python.org/3/library/pathlib.html
- source_snapshot.build_snapshot: live per-file fingerprints of the worktree (local module).
- studio_db: scene text rendering and unique HTML anchoring (local module).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from source_snapshot import SnapshotError, build_snapshot
from studio_db import StudioDB, html_to_text, unique_html_anchor, utc_now


FICTIONFORCE_ROOT = Path(
    os.getenv("FICTIONFORCE_ROOT", "/Volumes/Ready500/DEVELOPMENT/fiction-git-system")
)
GIT_TIMEOUT_SECONDS = 60
BALANCED_PAIRS = (("“", "”"), ("(", ")"), ("[", "]"))


class GovernanceError(ValueError):
    """A governance rule blocked an action. ``code`` is stable for clients."""

    def __init__(self, message: str, code: str = "governance_error") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Git and file helpers
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stripped stdout.

    Failure: GovernanceError(code="git_failed") with git's stderr, including
    hook failures, missing identity, or a timeout (e.g. a GPG prompt).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GovernanceError(f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s.", "git_failed") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)).strip()
        raise GovernanceError(f"git {args[0]} failed: {detail}", "git_failed") from exc
    return result.stdout.strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repo_root(source_root: str | None) -> Path:
    """Resolve the Git worktree root for a project's source_root.

    Failure: GovernanceError(code="no_source") when unset or not a Git worktree.
    """
    if not source_root:
        raise GovernanceError("This project has no source checkout.", "no_source")
    candidate = Path(source_root).expanduser().resolve()
    if not candidate.is_dir():
        raise GovernanceError(f"Source checkout {candidate} is not available.", "no_source")
    return Path(_git(candidate, "rev-parse", "--show-toplevel")).resolve()


def source_file(project: dict[str, Any], scene: dict[str, Any]) -> tuple[Path, str] | None:
    """Return (absolute path, repo-relative path) of a scene's Markdown source, or None."""
    relative = (scene.get("source_path") or "").strip()
    if not relative:
        return None
    try:
        root = repo_root(project.get("source_root"))
    except GovernanceError:
        return None
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path, relative


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically, preserving permissions."""
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        shutil.copymode(path, temp_name)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def current_source_state(db: StudioDB) -> dict[str, Any]:
    """Read live Git provenance and per-file fingerprints without changing the checkout.

    Returns branch, commit_sha, source_digest, working_tree_dirty, read_status,
    and ``file_digests`` (repo-relative path -> sha256). Falls back to the
    imported snapshot's metadata when the source cannot be read.
    """
    project = db.project_info()
    state: dict[str, Any] = {
        "source_root": project.get("source_root"),
        "source_subpath": project.get("source_subpath"),
        "branch": project.get("branch"),
        "commit_sha": project.get("commit_sha"),
        "source_digest": project.get("source_digest"),
        "working_tree_dirty": bool(project.get("working_tree_dirty")),
        "read_status": "project_snapshot",
        "file_digests": {},
    }
    source_root = project.get("source_root")
    if not source_root:
        return state
    try:
        snapshot = build_snapshot(project["project_id"], project["title"], source_root, project.get("source_subpath"))
    except SnapshotError as exc:
        state["read_status"] = "source_unavailable"
        state["read_error"] = str(exc)
        return state
    manifest = snapshot["manifest"]
    state.update(
        {
            "branch": manifest["branch"],
            "commit_sha": manifest["commit_sha"],
            "source_digest": manifest["source_digest"],
            "working_tree_dirty": manifest["working_tree_dirty"],
            "read_status": "live_worktree",
            "file_digests": {item["path"]: item["sha256"] for item in manifest["files"]},
        }
    )
    return state


# --------------------------------------------------------------------------- #
# Prose law (shared with FictionForce)
# --------------------------------------------------------------------------- #

_prose_law_module: Any = None
_prose_law_loaded = False
_prose_law_error: str | None = None


def prose_law() -> Any | None:
    """Load FictionForce's prose_law module once; None if it is unavailable.

    Set LITERARY_STUDIO_PROSE_LAW=off to disable enforcement.
    """
    global _prose_law_module, _prose_law_loaded, _prose_law_error
    if os.getenv("LITERARY_STUDIO_PROSE_LAW", "on").casefold() == "off":
        return None
    if not _prose_law_loaded:
        _prose_law_loaded = True
        path = FICTIONFORCE_ROOT / "fiction_git_system" / "tools" / "prose_law.py"
        if path.is_file():
            name = "fictionforce_prose_law"
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                # dataclasses resolve their module through sys.modules during exec.
                sys.modules[name] = module
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                _prose_law_module = module
            except Exception as exc:  # an unloadable checker must not block the desk
                sys.modules.pop(name, None)
                _prose_law_error = f"{type(exc).__name__}: {exc}"
                _prose_law_module = None
    return _prose_law_module


def prose_law_status() -> dict[str, Any]:
    module = prose_law()
    return {
        "enabled": module is not None,
        "source": str(FICTIONFORCE_ROOT / "fiction_git_system" / "tools" / "prose_law.py") if module else None,
        "error": _prose_law_error,
    }


# --------------------------------------------------------------------------- #
# Validation at preparation time
# --------------------------------------------------------------------------- #


def _balance_problems(original: str, replacement: str) -> list[str]:
    """Pairs whose open/close balance the replacement changes."""
    problems = [
        f"{opener}{closer}"
        for opener, closer in BALANCED_PAIRS
        if original.count(opener) - original.count(closer)
        != replacement.count(opener) - replacement.count(closer)
    ]
    if original.count('"') % 2 != replacement.count('"') % 2:
        problems.append('""')
    return problems


def validate_candidate(project: dict[str, Any], scene: dict[str, Any], original: str, replacement: str) -> dict[str, Any]:
    """Decide whether a candidate proposal may enter the desk.

    Parameters: project - project_info(); scene - the target scene;
    original/replacement - the proposed span and its replacement.
    Returns {errors, warnings, prose_law, writeback, source_path}. Any error
    means the candidate is rejected before staging. ``writeback.available``
    says whether accepting can also patch the Markdown source.
    Failure: none raised; problems are reported in the result.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if "\n" in original or "\n\n" in replacement:
        errors.append("The change crosses a paragraph boundary; proposals must stay inside one paragraph.")
    scene_text = html_to_text(scene.get("content_html", ""))
    occurrences = scene_text.count(original)
    if occurrences == 0:
        errors.append("The original text was not found in the scene.")
    elif occurrences > 1:
        errors.append(f"The original text appears {occurrences} times in the scene; it must be unique.")
    elif unique_html_anchor(scene.get("content_html", ""), original) is None:
        errors.append("The original text spans italic, bold, or link formatting and cannot be replaced exactly.")
    unbalanced = _balance_problems(original, replacement)
    if unbalanced:
        errors.append(f"The replacement unbalances {', '.join(unbalanced)}.")
    if replacement != replacement.strip() and original == original.strip():
        warnings.append("The replacement adds leading or trailing whitespace.")

    writeback: dict[str, Any] = {"available": False, "reason": "No source file is linked to this scene."}
    located = source_file(project, scene)
    source_text: str | None = None
    if located:
        path, relative = located
        source_text = path.read_bytes().decode("utf-8", errors="replace")
        count = source_text.count(original)
        if count == 1:
            writeback = {"available": True, "path": relative}
        elif count == 0:
            writeback = {
                "available": False,
                "path": relative,
                "reason": "The original text is not in the Markdown source as written (Markdown emphasis inside it, or the source has moved on). Accepting can only update the Studio copy.",
            }
        else:
            writeback = {
                "available": False,
                "path": relative,
                "reason": f"The original text appears {count} times in {relative}; write-back needs it unique in the file.",
            }
        if not writeback["available"]:
            warnings.append(writeback["reason"])

    findings: list[dict[str, Any]] = []
    module = prose_law()
    if module is not None and not errors:
        before = source_text if (source_text and writeback["available"]) else scene_text
        after = before.replace(original, replacement, 1)
        for finding in module.new_findings(before, after):
            item = finding.as_dict()
            findings.append(item)
            line = f"Prose law {item['rule']}: {item['message']} ({item['excerpt']})"
            (errors if item["severity"] == "error" else warnings).append(line)

    return {
        "errors": errors,
        "warnings": warnings,
        "prose_law": findings,
        "writeback": writeback,
        "source_path": scene.get("source_path"),
    }


# --------------------------------------------------------------------------- #
# Per-passage conflict checking
# --------------------------------------------------------------------------- #


def check_batch(db: StudioDB, batch_id: str, proposal_id: str | None = None) -> dict[str, Any]:
    """Check staged proposals in a batch against the live Studio scene and source.

    Parameters: proposal_id - limit the check to one proposal (used on accept).
    Behavior: blocking conflicts are branch change, missing scene, or an
    original text that is gone or ambiguous in the Studio scene or (when
    write-back applies) the source file. A changed file whose passage is
    intact is a warning only.
    Failure: GovernanceError(code="not_found") for an unknown batch.
    """
    batch = db.get_approval_batch(batch_id)
    if batch is None:
        raise GovernanceError("Approval batch not found.", "not_found")
    baseline = batch.get("baseline", {})
    current = current_source_state(db)
    project = db.project_info()
    conflicts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if baseline.get("branch") and current.get("branch") and baseline["branch"] != current["branch"]:
        conflicts.append({
            "kind": "branch_changed",
            "expected": baseline["branch"],
            "actual": current["branch"],
            "message": f"The source checkout moved from {baseline['branch']} to {current['branch']}.",
        })
    if baseline.get("commit_sha") and current.get("commit_sha") and baseline["commit_sha"] != current["commit_sha"]:
        warnings.append({
            "kind": "commit_moved",
            "message": "New commits landed since preparation; each passage was re-verified.",
        })

    baseline_files = baseline.get("file_digests", {})
    current_files = current.get("file_digests", {})
    checked: list[str] = []
    for proposal in batch.get("proposals", []):
        if proposal.get("status") != "staged":
            continue
        if proposal_id and proposal["proposal_id"] != proposal_id:
            continue
        checked.append(proposal["proposal_id"])
        pid, number = proposal["proposal_id"], proposal["scene_number"]
        original = proposal["original_verbatim_text"]
        scene = db.get_scene(int(number))
        if scene is None:
            conflicts.append({"kind": "scene_missing", "proposal_id": pid, "scene_number": number,
                              "message": "The scene no longer exists."})
            continue
        count = html_to_text(scene["content_html"]).count(original)
        if count != 1 or unique_html_anchor(scene["content_html"], original) is None:
            conflicts.append({
                "kind": "passage_changed",
                "proposal_id": pid,
                "scene_number": number,
                "occurrences": count,
                "message": "The original text is no longer present exactly once in the Studio scene.",
            })
            continue
        validation = proposal.get("validation") or {}
        relative = scene.get("source_path")
        if validation.get("writeback", {}).get("available") and relative:
            before, after = baseline_files.get(relative), current_files.get(relative)
            if before and after and before != after:
                located = source_file(project, scene)
                source_count = located[0].read_text(encoding="utf-8", errors="replace").count(original) if located else 0
                if source_count == 1:
                    warnings.append({"kind": "file_changed_passage_intact", "proposal_id": pid,
                                     "path": relative, "message": f"{relative} changed, but this passage is intact."})
                else:
                    conflicts.append({"kind": "source_passage_changed", "proposal_id": pid, "path": relative,
                                      "occurrences": source_count,
                                      "message": f"The original text is no longer present exactly once in {relative}."})

    public_current = {k: v for k, v in current.items() if k != "file_digests"}
    return {
        "batch_id": batch_id,
        "safe_to_apply": not conflicts,
        "baseline": {k: v for k, v in baseline.items() if k != "file_digests"},
        "current": public_current,
        "checked_proposals": checked,
        "conflicts": conflicts,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Write-back and per-batch commits
# --------------------------------------------------------------------------- #


def write_back(db: StudioDB, proposal: dict[str, Any], scene: dict[str, Any]) -> dict[str, Any]:
    """Patch the proposal into its Markdown source file on the pinned branch.

    Behavior: verifies branch, that the file has no uncommitted edits other
    than this batch's own earlier write-backs, and that the original text is
    unique in the file; then rewrites atomically.
    Returns {path, absolute_path, sha_before, sha_after, previous_bytes} for
    recording and rollback.
    Failure: GovernanceError with code branch_changed | source_dirty |
    anchor_not_in_source | no_source | no_batch | git_failed.
    """
    batch_id = proposal.get("batch_id")
    if not batch_id:
        raise GovernanceError("Only batch proposals can be written back.", "no_batch")
    batch = db.get_approval_batch(batch_id)
    if batch is None:
        raise GovernanceError("Approval batch not found.", "no_batch")
    project = db.project_info()
    root = repo_root(project.get("source_root"))
    located = source_file(project, scene)
    if not located:
        raise GovernanceError("This scene has no Markdown source file to write back to.", "no_source")
    path, relative = located

    expected_branch = batch.get("baseline", {}).get("branch")
    branch = _git(root, "branch", "--show-current")
    if expected_branch and branch != expected_branch:
        raise GovernanceError(
            f"The source checkout is on {branch or 'a detached HEAD'}, not {expected_branch}. Switch back to write this change.",
            "branch_changed",
        )

    current_bytes = path.read_bytes()
    current_sha = _sha256(current_bytes)
    ledger = batch.get("writeback") or {}
    ours = ledger.get("files", {}).get(relative)
    if _git(root, "status", "--porcelain", "--", relative) and ours != current_sha:
        raise GovernanceError(
            f"{relative} has uncommitted edits that aren't from this batch. Commit or stash them, or accept in Studio only.",
            "source_dirty",
        )

    text = current_bytes.decode("utf-8")
    original = proposal["original_verbatim_text"]
    count = text.count(original)
    if count != 1:
        raise GovernanceError(
            f"The original text appears {count} times in {relative}; write-back needs exactly one.",
            "anchor_not_in_source",
        )
    updated = text.replace(original, proposal["proposed_replacement_text"], 1).encode("utf-8")
    _atomic_write(path, updated)
    return {
        "path": relative,
        "absolute_path": str(path),
        "sha_before": current_sha,
        "sha_after": _sha256(updated),
        "previous_bytes": current_bytes,
    }


def restore(write: dict[str, Any]) -> None:
    """Undo a write_back whose database step failed."""
    _atomic_write(Path(write["absolute_path"]), write["previous_bytes"])


def record_write(db: StudioDB, batch_id: str, proposal_id: str, write: dict[str, Any]) -> None:
    """Add a successful write-back to the batch ledger."""
    batch = db.get_approval_batch(batch_id) or {}
    ledger = batch.get("writeback") or {"files": {}, "written": []}
    ledger.setdefault("files", {})[write["path"]] = write["sha_after"]
    ledger.setdefault("written", []).append({"proposal_id": proposal_id, "path": write["path"], "at": utc_now()})
    db.update_approval_batch(batch_id, writeback=ledger)


def _commit_message(batch: dict[str, Any], written_ids: set[str]) -> str:
    accepted = [p for p in batch.get("proposals", []) if p["proposal_id"] in written_ids]
    request = " ".join(batch.get("request_text", "").split())
    subject = f"Literary Studio: {len(accepted)} approved edit{'s' if len(accepted) != 1 else ''} ({batch['batch_id'][-8:]})"
    lines = [subject, "", f"Request: {request[:400]}", ""]
    for proposal in accepted:
        rationale = " ".join((proposal.get("editorial_rationale") or "").split())
        lines.append(f"- Scene {proposal['scene_number']}, {proposal['change_type']}: {rationale[:200]}")
    lines += ["", f"Studio-Batch: {batch['batch_id']}"]
    lines += [f"Studio-Proposal: {p['proposal_id']}" for p in accepted]
    return "\n".join(lines)


def commit_batch(db: StudioDB, batch_id: str) -> dict[str, Any]:
    """Commit a batch's written-back files as one Git commit.

    Behavior: commits only the batch's files (git commit --only), leaving any
    other staged or unstaged work untouched. Refuses if a file changed after
    write-back. Idempotent once committed.
    Returns {committed, commit_sha?, files?, reason?}.
    Failure: GovernanceError (branch_changed | source_dirty | git_failed); the
    batch is marked commit_failed with the reason so the desk can show it.
    """
    batch = db.get_approval_batch(batch_id)
    if batch is None:
        raise GovernanceError("Approval batch not found.", "not_found")
    ledger = batch.get("writeback") or {}
    if ledger.get("commit_sha"):
        return {"committed": True, "commit_sha": ledger["commit_sha"], "files": sorted(ledger.get("files", {})), "already": True}
    files: dict[str, str] = ledger.get("files", {})
    if not files:
        return {"committed": False, "reason": "No source files were changed by this batch."}

    project = db.project_info()
    try:
        root = repo_root(project.get("source_root"))
        expected_branch = batch.get("baseline", {}).get("branch")
        branch = _git(root, "branch", "--show-current")
        if expected_branch and branch != expected_branch:
            raise GovernanceError(f"The source checkout is on {branch}, not {expected_branch}.", "branch_changed")
        for relative, sha in files.items():
            if _sha256((root / relative).read_bytes()) != sha:
                raise GovernanceError(
                    f"{relative} was edited after write-back; review it, then commit the batch again.", "source_dirty"
                )
        written_ids = {item["proposal_id"] for item in ledger.get("written", [])}
        _git(root, "commit", "--only", "-m", _commit_message(batch, written_ids), "--", *sorted(files))
        commit_sha = _git(root, "rev-parse", "HEAD")
    except GovernanceError as exc:
        ledger["commit_error"] = {"code": exc.code, "message": str(exc), "at": utc_now()}
        db.update_approval_batch(batch_id, status="commit_failed", writeback=ledger)
        raise
    ledger.pop("commit_error", None)
    ledger.update({"commit_sha": commit_sha, "committed_at": utc_now(), "branch": branch})
    db.update_approval_batch(batch_id, status="committed", writeback=ledger)
    return {"committed": True, "commit_sha": commit_sha, "branch": branch, "files": sorted(files)}


def finalize_batch_if_done(db: StudioDB, batch_id: str | None) -> dict[str, Any] | None:
    """After a review, commit the batch once no proposals remain staged.

    Returns the commit result, a closed marker, or None while proposals remain.
    A commit failure is reported in the result rather than raised, because the
    review itself already succeeded.
    """
    if not batch_id:
        return None
    batch = db.get_approval_batch(batch_id)
    if batch is None or any(p.get("status") == "staged" for p in batch.get("proposals", [])):
        return None
    if (batch.get("writeback") or {}).get("files"):
        try:
            return commit_batch(db, batch_id)
        except GovernanceError as exc:
            return {"committed": False, "error": {"code": exc.code, "message": str(exc)}}
    db.update_approval_batch(batch_id, status="closed")
    return {"committed": False, "reason": "Batch closed with no source changes."}
