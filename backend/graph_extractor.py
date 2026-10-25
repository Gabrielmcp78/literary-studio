"""Conservative, provenance-first entity extraction for the local story graph.

This index deliberately records only textual evidence: an entity is connected to
the scene in which its name appears. It does not invent character relationships
or plot facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    label: str
    node_type: str
    strong: bool = False


TITLE_NAME = re.compile(
    r"\b(?:Captain|Commander|Major|Dr\.?|Doctor|Magistrate|Professor|Lieutenant|General|Chief|Director|Mr\.?|Ms\.?|Mrs\.?)\s+"
    r"([A-Z][A-Za-z’'\-]+(?:\s+[A-Z][A-Za-z’'\-]+){0,2})"
)
PROPER_NAME = re.compile(
    r"\b([A-Z][a-z][A-Za-z’'\-]*\s+[A-Z][a-z][A-Za-z’'\-]*)\b"
)
LOCATION = re.compile(
    r"\b([A-Z][A-Za-z0-9’'\-]*(?:\s+[A-Z][A-Za-z0-9’'\-]*){0,3}\s+"
    r"(?:Station|Bay|Span|Dock|Habitat|Row|Chain|Colony|Sector|Command|Building|University))\b"
)
ITALIC_PROPER = re.compile(r"\*([A-Z][A-Za-z0-9’'\-]*(?:\s+[A-Z][A-Za-z0-9’'\-]*){0,3})\*")

STOPWORDS = {
    "Act One", "Act Two", "Act Three", "Act Four", "Chapter One", "Chapter Two",
    "New York", "Fall", "The First", "The Second", "No One", "One Hundred",
}
LEADING_WORDS = {
    "a", "an", "and", "at", "but", "by", "for", "from", "good", "in", "inside",
    "into", "is", "it", "major", "on", "please", "that", "the", "then", "this", "when",
}
ROLE_WORDS = {
    "captain", "chief", "commander", "doctor", "dr", "director", "general", "lieutenant",
    "magistrate", "major", "mr", "mrs", "ms", "professor",
}


def _clean(label: str) -> str:
    clean = re.sub(r"\s+", " ", label.replace("’", "'").strip(" '.,:;!?-"))
    clean = re.sub(r"['’]s$", "", clean).strip()
    while clean.split() and clean.split()[0].casefold() in LEADING_WORDS:
        clean = " ".join(clean.split()[1:])
    return clean


def _add(
    target: dict[tuple[str, str], tuple[str, bool]],
    label: str,
    node_type: str,
    strong: bool,
) -> None:
    clean = _clean(label)
    if len(clean) < 3 or clean in STOPWORDS:
        return
    key = (clean.casefold(), node_type)
    existing = target.get(key)
    target[key] = (existing[0] if existing else clean, strong or (existing[1] if existing else False))


def extract_scene_entities(text: str, scene_number: int) -> list[Entity]:
    del scene_number
    found: dict[tuple[str, str], tuple[str, bool]] = {}
    for match in LOCATION.finditer(text):
        _add(found, match.group(1), "location", True)
    for match in TITLE_NAME.finditer(text):
        _add(found, match.group(1), "character", True)
    for match in PROPER_NAME.finditer(text):
        label = match.group(1)
        if label.split()[0].casefold().rstrip(".") in ROLE_WORDS:
            continue
        _add(found, label, "character", False)
    for match in ITALIC_PROPER.finditer(text):
        _add(found, match.group(1), "artifact", True)
    return [
        Entity(label=label, node_type=node_type, strong=strong)
        for (normalized, node_type), (label, strong) in sorted(found.items())
    ]


def build_graph(scenes: list[dict[str, object]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    nodes: dict[str, dict[str, str]] = {}
    edges: set[tuple[str, str, str]] = set()
    entity_scenes: dict[tuple[str, str], set[int]] = {}
    entity_labels: dict[tuple[str, str], str] = {}
    entity_strength: dict[tuple[str, str], bool] = {}

    ordered_scenes = sorted(scenes, key=lambda scene: int(scene["scene_number"]))
    for scene in ordered_scenes:
        number = int(scene["scene_number"])
        scene_id = f"scene:{number}"
        nodes[scene_id] = {
            "node_id": scene_id,
            "label": (
                f"{scene['chapter_title']} · Scene {scene['scene_number_in_chapter']}: {scene['title']}"
                if scene.get("chapter_title") and scene.get("scene_number_in_chapter")
                else str(scene["title"])
            ),
            "node_type": "scene",
        }
        if number > 1:
            edges.add((f"scene:{number - 1}", scene_id, "NEXT_SCENE"))
        for entity in extract_scene_entities(str(scene["content_text"]), number):
            entity_key = (entity.label.casefold(), entity.node_type)
            entity_scenes.setdefault(entity_key, set()).add(number)
            entity_labels.setdefault(entity_key, entity.label)
            entity_strength[entity_key] = entity_strength.get(entity_key, False) or entity.strong

    for (normalized, node_type), scene_numbers in sorted(entity_scenes.items()):
        if node_type == "character" and not entity_strength[(normalized, node_type)] and len(scene_numbers) < 2:
            continue
        label = entity_labels[(normalized, node_type)]
        entity_id = f"entity:{node_type}:{re.sub(r'[^a-z0-9]+', '-', normalized).strip('-')}"
        nodes[entity_id] = {"node_id": entity_id, "label": label, "node_type": node_type}
        for scene_number in sorted(scene_numbers):
            edges.add((entity_id, f"scene:{scene_number}", "MENTIONED_IN"))

    return list(nodes.values()), [
        {"source_id": source, "target_id": target, "relationship": relationship}
        for source, target, relationship in sorted(edges)
    ]
