"""Small durable development store for the Literary Studio vertical slice.

SQLite keeps the local app useful without requiring a second Python database
driver. The tables are deliberately shaped like the planned Postgres models so
the store can be replaced by Postgres/Neo4j projections without changing the
HTTP contracts.
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
from uuid import uuid4
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from threading import RLock
from contextlib import contextmanager
from typing import Any, Iterator

from graph_extractor import build_graph

PROJECTS_ROOT = Path(__file__).resolve().parent.parent / "build_artifacts" / "projects"
PROJECT_LOCAL_SCRATCH_PREFIX = "scratch/"
SEMANTIC_STYLE_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "frontend" / "data" / "semanticStyles.json"


def is_project_local_scratch_path(path: str | None) -> bool:
    """Return whether a path belongs to Studio's non-Git scratch namespace.

    Scratch material is durable desk state, but it is never manuscript source.
    Treat old forms such as ``manuscript/scratch/...`` as scratch too, so a
    legacy intake cannot accidentally become eligible for Git write-back.
    """
    normalized = str(path or "").replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = [part.casefold() for part in normalized.split("/") if part]
    return any(part == "scratch" for part in parts[:-1]) or normalized.casefold().startswith(PROJECT_LOCAL_SCRATCH_PREFIX)


def load_semantic_style_registry() -> list[dict[str, Any]]:
    """Load the one project-independent taxonomy used by editor and print paths."""
    try:
        raw = json.loads(SEMANTIC_STYLE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and isinstance(item.get("id"), str)]


def semantic_style_ids() -> set[str]:
    return {str(item["id"]).lower() for item in load_semantic_style_registry()}

DEFAULT_EDITOR_SETTINGS: dict[str, Any] = {
    "font_family": "literary-serif",
    "font_size": 18,
    "line_height": 1.7,
    "paragraph_spacing": "comfortable",
    "style_overrides": {},
}
EDITOR_FONT_FAMILIES = {
    "literary-serif",
    "palatino",
    "system-sans",
    "mono",
}
EDITOR_PARAGRAPH_SPACING = {"compact", "comfortable", "airy"}
EDITOR_ALIGNMENTS = {"left", "center", "right"}
EDITOR_FONT_WEIGHTS = {400, 500, 600, 700}
EDITOR_INDENTS = {"0", "1", "2"}
EDITOR_TEXT_TRANSFORMS = {"none", "uppercase"}


def normalise_style_overrides(value: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Keep project-wide semantic style presentation safe and CSS-independent."""
    if not isinstance(value, dict):
        return {}
    normalised: dict[str, dict[str, Any]] = {}
    known_style_ids = semantic_style_ids()
    for style_id, raw in value.items():
        if not isinstance(style_id, str) or not re.fullmatch(r"[a-z0-9-]+", style_id, re.IGNORECASE):
            continue
        if known_style_ids and style_id.lower() not in known_style_ids:
            continue
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {}
        font_family = raw.get("font_family")
        if font_family in EDITOR_FONT_FAMILIES:
            item["font_family"] = font_family
        try:
            font_size = int(raw["font_size"])
            if 8 <= font_size <= 72:
                item["font_size"] = font_size
        except (KeyError, TypeError, ValueError):
            pass
        try:
            line_height = round(float(raw["line_height"]), 2)
            if 0.8 <= line_height <= 3.0:
                item["line_height"] = line_height
        except (KeyError, TypeError, ValueError):
            pass
        align = raw.get("align")
        if align in EDITOR_ALIGNMENTS:
            item["align"] = align
        if isinstance(raw.get("italic"), bool):
            item["italic"] = raw["italic"]
        try:
            font_weight = int(raw["font_weight"])
            if font_weight in EDITOR_FONT_WEIGHTS:
                item["font_weight"] = font_weight
        except (KeyError, TypeError, ValueError):
            pass
        indent = raw.get("indent")
        if str(indent) in EDITOR_INDENTS:
            item["indent"] = str(indent)
        try:
            letter_spacing = round(float(raw["letter_spacing"]), 3)
            if -0.1 <= letter_spacing <= 0.5:
                item["letter_spacing"] = letter_spacing
        except (KeyError, TypeError, ValueError):
            pass
        text_transform = raw.get("text_transform")
        if text_transform in EDITOR_TEXT_TRANSFORMS:
            item["text_transform"] = text_transform
        if item:
            normalised[style_id.lower()] = item
    return normalised


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise_editor_settings(value: dict[str, Any] | None) -> dict[str, Any]:
    """Keep project typography settings small, safe, and CSS-independent."""
    candidate = value if isinstance(value, dict) else {}
    font_family = candidate.get("font_family", DEFAULT_EDITOR_SETTINGS["font_family"])
    if font_family not in EDITOR_FONT_FAMILIES:
        font_family = DEFAULT_EDITOR_SETTINGS["font_family"]
    try:
        font_size = int(candidate.get("font_size", DEFAULT_EDITOR_SETTINGS["font_size"]))
    except (TypeError, ValueError):
        font_size = DEFAULT_EDITOR_SETTINGS["font_size"]
    font_size = max(12, min(32, font_size))
    try:
        line_height = float(candidate.get("line_height", DEFAULT_EDITOR_SETTINGS["line_height"]))
    except (TypeError, ValueError):
        line_height = DEFAULT_EDITOR_SETTINGS["line_height"]
    line_height = round(max(1.2, min(2.4, line_height)), 2)
    paragraph_spacing = candidate.get("paragraph_spacing", DEFAULT_EDITOR_SETTINGS["paragraph_spacing"])
    if paragraph_spacing not in EDITOR_PARAGRAPH_SPACING:
        paragraph_spacing = DEFAULT_EDITOR_SETTINGS["paragraph_spacing"]
    return {
        "font_family": font_family,
        "font_size": font_size,
        "line_height": line_height,
        "paragraph_spacing": paragraph_spacing,
        "style_overrides": normalise_style_overrides(candidate.get("style_overrides")),
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "hr":
            self.parts.append("\n* * *\n")
        elif tag in {"p", "div", "br", "h1", "h2", "h3", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.parts.append("\n")


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _ParagraphTextExtractor(HTMLParser):
    """Extract editor paragraph text in the same order TipTap exposes it."""

    def __init__(self) -> None:
        super().__init__()
        self._active: list[str] | None = None
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "p" and self._active is None:
            self._active = []

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._active.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "p" and self._active is not None:
            self.paragraphs.append(re.sub(r"\s+", " ", "".join(self._active)).strip())
            self._active = None


def paragraph_texts(content_html: str) -> list[str]:
    parser = _ParagraphTextExtractor()
    parser.feed(content_html)
    parser.close()
    return parser.paragraphs


def unique_html_anchor(content_html: str, original: str) -> str | None:
    """Return the HTML spelling of ``original`` that occurs exactly once, else None.

    Scene HTML stores text escaped (``&amp;``), so both the raw and escaped
    forms are tried. None means the text is absent, repeated, or interrupted
    by inline markup (italics, bold, links) and cannot be replaced exactly.
    """
    if not original:
        return None
    for candidate in dict.fromkeys((
        original,
        escape(original, quote=False),
        escape(original, quote=True),
    )):
        if content_html.count(candidate) == 1:
            return candidate
    return None


_BLOCK_WITHOUT_ID = re.compile(r"<(p|h[1-6]|blockquote|li)(?![^>]*\bdata-id=)(\s[^>]*)?>", re.IGNORECASE)

_SEMANTIC_STYLE_COMMENT = re.compile(
    r"^\s*<!--\s*literary-style:\s*([a-z0-9-]+)(?:\s+(locked))?\s*-->\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

# A source-repair session can mark an arbitrary Markdown paragraph as a
# scene-title block.  The directive is intentionally the durable contract:
# the editor may render it as an h4 (or another heading level), but intake
# must not depend on that presentation detail.
_STYLED_SCENE_HEADING = re.compile(
    r"^\s*<!--\s*literary-style:\s*scene-title(?:\s+locked)?\s*-->\s*\n"
    r"\s*(?:#{1,6}\s+)?([^\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_STYLED_CHAPTER_HEADING = re.compile(
    r"^\s*<!--\s*literary-style:\s*chapter-title(?:\s+locked)?\s*-->\s*\n"
    r"\s*(?:#{1,6}\s+)?([^\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _semantic_style_attrs(style_id: str | None, locked: bool = False) -> str:
    """Return safe HTML attributes for a Markdown-backed semantic block style."""
    if not style_id or not re.fullmatch(r"[a-z0-9-]+", style_id, flags=re.IGNORECASE):
        return ""
    lock_attr = ' data-style-locked="true"' if locked else ""
    return f' data-semantic-style="{style_id}"{lock_attr}'


def assign_block_ids(content_html: str) -> str:
    """Give every paragraph-level block a ``data-id`` UUID if it lacks one.

    Matches the editor's TipTap UniqueID extension (types paragraph, heading,
    blockquote, listItem; attribute data-id; UUID v4), which keeps existing ids,
    so annotations can anchor to scenes that have never been opened in the
    editor. Text content is unchanged.
    """
    return _BLOCK_WITHOUT_ID.sub(
        lambda m: f'<{m.group(1)} data-id="{uuid4()}"{m.group(2) or ""}>', content_html
    )


def words(value: str) -> list[str]:
    return re.findall(r"[\w’'-]+", html_to_text(value), flags=re.UNICODE)


def word_delta(previous: str, current: str) -> tuple[int, int]:
    old_words = words(previous)
    new_words = words(current)
    added = 0
    deleted = 0
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, old_words, new_words, autojunk=False
    ).get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += old_end - old_start
        if tag in {"replace", "insert"}:
            added += new_end - new_start
    return added, deleted


PRESSURE_TERMS = re.compile(
    r"\b(?:attack|blood|break|burn|chase|crash|danger|dead|death|escape|fear|fire|gun|kill|pain|panic|run|threat|trap|warning|weapon|wound)\w*\b",
    flags=re.IGNORECASE,
)


def pressure_profile(text: str) -> dict[str, Any]:
    """Return a transparent, deterministic textual pressure estimate.

    This is a signal for navigation, not an editorial verdict. It combines
    sentence velocity, conflict-language density, and dialogue density so the
    author can compare scenes consistently and override the interpretation
    later without changing the manuscript source.
    """
    clean_text = re.sub(r"\s+", " ", text).strip()
    word_count = max(len(words(clean_text)), 1)
    sentence_count = max(len(re.findall(r"[^.!?…]+(?:[.!?…]+|$)", clean_text)), 1)
    average_sentence_words = word_count / sentence_count
    velocity = max(0.0, min(100.0, (32.0 - average_sentence_words) / 24.0 * 100.0))
    conflict_count = len(PRESSURE_TERMS.findall(clean_text))
    conflict_density = min(100.0, conflict_count / max(word_count / 180.0, 1.0) * 100.0)
    dialogue_marks = len(re.findall(r"[\"“”]", clean_text))
    dialogue_density = min(100.0, dialogue_marks / max(sentence_count * 1.5, 1.0) * 100.0)
    score = round(velocity * 0.4 + conflict_density * 0.35 + dialogue_density * 0.25)
    return {
        "score": score,
        "components": {
            "sentence_velocity": round(velocity),
            "conflict_density": round(conflict_density),
            "dialogue_density": round(dialogue_density),
        },
        "word_count": word_count,
        "sentence_count": sentence_count,
    }


def split_manuscript(
    text: str, fallback_title: str, split_mode: str = "single"
) -> list[tuple[str, str]]:
    def render_section(title: str, body: str) -> tuple[str, str] | None:
        paragraphs = re.split(r"\n\s*\n", body.strip())
        html = "".join(
            f"<p>{escape(paragraph.strip()).replace(chr(10), '<br>')}</p>"
            for paragraph in paragraphs
            if paragraph.strip()
        )
        return (title, html) if html else None

    if split_mode == "single":
        rendered = render_section(fallback_title.strip() or "Imported material", text)
        return [rendered] if rendered else []
    if split_mode != "headings":
        raise ValueError("split_mode must be 'single' or 'headings'.")

    heading_pattern = re.compile(
        r"^\s*#{0,6}\s*(chapter|scene)\s+([A-Za-z0-9IVXLC.-]+)"
        r"(?:\s*[-—:]\s*(.+?))?\s*$",
        flags=re.IGNORECASE,
    )
    sections: list[tuple[str, list[str]]] = []
    current_title = fallback_title.strip() or "Imported material"
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_title, body.splitlines()))
        current_lines = []

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = heading_pattern.match(line)
        if match:
            flush()
            label = match.group(3) or f"{match.group(1).title()} {match.group(2)}"
            current_title = label.strip()
        else:
            current_lines.append(line)
    flush()

    if not sections:
        body = text.strip()
        if body:
            sections.append((current_title, body.splitlines()))

    result: list[tuple[str, str]] = []
    for title, lines in sections:
        rendered = render_section(title, "\n".join(lines))
        if not rendered:
            continue
        # PDF/text exports often repeat a running header such as
        # "Manuscript Title – 11". If it was promoted to a heading, keep its
        # text with the preceding scene instead of creating a tiny fragment.
        if result and re.search(r"\s[-–—]\s*\d+\s*$", title):
            result[-1] = (result[-1][0], result[-1][1] + rendered[1])
        else:
            result.append(rendered)
    return result


def _natural_path_key(value: str) -> list[object]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


EXCLUDED_MANUSCRIPT_DIRS = {
    "worldbook", "editorial-history", "editor-passes", "editor-competition",
    "editorial-board", "roundtable", "roundtables", "outlines", "outline",
    "notes", "reviews", "assessments", "scaffolding", "handoff", "archive",
    "dossier", "dossiers", "cast", "synopsis", "bible", "research",
    "meta", "templates", "prompts", "evaluations", "passes", "docs",
    "exports", "reports", ".narrative",
}

EXCLUDED_FILE_PATTERNS = re.compile(
    r"(?:^|[._-])(?:pass|roundtable|editorial[._-]board|scaffolding|assessment|audit|briefing|character[._-]study|feedback|review|notes|outline|dossier)(?:$|[._-])",
    re.IGNORECASE,
)


def is_repository_manuscript_path(path: str) -> bool:
    parts = [part.strip() for part in path.replace("\\", "/").strip("/").split("/") if part.strip()]
    if not parts:
        return False
    if is_project_local_scratch_path(path):
        return False
    filename = parts[-1].casefold()
    if filename in {"readme.md", "license.md", "changelog.md"}:
        return False
    if any(part.startswith(".") for part in parts):
        return False

    dir_parts = [p.casefold() for p in parts[:-1]]
    # Reject paths inside excluded non-manuscript directories
    if any(any(ex in p for ex in EXCLUDED_MANUSCRIPT_DIRS) for p in dir_parts):
        return False

    # Reject files with non-manuscript stems (e.g. editorial passes, roundtables, outlines)
    stem = filename.rsplit(".", 1)[0]
    if EXCLUDED_FILE_PATTERNS.search(stem):
        return False

    # If the repository has a manuscript root directory, ensure the file is inside it
    if any("manuscript" in p for p in dir_parts):
        if not any(p in {"manuscript", "manuscript_draft", "manuscript_revised_draft"} for p in dir_parts):
            return False

    return any(
        re.match(r"^acts?(?:\s|[-_]|$)", part, re.IGNORECASE)
        or re.match(r"^chapters?$", part, re.IGNORECASE)
        or re.match(r"^appendix(?:\s|[-_]|$)", part, re.IGNORECASE)
        or re.search(r"chapter", part, re.IGNORECASE)
        or re.search(r"scratch", part, re.IGNORECASE)
        for part in parts
    )


@dataclass(frozen=True)
class ImportedSection:
    title: str
    content_html: str
    chapter_number: int | None = None
    chapter_title: str | None = None
    scene_number_in_chapter: int | None = None
    part_number: int | None = None
    part_title: str | None = None
    source_path: str | None = None


def _render_inline_markdown(value: str) -> str:
    """Render the small Markdown inline vocabulary used by manuscript files."""
    rendered = escape(value, quote=False)
    code_tokens: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_tokens.append(f"<code>{escape(match.group(1), quote=False)}</code>")
        return f"\x00CODE{len(code_tokens) - 1}\x00"

    rendered = re.sub(r"`([^`]+)`", stash_code, rendered)
    rendered = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)", r'<a href="\2">\1</a>', rendered)
    rendered = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"__([^_\n]+)__", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    rendered = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", rendered)
    rendered = rendered.replace("\n", "<br>")
    for index, token in enumerate(code_tokens):
        rendered = rendered.replace(f"\x00CODE{index}\x00", token)
    return rendered


