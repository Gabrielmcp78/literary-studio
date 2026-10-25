import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ai_provider import AIProviderUnavailable, generate_text
from approval_bridge import ApprovalBridgeError, check_approval_conflicts, prepare_for_approval
from compiler_engine import compile_shunn_pdf
from governance import GovernanceError, commit_batch, finalize_batch_if_done, record_write, restore, write_back
from context_package import CONTEXT_OPTIONS, ContextPackageError, build_context_package
from intake_audit import run_intake_audit
from manuscript_context import ManuscriptContextError, build_interrogation_context, context_prompt, manuscript_index
from persona_catalog import (
    PERSONA_PANELS,
    PERSONA_ROSTER,
    catalog_summary,
    get_panel,
    get_persona,
    public_persona,
)
from project_manager import ProjectManager, ProjectNotFound
from source_snapshot import SnapshotError, build_snapshot, write_snapshot
from studio_db import PROJECTS_ROOT, StudioDB, html_to_text, utc_now


app = FastAPI(title="Literary Studio API", version="1.1.0")
project_manager = ProjectManager()
project_manager.ensure_default_project()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_project_db(project_id: str) -> StudioDB:
    """Open a project by Studio id, title, slug, or FictionForce name."""
    try:
        return project_manager.open(project_manager.resolve(project_id))
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.get("/api/registry")
def project_registry():
    """Studio projects joined with FictionForce projects by manuscript root."""
    return project_manager.registry()


@app.get("/health")
def health_check():
    projects = project_manager.list_projects()
    return {
        "status": "healthy",
        "suite": "literary studio project-isolated vertical slice",
        "projects": len(projects),
    }


@app.get("/api/projects")
def list_projects():
    return project_manager.list_projects()


class ProjectCreateRequest(BaseModel):
    title: str
    project_id: str | None = None
    source_root: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    source_digest: str | None = None


@app.post("/api/projects")
def create_project(req: ProjectCreateRequest):
    try:
        return project_manager.create_project(
            req.title,
            req.project_id,
            req.source_root,
            req.branch,
            req.commit_sha,
            req.source_digest,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return get_project_db(project_id).project_info()


class SourceSnapshotRequest(BaseModel):
    source_root: str
    source_subpath: str | None = None
    replace_existing: bool = False


class ContextPackageRequest(BaseModel):
    chapter_start: int
    chapter_end: int
    include: List[str] = Field(default_factory=list)


class ApprovalPreparationRequest(BaseModel):
    request_text: str
    scene_numbers: List[int] = Field(default_factory=list)
    chapter_numbers: List[int] = Field(default_factory=list)
    include: List[str] = Field(default_factory=list)
    persona_ids: List[str] = Field(default_factory=list)
    panel_id: str | None = None


@app.post("/api/projects/{project_id}/context-package")
def create_context_package(project_id: str, req: ContextPackageRequest):
    """Build a read-only, provenance-pinned package for an external LLM chat."""
    db = get_project_db(project_id)
    unknown_options = sorted(set(req.include) - set(CONTEXT_OPTIONS))
    if unknown_options:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown context options: {', '.join(unknown_options)}",
        )
    try:
        return build_context_package(
            db,
            req.chapter_start,
            req.chapter_end,
            req.include,
        )
    except ContextPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/source-snapshot")
