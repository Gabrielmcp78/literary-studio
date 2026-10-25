"""Author-controlled bridge from IDE/chat requests to the governance queue."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ai_provider import AIProviderUnavailable, generate_text
from persona_catalog import get_persona
from source_snapshot import SnapshotError, build_snapshot
from studio_db import StudioDB, html_to_text


class ApprovalBridgeError(ValueError):
    """A request could not be safely prepared for approval."""


def _parse_json(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ApprovalBridgeError("The AI returned no structured approval proposals.") from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ApprovalBridgeError(f"The AI returned invalid approval JSON: {exc.msg}.") from exc
    if not isinstance(parsed, dict):
        raise ApprovalBridgeError("The AI approval response was not an object.")
    return parsed


def current_source_state(db: StudioDB) -> dict[str, Any]:
    """Read current Git provenance without changing the checkout."""
    project = db.project_info()
    state = {
        "source_root": project.get("source_root"),
        "source_subpath": project.get("source_subpath"),
        "branch": project.get("branch"),
        "commit_sha": project.get("commit_sha"),
        "source_digest": project.get("source_digest"),
        "working_tree_dirty": bool(project.get("working_tree_dirty")),
        "read_status": "project_snapshot",
    }
    source_root = project.get("source_root")
    if not source_root:
        return state
    try:
        snapshot = build_snapshot(
            project["project_id"],
            project["title"],
            source_root,
            project.get("source_subpath"),
        )
    except SnapshotError as exc:
        state["read_status"] = "source_unavailable"
        state["read_error"] = str(exc)
        return state
    manifest = snapshot["manifest"]
    state.update(
        {
            "branch": manifest["branch"],
            "commit_sha": manifest["commit_sha"],
            "source_digest": manifest["source_digest"],
            "working_tree_dirty": manifest["working_tree_dirty"],
            "read_status": "live_worktree",
        }
    )
    return state


def check_approval_conflicts(db: StudioDB, batch_id: str) -> dict[str, Any]:
    batch = db.get_approval_batch(batch_id)
    if batch is None:
        raise ApprovalBridgeError("Approval batch not found.")
    baseline = batch.get("baseline", {})
    current = current_source_state(db)
    conflicts: list[dict[str, Any]] = []
    for key in ("branch", "commit_sha", "source_digest"):
        expected = baseline.get(key)
        actual = current.get(key)
        if expected and actual and expected != actual:
            conflicts.append(
                {
                    "kind": "source_drift",
                    "field": key,
                    "expected": expected,
                    "actual": actual,
                    "message": f"{key} changed after this approval batch was prepared.",
                }
            )
    for proposal in batch.get("proposals", []):
        scene = db.get_scene(int(proposal["scene_number"]))
        if scene is None:
            conflicts.append(
                {
                    "kind": "scene_missing",
                    "proposal_id": proposal["proposal_id"],
                    "scene_number": proposal["scene_number"],
                }
            )
        elif proposal.get("source_scene_version") is not None and int(scene["version"]) != int(proposal["source_scene_version"]):
            conflicts.append(
                {
                    "kind": "scene_changed",
                    "proposal_id": proposal["proposal_id"],
                    "scene_number": proposal["scene_number"],
                    "expected_version": proposal["source_scene_version"],
                    "actual_version": scene["version"],
                    "message": "The scene was edited after this proposal was prepared.",
                }
            )
    return {
        "batch_id": batch_id,
        "safe_to_apply": not conflicts,
        "baseline": baseline,
        "current": current,
        "conflicts": conflicts,
    }


def prepare_for_approval(
    db: StudioDB,
    *,
    request_text: str,
    scene_numbers: list[int] | None = None,
    chapter_numbers: list[int] | None = None,
    include: list[str] | None = None,
    persona_ids: list[str] | None = None,
) -> dict[str, Any]:
    request_text = request_text.strip()
    if not request_text:
        raise ApprovalBridgeError("Approval preparation requires a change request.")
    include = [str(item).strip() for item in (include or []) if str(item).strip()]
    all_scenes = db.list_scenes()
    allowed = set(scene_numbers or [])
    if chapter_numbers:
        allowed.update(
            scene["scene_number"]
            for scene in all_scenes
            if scene.get("chapter_number") in set(chapter_numbers)
        )
    selected = [scene for scene in all_scenes if not allowed or scene["scene_number"] in allowed]
    if not selected:
        raise ApprovalBridgeError("The requested approval scope contains no imported scenes.")
    if len(selected) > 80:
        raise ApprovalBridgeError("Approval preparation is limited to 80 scenes per batch; narrow the scope.")

    project = db.project_info()
    baseline = {
        "branch": project.get("branch"),
        "commit_sha": project.get("commit_sha"),
        "source_digest": project.get("source_digest"),
        "scene_versions": {str(scene["scene_number"]): scene["version"] for scene in selected},
    }
    persona_context: list[dict[str, str]] = []
    for persona_id in persona_ids or []:
        persona = get_persona(persona_id)
        if persona:
            persona_context.append(
                {
                    "persona_id": persona["persona_id"],
                    "name": persona["name"],
                    "role": persona["title_or_archetype"],
                    "focus": persona["focus"],
                }
            )
    evidence = [
        {
            "scene_number": scene["scene_number"],
            "chapter_number": scene.get("chapter_number"),
            "chapter_title": scene.get("chapter_title"),
            "title": scene["title"],
            "version": scene["version"],
            "text": html_to_text(scene["content_html"])[:14000],
        }
        for scene in selected
    ]
    prompt = f"""