def _render_markdown_body(body_text: str) -> str:
    """Render manuscript Markdown while retaining paragraph and scene-break layout."""
    normalized = body_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = re.split(r"\n\s*\n", normalized) if normalized else []
    rendered: list[str] = []
    for paragraph in paragraphs:
        clean = paragraph.strip()
        if not clean:
            continue
        semantic_style: str | None = None
        semantic_locked = False
        style_match = _SEMANTIC_STYLE_COMMENT.match(clean)
        if style_match:
            semantic_style = style_match.group(1).lower()
            semantic_locked = bool(style_match.group(2))
            clean = style_match.group(3).strip()
            if not clean:
                continue
        semantic_attrs = _semantic_style_attrs(semantic_style, semantic_locked)
        if re.fullmatch(r"(?:\*\s*){3,}|[-_]{3,}|⁂", clean):
            rendered.append('<hr class="scene-break" />')
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", clean)
        if heading:
            level = min(len(heading.group(1)), 6)
            rendered.append(f"<h{level}{semantic_attrs}>{_render_inline_markdown(heading.group(2).strip())}</h{level}>")
        elif all(line.lstrip().startswith(">") for line in clean.splitlines()):
            quote = re.sub(r"^\s*>\s?", "", clean, flags=re.MULTILINE)
            rendered.append(f"<blockquote{semantic_attrs}><p>{_render_inline_markdown(quote)}</p></blockquote>")
        else:
            rendered.append(f"<p{semantic_attrs}>{_render_inline_markdown(clean)}</p>")
    return "".join(rendered)