def create_source_snapshot(project_id: str, req: SourceSnapshotRequest):
    db = get_project_db(project_id)
    has_existing_scenes = bool(db.list_scenes())
    if has_existing_scenes and not req.replace_existing:
        raise HTTPException(
            status_code=409,
            detail="This project already contains manuscript scenes. Set replace_existing=true only to replace this project's imported snapshot.",
        )
    try:
        project = db.project_info()
        canonical_id = project["project_id"]  # the URL may carry an alias
        snapshot = build_snapshot(
            canonical_id,
            project["title"],
            req.source_root,
            req.source_subpath,
        )
        output_dir = PROJECTS_ROOT / canonical_id / "source"
        snapshot_path, manifest_path = write_snapshot(snapshot, output_dir)
        if has_existing_scenes:
            db.reset_manuscript()
        db.update_source_metadata(
            source_root=snapshot["manifest"]["source_root"],
            source_subpath=snapshot["manifest"]["source_subpath"],
            branch=snapshot["manifest"]["branch"],
            commit_sha=snapshot["manifest"]["commit_sha"],
            source_digest=snapshot["manifest"]["source_digest"],
            snapshot_path=str(snapshot_path),
            manifest_path=str(manifest_path),
            working_tree_dirty=snapshot["manifest"]["working_tree_dirty"],
        )
        scenes = db.import_repository_scenes(snapshot["files"])
        db.rebuild_story_graph()
        intake_audit = run_intake_audit(db, source_label=f"{project['title']} · {snapshot['manifest']['branch']}")
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to write source snapshot: {exc}") from exc
    return {
        "status": "snapshotted",
        "project": db.project_info(),
        "manifest": snapshot["manifest"],
        "snapshot_path": str(snapshot_path),
        "manifest_path": str(manifest_path),
        "scenes": scenes,
        "intake_audit": intake_audit,
    }


@app.get("/api/projects/{project_id}/telemetry/latest")
def get_latest_telemetry(project_id: str):
    return get_project_db(project_id).telemetry()


@app.get("/api/projects/{project_id}/scenes")
def list_scenes(project_id: str):
    return get_project_db(project_id).list_scenes()


@app.get("/api/projects/{project_id}/manuscript/index")
def get_manuscript_index(project_id: str):
    """Return the complete scene roster for this project, without prose."""
    return manuscript_index(get_project_db(project_id))


class ManuscriptImportRequest(BaseModel):
    title: str = "Imported material"
    text: str
    split_mode: str = "single"


class RepositoryFile(BaseModel):
    path: str
    text: str


class RepositoryImportRequest(BaseModel):
    title: str = "Imported repository"
    files: List[RepositoryFile]


class IntakeAIRequest(BaseModel):
    material: str
    question: str = "What structural boundaries, continuity risks, or intake decisions should I review before importing this material?"
    source_label: str = "manuscript intake"


class IntakeAuditRequest(BaseModel):
    source_label: str = "manuscript intake"
    run_ai: bool = True


@app.post("/api/projects/{project_id}/intake/analyze")
def analyze_intake(project_id: str, req: IntakeAIRequest):
    """Provide an explicit AI intake preview without importing or mutating material."""
    get_project_db(project_id)
    material = req.material.strip()
    if not material:
        raise HTTPException(status_code=400, detail="Intake material cannot be empty.")
    if len(material) > 250_000:
        raise HTTPException(status_code=413, detail="Intake analysis is limited to 250,000 characters.")
    question = req.question.strip() or IntakeAIRequest.model_fields["question"].default
    prompt = f"""
You are the Literary Studio intake analyst.
Review the supplied manuscript material before it is imported into a project.
Do not rewrite it, do not invent canon, and do not import or mutate anything.
Answer the author's question with concise, evidence-based observations.
Identify explicit chapter headings, explicit internal scene breaks, continuity risks,
and any ambiguity that should be resolved by the author before ingestion.

AUTHOR QUESTION:
{question}

SOURCE LABEL: {req.source_label.strip() or "manuscript intake"}

MATERIAL:
{material}
""".strip()
    try:
        ai_response = generate_text(
            prompt,
            model=os.getenv("LITERARY_STUDIO_INTAKE_MODEL", "gemini-3.1-pro-preview"),
            temperature=0.2,
        )
    except AIProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"AI intake analysis unavailable: {exc}") from exc
    return {
        "project_id": project_id,
        "source_label": req.source_label,
        "question": question,
        "analysis": ai_response.text,
        "provider": ai_response.provider,
        "model": ai_response.model,
        "imported": False,
    }


@app.get("/api/projects/{project_id}/intake/audit/latest")
def latest_intake_audit(project_id: str):
    audit = get_project_db(project_id).latest_intake_audit()
    return audit or {"status": "none", "project_id": project_id}


