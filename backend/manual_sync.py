"""Boundary-safe comparison of saved Studio scenes with Markdown source files."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from compiler_engine import html_to_markdown
from governance import source_file
from studio_db import StudioDB


def _trimmed_span(source: str, start: int, end: int) -> tuple[int, int, str] | None:
    """Return a whitespace-trimmed source span without changing its contents."""
    segment = source[start:end]
    body = segment.strip()
    if not body:
        return None
    leading = len(segment) - len(segment.lstrip())
    trailing = len(segment) - len(segment.rstrip())
    body_start = start + leading
    body_end = end - trailing if trailing else end
    return body_start, body_end, body


def _scene_span(source: str, scene: dict[str, Any]) -> tuple[int, int, str] | None:
    """Return ``(start, end, body)`` for one imported Markdown scene.

    The span excludes the scene heading and preserves all surrounding file
    metadata/headings.  Full desk sync uses this instead of rebuilding a
    chapter from scratch, so the source's authored structure remains intact.
    """
    local = scene.get("scene_number_in_chapter")
    chapter = scene.get("chapter_number")
    if local is None:
        return None

    patterns: list[tuple[re.Pattern[str], re.Pattern[str]]] = []
    if chapter is not None:
        patterns.append((
            re.compile(rf"(?mi)^\s*###\s+{int(chapter)}\.{int(local)}\b[^\n]*$"),
            re.compile(r"(?mi)^\s*###\s+\d+\.\d+\b[^\n]*$"),
        ))
    patterns.append((
        re.compile(rf"(?mi)^\s*##\s+Scene\s+{int(local)}\b[^\n]*$"),
        re.compile(r"(?mi)^\s*##\s+Scene\s+\d+\b[^\n]*$"),
    ))
    matched: tuple[re.Match[str], re.Pattern[str]] | None = None
    for pattern, boundary in patterns:
        match = pattern.search(source)
        if match is not None:
            matched = (match, boundary)
            break
    if matched is None:
        return None
    match, scene_boundary = matched

    start = match.end()
    if source[start:start + 2] == "\r\n":
        start += 2
    elif source[start:start + 1] in {"\n", "\r"}:
        start += 1

    boundaries = [scene_boundary]
    if chapter is not None:
        boundaries.append(re.compile(r"(?mi)^\s*#\s+\d+\s*(?:\||[—–:-])\s*[^\n]*$"))
    next_positions = [match.start() for boundary in boundaries if (match := boundary.search(source[start:]))]
    end = start + min(next_positions) if next_positions else len(source)
    return _trimmed_span(source, start, end)


def _single_or_break_split_scene_body(source: str, scene: dict[str, Any]) -> str | None:
    """Return a scene imported from a chapter with no explicit scene heading.

    ``_markdown_scenes`` imports these files by splitting the chapter body on
    standalone scene-break markers.  The write-back comparison must use the
    same boundary rule; otherwise a one-scene chapter such as Chapter 2 is
    saved successfully but can never produce a manual-sync candidate.
    """
    span = _single_or_break_split_scene_span(source, scene)
    return span[2] if span else None


def _single_or_break_split_scene_span(
    source: str, scene: dict[str, Any]
) -> tuple[int, int, str] | None:
    """Return a source span for a chapter split by standalone scene breaks."""
    local = scene.get("scene_number_in_chapter")
    if local is None:
        return None

    # This mirrors _markdown_scenes: remove frontmatter, then remove the first
    # level-one file title. Opening matter may have no title heading; appendices use
    # an ``# Appendix | ...`` title instead of a numeric one.
    frontmatter = re.match(r"^---\s*\n.*?\n---\s*\n", source, flags=re.DOTALL)
    start = frontmatter.end() if frontmatter else 0
    title_heading = re.search(r"(?mi)^\s*#\s+(?!#)[^\n]*$", source[start:])
    if title_heading:
        start += title_heading.end()
    if source[start:start + 2] == "\r\n":
        start += 2
    elif source[start:start + 1] in {"\n", "\r"}:
        start += 1
    next_chapter = re.search(r"(?mi)^\s*#\s+(?!#)\S", source[start:])
    end = start + next_chapter.start() if next_chapter else len(source)

    markers = list(re.finditer(r"(?m)^\s*(?:\*\s*\*\s*\*|⁂)\s*$", source[start:end]))
    index = int(local) - 1
    if index < 0 or index > len(markers):
        return None
    part_start = start if index == 0 else start + markers[index - 1].end()
    part_end = end if index == len(markers) else start + markers[index].start()
    if source[part_start:part_start + 2] == "\r\n":
        part_start += 2
    elif source[part_start:part_start + 1] in {"\n", "\r"}:
        part_start += 1
    return _trimmed_span(source, part_start, part_end)


def scene_source_span(source: str, scene: dict[str, Any]) -> tuple[int, int, str] | None:
    """Locate a scene using the same boundary rules as intake and manual sync."""
    return _scene_span(source, scene) or _single_or_break_split_scene_span(source, scene)


def _blocks(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", value.strip()) if part.strip()]


def _canonical_block(value: str) -> str:
    if re.fullmatch(r"(?:\*\s*){3,}|[-_]\s*[-_]\s*[-_]+|⁂", value):
        return "***"
    return value


def _diff_hunks(source_body: str, studio_body: str) -> list[dict[str, str]]:
    source_blocks = _blocks(source_body)
    studio_blocks = _blocks(studio_body)
    matcher = difflib.SequenceMatcher(
        a=[_canonical_block(block) for block in source_blocks],
        b=[_canonical_block(block) for block in studio_blocks],
        autojunk=False,
    )
    hunks: list[dict[str, str]] = []
    for tag, source_start, source_end, studio_start, studio_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        original = "\n\n".join(source_blocks[source_start:source_end])
        replacement = "\n\n".join(studio_blocks[studio_start:studio_end])

        # An insertion has no source anchor. Anchor it to the adjacent source
        # paragraph so it can still pass the normal exact-write safeguards.
        if tag == "insert" and not original:
            if source_start < len(source_blocks):
                original = source_blocks[source_start]
                replacement = original + "\n\n" + replacement
            elif source_blocks:
                original = source_blocks[-1]
                replacement = original + "\n\n" + replacement
            else:
                continue
        if original == replacement:
            continue
        hunks.append({"original": original, "replacement": replacement, "operation": tag})
    return hunks


def _unsafe_scene_deletion(source_body: str, studio_body: str, hunks: list[dict[str, str]]) -> list[str]:
    """Reject diffs that look like a scene-boundary failure rather than an edit."""
    if not source_body.strip() or not hunks:
        return []
    source_headings = re.findall(r"(?m)^\s*###\s+\d+\.\d+\b[^\n]*$", source_body)
    if source_headings and any(
        hunk["operation"] == "delete" and re.search(r"(?m)^\s*###\s+\d+\.\d+\b", hunk["original"])
        for hunk in hunks
    ):
        return ["Manual sync refused a diff containing a Markdown scene heading; scene boundaries were not isolated safely."]
    deleted_chars = sum(
        len(hunk["original"])
        for hunk in hunks
        if hunk["operation"] == "delete"
        or (
            hunk["operation"] == "replace"
            and len(hunk.get("replacement", "")) < len(hunk["original"]) * 0.5
        )
    )
    if len(source_body) >= 2000 and deleted_chars >= max(2000, int(len(source_body) * 0.5)):
        return [
            "Manual sync refused a scene-sized deletion; review the scene boundary before preparing editor changes."
        ]
    if source_body.strip() and not studio_body.strip() and len(source_body) >= 500:
        return ["Manual sync refused an empty replacement for a non-empty scene."]
    return []


def build_manual_change_candidates(db: StudioDB, scene_numbers: list[int] | None = None) -> list[dict[str, Any]]:
    """Find saved Studio prose that differs from its exact imported baseline.

    Git-backed projects compare against the live Markdown scene boundary. A
    source-less reintake has no checkout to read, but it does retain the
    original import as a revision. Those projects can still produce exact
    Studio-only proposals; they simply cannot write the result back to Git
    until a source checkout is attached.
    """
    wanted = set(scene_numbers or [])
    project = db.project_info()
    candidates: list[dict[str, Any]] = []
    for scene in db.list_scenes():
        if wanted and scene["scene_number"] not in wanted:
            continue
        latest_revision = db.scene_history(scene["scene_number"])
        if not latest_revision or latest_revision[0].get("source") != "editor":
            continue
        located = source_file(project, scene)
        source_mode = "live_worktree"
        if located is not None:
            path, relative = located
            source = Path(path).read_text(encoding="utf-8", errors="replace")
            located_scene = scene_source_span(source, scene)
            source_body = located_scene[2] if located_scene else None
            if source_body is None:
                continue
            source_scene_version = scene["version"]
        else:
            # Repository imports preserve revision 1 as the exact intake
            # baseline. Use it when no checkout is configured, rather than
            # silently treating later editor autosaves as the only state.
            imported_revision = next(
                (revision for revision in reversed(latest_revision) if revision.get("source") == "import"),
                None,
            )
            if imported_revision is None:
                continue
            relative = scene.get("source_path") or "imported manuscript snapshot"
            source_body = html_to_markdown(imported_revision["content_html"])
            source_mode = "import_baseline"
            source_scene_version = imported_revision["version"]
        studio_body = html_to_markdown(scene["content_html"])
        hunks = _diff_hunks(source_body, studio_body)
        if not hunks:
            continue
        safety_errors = _unsafe_scene_deletion(source_body, studio_body, hunks)
        candidates.append({
            "scene_number": scene["scene_number"],
            "title": scene["title"],
            "chapter_number": scene.get("chapter_number"),
            "scene_number_in_chapter": scene.get("scene_number_in_chapter"),
            "source_path": relative,
            "source_scene_version": source_scene_version,
            "source_mode": source_mode,
            "studio_updated_at": scene["updated_at"],
            "hunks": [] if safety_errors else hunks,
            "safety_errors": safety_errors,
        })
    return candidates
