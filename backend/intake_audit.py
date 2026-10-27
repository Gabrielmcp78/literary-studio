"""Evidence-first intake oversight for imported manuscript material."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ai_provider import AIProviderUnavailable, generate_text
from studio_db import StudioDB, html_to_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_from_response(value: str) -> dict[str, Any] | None:
    candidate = value.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _structural_audit(scenes: list[dict[str, Any]], graph: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    chapters: dict[tuple[int | None, str], list[dict[str, Any]]] = {}
    total_paragraphs = 0
    for scene in scenes:
        text = html_to_text(scene["content_html"])
        paragraph_count = len(re.findall(r"<p(?:\s|>)", scene["content_html"]))
        total_paragraphs += paragraph_count
        scene["audit_paragraph_count"] = paragraph_count
        scene["audit_excerpt"] = re.sub(r"\s+", " ", text).strip()[:280]
        key = (scene.get("chapter_number"), scene.get("chapter_title") or "Unstructured material")
        chapters.setdefault(key, []).append(scene)
        if scene["word_count"] >= 800 and paragraph_count <= 1:
            issues.append({
                "kind": "paragraph_layout",
                "severity": "high",
                "scene_number": scene["scene_number"],
                "evidence": f"{scene['word_count']} words represented as {paragraph_count} paragraph.",
                "recommendation": "Review source paragraph boundaries before treating this scene as structurally complete.",
            })
        # An internal scene title is optional. Do not turn the importer’s old
        # placeholder labels ("Scene 1") into structural errors: a chapter
        # may legitimately be one untitled scene.

    for (chapter_number, chapter_title), chapter_scenes in chapters.items():
        local_numbers = sorted(
            scene["scene_number_in_chapter"]
            for scene in chapter_scenes
            if isinstance(scene.get("scene_number_in_chapter"), int)
        )
        if len(local_numbers) != len(set(local_numbers)):
            issues.append({
                "kind": "scene_sequence",
                "severity": "high",
                "chapter_number": chapter_number,
                "evidence": f"Duplicate internal scene numbers in {chapter_title}: {local_numbers}.",
                "recommendation": "Review the source headings and preserve the author's intended order.",
            })
        if local_numbers and local_numbers != list(range(min(local_numbers), max(local_numbers) + 1)):
            issues.append({
                "kind": "scene_sequence",
                "severity": "medium",
                "chapter_number": chapter_number,
                "evidence": f"Non-contiguous internal scene numbers in {chapter_title}: {local_numbers}.",
                "recommendation": "Confirm whether the missing numbers are intentional or represent an intake boundary error.",
            })

    return {
        "scene_count": len(scenes),
        "chapter_count": len(chapters),
        "paragraph_count": total_paragraphs,
        "issues": issues,
        "graph": {
            "node_count": len(graph.get("nodes", [])),
            "edge_count": len(graph.get("edges", [])),
        },
    }


def run_intake_audit(db: StudioDB, source_label: str = "manuscript intake", run_ai: bool = True) -> dict[str, Any]:
    scenes = db.list_scenes()
    if not scenes:
        raise ValueError("Import manuscript material before running intake oversight.")
    graph = db.story_graph()
    structural = _structural_audit(scenes, graph)
    manifest = [
        {
            "scene_number": scene["scene_number"],
            "chapter_number": scene.get("chapter_number"),
            "chapter_title": scene.get("chapter_title"),
            "scene_number_in_chapter": scene.get("scene_number_in_chapter"),
            "title": scene["title"],
            "word_count": scene["word_count"],
            "paragraph_count": scene["audit_paragraph_count"],
            "excerpt": scene["audit_excerpt"],
        }
        for scene in scenes
    ]
    audit: dict[str, Any] = {
        "audit_id": f"intake_{uuid4().hex}",
        "source_label": source_label.strip() or "manuscript intake",
        "created_at": _now(),
        "status": "deterministic_only",
        "structural_audit": structural,
        "corrections": [],
        "story_bible": {"characters": [], "plot_threads": [], "subplots": [], "key_objects": []},
        "questions": [],
        "ai": {"provider": None, "model": None},
    }

    if run_ai:
        prompt = f"""
You are the Literary Studio intake oversight editor.
The material has already been imported into a project-local database. Review the
evidence below for ingestion mistakes and missing story-bible structure. Do not
rewrite prose, silently correct canon, or invent facts. Return JSON only with this
shape:
{{
  "corrections": [{{"kind":"boundary|formatting|metadata|graph", "severity":"high|medium|low", "scene_number": 0, "evidence":"exact evidence", "recommendation":"author-reviewable correction"}}],
  "story_bible": {{"characters": [{{"name":"", "role":"", "evidence_scene_numbers": [], "confidence":"high|medium|low"}}], "plot_threads": [{{"name":"", "description":"", "evidence_scene_numbers": [], "confidence":"high|medium|low"}}], "subplots": [{{"name":"", "description":"", "evidence_scene_numbers": [], "confidence":"high|medium|low"}}], "key_objects": [{{"name":"", "function":"", "evidence_scene_numbers": [], "confidence":"high|medium|low"}}]}},
  "questions": ["questions the author should resolve"]
}}
Only cite scene numbers present in the evidence. Treat titles, paragraph counts,
and graph counts as evidence, not truth. Flag likely layout or boundary errors
instead of repairing them in place.

DETERMINISTIC AUDIT:
{json.dumps(structural, ensure_ascii=False)}

SCENE EVIDENCE:
{json.dumps(manifest, ensure_ascii=False)}
""".strip()
        try:
            response = generate_text(prompt, model="gemini-3.1-pro-preview", temperature=0.1)
            parsed = _json_from_response(response.text)
            if parsed:
                audit["corrections"] = parsed.get("corrections", [])
                audit["story_bible"] = parsed.get("story_bible", audit["story_bible"])
                audit["questions"] = parsed.get("questions", [])
                audit["status"] = "complete"
            else:
                audit["status"] = "ai_unstructured"
                audit["analysis"] = response.text
            audit["ai"] = {"provider": response.provider, "model": response.model}
        except AIProviderUnavailable as exc:
            audit["status"] = "ai_unavailable"
            audit["ai"] = {"provider": None, "model": None, "error": str(exc)}

    return db.save_intake_audit(audit)