@app.post("/api/projects/{project_id}/intake/audit")
def audit_imported_manuscript(project_id: str, req: IntakeAuditRequest):
    db = get_project_db(project_id)
    try:
        return run_intake_audit(db, req.source_label, req.run_ai)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/scenes/import")
def import_manuscript(project_id: str, req: ManuscriptImportRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Manuscript material cannot be empty.")
    try:
        db = get_project_db(project_id)
        scenes = db.import_scenes(req.title, req.text, req.split_mode)
        db.rebuild_story_graph()
        intake_audit = run_intake_audit(db, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "imported", "project_id": project_id, "scenes": scenes, "intake_audit": intake_audit}


@app.post("/api/projects/{project_id}/scenes/import-repository")
def import_repository(project_id: str, req: RepositoryImportRequest):
    if not req.files:
        raise HTTPException(status_code=400, detail="Repository contains no Markdown files.")
    if len(req.files) > 500:
        raise HTTPException(status_code=413, detail="Repository import is limited to 500 Markdown files.")
    try:
        db = get_project_db(project_id)
        scenes = db.import_repository_scenes(
            [file.model_dump() for file in req.files]
        )
        db.rebuild_story_graph()
        intake_audit = run_intake_audit(db, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "imported", "project_id": project_id, "scenes": scenes, "intake_audit": intake_audit}


@app.get("/api/projects/{project_id}/scenes/{scene_number}")
def get_scene(project_id: str, scene_number: int):
    scene = get_project_db(project_id).get_scene(scene_number)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found.")
    return scene


@app.get("/api/projects/{project_id}/scenes/{scene_number}/history")
def get_scene_history(project_id: str, scene_number: int):
    db = get_project_db(project_id)
    if db.get_scene(scene_number) is None:
        raise HTTPException(status_code=404, detail="Scene not found.")
    return db.scene_history(scene_number)


class SceneUpdate(BaseModel):
    content_html: str
    summary: str = "Scene saved"


class RestoreRevisionRequest(BaseModel):
    revision_id: int


@app.put("/api/projects/{project_id}/scenes/{scene_number}")
def update_scene(project_id: str, scene_number: int, req: SceneUpdate):
    if not req.content_html.strip():
        raise HTTPException(status_code=400, detail="Scene content cannot be empty.")
    try:
        return get_project_db(project_id).update_scene(scene_number, req.content_html, req.summary)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scene not found.") from exc


@app.post("/api/projects/{project_id}/scenes/{scene_number}/restore")
def restore_scene_revision(project_id: str, scene_number: int, req: RestoreRevisionRequest):
    try:
        return get_project_db(project_id).restore_revision(scene_number, req.revision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scene or revision not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/story-graph")
def get_story_graph(project_id: str):
    return get_project_db(project_id).story_graph()


@app.get("/api/projects/{project_id}/story-intelligence")
def get_story_intelligence(project_id: str):
    return get_project_db(project_id).story_intelligence()


@app.post("/api/projects/{project_id}/story-graph/rebuild")
def rebuild_story_graph(project_id: str):
    db = get_project_db(project_id)
    if not db.list_scenes():
        raise HTTPException(status_code=409, detail="Import manuscript scenes before building the story graph.")
    return db.rebuild_story_graph()


class EvaluationRequest(BaseModel):
    persona_id: str
    scene_number: int
    scene_text: str = ""
    focus_scope: Literal["scene", "selection"] = "scene"
    focus_instruction: str = ""
    focus_text: str = ""


class PanelEvaluationRequest(BaseModel):
    scene_number: int
    scene_text: str = ""
    persona_ids: List[str] | None = None
    panel_id: str | None = None
    focus_scope: Literal["scene", "selection"] = "scene"
    focus_instruction: str = ""
    focus_text: str = ""


def _parse_persona_result(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                pass
    return {
        "verdict": "REVIEW",
        "executive_reaction": raw_text,
        "margin_annotations": [],
        "staged_diff_proposals": [],
    }


def _evaluate_persona(
    scene_number: int,
    scene_text: str,
    persona: dict,
    *,
    focus_scope: str = "scene",
    focus_instruction: str = "",
    focus_text: str = "",
) -> dict:
    green_flags = ", ".join(persona["green_flags"])
    red_flags = ", ".join(persona["red_flags"])
    clean_focus = focus_instruction.strip() or "No additional author focus was supplied."
    clean_focus_text = focus_text.strip()[:20000]
    focus_block = f"""
AUTHOR FOCUS
Scope: {focus_scope}
Instruction: {clean_focus}
""".strip()
    if clean_focus_text:
        focus_block += f"""

SELECTED PASSAGE (exact text supplied by the author; prioritize this passage when scope is 'selection')
{clean_focus_text}
"""
    prompt = f"""
You are one independent editorial simulator in a multi-persona literary panel.
Do not imitate or claim to be a real person. Stay inside this role's dossier.
Evaluate Scene {scene_number} only, applying the author's focus below. If the scope is
'selection', constrain close-reading and quote anchors to the selected passage while
using the rest of the scene only for necessary context. Do not rewrite the manuscript and do not invent
facts, quotations, plot events, or market data. Every annotation anchor must be an
exact short quotation copied from the supplied scene, or the annotation list must be empty.

PERSONA DOSSIER
Name: {persona['name']}
Profile category: {persona['category']}
Role: {persona['title_or_archetype']}
Desk: {persona['organization_or_imprint']}
Taste: {persona['taste_summary']}
Primary focus: {persona['focus']}
Green flags: {green_flags}
Red flags: {red_flags}

SOURCE PROFILE (use as a lens, not as a claim that this real person reviewed the text)
{persona.get('profile_context', '')}

{focus_block}

Return JSON only with this shape:
{{
  "verdict": "REQUEST_FULL|PASS|REVISE",
  "executive_reaction": "brief, specific editorial reaction in this persona's voice",
  "margin_annotations": [
    {{"category":"voice|pacing|subtext|continuity|market_hook|line_craft", "severity":"praise|suggestion|warning|dealbreaker", "exact_quote_anchor":"exact short quote", "comment_body":"specific note"}}
  ],
  "staged_diff_proposals": []
}}

SCENE {scene_number}
{scene_text}
""".strip()
    ai_response = generate_text(
        prompt,
        model=os.getenv("LITERARY_STUDIO_EVAL_MODEL", "gemini-3.1-pro-preview"),
        temperature=0.2,
    )
    result = _parse_persona_result(ai_response.text)
    annotations = []
    for index, annotation in enumerate(result.get("margin_annotations", []), start=1):
        if not isinstance(annotation, dict):
            continue
        annotations.append(
            {
                "annotation_id": f"ann_panel_{scene_number}_{persona['persona_id']}_{index}",
                "scene_number": scene_number,
                "persona_id": persona["persona_id"],
                "persona_name": persona["name"],
                "persona_role": persona["role"],
                "category": annotation.get("category", "line_craft"),
                "severity": annotation.get("severity", "suggestion"),
                "exact_quote_anchor": annotation.get("exact_quote_anchor", ""),
                "comment_body": annotation.get("comment_body", ""),
            }
        )
    return {
        "persona_id": persona["persona_id"],
        "persona_name": persona["name"],
        "persona_role": persona["role"],
        "title_or_archetype": persona["title_or_archetype"],
        "verdict": result.get("verdict", "REVIEW"),
        "executive_reaction": result.get("executive_reaction", ai_response.text),
        "margin_annotations": annotations,
        "staged_diff_proposals": result.get("staged_diff_proposals", []),
        "provider": ai_response.provider,
        "model": ai_response.model,
        "independent": True,
    }


@app.get("/api/projects/{project_id}/personas")
def list_personas(project_id: str):
    get_project_db(project_id)
    return [public_persona(persona) for persona in PERSONA_ROSTER]


@app.get("/api/projects/{project_id}/personas/catalog")
def get_persona_catalog(project_id: str):
    get_project_db(project_id)
    return {
        "project_id": project_id,
        "summary": catalog_summary(),
        "panels": PERSONA_PANELS,
        "personas": [public_persona(persona) for persona in PERSONA_ROSTER],
    }


@app.get("/api/projects/{project_id}/personas/evaluations")
def list_persona_evaluations(project_id: str, scene_number: int | None = None):
    """Return saved advisory persona passes, optionally limited to one scene."""
    db = get_project_db(project_id)
    return {
        "project_id": project_id,
        "scene_number": scene_number,
        "evaluations": db.list_persona_evaluations(scene_number),
    }


@app.post("/api/projects/{project_id}/personas/evaluate")
def evaluate_scene_endpoint(project_id: str, req: EvaluationRequest):
    db = get_project_db(project_id)
    scene = db.get_scene(req.scene_number)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found.")
    persona = get_persona(req.persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found.")
    try:
        evaluation = {
            "project_id": project_id,
            "scene_number": req.scene_number,
            "focus_scope": req.focus_scope,
            "focus_instruction": req.focus_instruction,
            "focus_text_supplied": bool(req.focus_text.strip()),
            **_evaluate_persona(
                req.scene_number,
                html_to_text(scene["content_html"]),
                persona,
                focus_scope=req.focus_scope,
                focus_instruction=req.focus_instruction,
                focus_text=req.focus_text,
            ),
        }
        return db.save_persona_evaluation(evaluation)
    except AIProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Persona evaluation unavailable: {exc}") from exc


@app.post("/api/projects/{project_id}/personas/evaluate-all")
def evaluate_all_personas(project_id: str, req: PanelEvaluationRequest):
    db = get_project_db(project_id)
    scene = db.get_scene(req.scene_number)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found.")

    if req.persona_ids is not None:
        requested_ids = req.persona_ids
    elif req.panel_id:
        panel = get_panel(req.panel_id)
        if panel is None:
            raise HTTPException(status_code=400, detail=f"Unknown persona panel: {req.panel_id}")
        requested_ids = panel["persona_ids"]
    else:
        requested_ids = get_panel("editorial_workshop")["persona_ids"]  # type: ignore[index]
    personas = []
    for persona_id in requested_ids:
        persona = get_persona(persona_id)
        if persona is None:
            raise HTTPException(status_code=400, detail=f"Unknown persona: {persona_id}")
        if persona not in personas:
            personas.append(persona)
    if not personas:
        raise HTTPException(status_code=400, detail="At least one persona is required.")

    scene_text = html_to_text(scene["content_html"])
    evaluations: dict[str, dict] = {}
    failures: dict[str, str] = {}
    max_workers = max(1, min(len(personas), int(os.getenv("LITERARY_STUDIO_PANEL_WORKERS", "8"))))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _evaluate_persona,
                req.scene_number,
                scene_text,
                persona,
                focus_scope=req.focus_scope,
                focus_instruction=req.focus_instruction,
                focus_text=req.focus_text,
            ): persona
            for persona in personas
        }
        for future in as_completed(futures):
            persona = futures[future]
            try:
                evaluations[persona["persona_id"]] = future.result()
            except AIProviderUnavailable as exc:
                failures[persona["persona_id"]] = str(exc)
            except Exception as exc:  # One simulator must not take down the panel.
                failures[persona["persona_id"]] = f"Simulator failed: {exc}"

    ordered_results = [
        db.save_persona_evaluation({
            "project_id": project_id,
            "scene_number": req.scene_number,
            "focus_scope": req.focus_scope,
            "focus_instruction": req.focus_instruction,
            "focus_text_supplied": bool(req.focus_text.strip()),
            **evaluations[persona["persona_id"]],
        })
        for persona in personas
        if persona["persona_id"] in evaluations
    ]
    return {
        "project_id": project_id,
        "scene_number": req.scene_number,
        "panel_id": req.panel_id,
        "focus_scope": req.focus_scope,
        "focus_instruction": req.focus_instruction,
        "focus_text_supplied": bool(req.focus_text.strip()),
        "independent": True,
        "requested": len(personas),
        "completed": len(ordered_results),
        "failed": failures,
        "evaluations": ordered_results,
    }


class ChatMessage(BaseModel):
    role: str
    content: str


class InterrogationRequest(BaseModel):
    persona_id: str
    scene_number: int
    conversation_history: List[ChatMessage]
    user_message: str
    current_screen_text: str = ""
    target_scope: Literal["screen", "scene", "chapter"] = "screen"
    target_chapter_number: int | None = None
    target_scene_number_in_chapter: int | None = None
    target_global_scene_number: int | None = None


@app.post("/api/projects/{project_id}/interrogation/chat")
def interrogation_chat_endpoint(project_id: str, req: InterrogationRequest):
    db = get_project_db(project_id)
    try:
        context = build_interrogation_context(
            db,
            current_scene_number=req.scene_number,
            current_screen_text=req.current_screen_text,
            target_scope=req.target_scope,
            target_chapter_number=req.target_chapter_number,
            target_scene_number_in_chapter=req.target_scene_number_in_chapter,
            target_global_scene_number=req.target_global_scene_number,
            user_message=req.user_message,
        )
    except ManuscriptContextError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persona = get_persona(req.persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found.")
    system_instruction = (
        f"You are a profile-informed editorial simulator for {persona['name']}, based on a "
        f"source dossier from the Fiction Git System. This is a simulation, not a claim that "
        "the real person reviewed the manuscript. Provide rigorous, role-specific feedback "
        "on the supplied manuscript context. Stay inside this profile's sensibilities and do not "
        f"invent facts or rewrite the manuscript.\n\nSOURCE DOSSIER:\n{persona.get('profile_context', '')}"
    )
    transcript = "\n".join(
        f"{message.role.upper()}: {message.content}" for message in req.conversation_history
    )
    prompt = f"{system_instruction}\n\n{context_prompt(context)}\n\nCONVERSATION:\n{transcript}\nUSER: {req.user_message}".strip()
    try:
        ai_response = generate_text(
            prompt,
            model=os.getenv("LITERARY_STUDIO_CHAT_MODEL", "gemini-3.1-pro-preview"),
            temperature=0.7,
            system_instruction=system_instruction,
        )
    except AIProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"AI chat unavailable: {exc}") from exc

    return {
        "project_id": project_id,
        "response_message": ai_response.text or "...",
        "persona_id": req.persona_id,
        "provider": ai_response.provider,
        "model": ai_response.model,
        "context": {
            "current_scene_number": context["current_screen"]["scene_number"],
            "target_label": context["target_label"],
            "target_scope": context["target_scope"],
            "manuscript_scene_count": len(context["manuscript_index"]),
            "current_screen_source": context["current_screen"]["source"],
        },
    }


class CompilerRequest(BaseModel):
    title: str = "Manuscript"
    author: str = "Author"


@app.post("/api/projects/{project_id}/compiler/export-shunn")
def export_shunn_pdf(project_id: str, req: CompilerRequest | None = None):
    db = get_project_db(project_id)
    output_filename = f"{project_id}_Shunn.pdf"
    output_dir = PROJECTS_ROOT / get_project_db(project_id).project_info()["project_id"] / "build_artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = os.path.abspath(output_dir / output_filename)

    request = req or CompilerRequest()
    scenes = [
        {
            "scene_number": str(scene["scene_number"]),
            "title": scene["title"],
            "text": html_to_text(scene["content_html"]),
        }
        for scene in db.list_scenes()
    ]
    compile_shunn_pdf(scenes, output_path, request.title, request.author)
    return FileResponse(path=output_path, media_type="application/pdf", filename=output_filename)


@app.get("/api/projects/{project_id}/governance/proposals")
def get_staged_proposals(project_id: str):
    return get_project_db(project_id).staged_proposals()


@app.post("/api/projects/{project_id}/approval-batches")
def prepare_approval_batch(project_id: str, req: ApprovalPreparationRequest):
    """Prepare AI recommendations as source-pinned governance proposals."""
    try:
        return prepare_for_approval(
            get_project_db(project_id),
            request_text=req.request_text,
            scene_numbers=req.scene_numbers or None,
            chapter_numbers=req.chapter_numbers or None,
            include=req.include,
            persona_ids=req.persona_ids,
            panel_id=req.panel_id,
        )
    except ApprovalBridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/approval-batches/{batch_id}")
def get_approval_batch(project_id: str, batch_id: str):
    batch = get_project_db(project_id).get_approval_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Approval batch not found.")
    return batch


@app.post("/api/projects/{project_id}/approval-batches/{batch_id}/commit")
def commit_approval_batch(project_id: str, batch_id: str):
    """Commit a batch's written-back source files now (retry after a failure, or commit early)."""
    try:
        return commit_batch(get_project_db(project_id), batch_id)
    except GovernanceError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc


@app.get("/api/projects/{project_id}/governance/history")
def proposal_history(project_id: str, limit: int = 50, status: str | None = None):
    """Reviewed proposals, newest first; status may be 'accepted' or 'rejected'."""
    if status not in (None, "accepted", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'accepted' or 'rejected'.")
    return get_project_db(project_id).proposal_history(limit=limit, status=status)


@app.post("/api/projects/{project_id}/approval-batches/{batch_id}/check")
def check_approval_batch(project_id: str, batch_id: str):
    try:
        return check_approval_conflicts(get_project_db(project_id), batch_id)
    except ApprovalBridgeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class AnnotationCreateRequest(BaseModel):
    paragraph_id: str
    quote_text: str = ""
    body: str
    category: str = "note"


class AnnotationAdviceRequest(BaseModel):
    question: str = "What should I consider about this note?"
    persona_id: str = "narrative_editor"


@app.get("/api/projects/{project_id}/scenes/{scene_number}/annotations")
def list_scene_annotations(project_id: str, scene_number: int):
    try:
        return get_project_db(project_id).list_annotations(scene_number)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scene not found.") from exc


@app.post("/api/projects/{project_id}/scenes/{scene_number}/annotations")
def create_scene_annotation(
    project_id: str, scene_number: int, req: AnnotationCreateRequest
):
    try:
        return get_project_db(project_id).create_annotation(
            scene_number,
            req.paragraph_id,
            req.quote_text,
            req.body,
            req.category,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scene not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/scenes/{scene_number}/annotations/{annotation_id}/resolve")
def resolve_scene_annotation(project_id: str, scene_number: int, annotation_id: str):
    try:
        return get_project_db(project_id).resolve_annotation(scene_number, annotation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Annotation not found.") from exc


@app.post("/api/projects/{project_id}/scenes/{scene_number}/annotations/{annotation_id}/advice")
def advise_on_annotation(
    project_id: str,
    scene_number: int,
    annotation_id: str,
    req: AnnotationAdviceRequest,
):
    db = get_project_db(project_id)
    scene = db.get_scene(scene_number)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found.")
    annotation = next(
        (item for item in db.list_annotations(scene_number) if item["annotation_id"] == annotation_id),
        None,
    )
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found.")
    question = req.question.strip() or "What should I consider about this note?"
    persona_name = req.persona_id.replace("_", " ").strip().title() or "Narrative Editor"
    prompt = f"""
You are {persona_name}, advising an author on a private manuscript note.
Answer the author's question about the note using the scene as context.
Do not rewrite the manuscript, invent facts, or treat the note as canon.
Return JSON only with this shape:
{{
  "response": "concise, specific editorial input",
  "suggestions": ["concrete suggestion", "concrete suggestion"]
}}
Keep the response useful and bounded. Suggestions may be an empty list when none are warranted.

AUTHOR QUESTION:
{question}

NOTE CATEGORY: {annotation["category"]}
NOTE ANCHOR:
{annotation["quote_text"]}

AUTHOR NOTE:
{annotation["body"]}

SCENE {scene_number} — {scene["title"]}:
{html_to_text(scene["content_html"])}
""".strip()

    model = os.getenv(
        "LITERARY_STUDIO_NOTE_MODEL",
        os.getenv("LITERARY_STUDIO_EVAL_MODEL", "gemini-3.1-pro-preview"),
    )
    try:
        ai_response = generate_text(prompt, model=model, temperature=0.2)
    except AIProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"AI note advice unavailable: {exc}") from exc

    raw_text = ai_response.text
    try:
        cleaned = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        result = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        result = {"response": raw_text, "suggestions": []}

    suggestions = result.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    advice = {
        "question": question,
        "response": str(result.get("response", raw_text)).strip(),
        "suggestions": [str(item).strip() for item in suggestions if str(item).strip()],
        "persona_id": req.persona_id,
        "generated_at": utc_now(),
        "model": ai_response.model,
        "provider": ai_response.provider,
    }
    try:
        saved = db.update_annotation_advice(scene_number, annotation_id, advice)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Annotation not found.") from exc
    return {
        "project_id": project_id,
        "scene_number": scene_number,
        "annotation_id": annotation_id,
        "ai_advice": saved["ai_advice"],
    }


class ReviewRequest(BaseModel):
    action: str
    # Accepted for client compatibility and ignored: edits go to the batch's pinned branch.
    target_branch: str | None = None
    # Accept into the Studio copy only, without patching the Markdown source.
    studio_only: bool = False


def _review_error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


@app.post("/api/projects/{project_id}/governance/proposals/{proposal_id}/review")
def review_proposal_endpoint(project_id: str, proposal_id: str, req: ReviewRequest):
    """Accept or reject a staged proposal from the Approval Desk.

    Accept: re-verifies this proposal's passage, patches the Markdown source on
    the batch's pinned branch (unless studio_only), then updates the Studio
    scene. If the database step fails the source file is restored. When the
    batch's last proposal is reviewed, its source changes become one commit.
    Errors carry {code, message}; code "writeback_blocked" means the author can
    retry with studio_only.
    """
    if req.action not in {"accept", "reject"}:
        raise HTTPException(status_code=400, detail="Action must be 'accept' or 'reject'.")
    db = get_project_db(project_id)
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    if proposal["status"] != "staged":
        raise _review_error(409, "already_reviewed", "Proposal has already been reviewed.")
    batch_id = proposal.get("batch_id")

    write = None
    writeback_status = None
    if req.action == "accept":
        if batch_id:
            report = check_approval_conflicts(db, batch_id, proposal_id)
            if not report["safe_to_apply"]:
                raise _review_error(
                    409, "stale", "This proposal is stale and needs re-preparation.", conflicts=report["conflicts"]
                )
        scene = db.get_scene(int(proposal["scene_number"]))
        if scene is None:
            raise _review_error(409, "scene_missing", "The proposal scene no longer exists.")
        if req.studio_only:
            writeback_status = "studio_only"
        else:
            try:
                write = write_back(db, proposal, scene)
            except GovernanceError as exc:
                raise _review_error(409, "writeback_blocked", str(exc), reason=exc.code, studio_only_available=True) from exc
            writeback_status = "source_and_studio"
    try:
        result = db.review_proposal(proposal_id, req.action, writeback_status=writeback_status)
    except (KeyError, ValueError) as exc:
        if write:
            restore(write)
        raise _review_error(409, "review_failed", str(exc)) from exc
    except Exception:
        if write:
            restore(write)
        raise
    if write and batch_id:
        record_write(db, batch_id, proposal_id, write)
    commit = finalize_batch_if_done(db, batch_id)

    if req.action == "reject":
        message = "Proposal rejected."
    elif write:
        message = f"Accepted and written to {write['path']}."
    else:
        message = "Accepted in the Studio copy only; the Markdown source was not changed."
    if commit and commit.get("committed") and not commit.get("already"):
        message += f" Batch committed as {commit['commit_sha'][:8]} on {commit.get('branch')}."
    elif commit and commit.get("error"):
        message += f" Batch commit failed: {commit['error']['message']}"
    return {
        "status": "success",
        "project_id": db.project_info()["project_id"],
        "proposal_id": proposal_id,
        "new_status": result["new_status"],
        "writeback_status": writeback_status,
        "source_file": write["path"] if write else None,
        "batch_commit": commit,
        "message": message,
    }
