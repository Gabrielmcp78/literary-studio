"""Copy-edit gate for proposal text before it reaches the Approval Desk.

The mechanical pass is deliberately narrower than a line edit: it corrects
standard spelling, punctuation, capitalization, grammar, and spacing while
preserving voice, dialect, technical terms, quotations, and intentional
fragments.  The returned correction is the text that reaches the Approval
Desk. Saved author text remains available in proposal validation for provenance.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ai_provider import AIProviderUnavailable, generate_text


class MechanicalReviewError(ValueError):
    """The required mechanical pass could not produce a trustworthy result."""


def _parse_json(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise MechanicalReviewError("The mechanical editor returned no structured result.") from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise MechanicalReviewError(f"The mechanical editor returned invalid JSON: {exc.msg}.") from exc
    if not isinstance(parsed, dict):
        raise MechanicalReviewError("The mechanical editor result was not an object.")
    return parsed


def review_replacements(
    items: list[dict[str, str]],
    *,
    allowed_providers: tuple[str, ...] | None = None,
    preferred_provider: str | None = None,
) -> dict[str, Any]:
    """Review replacement text in one batch and return corrected text plus provenance.

    ``items`` must contain stable ``id``, ``original``, ``replacement``, and
    ``context`` strings.  The function fails closed: a missing or malformed
    response prevents the proposal from being presented for approval.
    """
    if not items:
        return {"provider": None, "model": None, "fallback_reasons": [], "items": {}}

    prompt = f"""
You are the standard copy editor for a literary manuscript. Perform a strict
mechanical pass on each proposed replacement below before it reaches an author
approval desk.

Correct only clear standard-English spelling and mechanical errors: spelling,
punctuation, capitalization, grammar agreement, sentence-boundary mechanics,
and accidental spacing. Preserve the author's voice, diction, rhythm, dialect,
intentional fragments, profanity, technical and scientific terms, musical
terms, names, quotations, mathematical notation, and deliberate typography.
Do not make developmental, stylistic, substantive, continuity, or
"improvement" edits. Do not change the meaning. If no clear correction is
needed, return the replacement exactly as supplied.

Return JSON only in this shape:
{{
  "items": [
    {{
      "id": "the supplied id",
      "replacement_text": "the corrected replacement, preserving line breaks",
      "issues": [
        {{"kind": "spelling|punctuation|capitalization|grammar|spacing", "description": "short description"}}
      ]
    }}
  ]
}}

PROPOSALS:
{json.dumps(items, ensure_ascii=False)}
""".strip()

    try:
        response = generate_text(
            prompt,
            model=os.getenv("LITERARY_STUDIO_MECHANICAL_MODEL", "gemini-3.1-pro-preview"),
            temperature=0.0,
            system_instruction=(
                "You are a conservative copy editor. Output only the requested JSON. "
                "Never rewrite literary voice or invent a correction."
            ),
            allowed_providers=allowed_providers,
            preferred_provider=preferred_provider,
        )
    except AIProviderUnavailable as exc:
        raise MechanicalReviewError(f"The required mechanical pass is unavailable: {exc}") from exc

    parsed = _parse_json(response.text)
    rows = parsed.get("items")
    if not isinstance(rows, list):
        raise MechanicalReviewError("The mechanical editor result did not contain an items list.")

    expected = {item["id"] for item in items}
    reviewed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MechanicalReviewError("The mechanical editor returned a non-object item.")
        item_id = str(row.get("id") or "")
        replacement = row.get("replacement_text")
        if item_id not in expected or not isinstance(replacement, str):
            raise MechanicalReviewError("The mechanical editor returned an unknown item or missing replacement text.")
        if item_id in reviewed:
            raise MechanicalReviewError(f"The mechanical editor returned item {item_id} more than once.")
        raw_issues = row.get("issues", [])
        if not isinstance(raw_issues, list):
            raise MechanicalReviewError(f"The mechanical editor returned invalid issues for item {item_id}.")
        issues: list[dict[str, str]] = []
        for raw_issue in raw_issues:
            if not isinstance(raw_issue, dict):
                continue
            kind = str(raw_issue.get("kind") or "mechanical")
            description = str(raw_issue.get("description") or "").strip()
            if description:
                issues.append({"kind": kind, "description": description})
        supplied = next(item["replacement"] for item in items if item["id"] == item_id)
        reviewed[item_id] = {
            "replacement_text": replacement,
            "issues": issues,
            "changed": replacement != supplied,
        }

    missing = expected - reviewed.keys()
    if missing:
        raise MechanicalReviewError(
            "The mechanical editor omitted proposal item(s): " + ", ".join(sorted(missing))
        )
    return {
        "provider": response.provider,
        "model": response.model,
        "fallback_reasons": list(response.fallback_reasons),
        "items": reviewed,
    }
