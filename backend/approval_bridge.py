"""Author-controlled bridge from IDE/chat requests to the governance queue."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ai_provider import AIProviderUnavailable, generate_text
from governance import GovernanceError, check_batch, current_source_state, prose_law_status, validate_candidate
from mechanical_review import MechanicalReviewError, review_replacements
from persona_catalog import get_panel, get_persona
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


def check_approval_conflicts(db: StudioDB, batch_id: str, proposal_id: str | None = None) -> dict[str, Any]:
    """Per-passage staleness check for a batch (see governance.check_batch)."""
    try:
        return check_batch(db, batch_id, proposal_id)
    except GovernanceError as exc:
        raise ApprovalBridgeError(str(exc)) from exc


def _proposal_context(scene: dict[str, Any], original: str) -> str:
    """Give the mechanical pass enough surrounding text to judge sentence mechanics."""
    scene_text = html_to_text(scene.get("content_html", ""))
    position = scene_text.find(original)
    if position < 0:
        return scene_text[:1200]
    start = max(0, position - 500)
    end = min(len(scene_text), position + len(original) + 500)
    return scene_text[start:end]


def prepare_for_approval(
    db: StudioDB,
    *,
    request_text: str,
    scene_numbers: list[int] | None = None,
    chapter_numbers: list[int] | None = None,
    include: list[str] | None = None,
    persona_ids: list[str] | None = None,
    panel_id: str | None = None,
    allowed_providers: tuple[str, ...] | None = None,
    preferred_provider: str | None = None,
) -> dict[str, Any]:
    """Turn an author request into validated, source-pinned proposals.

    Parameters: scene_numbers/chapter_numbers - scope (empty = whole manuscript,
    max 80 scenes); include - context lenses; persona_ids and/or panel_id -
    editorial voices.
    Behavior: pins the live branch, commit, and per-file digests; asks the AI
    provider for proposals; stages only candidates that pass governance
    validation; persists summary, questions, rejections, and provenance.
    Failure: ApprovalBridgeError for empty request/scope, unknown panel,
    provider unavailability, or unparseable AI output.
    """
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
    live = current_source_state(db)
    scene_paths = {scene.get("source_path") for scene in selected if scene.get("source_path")}
    baseline = {
        "branch": live.get("branch"),
        "commit_sha": live.get("commit_sha"),
        "source_digest": live.get("source_digest"),
        "read_status": live.get("read_status"),
        "scene_versions": {str(scene["scene_number"]): scene["version"] for scene in selected},
        "file_digests": {path: digest for path, digest in live.get("file_digests", {}).items() if path in scene_paths},
    }
    persona_ids = list(persona_ids or [])
    if panel_id:
        panel = get_panel(panel_id)
        if panel is None:
            raise ApprovalBridgeError(f"Unknown persona panel: {panel_id}.")
        persona_ids += [pid for pid in panel.get("persona_ids", []) if pid not in persona_ids]
    persona_context: list[dict[str, str]] = []
    for persona_id in persona_ids:
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
facts. If a change is uncertain, put it in questions instead of creating a proposal.

Every proposal is machine-checked and discarded if it breaks these rules:
- original_verbatim_text is copied character-for-character from one supplied scene,
  is contiguous, stays inside one paragraph, and occurs exactly once in that scene
  (include enough surrounding words to make it unique).
- Do not start or end a span inside italic or bold text.
- The replacement keeps quotation marks and brackets balanced: if you move dialogue,
  carry both its opening and closing quotation marks.
- The replacement must not introduce prose-law violations: binary-negative cadence
  ("Not X. Not Y.", "not as X, not as Y"), more em-dashes, three consecutive
  fragments of three words or fewer, or two spaces after sentence punctuation.
- When persona lenses are supplied, set proposed_by_persona_id and proposed_by_name
  to the persona whose lens the proposal comes from.

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
            allowed_providers=allowed_providers,
            preferred_provider=preferred_provider,
        )
    except AIProviderUnavailable as exc:
        raise ApprovalBridgeError(str(exc)) from exc
    parsed = _parse_json(response.text)
    batch = db.create_approval_batch(request_text, include, baseline)
    persona_names = {item["persona_id"]: item["name"] for item in persona_context}
    valid_scene_numbers = {scene["scene_number"] for scene in selected}
    by_scene = {scene["scene_number"]: scene for scene in selected}
    staged: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    candidates_for_mechanical_review: list[dict[str, Any]] = []
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
        candidates_for_mechanical_review.append(
            {
                "id": f"proposal-{len(candidates_for_mechanical_review)}",
                "candidate": candidate,
                "scene": scene,
                "scene_number": scene_number,
                "original": original,
                "replacement": replacement,
            }
        )

    mechanical_items = [
        {
            "id": item["id"],
            "original": item["original"],
            "replacement": item["replacement"],
            "context": _proposal_context(item["scene"], item["original"]),
        }
        for item in candidates_for_mechanical_review
    ]
    mechanical: dict[str, Any] = {
        "provider": None,
        "model": None,
        "fallback_reasons": [],
        "items": {},
        "status": "not_run",
    }
    if mechanical_items:
        try:
            mechanical_allowed_providers = allowed_providers or ("gemini", "agy")
            mechanical = review_replacements(
                mechanical_items,
                allowed_providers=mechanical_allowed_providers,
                preferred_provider=preferred_provider or os.getenv("LITERARY_STUDIO_EDITORIAL_PROVIDER", "auto"),
            )
            mechanical["status"] = "completed"
        except MechanicalReviewError as exc:
            # Mechanical review is an advisory copy-edit pass.  Model
            # unavailability (and other pass-level failures) must not prevent
            # an otherwise governance-valid proposal reaching the author desk.
            # Keep the AI's proposed text unchanged and surface the failure in
            # both validation and batch provenance.
            mechanical["status"] = "unavailable"
            mechanical["fallback_reasons"] = [f"Mechanical review did not run: {exc}"]

    for item in candidates_for_mechanical_review:
        candidate = item["candidate"]
        scene = item["scene"]
        original = item["original"]
        reviewed = mechanical["items"].get(item["id"])
        if reviewed is None:
            reviewed = {
                "replacement_text": item["replacement"],
                "issues": [],
                "changed": False,
                "status": "unavailable",
            }
        replacement = reviewed["replacement_text"]
        if not replacement.strip() or original == replacement:
            rejected.append({
                "candidate": candidate,
                "reason": "The mechanical pass produced an empty or unchanged text span.",
                "mechanical_review": reviewed,
            })
            continue
        validation = validate_candidate(project, scene, original, replacement)
        validation["mechanical_review"] = {
            "provider": mechanical["provider"],
            "model": mechanical["model"],
            "fallback_reasons": mechanical["fallback_reasons"],
            "issues": reviewed["issues"],
            "changed": reviewed["changed"],
            "status": mechanical["status"],
            "applied_correction": mechanical["status"] == "completed",
        }
        if mechanical["status"] == "unavailable":
            validation["warnings"].append(
                "Mechanical spelling and copy-edit pass did not run; the original proposal was submitted unchanged for author review."
            )
        if reviewed["issues"]:
            validation["warnings"].extend(
                f"Mechanical pass: {issue['description']}" for issue in reviewed["issues"]
            )
        if validation["errors"]:
            rejected.append({"candidate": candidate, "reason": " ".join(validation["errors"]), "validation": validation})
            continue
        persona_id = str(candidate.get("proposed_by_persona_id") or "").strip()
        if persona_id not in persona_names:
            persona_id = "studio_command_bridge"
        staged.append(
            db.create_staged_proposal(
                batch_id=batch["batch_id"],
                scene_number=item["scene_number"],
                proposed_by_persona_id=persona_id,
                proposed_by_name=persona_names.get(persona_id, "Studio Bridge"),
                change_type=str(candidate.get("change_type") or "other"),
                original_verbatim_text=original,
                proposed_replacement_text=replacement,
                editorial_rationale=str(candidate.get("editorial_rationale") or "Prepared for author review."),
                source_branch=baseline.get("branch"),
                source_commit_sha=baseline.get("commit_sha"),
                source_digest=baseline.get("source_digest"),
                source_scene_version=int(scene["version"]),
                validation=validation,
            )
        )
    result = {
        "summary": str(parsed.get("summary") or "Approval proposals prepared."),
        "questions": [str(item) for item in parsed.get("questions", []) if str(item).strip()],
        "rejected": rejected,
        "provenance": {
            "provider": response.provider,
            "model": response.model,
            "fallback_reasons": list(response.fallback_reasons),
            "persona_ids": persona_ids,
            "panel_id": panel_id,
            "prose_law": prose_law_status(),
            "mechanical_review": {
                "provider": mechanical["provider"],
                "model": mechanical["model"],
                "fallback_reasons": mechanical["fallback_reasons"],
                "status": mechanical["status"],
                "required": False,
                "advisory": True,
            },
        },
    }
    db.update_approval_batch(batch["batch_id"], result=result, status="pending" if staged else "closed")
    batch = db.get_approval_batch(batch["batch_id"]) or batch
    batch.update(result)
    batch["provider"], batch["model"] = response.provider, response.model
    return batch
