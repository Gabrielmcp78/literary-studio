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
import subprocess
import tomllib
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
SPECIALIST_ROSTER_FILE = ".github/agents/roster.json"
AUTHOR_PROFILE_GIT_REF = os.getenv(
    "LITERARY_STUDIO_AUTHOR_PROFILE_GIT_REF",
    "advanced/revision-material-2026-10-15",
)
AUTHOR_PROFILE_FILE = "docs/evaluations/author-profiles-29ad7f7.json"
AUTHOR_AGENT_FILES = (
    "docs/editorial-agents/author_advocate.toml",
    "docs/editorial-agents/author_architect.toml",
    "docs/editorial-agents/author_reviser.toml",
)


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


def _git_blob(root: Path, ref: str, relative_path: str, warnings: list[str]) -> str | None:
    """Read a source-backed profile from a local Git object without checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{ref}:{relative_path}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"Could not load Git profile {ref}:{relative_path}: {exc}")
        return None
    return result.stdout


def _blob_source_metadata(relative_path: str, content: str, ref: str) -> dict[str, str]:
    return {
        "source_file": f"{ref}:{relative_path}",
        "source_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "source_kind": "fiction-git-system-git-profile",
        "source_revision": ref,
    }


def _yaml_scalar(content: str, key: str, default: str = "") -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", content, flags=re.MULTILINE)
    if not match:
        return default
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _yaml_block(content: str, key: str) -> str:
    match = re.search(
        rf"^{re.escape(key)}:\s*\|\s*\n(?P<body>(?:[ \t]+.*(?:\n|$))*)",
        content,
        flags=re.MULTILINE,
    )
    if not match:
        return ""
    return "\n".join(line.strip() for line in match.group("body").splitlines()).strip()


def _specialist_persona(entry: dict[str, Any], root: Path) -> dict[str, Any] | None:
    relative_path = str(entry.get("persona_yaml") or "").strip()
    path = root / relative_path
    if not relative_path or not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")
    name = _yaml_scalar(content, "name", entry.get("id", "Specialist agent"))
    role = _yaml_scalar(content, "role", "Source-backed specialist agent")
    focus = _yaml_block(content, "specialty_focus") or role
    agent_relative_path = str(entry.get("agent_file") or "").strip()
    agent_path = root / agent_relative_path if agent_relative_path else None
    agent_content = agent_path.read_text(encoding="utf-8") if agent_path and agent_path.is_file() else ""
    source_material = content + ("\n\n" + agent_content if agent_content else "")
    return {
        "persona_id": f"specialist_{entry.get('id', _slug(name))}",
        "name": name,
        "category": "specialist",
        "role": "specialist",
        "title_or_archetype": role,
        "organization_or_imprint": "Fiction Git System editorial roster",
        "taste_summary": role,
        "focus": focus,
        "green_flags": [],
        "red_flags": [],
        "profile_context": source_material,
        **_source_metadata(relative_path, path),
    }


def _author_lens_persona(profile: dict[str, Any], index: int, source: dict[str, str]) -> dict[str, Any]:
    themes = profile.get("themes", [])
    theme_text = "; ".join(str(theme) for theme in themes if theme)
    return {
        "persona_id": f"author_profile_{index:02d}_{_slug(profile.get('name', 'author'))}",
        "name": f"Author Lens: {profile.get('name', 'Unnamed author')}",
        "category": "author_profile",
        "role": "author_craft",
        "title_or_archetype": "Thematic author craft lens",
        "organization_or_imprint": "Thyme Profiles source collection",
        "taste_summary": profile.get("style", "Source-backed authorial style profile."),
        "focus": "; ".join(
            value for value in (profile.get("philosophical_core"), theme_text) if value
        ),
        "green_flags": themes if isinstance(themes, list) else [],
        "red_flags": [],
        "profile_context": _json_profile_context(profile),
        **source,
    }


def _author_agent_persona(profile: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
    raw_name = str(profile.get("name", "author_agent"))
    display_name = raw_name.replace("_", " ").title()
    return {
        "persona_id": f"author_agent_{_slug(raw_name)}",
        "name": f"Author Agent: {display_name}",
        "category": "author_agent",
        "role": "author_craft",
        "title_or_archetype": profile.get("description", "Author-controlled craft and intent profile"),
        "organization_or_imprint": "Gabriel McPherson Fiction Studio",
        "taste_summary": profile.get("description", "Author-controlled craft and intent profile."),
        "focus": profile.get("description", "Defend authorial intent and protected material."),
        "green_flags": [],
        "red_flags": [],
        "profile_context": str(profile.get("developer_instructions", "")),
        **source,
    }


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
    return [], [], {
        "error": f"Fiction Git System not found at {FICTION_GIT_SYSTEM_ROOT}",
        "source_root": str(FICTION_GIT_SYSTEM_ROOT),
        "warnings": ["No source-backed persona catalog was loaded."],
    }


def _load_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    """Load one optional source document without taking down the API."""
    if not path.is_file():
        warnings.append(f"Missing persona source: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Could not load persona source {path}: {exc}")
        return {}
    if not isinstance(payload, dict):
        warnings.append(f"Persona source is not a JSON object: {path}")
        return {}
    return payload


def _load_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not FICTION_GIT_SYSTEM_ROOT.is_dir():
        return _empty_catalog()

    personas: list[dict[str, Any]] = []
    literary_agent_ids: list[str] = []
    editor_ids: list[str] = []
    craft_ids: list[str] = []
    author_ids: list[str] = []
    specialist_ids: list[str] = []
    warnings: list[str] = []

    for batch, relative_path in enumerate(AGENT_PROFILE_FILES, start=1):
        path = FICTION_GIT_SYSTEM_ROOT / relative_path
        payload = _load_json(path, warnings)
        for index, profile in enumerate(payload.get("agent_profiles", []), start=1):
            persona = _agent_persona(profile, batch, index, _source_metadata(relative_path, path))
            personas.append(persona)
            literary_agent_ids.append(persona["persona_id"])

    editor_path = FICTION_GIT_SYSTEM_ROOT / EDITOR_PROFILE_FILE
    editor_payload = _load_json(editor_path, warnings)
    for index, profile in enumerate(editor_payload.get("prestige_publishing_editors_nyc", []), start=1):
        persona = _editor_persona(profile, index, _source_metadata(EDITOR_PROFILE_FILE, editor_path))
        personas.append(persona)
        editor_ids.append(persona["persona_id"])

    cast_path = FICTION_GIT_SYSTEM_ROOT / CAST_INDEX_FILE
    cast_payload = _load_json(cast_path, warnings) if cast_path.is_file() else {}
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

    # The checked-out Fiction Git System branch currently has the specialist
    # roster and persona YAMLs, but not the optional cast/index.json. Load the
    # roster directly so those real profiles remain available instead of
    # silently presenting empty panels.
    if not specialist_ids:
        roster_path = FICTION_GIT_SYSTEM_ROOT / SPECIALIST_ROSTER_FILE
        roster_payload = _load_json(roster_path, warnings)
        for entry in roster_payload.get("agents", []):
            persona = _specialist_persona(entry, FICTION_GIT_SYSTEM_ROOT)
            if persona is None:
                continue
            personas.append(persona)
            specialist_ids.append(persona["persona_id"])

    # These author profiles are source-backed Git material on the revision
    # branch that introduced them. Read them through `git show` rather than
    # changing the user's current Fiction Git System checkout or branch.
    if not author_ids:
        author_profile_text = _git_blob(
            FICTION_GIT_SYSTEM_ROOT,
            AUTHOR_PROFILE_GIT_REF,
            AUTHOR_PROFILE_FILE,
            warnings,
        )
        if author_profile_text:
            try:
                author_payload = json.loads(author_profile_text)
            except json.JSONDecodeError as exc:
                warnings.append(f"Could not parse author profile collection: {exc}")
            else:
                source = _blob_source_metadata(AUTHOR_PROFILE_FILE, author_profile_text, AUTHOR_PROFILE_GIT_REF)
                authors = ((author_payload.get("thyme_profiles") or {}).get("authors") or [])
                for index, profile in enumerate(authors, start=1):
                    if not isinstance(profile, dict):
                        continue
                    persona = _author_lens_persona(profile, index, source)
                    personas.append(persona)
                    author_ids.append(persona["persona_id"])

        for relative_path in AUTHOR_AGENT_FILES:
            author_agent_text = _git_blob(
                FICTION_GIT_SYSTEM_ROOT,
                AUTHOR_PROFILE_GIT_REF,
                relative_path,
                warnings,
            )
            if not author_agent_text:
                continue
            try:
                profile = tomllib.loads(author_agent_text)
            except tomllib.TOMLDecodeError as exc:
                warnings.append(f"Could not parse author agent profile {relative_path}: {exc}")
                continue
            persona = _author_agent_persona(
                profile,
                _blob_source_metadata(relative_path, author_agent_text, AUTHOR_PROFILE_GIT_REF),
            )
            personas.append(persona)
            author_ids.append(persona["persona_id"])

    if not cast_path.is_file() and (specialist_ids or author_ids):
        warnings.append(
            "Optional FictionForce cast index is unavailable; specialist and author profiles were loaded from their checked-in Git sources."
        )
    elif not cast_path.is_file() and not specialist_ids and not author_ids:
        warnings.append("Optional FictionForce cast index and fallback editorial profile sources are unavailable.")

    by_name = {persona["name"]: persona["persona_id"] for persona in personas}
    workshop_names = ["Jordan Pavlin", "Jill Bialosky", "Allison Lorentzen", "Eric Chinski", "Deborah Treisman"]
    workshop_ids = [by_name[name] for name in workshop_names if name in by_name]
    author_profile_ids = [persona_id for persona_id in author_ids if persona_id.startswith("author_profile_")]
    author_agent_ids = [persona_id for persona_id in author_ids if persona_id.startswith("author_agent_")]
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
            "panel_id": "specialist_agents",
            "name": "Specialist Agents",
            "description": "The checked-in Fiction Git System specialist/editorial agent roster.",
            "persona_ids": specialist_ids,
            "source_file": SPECIALIST_ROSTER_FILE,
        },
        {
            "panel_id": "author_agents",
            "name": "Author Agents",
            "description": "Author-controlled intent, architecture, and revision profiles.",
            "persona_ids": author_agent_ids,
            "source_file": f"{AUTHOR_PROFILE_GIT_REF}:docs/editorial-agents/author_*.toml",
        },
        {
            "panel_id": "author_profiles",
            "name": "Author Profile Lenses",
            "description": "Source-defined authorial craft and thematic lenses.",
            "persona_ids": author_profile_ids,
            "source_file": f"{AUTHOR_PROFILE_GIT_REF}:{AUTHOR_PROFILE_FILE}",
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
        "author_profiles": sum(1 for persona_id in author_ids if persona_id.startswith("author_profile_")),
        "author_agents": sum(1 for persona_id in author_ids if persona_id.startswith("author_agent_")),
        "specialists": len(specialist_ids),
        "panels": len(panels),
        "source_root": str(FICTION_GIT_SYSTEM_ROOT),
        "warnings": warnings,
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