You are the Literary Studio approval-preparation editor. Turn the author's request
into exact, reviewable manuscript proposals. Do not apply changes. Do not invent
facts. Every original_verbatim_text must be copied exactly from one supplied scene,
must be short and contiguous, and must be replaced only in that scene. If a change
is uncertain, put it in questions instead of creating a proposal.

Return JSON only:
{{
  "summary": "brief description",
  "proposals": [
    {{
      "scene_number": 0,
      "change_type": "line_edit|continuity_fix|metadata|structure|other",
      "original_verbatim_text": "exact source text",
      "proposed_replacement_text": "replacement text",
      "editorial_rationale": "why this is proposed",
      "proposed_by_persona_id": "optional persona id",
      "proposed_by_name": "optional display name"
    }}
  ],
  "questions": ["author decision still needed"]
}}

AUTHOR REQUEST:
{request_text}

OPTIONAL CONTEXT TO INCLUDE:
{json.dumps(include, ensure_ascii=False)}

PERSONA LENSES:
{json.dumps(persona_context, ensure_ascii=False)}

SOURCE-PINNED SCENE EVIDENCE:
{json.dumps(evidence, ensure_ascii=False)}
""".strip()
    try:
        response = generate_text(
            prompt,
            model=os.getenv("LITERARY_STUDIO_APPROVAL_MODEL", "gemini-3.1-pro-preview"),
            temperature=0.1,
        )
    except AIProviderUnavailable as exc:
        raise ApprovalBridgeError(str(exc)) from exc
    parsed = _parse_json(response.text)
    batch = db.create_approval_batch(request_text, include, baseline)
    valid_scene_numbers = {scene["scene_number"] for scene in selected}
    by_scene = {scene["scene_number"]: scene for scene in selected}
    staged: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in parsed.get("proposals", []):
        if not isinstance(candidate, dict):
            rejected.append({"reason": "Proposal was not an object."})
            continue
        try:
            scene_number = int(candidate["scene_number"])
            original = str(candidate["original_verbatim_text"]).strip()
            replacement = str(candidate["proposed_replacement_text"])
        except (KeyError, TypeError, ValueError):
            rejected.append({"candidate": candidate, "reason": "Missing scene number or text anchor."})
            continue
        scene = by_scene.get(scene_number)
        if scene_number not in valid_scene_numbers or scene is None:
            rejected.append({"candidate": candidate, "reason": "Proposal targets a scene outside the requested scope."})
            continue
        if not original or not replacement.strip() or original == replacement:
            rejected.append({"candidate": candidate, "reason": "Proposal has an empty or unchanged text span."})
            continue
        if original not in html_to_text(scene["content_html"]):
            rejected.append({"candidate": candidate, "reason": "Exact text anchor was not found in the imported scene."})
            continue
        staged.append(
            db.create_staged_proposal(
                batch_id=batch["batch_id"],
                scene_number=scene_number,
                proposed_by_persona_id=str(candidate.get("proposed_by_persona_id") or "studio_command_bridge"),
                proposed_by_name=str(candidate.get("proposed_by_name") or response.provider.title()),
                change_type=str(candidate.get("change_type") or "other"),
                original_verbatim_text=original,
                proposed_replacement_text=replacement,
                editorial_rationale=str(candidate.get("editorial_rationale") or "Prepared for author review."),
                source_branch=project.get("branch"),
                source_commit_sha=project.get("commit_sha"),
                source_digest=project.get("source_digest"),
                source_scene_version=int(scene["version"]),
            )
        )
    batch = db.get_approval_batch(batch["batch_id"]) or batch
    batch.update(
        {
            "summary": str(parsed.get("summary") or "Approval proposals prepared."),
            "questions": [str(item) for item in parsed.get("questions", []) if str(item).strip()],
            "rejected": rejected,
            "provider": response.provider,
            "model": response.model,
            "proposals": staged,
        }
    )
    return batch
