import uuid
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field

def generate_uuid() -> str:
    return str(uuid.uuid4())

class UnifiedPersonaDossier(BaseModel):
    persona_id: str
    name: str
    role: Literal["editor", "agent", "author_craft"]
    organization_or_imprint: str
    tier: Optional[int] = 1
    title_or_archetype: str
    taste_summary: str
    preferred_genres: List[str] = Field(default_factory=list)
    voice_sensibility: str
    pacing_sensibility: str
    thematic_or_structural_focus: str
    notable_clients_or_authors: List[str] = Field(default_factory=list)
    green_flags: List[str] = Field(default_factory=list)
    red_flags_or_dealbreakers: List[str] = Field(default_factory=list)
    representative_excerpts: List[Dict[str, str]] = Field(default_factory=list)
    submission_fit_or_approach: Optional[str] = None

class MarginAnnotation(BaseModel):
    annotation_id: str = Field(default_factory=generate_uuid)
    scene_number: int
    persona_id: str
    persona_name: str
    persona_role: str
    category: Literal["voice", "pacing", "subtext", "continuity", "market_hook", "line_craft"]
    severity: Literal["praise", "suggestion", "warning", "dealbreaker"]
    exact_quote_anchor: str
    comment_body: str
    plot_tags: List[str] = Field(default_factory=list)
    status: Literal["open", "resolved", "dismissed"] = "open"

class StagedDiffProposal(BaseModel):
    proposal_id: str = Field(default_factory=generate_uuid)
    scene_number: int
    proposed_by_persona_id: str
    proposed_by_name: str
    change_type: Literal["line_tightening", "subtext_elevation", "continuity_fix", "structural_cut"]
    original_verbatim_text: str
    proposed_replacement_text: str
    editorial_rationale: str
    status: Literal["staged", "accepted", "rejected", "modified"] = "staged"

class SceneCommit(BaseModel):
    commit_id: str = Field(default_factory=generate_uuid)
    parent_commit_id: Optional[str] = None
    branch_name: str = "main"
    scene_number: int
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    author_or_persona: str
    commit_message: str
    applied_proposal_ids: List[str] = Field(default_factory=list)
    content_sha256: str
    full_scene_text: str

    @staticmethod
    def compute_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
