"""Project-scoped manuscript retrieval for the Interrogation Room."""

from __future__ import annotations

import re
from typing import Any

from studio_db import StudioDB, html_to_text


class ManuscriptContextError(ValueError):
    """Raised when a requested manuscript location cannot be resolved."""


def _scene_metadata(scene: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_number": int(scene["scene_number"]),
        "chapter_number": scene.get("chapter_number"),
        "chapter_title": scene.get("chapter_title"),
        "scene_number_in_chapter": scene.get("scene_number_in_chapter"),
        "title": scene["title"],
        "source_path": scene.get("source_path"),
        "version": scene.get("version"),
        "word_count": scene.get("word_count", 0),
    }


def manuscript_index(db: StudioDB) -> dict[str, Any]:
    """Return a compact map of every scene in this project, without prose."""
    project = db.project_info()
    scenes = db.list_scenes()
    return {
        "project_id": project["project_id"],
        "title": project["title"],
        "source": {
            "root": project.get("source_root"),
            "subpath": project.get("source_subpath"),
            "branch": project.get("branch"),
            "commit_sha": project.get("commit_sha"),
            "source_digest": project.get("source_digest"),
        },
        "total_scenes": len(scenes),
        "scenes": [_scene_metadata(scene) for scene in scenes],
    }


def _reference_from_message(message: str) -> tuple[int, int] | None:
    match = re.search(
        r"\b(?:chapter|ch\.)\s*(\d+)\s*[,;:/-]?\s*(?:scene|sc\.)\s*(\d+)\b",
        message,
        flags=re.IGNORECASE,
    )
    return (int(match.group(1)), int(match.group(2))) if match else None


