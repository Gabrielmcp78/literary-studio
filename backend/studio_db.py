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
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from threading import RLock
from typing import Any

from graph_extractor import build_graph

PROJECTS_ROOT = Path(__file__).resolve().parent.parent / "build_artifacts" / "projects"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def is_repository_manuscript_path(path: str) -> bool:
    parts = [part for part in path.replace("\\", "/").strip("/").split("/") if part]
    if not parts:
        return False
    filename = parts[-1].casefold()
    if filename in {"readme.md", "license.md", "changelog.md"}:
        return False
    if any(part.startswith(".") for part in parts):
        return False
    return any(
        re.match(r"^acts?(?:\s|[-_]|$)", part, re.IGNORECASE)
        or re.match(r"^chapters?$", part, re.IGNORECASE)
        or re.match(r"^appendix(?:\s|[-_]|$)", part, re.IGNORECASE)
        or re.search(r"chapter", part, re.IGNORECASE)
        for part in parts
    )


@dataclass(frozen=True)
class ImportedSection:
    title: str
    content_html: str
    chapter_number: int | None = None
    chapter_title: str | None = None
    scene_number_in_chapter: int | None = None
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
        if re.fullmatch(r"(?:\*\s*){3,}|[-_]{3,}|⁂", clean):
            rendered.append('<hr class="scene-break" />')
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", clean)
        if heading:
            level = min(len(heading.group(1)), 6)
            rendered.append(f"<h{level}>{_render_inline_markdown(heading.group(2).strip())}</h{level}>")
        elif all(line.lstrip().startswith(">") for line in clean.splitlines()):
            quote = re.sub(r"^\s*>\s?", "", clean, flags=re.MULTILINE)
            rendered.append(f"<blockquote><p>{_render_inline_markdown(quote)}</p></blockquote>")
        else:
            rendered.append(f"<p>{_render_inline_markdown(clean)}</p>")
    return "".join(rendered)


