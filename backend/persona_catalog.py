"""Load the Fiction Git System's real profile sources as simulator dossiers.

The catalog is source-backed. It does not invent replacement personas: the two
40-agent JSON batches, the 15-editor JSON, and the FictionForce cast index are
read from the configured Fiction Git System checkout. Results are explicitly
profile-informed simulations, never claims that a real person reviewed prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


FICTION_GIT_SYSTEM_ROOT = Path(
    os.getenv(
        "LITERARY_STUDIO_FICTION_GIT_SYSTEM_ROOT",
        "/Volumes/Ready500/DEVELOPMENT/fiction-git-system",
    )
).expanduser()

AGENT_PROFILE_FILES = (
    "fiction_git_system/Top 20 Agent Profiles_done.json",
    "fiction_git_system/Top 21-40 agents for string theory_done.json",
)
EDITOR_PROFILE_FILE = "fiction_git_system/Prestige NYC Editors.json"
CAST_INDEX_FILE = "fiction_git_system/cast/index.json"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "profile"


def _source_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_metadata(relative_path: str, path: Path) -> dict[str, str]:
    return {
        "source_file": relative_path,
        "source_sha256": _source_sha(path),
        "source_kind": "fiction-git-system",
    }


def _json_profile_context(profile: dict[str, Any]) -> str:
    return json.dumps(profile, ensure_ascii=False, indent=2)


def _agent_persona(profile: dict[str, Any], batch: int, index: int, source: dict[str, str]) -> dict[str, Any]:
    reading = profile.get("how_they_read", {})
    style = profile.get("style_preferences", {})
    return {
        "persona_id": f"literary_agent_{batch}_{index:02d}_{_slug(profile.get('name', 'agent'))}",
        "name": profile.get("name", f"Literary Agent {index}"),
        "category": "literary_agent",
        "role": "agent",
        "title_or_archetype": profile.get("personality_profile", {}).get("archetype", "Literary agent profile"),
        "organization_or_imprint": profile.get("agency", "Independent literary agency"),
        "taste_summary": profile.get("submission_fit_notes") or reading.get("reading_philosophy", "Profile-informed literary agent simulation."),
        "focus": "; ".join(
            value for value in (
                reading.get("first_page_focus"),
                reading.get("what_grabs_them"),
                style.get("prose"),
                style.get("pacing"),
            ) if value
        ),
        "green_flags": profile.get("specifically_loves", []) + profile.get("how_they_read", {}).get("what_grabs_them", "").split("; ")[:1],
        "red_flags": profile.get("deal_breakers", []) + profile.get("specifically_hates", []),
        "profile_context": _json_profile_context(profile),
        **source,
    }


def _editor_persona(profile: dict[str, Any], index: int, source: dict[str, str]) -> dict[str, Any]:
    sensibilities = profile.get("key_sensibilities", {})
    return {
        "persona_id": f"prestige_editor_{index:02d}_{_slug(profile.get('editor_name', 'editor'))}",
        "name": profile.get("editor_name", f"Prestige Editor {index}"),
        "category": "prestige_editor",
        "role": "editor",
        "title_or_archetype": profile.get("title", "Prestige publishing editor"),
        "organization_or_imprint": profile.get("publishing_house", "Independent imprint"),
        "taste_summary": profile.get("taste_summary", "Profile-informed prestige editor simulation."),
        "focus": "; ".join(
            value for value in (
                sensibilities.get("voice"),
                sensibilities.get("pacing"),
                sensibilities.get("thematic_focus"),
            ) if value
        ),
        "green_flags": profile.get("acquisitions_green_flags", []),
        "red_flags": profile.get("acquisitions_red_flags", []),
        "profile_context": _json_profile_context(profile),
        **source,
    }


def _cast_persona(entry: dict[str, Any], root: Path) -> dict[str, Any] | None:
    relative_path = entry.get("file")
    if not relative_path:
        return None
    path = root / relative_path
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")
    kind = entry.get("kind", "specialist")
    category = {
        "craft-editor": "craft_agent",
        "author": "author_profile",
        "specialist": "specialist",
    }.get(kind, kind)
    return {
        "persona_id": f"{category}_{entry.get('id', _slug(entry.get('name', 'profile')))}",
        "name": entry.get("name", "Unnamed profile"),
        "category": category,
        "role": "author_craft" if category in {"craft_agent", "author_profile"} else "specialist",
        "title_or_archetype": entry.get("description", "Source-backed FictionForce profile").split(":", 1)[0],
        "organization_or_imprint": "Fiction Git System cast",
        "taste_summary": entry.get("description", "Source-backed FictionForce profile."),
        "focus": "Source profile instructions and behavior brief",
        "green_flags": [],
        "red_flags": [],
        "profile_context": content,
        **_source_metadata(relative_path, path),
    }


def _empty_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    return [], [], {"error": f"Fiction Git System not found at {FICTION_GIT_SYSTEM_ROOT}"}


def _load_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not FICTION_GIT_SYSTEM_ROOT.is_dir():
        return _empty_catalog()

    personas: list[dict[str, Any]] = []
    literary_agent_ids: list[str] = []
    editor_ids: list[str] = []
    craft_ids: list[str] = []
    author_ids: list[str] = []
    specialist_ids: list[str] = []

    for batch, relative_path in enumerate(AGENT_PROFILE_FILES, start=1):
        path = FICTION_GIT_SYSTEM_ROOT / relative_path
        payload = json.loads(path.read_text(encoding="utf-8"))
        for index, profile in enumerate(payload.get("agent_profiles", []), start=1):
            persona = _agent_persona(profile, batch, index, _source_metadata(relative_path, path))
            personas.append(persona)
            literary_agent_ids.append(persona["persona_id"])

    editor_path = FICTION_GIT_SYSTEM_ROOT / EDITOR_PROFILE_FILE
    editor_payload = json.loads(editor_path.read_text(encoding="utf-8"))
    for index, profile in enumerate(editor_payload.get("prestige_publishing_editors_nyc", []), start=1):
        persona = _editor_persona(profile, index, _source_metadata(EDITOR_PROFILE_FILE, editor_path))
        personas.append(persona)
        editor_ids.append(persona["persona_id"])

    cast_path = FICTION_GIT_SYSTEM_ROOT / CAST_INDEX_FILE
    cast_payload = json.loads(cast_path.read_text(encoding="utf-8"))
    for entry in cast_payload.get("agents", []):
        if entry.get("kind") not in {"craft-editor", "author", "specialist"}:
            continue
        persona = _cast_persona(entry, FICTION_GIT_SYSTEM_ROOT)
        if persona is None:
            continue
        personas.append(persona)
        if persona["category"] == "craft_agent":
            craft_ids.append(persona["persona_id"])
        elif persona["category"] == "author_profile":
            author_ids.append(persona["persona_id"])
        else:
            specialist_ids.append(persona["persona_id"])

    by_name = {persona["name"]: persona["persona_id"] for persona in personas}
    workshop_names = ["Jordan Pavlin", "Jill Bialosky", "Allison Lorentzen", "Eric Chinski", "Deborah Treisman"]
    workshop_ids = [by_name[name] for name in workshop_names if name in by_name]
    panels = [
        {
            "panel_id": "editorial_workshop",
            "name": "Editorial Workshop",
            "description": "The source-defined five-editor competitive workshop.",
            "persona_ids": workshop_ids,
            "source_file": "fiction_git_system/cast/group/editorial_workshop_director.md",
        },
        {
            "panel_id": "editor_roundtable",
            "name": "Editor Roundtable",
            "description": "The source-defined five-editor roundtable synthesis.",
            "persona_ids": workshop_ids,
            "source_file": "fiction_git_system/cast/group/fiction_editor_roundtable.md",
        },
        {
            "panel_id": "all_editors",
            "name": "All 15 Prestige Editors",
            "description": "Every editor profile from Prestige NYC Editors.json.",
            "persona_ids": editor_ids,
            "source_file": EDITOR_PROFILE_FILE,
        },
        {
            "panel_id": "all_agents",
            "name": "All 40 Literary Agents",
            "description": "Both source JSON batches, preserving batch provenance and duplicate entries.",
            "persona_ids": literary_agent_ids,
            "source_file": "fiction_git_system/Top 20 Agent Profiles_done.json + Top 21-40 agents for string theory_done.json",
        },
        {
            "panel_id": "craft_agents",
            "name": "Craft Agents",
            "description": "The FictionForce craft-editor cast profiles.",
            "persona_ids": craft_ids,
            "source_file": "fiction_git_system/cast/index.json",
        },
        {
            "panel_id": "author_profiles",
            "name": "Author Profile Agents",
            "description": "The source-defined author cast profiles used as craft lenses.",
            "persona_ids": author_ids,
            "source_file": "fiction_git_system/cast/index.json",
        },
        {
            "panel_id": "full_studio",
            "name": "Full Studio Cast",
            "description": "All loaded agent, editor, craft, author, and specialist profiles.",
            "persona_ids": [persona["persona_id"] for persona in personas],
            "source_file": "fiction_git_system/cast/index.json + profile JSONs",
        },
    ]
    summary = {
        "total_profiles": len(personas),
        "literary_agents": len(literary_agent_ids),
        "prestige_editors": len(editor_ids),
        "craft_agents": len(craft_ids),
        "author_profiles": len(author_ids),
        "specialists": len(specialist_ids),
        "panels": len(panels),
        "source_root": str(FICTION_GIT_SYSTEM_ROOT),
    }
    return personas, panels, summary


PERSONA_ROSTER, PERSONA_PANELS, CATALOG_SUMMARY = _load_catalog()
PERSONA_BY_ID = {persona["persona_id"]: persona for persona in PERSONA_ROSTER}
PANEL_BY_ID = {panel["panel_id"]: panel for panel in PERSONA_PANELS}


def get_persona(persona_id: str) -> dict[str, Any] | None:
    return PERSONA_BY_ID.get(persona_id)


def get_panel(panel_id: str) -> dict[str, Any] | None:
    return PANEL_BY_ID.get(panel_id)


def catalog_summary() -> dict[str, Any]:
    return CATALOG_SUMMARY


def public_persona(persona: dict[str, Any]) -> dict[str, Any]:
    """Return dossier metadata without sending the full source prompt to the UI."""
    return {key: value for key, value in persona.items() if key != "profile_context"}