def _chapter_from_message(message: str) -> int | None:
    match = re.search(r"\b(?:chapter|ch\.)\s*(\d+)\b", message, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _find_target(
    db: StudioDB,
    *,
    target_scope: str,
    chapter_number: int | None,
    scene_number_in_chapter: int | None,
    global_scene_number: int | None,
    user_message: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    requested_label: str | None = None
    chapter = chapter_number
    local_scene = scene_number_in_chapter
    scenes = db.list_scenes()

    parsed_scene = _reference_from_message(user_message)
    parsed_chapter = _chapter_from_message(user_message)
    scope = target_scope
    if scope == "screen":
        scope = "scene" if parsed_scene else "chapter" if parsed_chapter is not None else "screen"
    if scope != "chapter" and (chapter is None or local_scene is None) and parsed_scene:
        chapter, local_scene = parsed_scene
        requested_label = f"Chapter {chapter}, Scene {local_scene}"

    if scope == "chapter":
        chapter = chapter if chapter is not None else parsed_chapter
        chapter_scenes = [
            item for item in scenes if item.get("chapter_number") == chapter
        ]
        if chapter is None or not chapter_scenes:
            raise ManuscriptContextError(
                f"Chapter {chapter if chapter is not None else '?'} was not found in this project."
            )
        chapter_title = next(
            (item.get("chapter_title") for item in chapter_scenes if item.get("chapter_title")),
            f"Chapter {chapter}",
        )
        return {
            "scope": "chapter",
            "chapter_number": chapter,
            "chapter_title": chapter_title,
            "scenes": [
                {
                    **_scene_metadata(item),
                    "text": html_to_text(item["content_html"]),
                }
                for item in chapter_scenes
            ],
        }, f"Chapter {chapter}", "chapter"

    if chapter is not None and local_scene is not None:
        scene = next(
            (
                item
                for item in scenes
                if item.get("chapter_number") == chapter
                and item.get("scene_number_in_chapter") == local_scene
            ),
            None,
        )
        if scene is None:
            raise ManuscriptContextError(
                f"{requested_label or f'Chapter {chapter}, Scene {local_scene}'} was not found in this project."
            )
        return {
            "scope": "scene",
            **_scene_metadata(scene),
            "text": html_to_text(scene["content_html"]),
        }, requested_label or f"Chapter {chapter}, Scene {local_scene}", "scene"

    if global_scene_number is not None:
        scene = db.get_scene(global_scene_number)
        if scene is None:
            raise ManuscriptContextError(f"Scene {global_scene_number} was not found in this project.")
        return {
            "scope": "scene",
            **_scene_metadata(scene),
            "text": html_to_text(scene["content_html"]),
        }, f"Scene {global_scene_number}", "scene"

    return None, None, None


def build_interrogation_context(
    db: StudioDB,
    *,
    current_scene_number: int,
    current_screen_text: str,
    target_scope: str = "screen",
    target_chapter_number: int | None = None,
    target_scene_number_in_chapter: int | None = None,
    target_global_scene_number: int | None = None,
    user_message: str = "",
) -> dict[str, Any]:
    """Build grounded context for one chat turn.

    The entire project is represented by its scene index. Full prose is loaded
    for the scene visible in the editor and for an explicitly selected or
    mentioned target scene. This keeps the room manuscript-aware without
    sending a whole novel on every turn.
    """
    current_scene = db.get_scene(current_scene_number)
    if current_scene is None:
        raise ManuscriptContextError(f"Scene {current_scene_number} was not found in this project.")

    if target_scope not in {"screen", "scene", "chapter"}:
        raise ManuscriptContextError(f"Unknown manuscript target scope: {target_scope}")

    target, target_label, resolved_scope = _find_target(
        db,
        target_scope=target_scope,
        chapter_number=target_chapter_number,
        scene_number_in_chapter=target_scene_number_in_chapter,
        global_scene_number=target_global_scene_number,
        user_message=user_message,
    )

    screen_text = html_to_text(current_screen_text.strip()) if current_screen_text.strip() else ""
    if not screen_text:
        screen_text = html_to_text(current_scene["content_html"])

    project = db.project_info()
    return {
        "project_id": project["project_id"],
        "project_title": project["title"],
        "source": {
            "root": project.get("source_root"),
            "subpath": project.get("source_subpath"),
            "branch": project.get("branch"),
            "commit_sha": project.get("commit_sha"),
            "source_digest": project.get("source_digest"),
        },
        "current_screen": {
            **_scene_metadata(current_scene),
            "text": screen_text,
            "source": "live editor contents" if current_screen_text.strip() else "saved project scene",
        },
        "target": target,
        "target_label": target_label,
        "target_scope": resolved_scope,
        "manuscript_index": manuscript_index(db)["scenes"],
    }


def context_prompt(context: dict[str, Any]) -> str:
    """Format retrieval results as explicit, provenance-aware prompt context."""
    source = context["source"]
    current = context["current_screen"]
    index_lines = []
    for scene in context["manuscript_index"]:
        chapter = scene.get("chapter_number")
        local = scene.get("scene_number_in_chapter")
        location = f"Chapter {chapter}, Scene {local}" if chapter is not None and local is not None else f"Scene {scene['scene_number']}"
        index_lines.append(f"- {location} · {scene['title']} · {scene['word_count']} words · global scene {scene['scene_number']}")

    sections = [
        "PROJECT-SCOPED MANUSCRIPT CONTEXT",
        f"Project: {context['project_title']} ({context['project_id']})",
        f"Source branch: {source.get('branch') or 'unknown'}",
        f"Source commit: {source.get('commit_sha') or 'local/unspecified'}",
        "The manuscript index below is the complete scene roster for this project. Full prose is supplied for the editor screen and any explicitly selected or mentioned target.",
        "\nMANUSCRIPT INDEX:\n" + "\n".join(index_lines),
        f"\nCURRENT EDITOR SCREEN — {current['title']} ({current['source']}):\n{current['text']}",
    ]
    target = context.get("target")
    if target is not None and target.get("scope") == "chapter":
        sections.append(
            f"\nREQUESTED TARGET CHAPTER — {target['chapter_title']} (Chapter {target['chapter_number']}):\n"
            + "\n\n".join(
                f"### Scene {scene.get('scene_number_in_chapter')}: {scene['title']}\n{scene['text']}"
                for scene in target["scenes"]
            )
        )
    elif target is not None and target["scene_number"] != current["scene_number"]:
        chapter = target.get("chapter_number")
        local = target.get("scene_number_in_chapter")
        label = f"Chapter {chapter}, Scene {local}" if chapter is not None and local is not None else f"Scene {target['scene_number']}"
        sections.append(f"\nREQUESTED TARGET — {label} · {target['title']}:\n{target['text']}")
    return "\n".join(sections)
