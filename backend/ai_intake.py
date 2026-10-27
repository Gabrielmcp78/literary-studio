"""AI-controlled manuscript intake, structure analysis, and intent resolution.

Replaces brittle regular expressions with provider-driven semantic comprehension
using the multi-provider engine (AGY, FModCLI, Ollama, OpenRouter, Gemini).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ai_provider import AIProviderUnavailable, generate_text
from studio_db import (
    ImportedSection,
    _chapter_number,
    _render_markdown_body,
    html_to_text,
    is_repository_manuscript_path,
)


def ai_classify_file(relative_path: str, preview_text: str) -> str:
    """Classify whether a file contains manuscript narrative prose or authorial support.

    Returns one of: 'manuscript', 'support', 'ignored'.
    """
    path_lower = relative_path.casefold()
    filename = os.path.basename(path_lower)
    if filename in {"readme.md", "license.md", "changelog.md"} or filename.startswith("."):
        return "ignored"

    # Fast deterministic rules for obvious paths
    if not is_repository_manuscript_path(relative_path):
        if any(kw in path_lower for kw in ("worldbook", "outline", "dossier", "pass", "note", "bible", "card", "roundtable")):
            return "support"
        return "ignored"

    # For files passing path checks, verify using AI if ambiguous
    snippet = preview_text.strip()[:1500]
    if not snippet:
        return "ignored"

    prompt = f"""
You are an expert editorial manuscript intake analyzer.
Analyze this file path and excerpt from an author's repository.
Determine if this file is:
1. "manuscript" — actual fiction story prose (novel chapters, scenes, prologue, epilogue, appendix story text).
2. "support" — authorial planning material, outlines, beat cards, worldbook lore, character dossiers, or editorial critique notes.
3. "ignored" — build artifacts, system files, test logs, licenses.

PATH: {relative_path}
EXCERPT:
{snippet}

Respond with JSON only:
{{"classification": "manuscript" | "support" | "ignored", "reason": "short explanation"}}
""".strip()

    try:
        response = generate_text(prompt, model="gemini-3.1-pro-preview", temperature=0.1)
        cleaned = response.text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        parsed = json.loads(cleaned)
        classification = parsed.get("classification", "").lower()
        if classification in {"manuscript", "support", "ignored"}:
            return classification
    except Exception:
        pass

    # Safe fallback if AI is unreachable
    return "manuscript" if is_repository_manuscript_path(relative_path) else "support"


def ai_resolve_user_target(
    user_message: str,
    manuscript_index: list[dict[str, Any]],
    current_scene: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve an author's natural language request to a target chapter or scene.

    Handles semantic requests like "the lecture chapter", "the scene where David drinks milk",
    "chapter 10", "this chapter", etc., without brittle regexes.
    """
    msg = user_message.strip()
    if not msg:
        return None

    # Build a compact index of available chapters and scenes
    chapters: dict[int, list[dict[str, Any]]] = {}
    for scene in manuscript_index:
        ch_num = scene.get("chapter_number")
        if ch_num is not None:
            chapters.setdefault(ch_num, []).append(scene)

    available_summaries: list[str] = []
    for ch_num, scenes in sorted(chapters.items()):
        ch_title = scenes[0].get("chapter_title") or f"Chapter {ch_num}"
        scene_titles = ", ".join(f"Sc {s.get('scene_number_in_chapter')}: {s.get('title')}" for s in scenes[:4])
        available_summaries.append(f"Chapter {ch_num} ({ch_title}): {scene_titles}")

    current_ch = current_scene.get("chapter_number")
    current_sc = current_scene.get("scene_number_in_chapter") or current_scene.get("scene_number")

    prompt = f"""
You are the Studio Interrogation Controller.
The author is in the editorial room chatting with a simulation of an editor/agent.
The author is currently looking at: Chapter {current_ch}, Scene {current_sc} ("{current_scene.get('title')}").

Analyze the author's message to determine what part of the manuscript they are asking about or want the editor to review.

AUTHOR MESSAGE: "{msg}"

AVAILABLE MANUSCRIPT CHAPTERS & SCENES:
{chr(10).join(available_summaries[:35])}

Respond with JSON only:
{{
  "target_scope": "chapter" | "scene" | "screen",
  "chapter_number": integer or null,
  "scene_number_in_chapter": integer or null,
  "confidence": "high" | "medium" | "low"
}}
If the author refers to "this chapter", "the chapter", "current chapter", or asks general questions about the text on screen, set "target_scope": "chapter", "chapter_number": {current_ch}.
If the author specifically refers to another chapter (by number, title, or topic), identify it.
""".strip()

    try:
        response = generate_text(prompt, model="gemini-3.1-pro-preview", temperature=0.1)
        cleaned = response.text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        parsed = json.loads(cleaned)
        if parsed.get("confidence") in {"high", "medium"}:
            return parsed
    except Exception:
        pass

    return None