def _chapter_number(value: str) -> int | None:
    match = re.search(r"(?:chapter|ch)[\s._-]*(\d+)", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


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
    title_match = re.search(r"^\s*#\s+(.+?)\s*$", body_text, flags=re.MULTILINE)
    if title_match:
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
    chapter_title = f"{act_title} / {file_title}" if act_title else file_title
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
                source_path=normalized_path,
            ))
        return sections

    scene_heading = re.compile(
        r"^\s*##\s+Scene\s+([0-9]+|[IVXLC]+)(?:\s*[—–:-]\s*(.*?))?\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    matches = list(scene_heading.finditer(body_text))
    if not matches:
        scene_parts = re.split(r"(?m)^\s*(?:\*\s*\*\s*\*|⁂)\s*$", body_text)
        sections: list[ImportedSection] = []
        for index, scene_part in enumerate(scene_parts, start=1):
            rendered = _render_markdown_body(scene_part)
            if rendered:
                sections.append(ImportedSection(
                    title=f"Scene {index}",
                    content_html=rendered,
                    chapter_number=chapter_number,
                    chapter_title=chapter_title,
                    scene_number_in_chapter=index,
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
        scene_title = re.sub(r"[`*_]", "", (match.group(2) or "").strip()) or f"Scene {token}"
        sections.append(ImportedSection(
            title=scene_title,
            content_html=rendered,
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            scene_number_in_chapter=scene_number,
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
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
            ):
                if column not in proposal_columns:
                    connection.execute(f"ALTER TABLE proposals ADD COLUMN {column} {definition}")
            scene_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(scenes)").fetchall()
            }
            for column, definition in (
                ("chapter_number", "INTEGER"),
                ("chapter_title", "TEXT"),
                ("scene_number_in_chapter", "INTEGER"),
                ("source_path", "TEXT"),
            ):
                if column not in scene_columns:
                    connection.execute(f"ALTER TABLE scenes ADD COLUMN {column} {definition}")
            annotation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(annotations)").fetchall()
            }
            for column, definition in (
                ("ai_advice", "TEXT"),
                ("ai_advice_updated_at", "TEXT"),
            ):
                if column not in annotation_columns:
                    connection.execute(f"ALTER TABLE annotations ADD COLUMN {column} {definition}")
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

    def project_info(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT project_id, title, source_root, source_subpath, branch, commit_sha, source_digest, snapshot_path, manifest_path, working_tree_dirty, created_at FROM project_meta WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(self.project_id)
            info = dict(row)
            info["storage"] = str(self.path)
            return info

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
        batch["proposals"] = self.batch_proposals(batch_id)
        return batch

    def batch_proposals(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE batch_id = ? ORDER BY scene_number, proposal_id",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO proposals(
                    proposal_id, batch_id, scene_number, proposed_by_persona_id,
                    proposed_by_name, change_type, original_verbatim_text,
                    proposed_replacement_text, editorial_rationale, source_branch,
                    source_commit_sha, source_digest, source_scene_version, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(proposal[key] for key in (
                    "proposal_id", "batch_id", "scene_number", "proposed_by_persona_id",
                    "proposed_by_name", "change_type", "original_verbatim_text",
                    "proposed_replacement_text", "editorial_rationale", "source_branch",
                    "source_commit_sha", "source_digest", "source_scene_version", "status",
                )),
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
                "SELECT scene_number, title, content_html, chapter_number, chapter_title, scene_number_in_chapter, source_path, version, updated_at FROM scenes ORDER BY scene_number"
            ).fetchall()
            return [self._scene(row) for row in rows]

    def get_scene(self, scene_number: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT scene_number, title, content_html, chapter_number, chapter_title, scene_number_in_chapter, source_path, version, updated_at FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            return self._scene(row) if row else None

    def update_scene(self, scene_number: int, content_html: str, summary: str = "Scene saved") -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT title, content_html, version FROM scenes WHERE scene_number = ?",
                (scene_number,),
            ).fetchone()
            if not row:
                raise KeyError(scene_number)
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
        return self.get_scene(scene_number)  # type: ignore[return-value]

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
                connection.execute(
                    """INSERT INTO scenes(
                        scene_number, title, content_html, chapter_number, chapter_title,
                        scene_number_in_chapter, source_path, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        scene_number,
                        section.title,
                        section.content_html,
                        section.chapter_number,
                        section.chapter_title,
                        section.scene_number_in_chapter,
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

    def staged_proposals(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE status = 'staged' ORDER BY scene_number, proposal_id"
            ).fetchall()
            return [dict(row) for row in rows]

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

    def review_proposal(self, proposal_id: str, action: str) -> dict[str, Any]:
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
                if (
                    proposal["source_scene_version"] is not None
                    and int(scene["version"]) != int(proposal["source_scene_version"])
                ):
                    raise ValueError("The scene changed after this proposal was prepared.")
                current = scene["content_html"]
                original = proposal["original_verbatim_text"]
                anchors = (original, escape(original, quote=False))
                anchor = next((candidate for candidate in anchors if candidate in current), None)
                if anchor is None:
                    raise ValueError("The proposal anchor is not present in the current scene.")
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
                "UPDATE proposals SET status = ? WHERE proposal_id = ?",
                (new_status, proposal_id),
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
                "SELECT COALESCE(SUM(words_added), 0), COALESCE(SUM(words_deleted), 0) FROM telemetry_events"
            ).fetchone()
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
                          chapter_title, scene_number_in_chapter, source_path
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
        chapters: dict[tuple[int | None, str], dict[str, Any]] = {}
        for row in scene_rows:
            text = html_to_text(row["content_html"])
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
            scene = {
                "scene_number": scene_number,
                "title": row["title"],
                "chapter_number": row["chapter_number"],
                "chapter_title": row["chapter_title"],
                "scene_number_in_chapter": row["scene_number_in_chapter"],
                "source_path": row["source_path"],
                "word_count": profile["word_count"],
                "pressure_score": profile["score"],
                "pressure_components": profile["components"],
                "tracked_entities": entities,
            }
            scenes.append(scene)
            chapter_key = (row["chapter_number"], row["chapter_title"] or "Unstructured material")
            chapter = chapters.setdefault(
                chapter_key,
                {
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
