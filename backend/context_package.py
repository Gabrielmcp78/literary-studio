"""Build read-only, provenance-pinned context packages for external LLM chats."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from studio_db import StudioDB, html_to_text, is_repository_manuscript_path


CONTEXT_OPTIONS = (
    "plot",
    "science",
    "character",
    "subtext",
    "causality",
    "documentation",
)
MAX_SUPPORTING_DOC_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_CHARS = 4_000_000


class ContextPackageError(ValueError):
    pass


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()


def _source_root(project: dict[str, Any]) -> Path | None:
    raw = project.get("source_root")
    if not raw:
        return None
    root = Path(str(raw)).expanduser().resolve()
    return root if root.is_dir() else None


def _scope_prefix(project: dict[str, Any], root: Path) -> str:
    raw = project.get("source_subpath")
    if not raw:
        return ""
    candidate = Path(str(raw))
    scope = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if scope == root or root in scope.parents:
        return scope.relative_to(root).as_posix()
    return ""


def _supporting_markdown(project: dict[str, Any], option: str) -> list[tuple[str, str]]:
    root = _source_root(project)
    if root is None:
        return []
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
        "*.yaml",
        "*.yml",
        "*.json",
    )
    if not raw:
        return []

    keywords = {
        "plot": ("plot", "narrative", "story", "arc", "beat", "timeline", "ledger", "continuity", "structure"),
        "science": ("science", "research", "technology", "physics", "biology", "medical", "world", "setting", "lore", "reference", "outpost"),
        "character": ("character", "cast", "people", "persona", "profile", "dynamics", "protagonist", "antagonist", "relationship"),
        "subtext": ("subtext", "theme", "historical", "alignment", "audit"),
        "causality": ("causal", "continuity", "ledger", "narrative", "structure", "revision", "plot", "story"),
        "documentation": ("readme", "doc", "guide", "bible", "notes", "manifest", "fiction", "project", "protocol", "briefing", "config"),
    }.get(option, ())
    candidates: list[tuple[str, str]] = []
    total_bytes = 0
    for relative_path in sorted(
        (path for path in raw.split("\0") if path),
        key=lambda value: (value.casefold(), value),
    ):
        if is_repository_manuscript_path(relative_path):
            continue
        path_parts = {part.casefold() for part in relative_path.split("/")}
        if path_parts & {".git", ".next", "node_modules", "build_artifacts", ".vscode"}:
            continue
        if "/.narrative/reports/" in f"/{relative_path}" or relative_path.startswith(".narrative/reports/"):
            continue
        lower_path = relative_path.casefold()
        if not any(keyword in lower_path for keyword in keywords):
            continue
        file_path = (root / relative_path).resolve()
        if root not in file_path.parents or not file_path.is_file():
            continue
        try:
            content_bytes = file_path.read_bytes()
        except OSError:
            continue
        if total_bytes + len(content_bytes) > MAX_SUPPORTING_DOC_BYTES:
            break
        total_bytes += len(content_bytes)
        candidates.append((relative_path, content_bytes.decode("utf-8", errors="replace")))
    return candidates


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body.strip()}\n"


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"


def _supporting_docs_body(project: dict[str, Any], option: str) -> str:
    docs = _supporting_markdown(project, option)
    if not docs:
        return "No matching source support documents were found in the selected project repository."
    return "\n\n".join(
        f"### Source: `{path}`\n\n{content.strip()}" for path, content in docs
    )


def _remote_base(root: Path, commit_sha: str, dirty: bool) -> str | None:
    if dirty or not commit_sha:
        return None
    remote = _git(root, "remote", "get-url", "origin")
    if not remote:
        return None
    if remote.startswith("git@") and ":" in remote:
        remote = "https://" + remote.split(":", 1)[1]
    remote = re.sub(r"\.git$", "", remote.rstrip("/"))
    if remote.startswith("http://"):
        remote = "https://" + remote.removeprefix("http://")
    if not remote.startswith("https://"):
        return None
    return f"{remote}/blob/{commit_sha}"


def build_context_package(
    db: StudioDB,
    chapter_start: int,
    chapter_end: int,
    include: list[str] | None = None,
) -> dict[str, Any]:
    if chapter_start < 1 or chapter_end < chapter_start:
        raise ContextPackageError("Chapter range is invalid.")
    selected_options = [option for option in (include or []) if option in CONTEXT_OPTIONS]
    selected_options = list(dict.fromkeys(selected_options))
    project = db.project_info()
    scenes = [
        scene
        for scene in db.list_scenes()
        if scene.get("chapter_number") is not None
        and chapter_start <= int(scene["chapter_number"]) <= chapter_end
    ]
    if not scenes:
        raise ContextPackageError(
            f"No manuscript scenes were found for chapters {chapter_start}-{chapter_end}."
        )

    scene_numbers = {int(scene["scene_number"]) for scene in scenes}
    scene_by_chapter: dict[int, list[dict[str, Any]]] = {}
    for scene in scenes:
        scene_by_chapter.setdefault(int(scene["chapter_number"]), []).append(scene)

    manifest = {
        "project_id": project["project_id"],
        "title": project["title"],
        "chapters": list(range(chapter_start, chapter_end + 1)),
        "branch": project.get("branch"),
        "commit_sha": project.get("commit_sha"),
        "source_digest": project.get("source_digest"),
        "working_tree_dirty": bool(project.get("working_tree_dirty")),
        "source_root": project.get("source_root"),
        "source_subpath": project.get("source_subpath"),
        "included": ["manuscript", *selected_options],
    }

    sections = [
        "# Literary Studio Context Package",
        "",
        "This is a read-only context package. Treat the manuscript text below as authoritative for the identified project, chapters, branch, commit, and source digest. Do not rewrite or submit changes to the source repository.",
        _section("Provenance", _json_block(manifest)),
    ]

    manuscript_lines: list[str] = []
    for chapter_number in sorted(scene_by_chapter):
        chapter_scenes = sorted(
            scene_by_chapter[chapter_number],
            key=lambda scene: int(scene.get("scene_number_in_chapter") or scene["scene_number"]),
        )
        chapter_title = chapter_scenes[0].get("chapter_title") or f"Chapter {chapter_number}"
        manuscript_lines.append(f"### Chapter {chapter_number}: {chapter_title}")
        for scene in chapter_scenes:
            scene_label = scene.get("scene_number_in_chapter") or scene["scene_number"]
            source_path = f" · `{scene['source_path']}`" if scene.get("source_path") else ""
            manuscript_lines.append(f"#### Scene {scene_label}: {scene['title']}{source_path}\n")
            manuscript_lines.append(html_to_text(scene["content_html"]).strip())
            manuscript_lines.append("")
    sections.append(_section("Manuscript text", "\n".join(manuscript_lines)))

    intelligence = db.story_intelligence()
    selected_intelligence_scenes = [
        scene for scene in intelligence["scenes"] if int(scene["scene_number"]) in scene_numbers
    ]
    selected_intelligence_chapters = [
        chapter
        for chapter in intelligence["chapters"]
        if any(int(number) in scene_numbers for number in chapter["scene_numbers"])
    ]
    selected_tracks = [
        track
        for track in intelligence["tracks"]
        if any(int(number) in scene_numbers for number in track["appearances"])
    ]

    if "plot" in selected_options:
        plot_docs = _supporting_docs_body(project, "plot")
        sections.append(
            _section(
                "Plot and narrative pressure",
                _json_block(
                    {
                        "pressure_method": intelligence["pressure_method"],
                        "chapters": selected_intelligence_chapters,
                        "scenes": selected_intelligence_scenes,
                        "tracked_entities": selected_tracks,
                    }
                )
                + "\n\n### Source plot and narrative material\n\n"
                + plot_docs,
            )
        )

    graph = db.story_graph()
    selected_graph_nodes = [
        node
        for node in graph["nodes"]
        if node["data"]["id"] in {f"scene:{number}" for number in scene_numbers}
        or node["data"]["type"] in {"character", "artifact", "location"}
    ]
    selected_graph_edges = [
        edge
        for edge in graph["edges"]
        if edge["data"]["source"] in {f"scene:{number}" for number in scene_numbers}
        or edge["data"]["target"] in {f"scene:{number}" for number in scene_numbers}
    ]

    if "character" in selected_options:
        character_nodes = [
            node for node in selected_graph_nodes if node["data"]["type"] == "character"
        ]
        character_edges = [
            edge
            for edge in selected_graph_edges
            if edge["data"]["source"] in {node["data"]["id"] for node in character_nodes}
            or edge["data"]["target"] in {node["data"]["id"] for node in character_nodes}
        ]
        body = _json_block({"nodes": character_nodes, "evidence_edges": character_edges})
        body += "\n\n### Source character material\n\n" + _supporting_docs_body(project, "character")
        sections.append(_section("Character and entity evidence", body))

    if "causality" in selected_options:
        causality_docs = _supporting_docs_body(project, "causality")
        sections.append(
            _section(
                "Causality and scene adjacency evidence",
                "This section reports extracted evidence only; it is not an inferred causal verdict.\n\n"
                + _json_block({"scene_edges": selected_graph_edges, "scene_sequence": selected_intelligence_scenes})
                + "\n\n### Source continuity and causality material\n\n"
                + causality_docs,
            )
        )

    if "subtext" in selected_options:
        notes: list[dict[str, Any]] = []
        for scene_number in sorted(scene_numbers):
            notes.extend(db.list_annotations(scene_number))
        note_lines = [
            f"- Scene {note['scene_number']} · {note['category']} · {note['status']}: {note['body']}\n  Anchor: {note['quote_text']}"
            for note in notes
        ]
        subtext_docs = _supporting_docs_body(project, "subtext")
        sections.append(
            _section(
                "Subtext and author notes",
                ("\n".join(note_lines) if note_lines else "No saved notes or subtext annotations exist for the selected chapters.")
                + "\n\n### Source subtext and thematic material\n\n"
                + subtext_docs,
            )
        )

    for option in ("science", "documentation"):
        if option not in selected_options:
            continue
        body = _supporting_docs_body(project, option)
        sections.append(_section("Science and research" if option == "science" else "Project documentation", body))

    root = _source_root(project)
    git_links: list[dict[str, str]] = []
    remote_base = _remote_base(
        root,
        str(project.get("commit_sha") or ""),
        bool(project.get("working_tree_dirty")),
    ) if root else None
    if remote_base:
        for scene in scenes:
            if scene.get("source_path"):
                git_links.append(
                    {
                        "label": f"Chapter {scene.get('chapter_number')} · {scene['source_path']}",
                        "url": f"{remote_base}/{scene['source_path']}",
                    }
                )

    if "documentation" in selected_options:
        link_body = (
            "\n".join(f"- [{item['label']}]({item['url']})" for item in git_links)
            if git_links
            else "No stable Git links were emitted. The source is either a dirty working tree, has no origin remote, or is not a Git-backed project. Use this package as the authoritative snapshot."
        )
        sections.append(_section("Git links", link_body))

    markdown = "\n".join(sections).strip() + "\n"
    if len(markdown) > MAX_PACKAGE_CHARS:
        raise ContextPackageError("The selected context package is too large. Reduce the chapter range or optional materials.")
    package_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    paste_prompt = (
        f"I am providing a read-only Literary Studio context package for {project['title']}, "
        f"chapters {chapter_start}-{chapter_end}. Use the provenance block and selected evidence "
        "as source context. Do not assume that omitted sections are absent from the novel, and "
        "do not rewrite canon without asking me first.\n\n"
        + markdown
    )
    return {
        "project_id": project["project_id"],
        "title": project["title"],
        "selected_chapters": list(range(chapter_start, chapter_end + 1)),
        "included": ["manuscript", *selected_options],
        "manifest": manifest,
        "package_sha256": package_sha256,
        "git_links": git_links,
        "markdown": markdown,
        "paste_prompt": paste_prompt,
    }