def _chapter_number(value: str) -> int | None:
    match = re.search(r"(?:chapter|ch)[\s._-]*(\d+)", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match_leading = re.match(r"^\s*(\d+)\s*[|—:-]", value)
    return int(match_leading.group(1)) if match_leading else None


def _part_metadata(act_title: str | None) -> tuple[int | None, str | None]:
    """Turn a repository act directory into editable project taxonomy."""
    match = re.search(r"^act(?:[-_ ]?)(\d+)$", str(act_title or "").strip(), flags=re.IGNORECASE)
    if not match:
        return None, None
    number = int(match.group(1))
    return number, "Epigraph" if number == 0 else f"Part {number}"


def _markdown_scenes(path: str, text: str) -> list[ImportedSection]:
    """Render a manuscript file into its actual internal scene sections."""
    normalized_path = path.replace("\\", "/").strip("/")
    path_parts = [part for part in normalized_path.split("/") if part]
    if not path_parts:
        return []
    filename = path_parts[-1]
    if not is_repository_manuscript_path(normalized_path):
        return []

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    body_text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", normalized_text, count=1, flags=re.DOTALL)
    styled_chapter_match = _STYLED_CHAPTER_HEADING.search(body_text)
    title_match = re.search(r"^\s*#\s+(.+?)\s*$", body_text, flags=re.MULTILINE)
    if styled_chapter_match and (not title_match or styled_chapter_match.start() < title_match.start()):
        file_title = re.sub(r"[`*_]", "", styled_chapter_match.group(1)).strip()
        body_text = body_text[: styled_chapter_match.start()] + body_text[styled_chapter_match.end() :]
    elif title_match:
        file_title = re.sub(r"[`*_]", "", title_match.group(1)).strip()
        body_text = body_text[: title_match.start()] + body_text[title_match.end() :]
    else:
        file_title = Path(filename).stem.replace("_", " ").replace("-", " - ")
        file_title = re.sub(r"\s+", " ", file_title).strip()

    act_title = next(
        (part for part in path_parts[:-1] if re.match(r"^act(?:\s|[-_]|$)", part, re.IGNORECASE)),
        None,
    )
    chapter_number = _chapter_number(file_title) or _chapter_number(filename)
    clean_title = re.sub(r"^\d+\s*[|—:-]\s*", "", file_title).strip() or file_title
    if chapter_number == 0:
        clean_title = "Epigraph"
    chapter_title = f"{act_title} / {clean_title}" if act_title else clean_title
    part_number, part_title = _part_metadata(act_title)
    numbered_scene_heading = re.compile(
        r"^\s*###\s+(\d+)\.(\d+)\s+(.+?)\s*$",
        flags=re.MULTILINE,
    )
    numbered_matches = list(numbered_scene_heading.finditer(body_text))
    if numbered_matches:
        sections: list[ImportedSection] = []
        for index, match in enumerate(numbered_matches):
            end = numbered_matches[index + 1].start() if index + 1 < len(numbered_matches) else len(body_text)
            rendered = _render_markdown_body(body_text[match.end() : end])
            if not rendered:
                continue
            chapter_token, scene_token, raw_title = match.groups()
            scene_title = re.sub(r"[`*_]", "", raw_title.strip()) or f"Scene {chapter_token}.{scene_token}"
            sections.append(ImportedSection(
                title=scene_title,
                content_html=rendered,
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                scene_number_in_chapter=int(scene_token),
                part_number=part_number,
                part_title=part_title,
                source_path=normalized_path,
            ))
        return sections

    styled_scene_matches = list(_STYLED_SCENE_HEADING.finditer(body_text))
    if styled_scene_matches:
        sections: list[ImportedSection] = []
        for index, match in enumerate(styled_scene_matches, start=1):
            end = styled_scene_matches[index].start() if index < len(styled_scene_matches) else len(body_text)
            rendered = _render_markdown_body(body_text[match.end() : end])
            if not rendered:
                continue
            scene_title = re.sub(r"[`*_]", "", match.group(1).strip()) or f"Scene {index}"
            sections.append(ImportedSection(
                title=scene_title,
                content_html=rendered,
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                scene_number_in_chapter=index,
                part_number=part_number,
                part_title=part_title,
                source_path=normalized_path,
            ))
        return sections

    scene_heading = re.compile(
        r"^\s*##\s+Scene\s+([0-9]+|[IVXLC]+)(?:\s*[—–:-]\s*(.*?))?\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    matches = list(scene_heading.finditer(body_text))
    if not matches:
        scene_break_pattern = r"(?m)^\s*(?:\*\s*\*\s*\*|⁂)\s*$"
        scene_breaks = list(re.finditer(scene_break_pattern, body_text))
        # A chapter with no explicit scene heading and no marked scene break is
        # one scene.  Keep this branch explicit: a file-level chapter heading,
        # musical heading, or other Markdown structure must never become an
        # inferred scene boundary.
        if not scene_breaks:
            rendered = _render_markdown_body(body_text)
            return [ImportedSection(
                title="",
                content_html=rendered,
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                scene_number_in_chapter=1,
                part_number=part_number,
                part_title=part_title,
                source_path=normalized_path,
            )] if rendered else []

        scene_parts = re.split(scene_break_pattern, body_text)
        sections: list[ImportedSection] = []
        for index, scene_part in enumerate(scene_parts, start=1):
            rendered = _render_markdown_body(scene_part)
            if rendered:
                sections.append(ImportedSection(
                    title="",
                    content_html=rendered,
                    chapter_number=chapter_number,
                    chapter_title=chapter_title,
                    scene_number_in_chapter=index,
                    part_number=part_number,
                    part_title=part_title,
                    source_path=normalized_path,
                ))
        return sections

    sections: list[ImportedSection] = []
    for index, match in enumerate(matches, start=1):
        end = matches[index].start() if index < len(matches) else len(body_text)
        rendered = _render_markdown_body(body_text[match.end() : end])
        if not rendered:
            continue
        token = match.group(1)
        scene_number = int(token) if token.isdigit() else index
        scene_title = re.sub(r"[`*_]", "", (match.group(2) or "").strip())
        sections.append(ImportedSection(
            title=scene_title,
            content_html=rendered,
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            scene_number_in_chapter=scene_number,
            part_number=part_number,
            part_title=part_title,
            source_path=normalized_path,
        ))
    return sections


class StudioDB:
    def __init__(
        self,
        path: str | Path,
        project_id: str,
        project_title: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.project_id = project_id
        self.project_title = project_title or "Untitled manuscript"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one short-lived connection and always close it.

        ``sqlite3.Connection`` implements a transaction context manager, but
        its ``__exit__`` does not close the connection.  The Studio uses this
        helper throughout its request handlers, so leaving connections open
        eventually exhausted the process file-descriptor limit and made
        otherwise healthy project/source lookups fail with ``unable to open
        database file``.
        """
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        migrated_scratch_scene_numbers: list[int] = []
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenes (
                    scene_number INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    content_html TEXT NOT NULL,
                    chapter_number INTEGER,
                    chapter_title TEXT,
                    scene_number_in_chapter INTEGER,
                    part_number INTEGER,
                    part_title TEXT,
                    source_path TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_meta (
                    project_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_root TEXT,
                    source_subpath TEXT,
                    branch TEXT,
                    commit_sha TEXT,
                    source_digest TEXT,
                    snapshot_path TEXT,
                    manifest_path TEXT,
                    working_tree_dirty INTEGER NOT NULL DEFAULT 0,
                    editor_settings_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    scene_number INTEGER NOT NULL,
                    proposed_by_persona_id TEXT NOT NULL,
                    proposed_by_name TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    original_verbatim_text TEXT NOT NULL,
                    proposed_replacement_text TEXT NOT NULL,
                    editorial_rationale TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_batches (
                    batch_id TEXT PRIMARY KEY,
                    request_text TEXT NOT NULL,
                    include_json TEXT NOT NULL,
                    baseline_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS annotations (
                    annotation_id TEXT PRIMARY KEY,
                    scene_number INTEGER NOT NULL,
                    paragraph_id TEXT NOT NULL,
                    quote_text TEXT NOT NULL,
                    body TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (scene_number) REFERENCES scenes(scene_number)
                );
                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scene_number INTEGER,
                    event_type TEXT NOT NULL,
                    words_added INTEGER NOT NULL DEFAULT 0,
                    words_deleted INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scene_revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scene_number INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    content_html TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (scene_number) REFERENCES scenes(scene_number)
                );
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    node_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    node_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_edges (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relationship TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_id, relationship),
                    FOREIGN KEY (source_id) REFERENCES graph_nodes(node_id),
                    FOREIGN KEY (target_id) REFERENCES graph_nodes(node_id)
                );
                CREATE TABLE IF NOT EXISTS intake_audits (
                    audit_id TEXT PRIMARY KEY,
                    source_label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS persona_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    scene_number INTEGER NOT NULL,
                    persona_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (scene_number) REFERENCES scenes(scene_number)
                );
                CREATE TABLE IF NOT EXISTS agent_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(agent_id, channel)
                );
                CREATE TABLE IF NOT EXISTS agent_conversation_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES agent_conversations(conversation_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS support_documents (
                    document_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    chapter_number INTEGER,
                    source_scene_numbers_json TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'tethered',
                    tether_enabled INTEGER NOT NULL DEFAULT 1,
                    base_document_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    archived_at TEXT,
                    archive_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_documents (
                    document_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content_html TEXT NOT NULL,
                    source_scene_numbers_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES draft_sessions(session_id) ON DELETE CASCADE
                );
                """
            )
            connection.execute(
                """INSERT OR IGNORE INTO project_meta(
                    project_id, title, created_at
                ) VALUES (?, ?, ?)""",
                (self.project_id, self.project_title, utc_now()),
            )
            existing_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(project_meta)").fetchall()
            }
            for column, definition in (
                ("source_subpath", "TEXT"),
                ("snapshot_path", "TEXT"),
                ("manifest_path", "TEXT"),
                ("working_tree_dirty", "INTEGER NOT NULL DEFAULT 0"),
                ("editor_settings_json", "TEXT"),
            ):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE project_meta ADD COLUMN {column} {definition}")
            proposal_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(proposals)").fetchall()
            }
            for column, definition in (
                ("batch_id", "TEXT"),
                ("source_branch", "TEXT"),
                ("source_commit_sha", "TEXT"),
                ("source_digest", "TEXT"),
                ("source_scene_version", "INTEGER"),
                ("validation_json", "TEXT"),
                ("writeback_status", "TEXT"),
                ("reviewed_at", "TEXT"),
            ):
                if column not in proposal_columns:
                    connection.execute(f"ALTER TABLE proposals ADD COLUMN {column} {definition}")
            batch_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(approval_batches)").fetchall()
            }
            for column, definition in (
                ("writeback_json", "TEXT"),
                ("result_json", "TEXT"),
            ):
                if column not in batch_columns:
                    connection.execute(f"ALTER TABLE approval_batches ADD COLUMN {column} {definition}")
            scene_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(scenes)").fetchall()
            }
            for column, definition in (
                ("chapter_number", "INTEGER"),
                ("chapter_title", "TEXT"),
                ("scene_number_in_chapter", "INTEGER"),
                ("part_number", "INTEGER"),
                ("part_title", "TEXT"),
                ("source_path", "TEXT"),
            ):
                if column not in scene_columns:
                    connection.execute(f"ALTER TABLE scenes ADD COLUMN {column} {definition}")
            # Older imports encoded the act in chapter_title/source_path. Seed
            # the new editable part taxonomy once, without touching prose.
            legacy_part_rows = connection.execute(
                "SELECT scene_number, source_path FROM scenes WHERE part_number IS NULL AND source_path IS NOT NULL"
            ).fetchall()
            for legacy_row in legacy_part_rows:
                act_match = re.search(r"(?:^|/)act[-_ ]?(\d+)(?:/|$)", str(legacy_row["source_path"]), flags=re.IGNORECASE)
                if act_match:
                    part_number = int(act_match.group(1))
                    connection.execute(
                        "UPDATE scenes SET part_number = ?, part_title = ? WHERE scene_number = ?",
                        (part_number, f"Part {part_number}", legacy_row["scene_number"]),
                    )
            connection.execute(
                "UPDATE scenes SET part_title = 'Epigraph' WHERE part_number = 0 AND part_title IN ('Part 0', 'Prologue')"
            )
            connection.execute(
                """UPDATE scenes
                   SET title = ''
                   WHERE title GLOB 'Scene [0-9]*'
                     AND source_path IS NOT NULL
                     AND lower(source_path) NOT LIKE '%scratch%'"""
            )
            draft_session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(draft_sessions)").fetchall()
            }
            for column, definition in (
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("archived_at", "TEXT"),
                ("archive_reason", "TEXT"),
            ):
                if column not in draft_session_columns:
                    connection.execute(f"ALTER TABLE draft_sessions ADD COLUMN {column} {definition}")
            annotation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(annotations)").fetchall()
            }
            for column, definition in (
                ("ai_advice", "TEXT"),
                ("ai_advice_updated_at", "TEXT"),
            ):
                if column not in annotation_columns:
                    connection.execute(f"ALTER TABLE annotations ADD COLUMN {column} {definition}")
            # One-time, text-neutral backfill of block ids (see assign_block_ids).
            for row in connection.execute("SELECT scene_number, content_html FROM scenes").fetchall():
                with_ids = assign_block_ids(row["content_html"])
                if with_ids != row["content_html"]:
                    connection.execute(
                        "UPDATE scenes SET content_html = ? WHERE scene_number = ?",
                        (with_ids, row["scene_number"]),
                    )
            scene_rows = connection.execute(
                "SELECT scene_number, content_html, version, updated_at FROM scenes"
            ).fetchall()
            for scene in scene_rows:
                has_revision = connection.execute(
                    "SELECT 1 FROM scene_revisions WHERE scene_number = ? LIMIT 1",
                    (scene["scene_number"],),
                ).fetchone()
                if not has_revision:
                    connection.execute(
                        """INSERT INTO scene_revisions(
                            scene_number, version, content_html, summary, source, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            scene["scene_number"],
                            scene["version"],
                            scene["content_html"],
                            "Initial scene snapshot",
                            "seed",
                            scene["updated_at"],
                        ),
                    )

            # Move legacy scratch records out of the linked manuscript path.
            # The old files are deliberately left untouched for recovery; the
            # Studio record now autosaves only to ignored project-local state.
            legacy_scratch_rows = connection.execute(
                """SELECT scene_number, source_path
                   FROM scenes
                   WHERE lower(source_path) LIKE 'manuscript/scratch/%'
                     AND (chapter_number IS NULL OR lower(chapter_title) LIKE '%scratch%')"""
            ).fetchall()
            existing_paths = {
                str(row[0]).replace("\\", "/")
                for row in connection.execute(
                    "SELECT source_path FROM scenes WHERE source_path IS NOT NULL"
                ).fetchall()
            }
            for legacy_row in legacy_scratch_rows:
                old_path = str(legacy_row["source_path"]).replace("\\", "/")
                filename = Path(old_path).name or f"scratch-{legacy_row['scene_number']}.md"
                new_path = f"{PROJECT_LOCAL_SCRATCH_PREFIX}{filename}"
                if new_path in existing_paths:
                    stem = Path(filename).stem
                    suffix = Path(filename).suffix or ".md"
                    new_path = f"{PROJECT_LOCAL_SCRATCH_PREFIX}legacy-{legacy_row['scene_number']}-{stem}{suffix}"
                connection.execute(
                    "UPDATE scenes SET source_path = ? WHERE scene_number = ?",
                    (new_path, legacy_row["scene_number"]),
                )
                existing_paths.add(new_path)
                migrated_scratch_scene_numbers.append(int(legacy_row["scene_number"]))

        for scene_number in migrated_scratch_scene_numbers:
            scene = self.get_scene(scene_number)
            if scene:
                self._sync_scene_to_disk(scene_number, scene.get("source_path"), scene["content_html"])

    def project_info(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT project_id, title, source_root, source_subpath, branch, commit_sha, source_digest, snapshot_path, manifest_path, working_tree_dirty, editor_settings_json, created_at FROM project_meta WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(self.project_id)
            info = dict(row)
            raw_settings = info.pop("editor_settings_json", None)
            try:
                stored_settings = json.loads(raw_settings) if raw_settings else None
            except (TypeError, json.JSONDecodeError):
                stored_settings = None
            info["editor_settings"] = normalise_editor_settings(stored_settings)
            info["storage"] = str(self.path)
            return info

    def get_editor_settings(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT editor_settings_json FROM project_meta WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(self.project_id)
        try:
            stored_settings = json.loads(row["editor_settings_json"]) if row["editor_settings_json"] else None
        except (TypeError, json.JSONDecodeError):
            stored_settings = None
        return normalise_editor_settings(stored_settings)

    def update_editor_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        normalised = normalise_editor_settings(settings)
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE project_meta SET editor_settings_json = ? WHERE project_id = ?",
                (json.dumps(normalised, sort_keys=True), self.project_id),
            )
        return normalised

    def upsert_support_document(
        self,
        *,
        document_key: str,
        kind: str,
        title: str,
        source_path: str,
        source_sha256: str,
        content: str,
    ) -> dict[str, Any]:
        """Store a project-owned reference document without touching manuscript prose."""
        key = document_key.strip()
        if not key:
            raise ValueError("Support document key cannot be empty.")
        now = utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM support_documents WHERE document_key = ?",
                (key,),
            ).fetchone()
            if existing is None:
                created_at = now
                connection.execute(
                    """INSERT INTO support_documents(
                        document_key, kind, title, source_path, source_sha256,
                        content, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, kind, title, source_path, source_sha256, content, created_at, now),
                )
            else:
                created_at = existing["created_at"]
                connection.execute(
                    """UPDATE support_documents
                       SET kind = ?, title = ?, source_path = ?, source_sha256 = ?,
                           content = ?, updated_at = ?
                       WHERE document_key = ?""",
                    (kind, title, source_path, source_sha256, content, now, key),
                )
        return {
            "document_key": key,
            "kind": kind,
            "title": title,
            "source_path": source_path,
            "source_sha256": source_sha256,
            "content": content,
            "created_at": created_at,
            "updated_at": now,
        }

    def list_support_documents(self, *, include_content: bool = False) -> list[dict[str, Any]]:
        """List only the support material owned by this isolated project."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT document_key, kind, title, source_path, source_sha256,
                          content, created_at, updated_at
                   FROM support_documents ORDER BY title"""
            ).fetchall()
        documents = []
        for row in rows:
            document = dict(row)
            if not include_content:
                document.pop("content", None)
            documents.append(document)
        return documents

    def get_support_document(self, document_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT document_key, kind, title, source_path, source_sha256,
                          content, created_at, updated_at
                   FROM support_documents WHERE document_key = ?""",
                (document_key,),
            ).fetchone()
        return dict(row) if row else None

    def update_source_metadata(
        self,
        *,
        source_root: str,
        source_subpath: str | None,
        branch: str,
        commit_sha: str,
        source_digest: str,
        snapshot_path: str,
        manifest_path: str,
        working_tree_dirty: bool,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE project_meta
                   SET source_root = ?, source_subpath = ?, branch = ?, commit_sha = ?, source_digest = ?,
                       snapshot_path = ?, manifest_path = ?, working_tree_dirty = ?
                   WHERE project_id = ?""",
                (
                    source_root,
                    source_subpath,
                    branch,
                    commit_sha,
                    source_digest,
                    snapshot_path,
                    manifest_path,
                    int(working_tree_dirty),
                    self.project_id,
                ),
            )
        return self.project_info()

    def reset_manuscript(self) -> None:
        """Clear only this project's imported manuscript and derived state."""
        with self._lock, self._connect() as connection:
            # Delete dependents first so this remains valid with foreign keys on.
            for table in (
                "intake_audits",
                "persona_evaluations",
                "agent_conversation_messages",
                "agent_conversations",
                "approval_batches",
                "annotations",
                "scene_revisions",
                "telemetry_events",
                "proposals",
                "graph_edges",
                "graph_nodes",
                "scenes",
            ):
                connection.execute(f"DELETE FROM {table}")

    def create_approval_batch(
        self,
        request_text: str,
        include: list[str],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        batch = {
            "batch_id": f"batch_{uuid4().hex}",
            "request_text": request_text.strip(),
            "include": include,
            "baseline": baseline,
            "status": "pending",
            "created_at": utc_now(),
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO approval_batches(
                    batch_id, request_text, include_json, baseline_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch["batch_id"],
                    batch["request_text"],
                    json.dumps(include, ensure_ascii=False),
                    json.dumps(baseline, ensure_ascii=False),
                    batch["status"],
                    batch["created_at"],
                ),
            )
        return batch

    def get_approval_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if not row:
            return None
        batch = dict(row)
        try:
            batch["include"] = json.loads(batch.pop("include_json"))
            batch["baseline"] = json.loads(batch.pop("baseline_json"))
        except (KeyError, json.JSONDecodeError):
            batch["include"] = []
            batch["baseline"] = {}
        for column, key in (("writeback_json", "writeback"), ("result_json", "result")):
            raw = batch.pop(column, None)
            try:
                batch[key] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                batch[key] = {}
        batch["proposals"] = self.batch_proposals(batch_id)
        return batch

    def pending_commit_batches(self) -> list[dict[str, Any]]:
        """Return reviewed batches that are ready for an author branch decision.

        A failed commit is deliberately not returned here.  It is a historical
        failure that needs review/re-preparation, not a live dirty-worktree
        checkpoint.  Keeping it out of this collection prevents the frontend
        from blocking navigation every time the desk opens.
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT batch_id, baseline_json, status, created_at, writeback_json "
                "FROM approval_batches ORDER BY created_at"
            ).fetchall()

        pending: list[dict[str, Any]] = []
        for row in rows:
            raw_writeback = row["writeback_json"]
            try:
                writeback = json.loads(raw_writeback) if raw_writeback else {}
            except json.JSONDecodeError:
                writeback = {}
            files = writeback.get("files") if isinstance(writeback, dict) else None
            if not isinstance(files, dict) or not files or writeback.get("commit_sha"):
                continue
            if row["status"] == "commit_failed":
                continue

            proposals = self.batch_proposals(row["batch_id"])
            if not proposals or any(item.get("status") == "staged" for item in proposals):
                continue
            try:
                baseline = json.loads(row["baseline_json"])
            except (TypeError, json.JSONDecodeError):
                baseline = {}
            pending.append(
                {
                    "batch_id": row["batch_id"],
                    "branch": baseline.get("branch"),
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "proposal_count": sum(item.get("status") == "accepted" for item in proposals),
                    "files": sorted(files),
                    "commit_error": writeback.get("commit_error"),
                }
            )
        return pending

    def failed_commit_batches(self) -> list[dict[str, Any]]:
        """Return historical commit failures without making them live blockers."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT batch_id, baseline_json, status, created_at, writeback_json "
                "FROM approval_batches WHERE status = 'commit_failed' ORDER BY created_at"
            ).fetchall()

        failed: list[dict[str, Any]] = []
        for row in rows:
            raw_writeback = row["writeback_json"]
            try:
                writeback = json.loads(raw_writeback) if raw_writeback else {}
            except json.JSONDecodeError:
                writeback = {}
            files = writeback.get("files") if isinstance(writeback, dict) else None
            if not isinstance(files, dict) or not files or writeback.get("commit_sha"):
                continue
            proposals = self.batch_proposals(row["batch_id"])
            if not proposals:
                continue
            try:
                baseline = json.loads(row["baseline_json"])
            except (TypeError, json.JSONDecodeError):
                baseline = {}
            failed.append(
                {
                    "batch_id": row["batch_id"],
                    "branch": baseline.get("branch"),
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "proposal_count": sum(item.get("status") == "accepted" for item in proposals),
                    "files": sorted(files),
                    "commit_error": writeback.get("commit_error"),
                }
            )
        return failed

    def batch_proposals(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE batch_id = ? ORDER BY scene_number, proposal_id",
                (batch_id,),
            ).fetchall()
        return [self._proposal(row) for row in rows]

    @staticmethod
    def _proposal(row: sqlite3.Row) -> dict[str, Any]:
        proposal = dict(row)
        raw = proposal.pop("validation_json", None)
        try:
            proposal["validation"] = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            proposal["validation"] = None
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return self._proposal(row) if row else None

    def update_staged_proposal(
        self,
        proposal_id: str,
        *,
        original_verbatim_text: str,
        proposed_replacement_text: str,
        editorial_rationale: str,
        change_type: str,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an author edit to a still-staged proposal."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if not row:
                raise KeyError(proposal_id)
            if row["status"] != "staged":
                raise ValueError("Proposal has already been reviewed.")
            connection.execute(
                """UPDATE proposals
                   SET original_verbatim_text = ?,
                       proposed_replacement_text = ?,
                       editorial_rationale = ?,
                       change_type = ?,
                       validation_json = ?
                   WHERE proposal_id = ?""",
                (
                    original_verbatim_text,
                    proposed_replacement_text,
                    editorial_rationale,
                    change_type,
                    json.dumps(validation, ensure_ascii=False),
                    proposal_id,
                ),
            )
        updated = self.get_proposal(proposal_id)
        if updated is None:
            raise KeyError(proposal_id)
        return updated

    def rebind_staged_proposal_source(
        self,
        proposal_id: str,
        *,
        source_branch: str | None,
        source_commit_sha: str | None,
        source_digest: str | None,
        source_scene_version: int | None,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        """Refresh source provenance on a still-staged proposal after linking a checkout."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if not row:
                raise KeyError(proposal_id)
            if row["status"] != "staged":
                raise ValueError("Proposal has already been reviewed.")
            connection.execute(
                """UPDATE proposals
                   SET source_branch = ?, source_commit_sha = ?, source_digest = ?,
                       source_scene_version = ?, validation_json = ?
                   WHERE proposal_id = ?""",
                (
                    source_branch,
                    source_commit_sha,
                    source_digest,
                    source_scene_version,
                    json.dumps(validation, ensure_ascii=False),
                    proposal_id,
                ),
            )
        updated = self.get_proposal(proposal_id)
        if updated is None:
            raise KeyError(proposal_id)
        return updated

    def update_approval_batch(
        self,
        batch_id: str,
        *,
        status: str | None = None,
        writeback: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        baseline: dict[str, Any] | None = None,
    ) -> None:
        """Update a batch's status, write-back ledger, or preparation result."""
        assignments: list[str] = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if writeback is not None:
            assignments.append("writeback_json = ?")
            values.append(json.dumps(writeback, ensure_ascii=False))
        if result is not None:
            assignments.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        if baseline is not None:
            assignments.append("baseline_json = ?")
            values.append(json.dumps(baseline, ensure_ascii=False))
        if not assignments:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE approval_batches SET {', '.join(assignments)} WHERE batch_id = ?",
                (*values, batch_id),
            )

    def proposal_history(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        """Reviewed proposals, newest first, with their batch request for context."""
        query = """SELECT p.*, b.request_text AS batch_request_text
                   FROM proposals p LEFT JOIN approval_batches b ON b.batch_id = p.batch_id
                   WHERE p.status != 'staged'"""
        params: list[Any] = []
        if status:
            query += " AND p.status = ?"
            params.append(status)
        query += " ORDER BY COALESCE(p.reviewed_at, '') DESC, p.proposal_id LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._proposal(row) for row in rows]

    def create_staged_proposal(
        self,
        *,
        batch_id: str,
        scene_number: int,
        proposed_by_persona_id: str,
        proposed_by_name: str,
        change_type: str,
        original_verbatim_text: str,
        proposed_replacement_text: str,
        editorial_rationale: str,
        source_branch: str | None,
        source_commit_sha: str | None,
        source_digest: str | None,
        source_scene_version: int,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proposal = {
            "proposal_id": f"prop_{uuid4().hex}",
            "batch_id": batch_id,
            "scene_number": scene_number,
            "proposed_by_persona_id": proposed_by_persona_id,
            "proposed_by_name": proposed_by_name,
            "change_type": change_type,
            "original_verbatim_text": original_verbatim_text,
            "proposed_replacement_text": proposed_replacement_text,
            "editorial_rationale": editorial_rationale,
            "source_branch": source_branch,
            "source_commit_sha": source_commit_sha,
            "source_digest": source_digest,
            "source_scene_version": source_scene_version,
            "status": "staged",
            "validation": validation,
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO proposals(
                    proposal_id, batch_id, scene_number, proposed_by_persona_id,
                    proposed_by_name, change_type, original_verbatim_text,
                    proposed_replacement_text, editorial_rationale, source_branch,
                    source_commit_sha, source_digest, source_scene_version, status,
                    validation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*tuple(proposal[key] for key in (
                    "proposal_id", "batch_id", "scene_number", "proposed_by_persona_id",
                    "proposed_by_name", "change_type", "original_verbatim_text",
                    "proposed_replacement_text", "editorial_rationale", "source_branch",
                    "source_commit_sha", "source_digest", "source_scene_version", "status",
                )), json.dumps(validation, ensure_ascii=False) if validation is not None else None),
            )
        return proposal

    def save_persona_evaluation(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        """Persist one advisory persona pass inside this isolated project."""
        payload = dict(evaluation)
        payload.setdefault("evaluation_id", f"eval_{uuid4().hex}")
        payload.setdefault("created_at", utc_now())
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO persona_evaluations(
                    evaluation_id, scene_number, persona_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    payload["evaluation_id"],
                    payload["scene_number"],
                    payload["persona_id"],
                    json.dumps(payload, ensure_ascii=False),
                    payload["created_at"],
                ),
            )
        return payload

    def list_persona_evaluations(
        self, scene_number: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self._lock, self._connect() as connection:
            if scene_number is None:
                rows = connection.execute(
                    """SELECT payload_json FROM persona_evaluations
                       ORDER BY created_at DESC LIMIT ?""",
                    (safe_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT payload_json FROM persona_evaluations
                       WHERE scene_number = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (scene_number, safe_limit),
                ).fetchall()
        evaluations: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                evaluations.append(payload)
        return evaluations

    def get_or_create_agent_conversation(self, agent_id: str, channel: str) -> dict[str, Any]:
        """Return one durable thread for an agent and Studio surface."""
        agent_id = agent_id.strip()
        channel = channel.strip()
        if not agent_id or channel not in {"roleplay", "analysis"}:
            raise ValueError("Agent conversation requires an agent and a roleplay or analysis channel.")
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_conversations WHERE agent_id = ? AND channel = ?",
                (agent_id, channel),
            ).fetchone()
            if row is None:
                conversation_id = f"conv_{uuid4().hex}"
                connection.execute(
                    """INSERT INTO agent_conversations(
                        conversation_id, agent_id, channel, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (conversation_id, agent_id, channel, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM agent_conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
        if row is None:
            raise KeyError(f"{agent_id}:{channel}")
        return self._agent_conversation(row)

    @staticmethod
    def _agent_conversation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "conversation_id": row["conversation_id"],
            "agent_id": row["agent_id"],
            "channel": row["channel"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_agent_conversation(self, agent_id: str, channel: str) -> dict[str, Any]:
        conversation = self.get_or_create_agent_conversation(agent_id, channel)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT message_id, role, content, metadata_json, created_at
                   FROM agent_conversation_messages
                   WHERE conversation_id = ?
                   ORDER BY message_id""",
                (conversation["conversation_id"],),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            except json.JSONDecodeError:
                metadata = {}
            messages.append(
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    **metadata,
                }
            )
        conversation["messages"] = messages
        return conversation

    def list_agent_conversations(self, channel: str, limit: int = 100) -> list[dict[str, Any]]:
        """List non-empty durable threads for one Studio surface."""
        if channel not in {"roleplay", "analysis"}:
            raise ValueError("Agent conversation requires a roleplay or analysis channel.")
        safe_limit = max(1, min(limit, 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT c.conversation_id, c.agent_id, c.channel, c.created_at, c.updated_at,
                          COUNT(m.message_id) AS message_count,
                          MAX(m.created_at) AS last_message_at
                   FROM agent_conversations c
                   JOIN agent_conversation_messages m ON m.conversation_id = c.conversation_id
                   WHERE c.channel = ?
                   GROUP BY c.conversation_id
                   ORDER BY c.updated_at DESC
                   LIMIT ?""",
                (channel, safe_limit),
            ).fetchall()
            summaries: list[dict[str, Any]] = []
            for row in rows:
                last_message = connection.execute(
                    """SELECT metadata_json FROM agent_conversation_messages
                       WHERE conversation_id = ? ORDER BY message_id DESC LIMIT 1""",
                    (row["conversation_id"],),
                ).fetchone()
                try:
                    metadata = json.loads(last_message["metadata_json"]) if last_message and last_message["metadata_json"] else {}
                except json.JSONDecodeError:
                    metadata = {}
                summaries.append(
                    {
                        **self._agent_conversation(row),
                        "message_count": row["message_count"],
                        "last_message_at": row["last_message_at"],
                        "last_context_label": metadata.get("context_label"),
                        "last_scene_number": metadata.get("scene_number"),
                    }
                )
        return summaries

    def append_agent_conversation_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "model"} or not content.strip():
            raise ValueError("Agent conversation messages require a user or model role and content.")
        now = utc_now()
        with self._lock, self._connect() as connection:
            conversation = connection.execute(
                "SELECT conversation_id FROM agent_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            cursor = connection.execute(
                """INSERT INTO agent_conversation_messages(
                    conversation_id, role, content, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now),
            )
            connection.execute(
                "UPDATE agent_conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
            message_id = cursor.lastrowid
        return {
            "message_id": message_id,
            "role": role,
            "content": content,
            "created_at": now,
            **(metadata or {}),
        }

    def save_intake_audit(self, audit: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO intake_audits(audit_id, source_label, status, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    audit["audit_id"],
                    audit["source_label"],
                    audit["status"],
                    json.dumps(audit, ensure_ascii=False),
                    audit["created_at"],
                ),
            )
        return audit

    def latest_intake_audit(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM intake_audits ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _scene(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["word_count"] = len(words(item["content_html"]))
        return item

    def list_scenes(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT scene_number, title, content_html, chapter_number, chapter_title, scene_number_in_chapter, part_number, part_title, source_path, version, updated_at FROM scenes ORDER BY scene_number"
            ).fetchall()
            return [self._scene(row) for row in rows]

    def get_scene(self, scene_number: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT scene_number, title, content_html, chapter_number, chapter_title, scene_number_in_chapter, part_number, part_title, source_path, version, updated_at FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            return self._scene(row) if row else None

    def _sync_scene_to_disk(self, scene_number: int, source_path: str | None, content_html: str) -> bool:
        if not source_path:
            return False
        from compiler_engine import html_to_markdown
        md_text = html_to_markdown(content_html)

        info = self.project_info()
        source_root = info.get("source_root")
        target_paths: list[Path] = []
        # Scratch files are deliberately project-local runtime material. They
        # must never be written into the linked manuscript checkout, even when
        # that checkout is configured for the project.
        project_local_scratch = source_path.replace("\\", "/").lstrip("/").startswith(PROJECT_LOCAL_SCRATCH_PREFIX)
        if source_root and not project_local_scratch:
            root = Path(source_root).resolve()
            relative = Path(source_path)
            candidates = [root / relative]
            project_prefix = f"{self.project_id}/"
            if source_path.startswith(project_prefix):
                candidates.insert(0, root / source_path[len(project_prefix):])
            source_subpath = str(info.get("source_subpath") or "").strip("/")
            if source_subpath and not source_path.startswith(f"{source_subpath}/"):
                candidates.append(root / source_subpath / relative)
            # Prefer the existing in-scope file. This handles imported paths
            # such as ``string-theories-v11/manuscript/...`` without creating
            # a duplicate nested checkout directory on write-back.
            target_paths.append(next((candidate for candidate in candidates if candidate.is_file()), candidates[0]))

        project_dir = self.path.parent / self.project_id
        target_paths.append(project_dir / source_path)

        written = False
        for target in target_paths:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(md_text, encoding="utf-8")
                written = True
            except OSError:
                pass
        return written

    def create_scene(
        self,
        title: str = "New Scene",
        content_html: str = "<p></p>",
        chapter_number: int | None = None,
        chapter_title: str | None = None,
        scene_number_in_chapter: int | None = None,
        part_number: int | None = None,
        part_title: str | None = None,
        source_path: str | None = None,
        is_scratch: bool = False,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            next_scene_number = connection.execute(
                "SELECT COALESCE(MAX(scene_number), 0) + 1 FROM scenes"
            ).fetchone()[0]

            clean_title = (title or "Untitled").strip()

            if is_scratch:
                chapter_number = None
                chapter_title = chapter_title or "Scratch & Drafts"
                scene_number_in_chapter = None
                requested_name = Path(source_path).name if source_path else clean_title
                requested_stem = Path(requested_name).stem
                slug = re.sub(r"[^a-z0-9]+", "-", requested_stem.casefold()).strip("-") or f"scratch-{next_scene_number}"
                source_path = f"{PROJECT_LOCAL_SCRATCH_PREFIX}{slug}.md"
            elif chapter_number is not None:
                if not chapter_title:
                    existing = connection.execute(
                        "SELECT chapter_title FROM scenes WHERE chapter_number = ? AND chapter_title IS NOT NULL LIMIT 1",
                        (chapter_number,),
                    ).fetchone()
                    chapter_title = existing[0] if existing else f"Chapter {chapter_number}"
                if scene_number_in_chapter is None:
                    max_in_ch = connection.execute(
                        "SELECT COALESCE(MAX(scene_number_in_chapter), 0) + 1 FROM scenes WHERE chapter_number = ?",
                        (chapter_number,),
                    ).fetchone()[0]
                    scene_number_in_chapter = max_in_ch
                if not source_path:
                    slug = re.sub(r"[^a-z0-9]+", "-", clean_title.casefold()).strip("-") or f"scene-{next_scene_number}"
                    source_path = f"manuscript/chapter-{chapter_number:02d}-{slug}.md"
            else:
                chapter_title = chapter_title or "Unassigned Scenes"
                if not source_path:
                    slug = re.sub(r"[^a-z0-9]+", "-", clean_title.casefold()).strip("-") or f"scene-{next_scene_number}"
                    source_path = f"manuscript/{slug}.md"

            if part_number is not None and not part_title:
                part_title = f"Part {part_number}"

            content_html = assign_block_ids(content_html or "<p></p>")
            now = utc_now()

            connection.execute(
                """INSERT INTO scenes(
                    scene_number, title, content_html, chapter_number, chapter_title,
                    scene_number_in_chapter, part_number, part_title, source_path, version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    next_scene_number,
                    clean_title,
                    content_html,
                    chapter_number,
                    chapter_title,
                    scene_number_in_chapter,
                    part_number,
                    part_title,
                    source_path,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO scene_revisions(
                    scene_number, version, content_html, summary, source, created_at
                ) VALUES (?, 1, ?, 'Created new scene/scratch file', 'editor', ?)""",
                (next_scene_number, content_html, now),
            )
            connection.execute(
                """INSERT INTO telemetry_events(
                    scene_number, event_type, words_added, words_deleted, summary, created_at
                ) VALUES (?, 'scene_created', ?, 0, ?, ?)""",
                (
                    next_scene_number,
                    len(words(content_html)),
                    f"Created {'scratch file' if is_scratch else 'scene'} {next_scene_number}: {clean_title}",
                    now,
                ),
            )

        self._sync_scene_to_disk(next_scene_number, source_path, content_html)
        return self.get_scene(next_scene_number)  # type: ignore[return-value]

    def update_scene_metadata(
        self,
        scene_number: int,
        *,
        title: str,
        chapter_number: int | None,
        chapter_title: str | None,
        scene_number_in_chapter: int | None,
        part_number: int | None,
        part_title: str | None,
    ) -> dict[str, Any]:
        """Update taxonomy only; never rewrite scene prose or source files."""
        clean_title = (title or "").strip()
        clean_chapter_title = (chapter_title or "").strip() or None
        clean_part_title = (part_title or "").strip() or (f"Part {part_number}" if part_number is not None else None)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT scene_number FROM scenes WHERE scene_number = ?", (scene_number,)
            ).fetchone()
            if row is None:
                raise KeyError(scene_number)
            now = utc_now()
            connection.execute(
                """UPDATE scenes
                   SET title = ?, chapter_number = ?, chapter_title = ?,
                       scene_number_in_chapter = ?, part_number = ?, part_title = ?,
                       updated_at = ?
                   WHERE scene_number = ?""",
                (
                    clean_title,
                    chapter_number,
                    clean_chapter_title,
                    scene_number_in_chapter,
                    part_number,
                    clean_part_title,
                    now,
                    scene_number,
                ),
            )
        return self.get_scene(scene_number)  # type: ignore[return-value]

    def update_scene(self, scene_number: int, content_html: str, summary: str = "Scene saved") -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT title, content_html, version, source_path FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            if not row:
                raise KeyError(scene_number)
            content_html = assign_block_ids(content_html or "<p></p>")
            added, deleted = word_delta(row["content_html"], content_html)
            now = utc_now()
            connection.execute(
                "UPDATE scenes SET content_html = ?, version = ?, updated_at = ? WHERE scene_number = ?",
                (content_html, row["version"] + 1, now, scene_number),
            )
            connection.execute(
                """INSERT INTO scene_revisions(
                    scene_number, version, content_html, summary, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (scene_number, row["version"] + 1, content_html, summary, "editor", now),
            )
            connection.execute(
                """INSERT INTO telemetry_events(
                    scene_number, event_type, words_added, words_deleted, summary, created_at
                ) VALUES (?, 'scene_saved', ?, ?, ?, ?)""",
                (scene_number, added, deleted, summary, now),
            )

            count = 0
            if row["source_path"]:
                count = connection.execute(
                    "SELECT COUNT(*) FROM scenes WHERE source_path = ?",
                    (row["source_path"],),
                ).fetchone()[0]

        if count == 1 and row["source_path"]:
            self._sync_scene_to_disk(scene_number, row["source_path"], content_html)

        return self.get_scene(scene_number)  # type: ignore[return-value]

    @staticmethod
    def _draft_document(row: sqlite3.Row) -> dict[str, Any]:
        document = dict(row)
        try:
            document["source_scene_numbers"] = json.loads(document.pop("source_scene_numbers_json"))
        except (KeyError, TypeError, json.JSONDecodeError):
            document["source_scene_numbers"] = []
        return document

    def _draft_session_payload(
        self, connection: sqlite3.Connection, session: sqlite3.Row
    ) -> dict[str, Any]:
        try:
            source_scene_numbers = json.loads(session["source_scene_numbers_json"])
        except (TypeError, json.JSONDecodeError):
            source_scene_numbers = []
        documents = connection.execute(
            """SELECT document_id, session_id, label, kind, content_html,
                      source_scene_numbers_json, version, created_at, updated_at
               FROM draft_documents
               WHERE session_id = ?
               ORDER BY CASE kind WHEN 'source' THEN 0 ELSE 1 END, created_at""",
            (session["session_id"],),
        ).fetchall()
        return {
            "session_id": session["session_id"],
            "title": session["title"],
            "chapter_number": session["chapter_number"],
            "source_scene_numbers": source_scene_numbers,
            "mode": session["mode"],
            "tether_enabled": bool(session["tether_enabled"]),
            "base_document_id": session["base_document_id"],
            "status": session["status"],
            "archived_at": session["archived_at"],
            "archive_reason": session["archive_reason"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "documents": [self._draft_document(row) for row in documents],
        }

    def create_draft_session(
        self,
        *,
        title: str,
        chapter_number: int | None = None,
        scene_number: int | None = None,
        mode: str = "tethered",
    ) -> dict[str, Any]:
        """Create a project-local side-by-side drafting session.

        The source document is a snapshot assembled from the selected chapter
        (or one scene for scratch material). It is deliberately stored outside
        ``scenes`` so opening and editing a draft side cannot alter canon.
        """
        if chapter_number is None and scene_number is None:
            raise ValueError("Choose a chapter or scene before starting a draft session.")
        if mode not in {"tethered", "piece_together"}:
            raise ValueError("Unknown draft session mode.")

        with self._lock, self._connect() as connection:
            if scene_number is not None:
                rows = connection.execute(
                    """SELECT scene_number, title, content_html
                       FROM scenes WHERE scene_number = ?""",
                    (scene_number,),
                ).fetchall()
            elif chapter_number is not None:
                rows = connection.execute(
                    """SELECT scene_number, title, content_html
                       FROM scenes WHERE chapter_number = ?
                       ORDER BY COALESCE(scene_number_in_chapter, 999999), scene_number""",
                    (chapter_number,),
                ).fetchall()
            else:
                rows = []
            if not rows:
                raise KeyError(chapter_number if chapter_number is not None else scene_number)

            source_scene_numbers = [int(row["scene_number"]) for row in rows]
            source_parts: list[str] = []
            for index, row in enumerate(rows):
                if len(rows) > 1:
                    source_parts.append(f"<h2>{escape(row['title'])}</h2>")
                source_parts.append(row["content_html"])
                if index < len(rows) - 1:
                    source_parts.append('<hr class="scene-break">')
            source_html = "".join(source_parts) or "<p></p>"

            session_id = f"draft_{uuid4().hex}"
            source_document_id = f"draftdoc_{uuid4().hex}"
            draft_document_id = f"draftdoc_{uuid4().hex}"
            now = utc_now()
            serialized_numbers = json.dumps(source_scene_numbers)
            connection.execute(
                """INSERT INTO draft_sessions(
                    session_id, title, chapter_number, source_scene_numbers_json,
                    mode, tether_enabled, base_document_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    session_id,
                    title.strip() or "Draft session",
                    chapter_number,
                    serialized_numbers,
                    mode,
                    source_document_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO draft_documents(
                    document_id, session_id, label, kind, content_html,
                    source_scene_numbers_json, version, created_at, updated_at
                ) VALUES (?, ?, ?, 'source', ?, ?, 1, ?, ?)""",
                (
                    source_document_id,
                    session_id,
                    "Original chapter",
                    source_html,
                    serialized_numbers,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO draft_documents(
                    document_id, session_id, label, kind, content_html,
                    source_scene_numbers_json, version, created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', '<p></p>', ?, 1, ?, ?)""",
                (
                    draft_document_id,
                    session_id,
                    "Working draft",
                    serialized_numbers,
                    now,
                    now,
                ),
            )
            session = connection.execute(
                "SELECT * FROM draft_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._draft_session_payload(connection, session)

    def get_draft_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM draft_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._draft_session_payload(connection, session) if session else None

    def list_draft_sessions(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return durable session metadata for the project navigator."""
        with self._lock, self._connect() as connection:
            status_clause = "" if include_archived else " WHERE status = 'active'"
            rows = connection.execute(
                """SELECT session_id, title, chapter_number, source_scene_numbers_json,
                          mode, tether_enabled, base_document_id, status,
                          archived_at, archive_reason, created_at, updated_at
                   FROM draft_sessions""" + status_clause + " ORDER BY updated_at DESC"
            ).fetchall()
        sessions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["source_scene_numbers"] = json.loads(item.pop("source_scene_numbers_json"))
            except (KeyError, TypeError, json.JSONDecodeError):
                item["source_scene_numbers"] = []
            item["tether_enabled"] = bool(item["tether_enabled"])
            sessions.append(item)
        return sessions

    def delete_draft_session(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM draft_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not exists:
                raise KeyError(session_id)
            connection.execute("DELETE FROM draft_documents WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM draft_sessions WHERE session_id = ?", (session_id,))

    def resolve_draft_session(self, session_id: str, action: str) -> dict[str, Any]:
        """Resolve a draft explicitly as canon or preserve canon and archive it.

        ``promote`` replaces exactly one Studio scene with the working-draft
        document, records a normal scene revision, and archives every active
        draft session that contains that scene. ``keep_canon`` leaves the
        manuscript untouched and archives only the selected draft session.
        """
        if action not in {"promote", "keep_canon"}:
            raise ValueError("Unknown draft resolution action.")
        with self._lock, self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM draft_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise KeyError(session_id)
            if session["status"] != "active":
                raise ValueError("Draft session is already archived.")
            try:
                source_scene_numbers = json.loads(session["source_scene_numbers_json"])
            except (TypeError, json.JSONDecodeError):
                source_scene_numbers = []

            now = utc_now()
            if action == "keep_canon":
                connection.execute(
                    """UPDATE draft_sessions
                       SET status = 'archived', archived_at = ?, archive_reason = ?, updated_at = ?
                       WHERE session_id = ?""",
                    (now, "Author kept manuscript canon", now, session_id),
                )
                return {
                    "status": "archived",
                    "action": action,
                    "session_id": session_id,
                    "scene_numbers": source_scene_numbers,
                }

            if len(source_scene_numbers) != 1:
                raise ValueError("Only a scene-scoped draft can be promoted to canon.")
            scene_number = int(source_scene_numbers[0])
            draft = connection.execute(
                """SELECT content_html FROM draft_documents
                   WHERE session_id = ? AND kind = 'draft'""",
                (session_id,),
            ).fetchone()
            if not draft or not html_to_text(draft["content_html"]).strip():
                raise ValueError("The working draft is empty; write draft prose before promoting it to canon.")

            active_sessions = connection.execute(
                """SELECT session_id, source_scene_numbers_json FROM draft_sessions
                   WHERE status = 'active'"""
            ).fetchall()
            archive_ids: list[str] = []
            for other in active_sessions:
                try:
                    other_scenes = json.loads(other["source_scene_numbers_json"])
                except (TypeError, json.JSONDecodeError):
                    other_scenes = []
                if scene_number in other_scenes:
                    archive_ids.append(other["session_id"])
            for archive_id in archive_ids:
                connection.execute(
                    """UPDATE draft_sessions
                       SET status = 'archived', archived_at = ?, archive_reason = ?, updated_at = ?
                       WHERE session_id = ?""",
                    (now, f"Superseded by canonical draft {session_id}", now, archive_id),
                )

        updated_scene = self.update_scene(
            scene_number,
            draft["content_html"],
            summary=f"Promoted draft session {session_id} to canon",
        )
        return {
            "status": "canonicalized",
            "action": action,
            "session_id": session_id,
            "scene_number": scene_number,
            "archived_session_ids": archive_ids,
            "scene": updated_scene,
        }

    def update_draft_session(
        self,
        session_id: str,
        *,
        mode: str | None = None,
        tether_enabled: bool | None = None,
        base_document_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM draft_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise KeyError(session_id)
            if mode is not None and mode not in {"tethered", "piece_together"}:
                raise ValueError("Unknown draft session mode.")
            if base_document_id is not None:
                found = connection.execute(
                    "SELECT 1 FROM draft_documents WHERE document_id = ? AND session_id = ?",
                    (base_document_id, session_id),
                ).fetchone()
                if not found:
                    raise ValueError("Base draft does not belong to this session.")
            now = utc_now()
            connection.execute(
                """UPDATE draft_sessions SET
                    mode = COALESCE(?, mode),
                    tether_enabled = COALESCE(?, tether_enabled),
                    base_document_id = COALESCE(?, base_document_id),
                    updated_at = ?
                   WHERE session_id = ?""",
                (
                    mode,
                    None if tether_enabled is None else int(tether_enabled),
                    base_document_id,
                    now,
                    session_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM draft_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._draft_session_payload(connection, updated)

    def update_draft_document(
        self, session_id: str, document_id: str, content_html: str
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM draft_documents
                   WHERE document_id = ? AND session_id = ?""",
                (document_id, session_id),
            ).fetchone()
            if not row:
                raise KeyError(document_id)
            now = utc_now()
            connection.execute(
                """UPDATE draft_documents SET content_html = ?, version = ?, updated_at = ?
                   WHERE document_id = ? AND session_id = ?""",
                (content_html or "<p></p>", row["version"] + 1, now, document_id, session_id),
            )
            connection.execute(
                "UPDATE draft_sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            updated = connection.execute(
                "SELECT * FROM draft_documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            return self._draft_document(updated)

    def import_scenes(
        self, title: str, text: str, split_mode: str = "single"
    ) -> list[dict[str, Any]]:
        sections = split_manuscript(text, title, split_mode)
        return self._persist_imported_sections([
            ImportedSection(title=scene_title, content_html=content_html)
            for scene_title, content_html in sections
        ])

    def import_repository_scenes(
        self, files: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        sections: list[ImportedSection] = []
        for item in sorted(files, key=lambda entry: _natural_path_key(entry["path"])):
            path = item.get("path", "")
            if Path(path).suffix.casefold() not in {".md", ".markdown"}:
                continue
            sections.extend(_markdown_scenes(path, item.get("text", "")))
        return self._persist_imported_sections(sections)

    def _persist_imported_sections(
        self, sections: list[ImportedSection]
    ) -> list[dict[str, Any]]:
        if not sections:
            raise ValueError("No manuscript material was found.")

        with self._lock, self._connect() as connection:
            next_scene_number = connection.execute(
                "SELECT COALESCE(MAX(scene_number), 0) + 1 FROM scenes"
            ).fetchone()[0]
            now = utc_now()
            imported_numbers: list[int] = []
            for offset, section in enumerate(sections):
                scene_number = next_scene_number + offset
                imported_numbers.append(scene_number)
                section = replace(section, content_html=assign_block_ids(section.content_html))
                connection.execute(
                    """INSERT INTO scenes(
                        scene_number, title, content_html, chapter_number, chapter_title,
                        scene_number_in_chapter, part_number, part_title, source_path, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        scene_number,
                        section.title,
                        section.content_html,
                        section.chapter_number,
                        section.chapter_title,
                        section.scene_number_in_chapter,
                        section.part_number,
                        section.part_title,
                        section.source_path,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO scene_revisions(
                        scene_number, version, content_html, summary, source, created_at
                    ) VALUES (?, 1, ?, ?, ?, ?)""",
                    (scene_number, section.content_html, "Imported manuscript material", "import", now),
                )
                connection.execute(
                    """INSERT INTO telemetry_events(
                        scene_number, event_type, words_added, words_deleted, summary, created_at
                    ) VALUES (?, 'manuscript_imported', ?, 0, ?, ?)""",
                    (
                        scene_number,
                        len(words(section.content_html)),
                        f"Imported scene {scene_number}: {section.title}",
                        now,
                    ),
                )

        return [self.get_scene(scene_number) for scene_number in imported_numbers]  # type: ignore[misc]

    def replace_imported_file(
        self,
        source_path: str,
        sections: list[ImportedSection],
        *,
        source: str = "source_repair",
        summary: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[int]]:
        """Replace one file's imported scene roster without touching other files.

        Existing scene IDs are reused in source order where possible so maps,
        navigator links, and open editor references remain stable. Removed
        trailing scenes have their scene-bound derived records removed before
        the scene row is deleted. The caller owns the source-file write and
        must perform its hash check first.
        """
        normalized_path = source_path.replace("\\", "/").strip("/")
        effective_summary = summary or f"Recompiled source file {normalized_path}"
        effective_telemetry = "git_pull_sync" if source == "git_pull" else "source_recompiled"
        with self._lock, self._connect() as connection:
            existing_rows = connection.execute(
                """SELECT * FROM scenes WHERE source_path = ?
                   ORDER BY COALESCE(scene_number_in_chapter, 999999), scene_number""",
                (normalized_path,),
            ).fetchall()
            if not existing_rows:
                # Repository-folder imports from older versions sometimes kept
                # the project id in source_path. Reuse those rows when the
                # canonical repo-relative path is now available.
                legacy_prefix = f"{self.project_id}/"
                existing_rows = connection.execute(
                    """SELECT * FROM scenes WHERE source_path = ?
                       ORDER BY COALESCE(scene_number_in_chapter, 999999), scene_number""",
                    (f"{legacy_prefix}{normalized_path}",),
                ).fetchall()

            now = utc_now()
            updated_numbers: list[int] = []
            for index, section in enumerate(sections, start=1):
                section = replace(section, content_html=assign_block_ids(section.content_html))
                if index <= len(existing_rows):
                    scene_number = int(existing_rows[index - 1]["scene_number"])
                    previous = existing_rows[index - 1]
                    version = int(previous["version"]) + 1
                    connection.execute(
                        """UPDATE scenes SET title = ?, content_html = ?, chapter_number = ?,
                           chapter_title = ?, scene_number_in_chapter = ?, part_number = ?,
                           part_title = ?, source_path = ?, version = ?, updated_at = ?
                           WHERE scene_number = ?""",
                        (
                            section.title,
                            section.content_html,
                            section.chapter_number,
                            section.chapter_title,
                            section.scene_number_in_chapter,
                            section.part_number,
                            section.part_title,
                            normalized_path,
                            version,
                            now,
                            scene_number,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO scene_revisions(
                           scene_number, version, content_html, summary, source, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            scene_number,
                            version,
                            section.content_html,
                            effective_summary,
                            source,
                            now,
                        ),
                    )
                else:
                    scene_number = int(connection.execute(
                        "SELECT COALESCE(MAX(scene_number), 0) + 1 FROM scenes"
                    ).fetchone()[0])
                    connection.execute(
                        """INSERT INTO scenes(
                           scene_number, title, content_html, chapter_number, chapter_title,
                           scene_number_in_chapter, part_number, part_title, source_path,
                           version, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                        (
                            scene_number,
                            section.title,
                            section.content_html,
                            section.chapter_number,
                            section.chapter_title,
                            section.scene_number_in_chapter,
                            section.part_number,
                            section.part_title,
                            normalized_path,
                            now,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO scene_revisions(
                           scene_number, version, content_html, summary, source, created_at
                           ) VALUES (?, 1, ?, ?, ?, ?)""",
                        (
                            scene_number,
                            section.content_html,
                            effective_summary,
                            source,
                            now,
                        ),
                    )
                updated_numbers.append(scene_number)
                connection.execute(
                    """INSERT INTO telemetry_events(
                       scene_number, event_type, words_added, words_deleted, summary, created_at
                       ) VALUES (?, ?, 0, 0, ?, ?)""",
                    (scene_number, effective_telemetry, f"{effective_summary} (v{section.title})", now),
                )

            removed_scene_numbers = [
                int(row["scene_number"])
                for row in existing_rows[len(sections):]
            ]
            for scene_number in removed_scene_numbers:
                for table in ("annotations", "persona_evaluations", "telemetry_events", "scene_revisions", "proposals"):
                    connection.execute(f"DELETE FROM {table} WHERE scene_number = ?", (scene_number,))
                connection.execute("DELETE FROM scenes WHERE scene_number = ?", (scene_number,))

        return [self.get_scene(number) for number in updated_numbers], removed_scene_numbers  # type: ignore[misc]

    def staged_proposals(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE status = 'staged' ORDER BY scene_number, proposal_id"
            ).fetchall()
            return [self._proposal(row) for row in rows]

    @staticmethod
    def _annotation(row: sqlite3.Row) -> dict[str, Any]:
        annotation = dict(row)
        raw_advice = annotation.get("ai_advice")
        if raw_advice:
            try:
                annotation["ai_advice"] = json.loads(raw_advice)
            except (TypeError, json.JSONDecodeError):
                annotation["ai_advice"] = None
        else:
            annotation["ai_advice"] = None
        return annotation

    def list_annotations(self, scene_number: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            scene = connection.execute(
                "SELECT content_html FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            if not scene:
                raise KeyError(scene_number)
            rows = connection.execute(
                """SELECT annotation_id, scene_number, paragraph_id, quote_text, body,
                          category, status, created_at, updated_at,
                          ai_advice, ai_advice_updated_at
                   FROM annotations
                   WHERE scene_number = ?
                   ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC""",
                (scene_number,),
            ).fetchall()
            current_html = scene["content_html"]
            annotations = []
            for row in rows:
                annotation = self._annotation(row)
                annotation["anchor_present"] = annotation["paragraph_id"] in current_html
                annotations.append(annotation)
            return annotations

    def create_annotation(
        self,
        scene_number: int,
        paragraph_id: str,
        quote_text: str,
        body: str,
        category: str = "note",
    ) -> dict[str, Any]:
        paragraph_id = paragraph_id.strip()
        quote_text = quote_text.strip()
        body = body.strip()
        category = category.strip() or "note"
        if not paragraph_id or not body:
            raise ValueError("An annotation requires a paragraph anchor and note body.")

        with self._lock, self._connect() as connection:
            scene = connection.execute(
                "SELECT content_html FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            if not scene:
                raise KeyError(scene_number)
            if paragraph_id not in scene["content_html"]:
                raise ValueError("The paragraph anchor is not present in the current scene.")

            annotation_id = f"ann_{uuid4().hex}"
            now = utc_now()
            connection.execute(
                """INSERT INTO annotations(
                    annotation_id, scene_number, paragraph_id, quote_text, body,
                    category, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (
                    annotation_id,
                    scene_number,
                    paragraph_id,
                    quote_text,
                    body,
                    category,
                    now,
                    now,
                ),
            )
        return next(
            annotation
            for annotation in self.list_annotations(scene_number)
            if annotation["annotation_id"] == annotation_id
        )

    def resolve_annotation(self, scene_number: int, annotation_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM annotations WHERE annotation_id = ? AND scene_number = ?",
                (annotation_id, scene_number),
            ).fetchone()
            if not row:
                raise KeyError(annotation_id)
            if row["status"] == "resolved":
                return dict(row)
            now = utc_now()
            connection.execute(
                "UPDATE annotations SET status = 'resolved', updated_at = ? WHERE annotation_id = ?",
                (now, annotation_id),
            )
            updated = connection.execute(
                "SELECT * FROM annotations WHERE annotation_id = ?",
                (annotation_id,),
            ).fetchone()
            return self._annotation(updated)

    def update_annotation_advice(
        self, scene_number: int, annotation_id: str, advice: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM annotations WHERE annotation_id = ? AND scene_number = ?",
                (annotation_id, scene_number),
            ).fetchone()
            if not row:
                raise KeyError(annotation_id)
            now = utc_now()
            connection.execute(
                """UPDATE annotations
                   SET ai_advice = ?, ai_advice_updated_at = ?, updated_at = ?
                   WHERE annotation_id = ? AND scene_number = ?""",
                (json.dumps(advice, ensure_ascii=False), now, now, annotation_id, scene_number),
            )
            updated = connection.execute(
                "SELECT * FROM annotations WHERE annotation_id = ? AND scene_number = ?",
                (annotation_id, scene_number),
            ).fetchone()
            return self._annotation(updated)

    def review_proposal(
        self, proposal_id: str, action: str, writeback_status: str | None = None
    ) -> dict[str, Any]:
        """Accept or reject a staged proposal.

        Accepting replaces the proposal's original text in the Studio scene.
        Staleness is per passage: the original must still appear exactly once
        (sibling proposals in the same scene stay valid). ``writeback_status``
        records whether the Markdown source was also patched.
        Failure: KeyError (unknown proposal); ValueError (already reviewed,
        scene gone, or passage no longer unique).
        """
        with self._lock, self._connect() as connection:
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if not proposal:
                raise KeyError(proposal_id)
            if proposal["status"] != "staged":
                raise ValueError("Proposal has already been reviewed.")

            if action == "accept":
                scene = connection.execute(
                    "SELECT content_html, version FROM scenes WHERE scene_number = ?",
                    (proposal["scene_number"],),
                ).fetchone()
                if not scene:
                    raise ValueError("The proposal scene no longer exists.")
                current = scene["content_html"]
                manual_sync = proposal["change_type"] == "manual_editor_sync"
                if manual_sync:
                    # The editor already contains the saved replacement. The
                    # approval only authorizes the matching Markdown write-back;
                    # never replace the author's live Studio copy with an old
                    # proposal snapshot.
                    updated = current
                else:
                    anchor = unique_html_anchor(current, proposal["original_verbatim_text"])
                    if anchor is None:
                        raise ValueError("The proposal's original text is no longer present exactly once in the scene.")
                    replacement = escape(proposal["proposed_replacement_text"], quote=False)
                    updated = current.replace(anchor, replacement, 1)
                now = utc_now()
                old_version = connection.execute(
                    "SELECT version FROM scenes WHERE scene_number = ?",
                    (proposal["scene_number"],),
                ).fetchone()[0]
                added, deleted = word_delta(current, updated)
                connection.execute(
                    "UPDATE scenes SET content_html = ?, version = ?, updated_at = ? WHERE scene_number = ?",
                    (updated, old_version + 1, now, proposal["scene_number"]),
                )
                connection.execute(
                    """INSERT INTO scene_revisions(
                        scene_number, version, content_html, summary, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        proposal["scene_number"],
                        old_version + 1,
                        updated,
                        f"Accepted proposal {proposal_id}",
                        "governance",
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO telemetry_events(
                        scene_number, event_type, words_added, words_deleted, summary, created_at
                    ) VALUES (?, 'proposal_accepted', ?, ?, ?, ?)""",
                    (
                        proposal["scene_number"],
                        added,
                        deleted,
                        f"Accepted proposal {proposal_id}",
                        now,
                    ),
                )

            new_status = "accepted" if action == "accept" else "rejected"
            connection.execute(
                "UPDATE proposals SET status = ?, writeback_status = ?, reviewed_at = ? WHERE proposal_id = ?",
                (new_status, writeback_status if action == "accept" else None, utc_now(), proposal_id),
            )
            return {"proposal_id": proposal_id, "new_status": new_status}

    def scene_history(self, scene_number: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT revision_id, scene_number, version, content_html,
                          summary, source, created_at
                   FROM scene_revisions
                   WHERE scene_number = ?
                   ORDER BY version DESC, revision_id DESC""",
                (scene_number,),
            ).fetchall()
            revisions = []
            for row in rows:
                revision = dict(row)
                revision["content_text"] = html_to_text(revision["content_html"])
                revisions.append(revision)
            return revisions

    def restore_revision(self, scene_number: int, revision_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            revision = connection.execute(
                """SELECT version, content_html FROM scene_revisions
                   WHERE revision_id = ? AND scene_number = ?""",
                (revision_id, scene_number),
            ).fetchone()
            if not revision:
                raise KeyError(revision_id)

            scene = connection.execute(
                "SELECT content_html, version FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            if not scene:
                raise KeyError(scene_number)
            if scene["content_html"] == revision["content_html"]:
                raise ValueError("That revision is already the current scene content.")

            now = utc_now()
            new_version = scene["version"] + 1
            summary = f"Restored revision v{revision['version']}"
            added, deleted = word_delta(scene["content_html"], revision["content_html"])
            connection.execute(
                """UPDATE scenes
                   SET content_html = ?, version = ?, updated_at = ?
                   WHERE scene_number = ?""",
                (revision["content_html"], new_version, now, scene_number),
            )
            connection.execute(
                """INSERT INTO scene_revisions(
                    scene_number, version, content_html, summary, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (scene_number, new_version, revision["content_html"], summary, "history", now),
            )
            connection.execute(
                """INSERT INTO telemetry_events(
                    scene_number, event_type, words_added, words_deleted, summary, created_at
                ) VALUES (?, 'history_restored', ?, ?, ?, ?)""",
                (scene_number, added, deleted, summary, now),
            )
        return self.get_scene(scene_number)  # type: ignore[return-value]

    def telemetry(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            totals = connection.execute(
                """SELECT COALESCE(SUM(words_added), 0), COALESCE(SUM(words_deleted), 0)
                   FROM telemetry_events WHERE event_type != 'manuscript_imported'"""
            ).fetchone()
            imported = connection.execute(
                "SELECT COALESCE(SUM(words_added), 0) FROM telemetry_events WHERE event_type = 'manuscript_imported'"
            ).fetchone()[0]
            latest = connection.execute(
                "SELECT scene_number, summary, created_at FROM telemetry_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            graph_count = connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
            edge_count = connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        added = int(totals[0])
        deleted = int(totals[1])
        return {
            "session_id": "local-studio",
            "net_word_change": added - deleted,
            "words_added": added,
            "words_deleted": deleted,
            "imported_words": int(imported),
            "active_plot_tags": [
                f"#scene:{latest['scene_number']}" if latest else "#scene:none",
                "#graph:story-bible",
            ],
            "graph_status": f"Local graph ready ({graph_count} nodes, {edge_count} edges)",
            "semantic_summary": latest["summary"] if latest else "No scene edits recorded yet.",
            "last_updated": latest["created_at"] if latest else None,
        }

    def story_graph(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            nodes = [dict(row) for row in connection.execute("SELECT * FROM graph_nodes ORDER BY node_id")]
            edges = [dict(row) for row in connection.execute("SELECT * FROM graph_edges ORDER BY source_id, target_id")]
        return {
            "source": "local-story-graph",
            "status": "indexed" if nodes else "empty",
            "nodes": [
                {"data": {"id": node["node_id"], "label": node["label"], "type": node["node_type"]}}
                for node in nodes
            ],
            "edges": [
                {
                    "data": {
                        "source": edge["source_id"],
                        "target": edge["target_id"],
                        "relationship": edge["relationship"],
                    }
                }
                for edge in edges
            ],
        }

    def story_intelligence(self) -> dict[str, Any]:
        """Build ordered chapter, pressure, and entity-track data for the UI."""
        with self._lock, self._connect() as connection:
            scene_rows = connection.execute(
                """SELECT scene_number, title, content_html, chapter_number,
                          chapter_title, scene_number_in_chapter, part_number,
                          part_title, source_path
                   FROM scenes ORDER BY scene_number"""
            ).fetchall()
            node_rows = connection.execute(
                "SELECT node_id, label, node_type FROM graph_nodes"
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT source_id, target_id, relationship FROM graph_edges"
            ).fetchall()

        nodes = {row["node_id"]: dict(row) for row in node_rows}
        entity_scene_ids: dict[str, set[int]] = {}
        for edge in edge_rows:
            if edge["relationship"] != "MENTIONED_IN" or not edge["target_id"].startswith("scene:"):
                continue
            try:
                scene_number = int(edge["target_id"].split(":", 1)[1])
            except ValueError:
                continue
            entity_scene_ids.setdefault(edge["source_id"], set()).add(scene_number)

        scenes: list[dict[str, Any]] = []
        entity_anchors: dict[str, list[dict[str, Any]]] = {}
        chapters: dict[tuple[int | None, int | None, str], dict[str, Any]] = {}
        for row in scene_rows:
            text = html_to_text(row["content_html"])
            paragraphs = paragraph_texts(row["content_html"])
            profile = pressure_profile(text)
            scene_number = int(row["scene_number"])
            entity_ids = sorted(
                entity_id
                for entity_id, scene_numbers in entity_scene_ids.items()
                if scene_number in scene_numbers and entity_id in nodes
            )
            entities = [
                {
                    "id": entity_id,
                    "label": nodes[entity_id]["label"],
                    "type": nodes[entity_id]["node_type"],
                }
                for entity_id in entity_ids
            ]
            scene_entity_anchors: dict[str, list[dict[str, Any]]] = {}
            for entity in entities:
                label = entity["label"]
                matches = [
                    {
                        "scene_number": scene_number,
                        "paragraph_index": paragraph_index,
                        "quote_text": paragraph[:280],
                    }
                    for paragraph_index, paragraph in enumerate(paragraphs)
                    if paragraph and re.search(re.escape(label), paragraph, flags=re.IGNORECASE)
                ]
                if matches:
                    scene_entity_anchors[entity["id"]] = matches
                    entity_anchors.setdefault(entity["id"], []).extend(matches)
            scene = {
                "scene_number": scene_number,
                "title": row["title"],
                "chapter_number": row["chapter_number"],
                "chapter_title": row["chapter_title"],
                "scene_number_in_chapter": row["scene_number_in_chapter"],
                "part_number": row["part_number"],
                "part_title": row["part_title"],
                "source_path": row["source_path"],
                "word_count": profile["word_count"],
                "pressure_score": profile["score"],
                "pressure_components": profile["components"],
                "tracked_entities": entities,
                "entity_anchors": scene_entity_anchors,
            }
            scenes.append(scene)
            chapter_key = (row["part_number"], row["chapter_number"], row["chapter_title"] or "Unstructured material")
            chapter = chapters.setdefault(
                chapter_key,
                {
                    "part_number": row["part_number"],
                    "part_title": row["part_title"],
                    "chapter_number": row["chapter_number"],
                    "title": row["chapter_title"] or "Unstructured material",
                    "scene_numbers": [],
                    "scenes": [],
                },
            )
            chapter["scene_numbers"].append(scene_number)
            chapter["scenes"].append(scene)

        tracks = []
        for entity_id, scene_numbers in entity_scene_ids.items():
            node = nodes.get(entity_id)
            if not node or not scene_numbers:
                continue
            node_type = node["node_type"]
            tracks.append(
                {
                    "track_id": entity_id,
                    "label": node["label"],
                    "type": node_type,
                    "track_kind": "key_object" if node_type == "artifact" else "story_entity",
                    "appearances": sorted(scene_numbers),
                    "appearance_anchors": sorted(
                        entity_anchors.get(entity_id, []),
                        key=lambda anchor: (anchor["scene_number"], anchor["paragraph_index"]),
                    ),
                    "first_scene": min(scene_numbers),
                    "last_scene": max(scene_numbers),
                    "recurrence": len(scene_numbers),
                }
            )
        tracks.sort(
            key=lambda track: (
                0 if track["track_kind"] == "key_object" else 1,
                -track["recurrence"],
                track["label"].casefold(),
            )
        )

        ordered_chapters = list(chapters.values())
        for chapter in ordered_chapters:
            chapter["pressure_average"] = round(
                sum(scene["pressure_score"] for scene in chapter["scenes"])
                / max(len(chapter["scenes"]), 1)
            )
            chapter["word_count"] = sum(scene["word_count"] for scene in chapter["scenes"])

        return {
            "project_id": self.project_id,
            "source": "project-manuscript-and-local-story-graph",
            "pressure_method": "Sentence velocity, conflict-language density, and dialogue density; navigation signal, not editorial verdict.",
            "scene_count": len(scenes),
            "chapter_count": len(ordered_chapters),
            "chapters": ordered_chapters,
            "scenes": scenes,
            "tracks": tracks,
        }

    def rebuild_story_graph(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            scene_rows = connection.execute(
                "SELECT scene_number, title, chapter_title, scene_number_in_chapter, content_html FROM scenes ORDER BY scene_number"
            ).fetchall()
            scenes = [
                {
                    "scene_number": row["scene_number"],
                    "title": row["title"],
                    "chapter_title": row["chapter_title"],
                    "scene_number_in_chapter": row["scene_number_in_chapter"],
                    "content_text": html_to_text(row["content_html"]),
                }
                for row in scene_rows
            ]
            nodes, edges = build_graph(scenes)
            connection.execute("DELETE FROM graph_edges")
            connection.execute("DELETE FROM graph_nodes")
            connection.executemany(
                "INSERT INTO graph_nodes(node_id, label, node_type) VALUES (?, ?, ?)",
                [(node["node_id"], node["label"], node["node_type"]) for node in nodes],
            )
            connection.executemany(
                "INSERT INTO graph_edges(source_id, target_id, relationship) VALUES (?, ?, ?)",
                [
                    (edge["source_id"], edge["target_id"], edge["relationship"])
                    for edge in edges
                ],
            )
        return self.story_graph()
