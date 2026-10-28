import json
import hashlib
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Literal

from config import load_environment

load_environment()

# Ensure standard TeX binaries and Homebrew paths are in PATH on macOS
for _bin_dir in (
    "/Library/TeX/texbin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
):
    if os.path.isdir(_bin_dir) and _bin_dir not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{_bin_dir}:{os.environ.get('PATH', '')}"

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ai_provider import AIProviderUnavailable, generate_text
from approval_bridge import ApprovalBridgeError, check_approval_conflicts, prepare_for_approval
from compiler_engine import (
    DEFAULT_AUTHOR,
    FormatterUnavailable,
    build_full_book_scene_page_map,
    compile_shunn_pdf,
    list_format_profiles,
    render_markdown_export,
    select_export_scenes,
)
from google_docs_export import GoogleDocsExportUnavailable, export_markdown_to_docx
from governance import (
    GovernanceError,
    _git,
    commit_batch,
    current_source_state,
    finalize_batch_if_done,
    reconcile_failed_commit_batch,
    record_write,
    repo_root,
    restore,
    validate_candidate,
    write_back,
    source_worktree_status,
    source_worktree_diff,
)
from mechanical_review import MechanicalReviewError, review_replacements
from context_package import CONTEXT_OPTIONS, ContextPackageError, build_context_package
from desk_push import commit_scene_to_git, pull_desk_from_git, push_desk_to_git
from intake_audit import run_intake_audit
from manual_sync import build_manual_change_candidates
from manuscript_context import (
    ManuscriptContextError,
    build_interrogation_context,
    bounded_conversation_transcript,
    context_prompt,
    manuscript_index,
    supporting_context_prompt,
)
from persona_catalog import (
    PERSONA_PANELS,
    PERSONA_ROSTER,
    catalog_summary,
    get_panel,
    get_persona,
    public_persona,
)
from project_manager import ProjectManager, ProjectNotFound, live_source_state
from source_snapshot import SnapshotError, build_snapshot, write_snapshot
from source_repair import (
    SourceRepairError,
    list_source_repair_files,
    read_source_repair_file,
    recompile_source_repair_file,
)
from studio_db import (
    PROJECTS_ROOT,
    StudioDB,
    _markdown_scenes,
    html_to_text,
    load_semantic_style_registry,
    utc_now,
)


app = FastAPI(title="Literary Studio API", version="1.1.0")
project_manager = ProjectManager()
project_manager.ensure_default_project()


# String Theories' planning references are project support material, not
# manuscript prose. They are copied into that project's isolated SQLite store
# at startup so roleplay and evaluation calls remain project-scoped.
PROJECT_SUPPORT_SOURCES = {
    "string-theories": [
        {
            "document_key": "string-theories-revision-11-outline",
            "kind": "outline",
            "title": "String Theories — Revision 11 Outline",
            "source_path": os.getenv(
                "STRING_THEORIES_OUTLINE_PATH",
                "/Users/gabrielmcp/Downloads/StringTheories_Rev 11_Outline.md",
            ),
        },
        {
            "document_key": "string-theories-story-beat-cards",
            "kind": "story_beat_cards",
            "title": "String Theories — Story Beat Cards",
            "source_path": os.getenv(
                "STRING_THEORIES_BEAT_CARDS_PATH",
                "/Users/gabrielmcp/.gemini/antigravity/brain/7ec44454-a070-498c-b740-a1e9217bee01/scratch/StringTheories_story_beat_cards.json",
            ),
        },
    ]
}


def sync_project_support_documents() -> None:
    for project_id, documents in PROJECT_SUPPORT_SOURCES.items():
        try:
            db = project_manager.open(project_id)
        except ProjectNotFound:
            continue
        for document in documents:
            source_path = Path(document["source_path"])
            try:
                content = source_path.read_text(encoding="utf-8")
            except OSError:
                continue
            db.upsert_support_document(
                **document,
                source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                content=content,
            )


sync_project_support_documents()

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


@app.get("/api/projects/{project_id}/support-documents")
def list_support_documents(project_id: str):
    """Expose project-owned context references without returning their full contents."""
    db = get_project_db(project_id)
    return {
        "project_id": project_id,
        "documents": db.list_support_documents(include_content=False),
    }


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
    project = get_project_db(project_id).project_info()
    project.update(live_source_state(project))
    return project


class EditorSettingsRequest(BaseModel):
    font_family: str = "literary-serif"
    font_size: int = 18
    line_height: float = 1.7
    paragraph_spacing: str = "comfortable"
    style_overrides: dict[str, dict[str, object]] = Field(default_factory=dict)


@app.get("/api/semantic-styles")
def get_semantic_styles():
    """Return the canonical style taxonomy shared by editor and formatter."""
    return load_semantic_style_registry()


@app.get("/api/projects/{project_id}/editor-settings")
def get_editor_settings(project_id: str):
    return get_project_db(project_id).get_editor_settings()


@app.put("/api/projects/{project_id}/editor-settings")
def update_editor_settings(project_id: str, req: EditorSettingsRequest):
    return get_project_db(project_id).update_editor_settings(req.model_dump())


class SourceSnapshotRequest(BaseModel):
    source_root: str
    source_subpath: str | None = None
    replace_existing: bool = False


class SourceFileActionRequest(BaseModel):
    path: str


class SourceRepairRequest(BaseModel):
    path: str
    expected_sha256: str
    content_html: str
    frontmatter: str = ""


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


class ScenePassRequest(BaseModel):
    pass_type: Literal["mechanical", "slop_tell_habit"]


class ManualChangesRequest(BaseModel):
    scene_numbers: List[int] = Field(default_factory=list)


class DeskPushRequest(BaseModel):
    # Full desk synchronization is an author-controlled write and commit.
    confirm: bool = False
    # Whole-scene deletion/large reductions are blocked unless the author
    # explicitly opts into them after reviewing the exact desk/source counts.
    allow_unsafe: bool = False
    # Explicit author override for a pre-existing dirty worktree. This still
    # commits only files changed by the saved Studio desk.
    override_dirty: bool = False


class GitPullRequest(BaseModel):
    pull_remote: bool = False
    override_dirty: bool = False


class DirectCommitRequest(BaseModel):
    message: str | None = None
    override_dirty: bool = False


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


@app.post("/api/projects/{project_id}/context-package/google-doc")
def export_context_package_google_doc(project_id: str, req: ContextPackageRequest):
    """Download the selected context package as a Google Docs-compatible DOCX.

    This is a generated, project-scoped artifact. It never writes manuscript
    Markdown, Studio scene content, or Git state.
    """
    db = get_project_db(project_id)
    unknown_options = sorted(set(req.include) - set(CONTEXT_OPTIONS))
    if unknown_options:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown context options: {', '.join(unknown_options)}",
        )
    try:
        package = build_context_package(
            db,
            req.chapter_start,
            req.chapter_end,
            req.include,
        )
    except ContextPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    canonical_id = db.project_info()["project_id"]
    output_filename = (
        f"{canonical_id}_chapters_{req.chapter_start}-{req.chapter_end}_google-doc.docx"
    )
    output_path = PROJECTS_ROOT / canonical_id / "build_artifacts" / output_filename
    try:
        project = db.project_info()
        source_state = live_source_state(project) or current_source_state(db)
        export_provenance = {
            "author": DEFAULT_AUTHOR,
            "generated_at": utc_now(),
            "branch": source_state.get("branch"),
            "commit_sha": source_state.get("commit_sha"),
        }
        export_markdown_to_docx(
            package["markdown"],
            output_path,
            package["title"],
            provenance=export_provenance,
            page_start=1,
        )
    except GoogleDocsExportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return FileResponse(
        path=output_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=output_filename,
        headers={"X-Literary-Studio-Package-SHA256": package["package_sha256"]},
    )


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


@app.get("/api/projects/{project_id}/source-repair/files")
def source_repair_files(project_id: str):
    """List Markdown files available to the author-controlled repair surface."""
    try:
        return {"project_id": project_id, "files": list_source_repair_files(get_project_db(project_id))}
    except SourceRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/source-repair/file")
def source_repair_file(project_id: str, path: str):
    """Return one complete Markdown file as an editor-ready semantic document."""
    try:
        return read_source_repair_file(get_project_db(project_id), path)
    except SourceRepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to read source file: {exc}") from exc


@app.post("/api/projects/{project_id}/source-repair/recompile")
def recompile_source_repair(project_id: str, req: SourceRepairRequest):
    """Write and re-index exactly one Markdown source file.

    The hash pin prevents a repair modal from overwriting edits made after the
    file was opened. The write remains a normal dirty working-tree change; no
    Git commit is performed here.
    """
    db = get_project_db(project_id)
    try:
        result = recompile_source_repair_file(
            db,
            relative_path=req.path,
            expected_sha256=req.expected_sha256,
            content_html=req.content_html,
            frontmatter=req.frontmatter,
        )
        project = db.project_info()
        snapshot = build_snapshot(
            project["project_id"],
            project["title"],
            project["source_root"],
            project.get("source_subpath"),
        )
        output_dir = PROJECTS_ROOT / project["project_id"] / "source"
        snapshot_path, manifest_path = write_snapshot(snapshot, output_dir)
        updated_project = db.update_source_metadata(
            source_root=snapshot["manifest"]["source_root"],
            source_subpath=snapshot["manifest"]["source_subpath"],
            branch=snapshot["manifest"]["branch"],
            commit_sha=snapshot["manifest"]["commit_sha"],
            source_digest=snapshot["manifest"]["source_digest"],
            snapshot_path=str(snapshot_path),
            manifest_path=str(manifest_path),
            working_tree_dirty=snapshot["manifest"]["working_tree_dirty"],
        )
        graph = db.rebuild_story_graph()
        audit = run_intake_audit(db, source_label=f"Source repair · {req.path}", run_ai=False)
        result.update({
            "project": updated_project,
            "graph": graph,
            "intake_audit": audit,
            "commit_required": True,
        })
        return result
    except SourceRepairError as exc:
        status = 409 if exc.code == "source_changed" else 400
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc
    except (SnapshotError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"Unable to refresh source index: {exc}") from exc


@app.post("/api/projects/{project_id}/source-provenance/refresh")
def refresh_source_provenance(project_id: str):
    """Refresh the generated source pin without replacing imported Studio scenes."""
    db = get_project_db(project_id)
    project = db.project_info()
    source_root = project.get("source_root")
    source_subpath = project.get("source_subpath")
    if not source_root:
        raise HTTPException(status_code=400, detail="This project has no configured manuscript source checkout.")
    try:
        snapshot = build_snapshot(
            project["project_id"],
            project["title"],
            source_root,
            source_subpath,
        )
        output_dir = PROJECTS_ROOT / project["project_id"] / "source"
        snapshot_path, manifest_path = write_snapshot(snapshot, output_dir)
        updated_project = db.update_source_metadata(
            source_root=snapshot["manifest"]["source_root"],
            source_subpath=snapshot["manifest"]["source_subpath"],
            branch=snapshot["manifest"]["branch"],
            commit_sha=snapshot["manifest"]["commit_sha"],
            source_digest=snapshot["manifest"]["source_digest"],
            snapshot_path=str(snapshot_path),
            manifest_path=str(manifest_path),
            working_tree_dirty=snapshot["manifest"]["working_tree_dirty"],
        )
    except SnapshotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to write source provenance: {exc}") from exc
    return {
        "status": "provenance_refreshed",
        "project": updated_project,
        "manifest": snapshot["manifest"],
        "scenes_preserved": True,
        "scene_reimport_performed": False,
    }


@app.get("/api/projects/{project_id}/source-provenance/status")
def get_source_provenance_status(project_id: str):
    """Show exactly which files make the configured source checkout dirty."""
    db = get_project_db(project_id)
    try:
        return source_worktree_status(db)
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/source-provenance/diff")
def get_source_provenance_diff(project_id: str, path: str):
    """Return one dirty file's diff without changing the checkout."""
    try:
        return source_worktree_diff(get_project_db(project_id), path)
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/source-provenance/open")
def open_source_provenance_file(project_id: str, req: SourceFileActionRequest):
    """Open one dirty source file in the local Antigravity IDE."""
    db = get_project_db(project_id)
    status = source_worktree_status(db)
    if not any(item["path"] == req.path for item in status["files"]):
        raise HTTPException(status_code=400, detail="That file is not currently dirty in the configured source checkout.")
    target = (Path(status["source_root"]) / req.path).resolve()
    root = Path(status["source_root"])
    if target == root or root not in target.parents or not target.is_file():
        raise HTTPException(status_code=400, detail="That source path is not a readable file in the configured checkout.")
    try:
        subprocess.Popen(["open", "-a", "Antigravity IDE", "--", str(target)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to open the file in Antigravity IDE: {exc}") from exc
    return {"status": "opened", "path": req.path}


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


class SceneCreateRequest(BaseModel):
    title: str = "New Scene"
    content_html: str = "<p></p>"
    chapter_number: int | None = None
    chapter_title: str | None = None
    scene_number_in_chapter: int | None = None
    part_number: int | None = None
    part_title: str | None = None
    source_path: str | None = None
    is_scratch: bool = False


class SceneUpdate(BaseModel):
    content_html: str
    summary: str = "Scene saved"


class SceneMetadataUpdate(BaseModel):
    """Editable outline taxonomy; deliberately separate from prose updates."""

    title: str
    chapter_number: int | None = None
    chapter_title: str | None = None
    scene_number_in_chapter: int | None = None
    part_number: int | None = None
    part_title: str | None = None


class RestoreRevisionRequest(BaseModel):
    revision_id: int


class DraftSessionCreateRequest(BaseModel):
    title: str = "Draft session"
    chapter_number: int | None = None
    scene_number: int | None = None
    mode: Literal["tethered", "piece_together"] = "tethered"


class DraftSessionUpdateRequest(BaseModel):
    mode: Literal["tethered", "piece_together"] | None = None
    tether_enabled: bool | None = None
    base_document_id: str | None = None


class DraftDocumentUpdateRequest(BaseModel):
    content_html: str


class DraftSessionResolveRequest(BaseModel):
    action: Literal["promote", "keep_canon"]


@app.post("/api/projects/{project_id}/draft-sessions")
def create_draft_session(project_id: str, req: DraftSessionCreateRequest):
    try:
        return get_project_db(project_id).create_draft_session(
            title=req.title,
            chapter_number=req.chapter_number,
            scene_number=req.scene_number,
            mode=req.mode,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Chapter or scene not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/draft-sessions")
def list_draft_sessions(project_id: str):
    return get_project_db(project_id).list_draft_sessions()


@app.get("/api/projects/{project_id}/draft-sessions/{session_id}")
def get_draft_session(project_id: str, session_id: str):
    session = get_project_db(project_id).get_draft_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Draft session not found.")
    return session


@app.patch("/api/projects/{project_id}/draft-sessions/{session_id}")
def update_draft_session(
    project_id: str, session_id: str, req: DraftSessionUpdateRequest
):
    try:
        return get_project_db(project_id).update_draft_session(
            session_id,
            mode=req.mode,
            tether_enabled=req.tether_enabled,
            base_document_id=req.base_document_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft session not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects/{project_id}/draft-sessions/{session_id}")
def delete_draft_session(project_id: str, session_id: str):
    try:
        get_project_db(project_id).delete_draft_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft session not found.") from exc
    return {"status": "deleted", "session_id": session_id}


@app.post("/api/projects/{project_id}/draft-sessions/{session_id}/resolve")
def resolve_draft_session(
    project_id: str, session_id: str, req: DraftSessionResolveRequest
):
    try:
        return get_project_db(project_id).resolve_draft_session(session_id, req.action)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft session not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/projects/{project_id}/draft-sessions/{session_id}/documents/{document_id}")
def update_draft_document(
    project_id: str,
    session_id: str,
    document_id: str,
    req: DraftDocumentUpdateRequest,
):
    try:
        return get_project_db(project_id).update_draft_document(
            session_id, document_id, req.content_html
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Draft document not found.") from exc


@app.post("/api/projects/{project_id}/scenes")
def create_scene(project_id: str, req: SceneCreateRequest):
    try:
        db = get_project_db(project_id)
        return db.create_scene(
            title=req.title,
            content_html=req.content_html,
            chapter_number=req.chapter_number,
            chapter_title=req.chapter_title,
            scene_number_in_chapter=req.scene_number_in_chapter,
            part_number=req.part_number,
            part_title=req.part_title,
            source_path=req.source_path,
            is_scratch=req.is_scratch,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/projects/{project_id}/scenes/{scene_number}")
def update_scene(project_id: str, scene_number: int, req: SceneUpdate):
    try:
        return get_project_db(project_id).update_scene(scene_number, req.content_html, req.summary)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scene not found.") from exc


@app.put("/api/projects/{project_id}/scenes/{scene_number}/metadata")
def update_scene_metadata(project_id: str, scene_number: int, req: SceneMetadataUpdate):
    """Persist outline labels without changing prose, revisions, or source files."""
    try:
        return get_project_db(project_id).update_scene_metadata(
            scene_number,
            title=req.title,
            chapter_number=req.chapter_number,
            chapter_title=req.chapter_title,
            scene_number_in_chapter=req.scene_number_in_chapter,
            part_number=req.part_number,
            part_title=req.part_title,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scene not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    focus_scope: Literal["scene", "chapter", "selection"] = "scene"
    focus_instruction: str = ""
    focus_text: str = ""


class PanelEvaluationRequest(BaseModel):
    scene_number: int
    scene_text: str = ""
    persona_ids: List[str] | None = None
    panel_id: str | None = None
    focus_scope: Literal["scene", "chapter", "selection"] = "scene"
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


def _evaluation_material(
    db: StudioDB,
    scene: dict[str, object],
    *,
    focus_scope: str,
    current_screen_text: str,
) -> tuple[str, dict[str, object], str, int | None]:
    """Resolve the exact prose and retrieval context for an evaluation."""
    target_scope = "chapter" if focus_scope == "chapter" else "scene"
    context = build_interrogation_context(
        db,
        current_scene_number=int(scene["scene_number"]),
        current_screen_text=current_screen_text,
        target_scope=target_scope,
    )
    if target_scope == "chapter":
        target = context.get("target") or {}
        chapter_number = target.get("chapter_number")
        chapter_label = target.get("chapter_title") or f"Chapter {chapter_number}"
        chapter_scenes = target.get("scenes") or []
        material = "\n\n".join(
            f"[Chapter {item.get('chapter_number')}, Scene {item.get('scene_number_in_chapter') or item.get('scene_number')}: {item.get('title', '')}]\n"
            f"{item.get('text', '')}"
            for item in chapter_scenes
        )
        return material, context, f"Chapter {chapter_number}: {chapter_label}", int(chapter_number) if chapter_number is not None else None

    scene_number = int(scene["scene_number"])
    return current_screen_text, context, f"Scene {scene_number}: {scene.get('title', '')}", scene.get("chapter_number") if isinstance(scene.get("chapter_number"), int) else None


def _evaluate_persona(
    scene_number: int,
    scene_text: str,
    persona: dict,
    *,
    focus_scope: str = "scene",
    focus_instruction: str = "",
    focus_text: str = "",
    supporting_context: str = "",
    scope_label: str = "",
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
    support_block = supporting_context.strip() or (
        "No project support documents are registered for this manuscript."
    )
    target_label = scope_label or f"Scene {scene_number}"
    prompt = f"""
You are one independent editorial simulator in a multi-persona literary panel.
Do not imitate or claim to be a real person. Stay inside this role's dossier.
Evaluate {target_label} only, applying the author's focus below. If the scope is
'selection', constrain close-reading and quote anchors to the selected passage while
using the rest of the scene only for necessary context. If the scope is 'chapter', you have the complete
chapter below; analyze its scenes as a connected chapter without merely recapping them. Do not rewrite the manuscript and do not invent
facts, quotations, plot events, or market data. Every annotation anchor must be an
exact short quotation copied from the supplied manuscript text, or the annotation list must be empty.

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

{support_block}

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

{target_label.upper()}
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
        scene_text = html_to_text(scene["content_html"])
        evaluation_text, context, scope_label, focus_chapter_number = _evaluation_material(
            db,
            scene,
            focus_scope=req.focus_scope,
            current_screen_text=scene_text,
        )
        support_context = supporting_context_prompt(context)
        evaluation = {
            "project_id": project_id,
            "scene_number": req.scene_number,
            "focus_scope": req.focus_scope,
            "focus_instruction": req.focus_instruction,
            "focus_text_supplied": bool(req.focus_text.strip()),
            "focus_text": req.focus_text[:20000],
            "focus_chapter_number": focus_chapter_number,
            "support_context_documents": [
                {
                    "document_key": document["document_key"],
                    "title": document["title"],
                    "kind": document["kind"],
                    "source_sha256": document["source_sha256"],
                    "included_chapters": document.get("included_chapters", []),
                }
                for document in context.get("supporting_documents", [])
            ],
            **_evaluate_persona(
                req.scene_number,
                evaluation_text,
                persona,
                focus_scope=req.focus_scope,
                focus_instruction=req.focus_instruction,
                focus_text=req.focus_text,
                supporting_context=support_context,
                scope_label=scope_label,
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

    try:
        current_scene_text = html_to_text(scene["content_html"])
        evaluation_text, context, scope_label, focus_chapter_number = _evaluation_material(
            db,
            scene,
            focus_scope=req.focus_scope,
            current_screen_text=current_scene_text,
        )
    except ManuscriptContextError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    support_context = supporting_context_prompt(context)
    support_context_documents = [
        {
            "document_key": document["document_key"],
            "title": document["title"],
            "kind": document["kind"],
            "source_sha256": document["source_sha256"],
            "included_chapters": document.get("included_chapters", []),
        }
        for document in context.get("supporting_documents", [])
    ]
    evaluations: dict[str, dict] = {}
    failures: dict[str, str] = {}
    max_workers = max(1, min(len(personas), int(os.getenv("LITERARY_STUDIO_PANEL_WORKERS", "8"))))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
                executor.submit(
                _evaluate_persona,
                req.scene_number,
                evaluation_text,
                persona,
                focus_scope=req.focus_scope,
                focus_instruction=req.focus_instruction,
                focus_text=req.focus_text,
                supporting_context=support_context,
                scope_label=scope_label,
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
            "focus_text": req.focus_text[:20000],
            "focus_chapter_number": focus_chapter_number,
            "support_context_documents": support_context_documents,
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
        "focus_chapter_number": focus_chapter_number,
        "independent": True,
        "requested": len(personas),
        "completed": len(ordered_results),
        "failed": failures,
        "support_context_documents": support_context_documents,
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
    conversation_channel: Literal["roleplay", "analysis"] = "roleplay"
    current_screen_text: str = ""
    target_scope: Literal["screen", "scene", "chapter"] = "screen"
    target_chapter_number: int | None = None
    target_scene_number_in_chapter: int | None = None
    target_global_scene_number: int | None = None
    preferred_provider: str | None = None


ROLEPLAY_RESPONSE_CONTRACT = """
ROLEPLAY RESPONSE CONTRACT — THIS OVERRIDES THE TEMPTATION TO SUMMARIZE:
- Treat the manuscript context as private evidence for your reasoning. Do not
  replay, chunk, or retell the supplied chapter/scene in your answer.
- Unless the author explicitly asks for a plot summary, use no more than two
  brief sentences of recap. Spend the response on interpretation: what the
  prose is doing, what the character or relationship reveals, where the
  tension/subtext/voice is working or failing, and what the author could test.
- Do not produce a chapter walk-through, scene-by-scene chronology, premise
  restatement, or context dump. Do not reproduce the context headings.
- Answer the author's actual question first. Stay in the selected persona's
  sensibility, but make the editorial reasoning visible rather than performing
  a generic plot synopsis.
- Default shape: a direct persona reaction, two or three evidence-grounded
  interpretive observations, and one focused question or next move. Keep it
  concise (normally 250–650 words) unless the author explicitly asks for more.
- Quote only short anchors when useful; never paste blocks of manuscript prose.
""".strip()


def _roleplay_response_needs_rewrite(response: str) -> bool:
    """Detect an obvious plot dump without pretending to judge literary quality."""
    text = str(response or "").strip()
    words = re.findall(r"\b[\w'’-]+\b", text)
    if len(words) < 220:
        return False
    if re.search(
        r"(?:^|\n)\s*(?:chapter|scene|plot)\s+(?:summary|overview|recap)\s*:?",
        text,
    ):
        return True
    chronological_markers = len(
        re.findall(
            r"\b(?:then|later|after that|before that|next|eventually|finally|meanwhile|the scene begins|the chapter opens)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    # A long answer with repeated timeline transitions is the characteristic
    # failure mode reported in the roleplay room; shorter analytical answers
    # are left untouched.
    return len(words) >= 320 and chronological_markers >= max(4, sentences // 6)


def _rewrite_roleplay_response(
    response: str,
    req: InterrogationRequest,
    persona_name: str,
) -> str:
    """Ask the same provider for a focused rewrite only after a clear drift."""
    rewrite_prompt = f"""
You are the final response editor for a Literary Studio roleplay answer from
the simulated persona {persona_name}. The answer below drifted into a plot
recap. Rewrite it; do not add new facts and do not invent quotations.

{ROLEPLAY_RESPONSE_CONTRACT}

AUTHOR'S ACTUAL QUESTION:
{req.user_message}

DRIFTING DRAFT:
{response}

Return only the revised answer. Do not mention this rewrite instruction.
""".strip()
    try:
        revised = generate_text(
            rewrite_prompt,
            model=os.getenv("LITERARY_STUDIO_CHAT_MODEL", "gemini-3.1-pro-preview"),
            temperature=0.35,
            preferred_provider=req.preferred_provider,
        ).text.strip()
        return revised or response
    except AIProviderUnavailable:
        return response


@app.get("/api/projects/{project_id}/agent-conversations/{channel}/{agent_id}")
def get_agent_conversation(project_id: str, channel: Literal["roleplay", "analysis"], agent_id: str):
    """Return the durable conversation thread for one project agent and surface."""
    db = get_project_db(project_id)
    try:
        return db.get_agent_conversation(agent_id, channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/agent-conversations/{channel}")
def list_agent_conversations(project_id: str, channel: Literal["roleplay", "analysis"]):
    """List saved project-scoped threads without creating empty conversations."""
    db = get_project_db(project_id)
    try:
        return {
            "project_id": project_id,
            "channel": channel,
            "conversations": db.list_agent_conversations(channel),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        "the real person reviewed the manuscript.\n\n"
        "MANDATORY EDITORIAL DIRECTIVES:\n"
        "1. You are actively reading the author's manuscript prose provided in the context below.\n"
        "2. Base all your reactions, thoughts, dialogue, critiques, and roleplay on the ACTUAL MANUSCRIPT TEXT — "
        "its prose style, narrative voice, character actions, dialogue, pacing, and scene dynamics.\n"
        "3. Do NOT cite, summarize, or lean on the story graph entity index or metadata in place of reading the chapter prose. "
        "The story graph is only an auxiliary cross-reference for character continuity.\n"
        "4. Stay firmly inside your persona profile's tastes, editorial sensibilities, and voice.\n"
        "5. Do not invent facts, quotes, or plot points that are not in the supplied manuscript.\n\n"
        f"{ROLEPLAY_RESPONSE_CONTRACT}\n\n"
        f"SOURCE DOSSIER:\n{persona.get('profile_context', '')}"
    )
    try:
        conversation = db.get_agent_conversation(req.persona_id, req.conversation_channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stored_messages = conversation.get("messages", [])
    transcript_messages = stored_messages or [message.model_dump() for message in req.conversation_history]
    transcript = bounded_conversation_transcript(transcript_messages)
    prompt = f"{system_instruction}\n\n{context_prompt(context)}\n\nCONVERSATION (recent roleplay memory only):\n{transcript}\nUSER: {req.user_message}".strip()
    try:
        ai_response = generate_text(
            prompt,
            model=os.getenv("LITERARY_STUDIO_CHAT_MODEL", "gemini-3.1-pro-preview"),
            temperature=0.7,
            preferred_provider=req.preferred_provider,
        )
    except AIProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"AI chat unavailable: {exc}") from exc

    response_message = ai_response.text or "..."
    if req.conversation_channel == "roleplay" and _roleplay_response_needs_rewrite(response_message):
        response_message = _rewrite_roleplay_response(response_message, req, persona["name"])
    context_label = (
        f"Context: {context['target_label']}"
        if context.get("target_label")
        else "Context: current editor screen"
    )
    card_documents = [
        document for document in context.get("supporting_documents", [])
        if document.get("kind") == "story_beat_cards" and document.get("included_text")
    ]
    if card_documents:
        context_label += " + story beat cards"
    db.append_agent_conversation_message(
        conversation["conversation_id"],
        "user",
        req.user_message,
        {
            "scene_number": req.scene_number,
            "target_scope": req.target_scope,
            "context_label": context_label,
            "source_request": req.user_message if req.conversation_channel == "roleplay" else None,
        },
    )
    db.append_agent_conversation_message(
        conversation["conversation_id"],
        "model",
        response_message,
        {
            "scene_number": req.scene_number,
            "target_scope": req.target_scope,
            "context_label": context_label,
            "source_request": req.user_message if req.conversation_channel == "roleplay" else None,
        },
    )
    return {
        "project_id": project_id,
        "response_message": response_message,
        "persona_id": req.persona_id,
        "conversation_id": conversation["conversation_id"],
        "conversation_channel": req.conversation_channel,
        "provider": ai_response.provider,
        "model": ai_response.model,
        "context": {
            "current_scene_number": context["current_screen"]["scene_number"],
            "target_label": context["target_label"],
            "target_scope": context["target_scope"],
            "manuscript_scene_count": len(context["manuscript_index"]),
            "current_screen_source": context["current_screen"]["source"],
            "context_label": context_label,
            "support_context_documents": [
                {
                    "document_key": document["document_key"],
                    "title": document["title"],
                    "kind": document["kind"],
                    "source_sha256": document["source_sha256"],
                    "included_chapters": document.get("included_chapters", []),
                }
                for document in context.get("supporting_documents", [])
            ],
        },
    }


class CompilerRequest(BaseModel):
    title: str = "Manuscript"
    author: str = DEFAULT_AUTHOR
    profile_id: str = "manuscript_submission"
    output_format: Literal["pdf", "markdown", "docx"] = "pdf"
    scene_numbers: List[int] = Field(default_factory=list)
    chapter_numbers: List[int] = Field(default_factory=list)
    part_numbers: List[int] = Field(default_factory=list)
    selection_active: bool = False


def _compiler_scene_payload(db: StudioDB, request: CompilerRequest) -> list[dict[str, object]]:
    scenes = [
        {
            "scene_number": scene["scene_number"],
            "title": scene["title"],
            "content_html": scene["content_html"],
            "chapter_number": scene.get("chapter_number"),
            "chapter_title": scene.get("chapter_title"),
            "scene_number_in_chapter": scene.get("scene_number_in_chapter"),
            "part_number": scene.get("part_number"),
            "part_title": scene.get("part_title"),
            "source_path": scene.get("source_path"),
        }
        for scene in db.list_scenes()
    ]
    selected = [
        dict(scene)
        for scene in select_export_scenes(
            scenes,
            request.scene_numbers,
            request.chapter_numbers,
            request.part_numbers,
        )
    ]
    if request.selection_active and not any((request.scene_numbers, request.chapter_numbers, request.part_numbers)):
        return []
    return selected


def _compiler_provenance(db: StudioDB, author: str) -> dict[str, str | None]:
    project = db.project_info()
    source_state = live_source_state(project) or current_source_state(db)
    return {
        "author": author.strip() or DEFAULT_AUTHOR,
        "generated_at": utc_now(),
        "branch": source_state.get("branch"),
        "commit_sha": source_state.get("commit_sha"),
    }


def _compiler_page_start(
    db: StudioDB,
    request: CompilerRequest,
    selected_scenes: list[dict[str, object]],
    output_dir: Path,
    provenance: dict[str, str | None],
) -> int:
    """Resolve a selected reader's first page against the full manuscript.

    The map is generated from the same authoritative formatter/profile and
    current Git snapshot as the requested export. For a full-book export the
    manuscript naturally starts at page 1. Scene/chapter/part readers begin
    at the page on which their first selected scene begins in that book.
    """
    if not any((request.scene_numbers, request.chapter_numbers, request.part_numbers)):
        return 1
    all_scenes = _compiler_scene_payload(db, CompilerRequest())
    page_map = build_full_book_scene_page_map(
        all_scenes,
        request.title,
        request.author,
        request.profile_id,
        output_dir / ".typeset" / "page-map" / request.profile_id,
        provenance,
    )
    starts = []
    for scene in selected_scenes:
        try:
            scene_number = int(scene.get("scene_number"))
        except (TypeError, ValueError):
            continue
        if scene_number in page_map:
            starts.append(page_map[scene_number])
    return min(starts) if starts else 1


def _compiler_artifact(db: StudioDB, request: CompilerRequest) -> tuple[Path, str, str]:
    """Create one selected compiler artifact and return path, filename, media type."""
    canonical_id = db.project_info()["project_id"]
    output_dir = PROJECTS_ROOT / canonical_id / "build_artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    scope = "full-manuscript" if not any((request.scene_numbers, request.chapter_numbers, request.part_numbers)) else "selection"
    base_name = f"{canonical_id}_{scope}"
    scenes = _compiler_scene_payload(db, request)
    if not scenes:
        raise ValueError("No manuscript scenes match the selected export scope.")
    provenance = _compiler_provenance(db, request.author)
    page_start = (
        _compiler_page_start(db, request, scenes, output_dir, provenance)
        if request.output_format in {"pdf", "docx"}
        else 1
    )

    if request.output_format == "pdf":
        try:
            profiles = list_format_profiles()
        except FormatterUnavailable:
            raise
        profile_ids = {profile["id"] for profile in profiles}
        if request.profile_id not in profile_ids:
            raise ValueError(f"Unknown formatter profile: {request.profile_id}")
        filename = f"{base_name}_{request.profile_id}.pdf"
        output_path = output_dir / filename
        compile_shunn_pdf(
            scenes,
            output_path,
            request.title,
            request.author,
            request.profile_id,
            provenance=provenance,
            page_start=page_start,
        )
        return output_path, filename, "application/pdf"

    markdown = render_markdown_export(scenes, request.title, request.author, provenance=provenance)
    if request.output_format == "markdown":
        filename = f"{base_name}.md"
        output_path = output_dir / filename
        output_path.write_text(markdown, encoding="utf-8")
        return output_path, filename, "text/markdown; charset=utf-8"

    filename = f"{base_name}.docx"
    output_path = output_dir / filename
    export_markdown_to_docx(
        markdown,
        output_path,
        request.title,
        provenance=provenance,
        page_start=page_start,
    )
    return output_path, filename, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@app.get("/api/compiler/profiles")
def get_compiler_profiles():
    """Return the real format profiles provided by Fiction Git Studio."""
    try:
        return list_format_profiles()
    except FormatterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/compiler/export")
def export_compiler_artifact(project_id: str, req: CompilerRequest | None = None):
    try:
        output_path, filename, media_type = _compiler_artifact(get_project_db(project_id), req or CompilerRequest())
    except FormatterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GoogleDocsExportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FileResponse(path=output_path, media_type=media_type, filename=filename)


@app.post("/api/projects/{project_id}/compiler/export-shunn")
def export_shunn_pdf(project_id: str, req: CompilerRequest | None = None):
    request = req or CompilerRequest()
    request.output_format = "pdf"
    return export_compiler_artifact(project_id, request)


@app.post("/api/projects/{project_id}/compiler/airdrop")
def airdrop_compiler_artifact(project_id: str, req: CompilerRequest | None = None):
    if os.uname().sysname != "Darwin":
        raise HTTPException(status_code=501, detail="AirDrop handoff is available only on macOS.")
    try:
        output_path, filename, _ = _compiler_artifact(get_project_db(project_id), req or CompilerRequest())
        subprocess.run(["open", "-R", str(output_path)], check=True, capture_output=True, text=True)
    except FormatterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GoogleDocsExportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not open the export for AirDrop: {exc}") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "status": "ready",
        "filename": filename,
        "path": str(output_path),
        "message": "Finder opened with the export selected. Choose Share → AirDrop to send it.",
    }


@app.get("/api/projects/{project_id}/governance/proposals")
def get_staged_proposals(project_id: str):
    return get_project_db(project_id).staged_proposals()


@app.get("/api/projects/{project_id}/manual-changes")
def get_manual_changes(project_id: str):
    """Compare saved Studio scenes with their exact Markdown source boundaries."""
    db = get_project_db(project_id)
    candidates = build_manual_change_candidates(db)
    project = db.project_info()
    existing_manual = {
        (
            proposal["scene_number"],
            proposal["original_verbatim_text"],
            proposal["proposed_replacement_text"],
        )
        for proposal in db.staged_proposals()
        if proposal.get("change_type") == "manual_editor_sync"
    }
    available_count = 0
    blocked: list[dict[str, object]] = []
    studio_only_count = 0
    for candidate in candidates:
        if candidate.get("safety_errors"):
            blocked.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "kind": "unsafe_scene_boundary",
                "reason": " ".join(candidate["safety_errors"]),
            })
            continue

        # An attached Git checkout is the canonical source. A saved diff whose
        # only baseline is the intake copy (usually a project-local scratch
        # file) cannot be presented as Git-ready: acceptance has nowhere safe
        # to write it. Source-less reintakes remain eligible as Studio-only
        # proposals and are intentionally not classified as blocked here.
        if project.get("source_root") and candidate.get("source_mode") == "import_baseline":
            studio_only_count += len(candidate.get("hunks", [])) or 1
            blocked.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "kind": "studio_only",
                "reason": "This saved edit is based on an intake baseline and has no linked Markdown file in the configured Git checkout.",
            })
            continue

        for hunk in candidate.get("hunks", []):
            if (candidate["scene_number"], hunk["original"], hunk["replacement"]) not in existing_manual:
                available_count += 1
    return {
        "project_id": project_id,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "available_count": available_count,
        "blocked_count": len(blocked),
        "studio_only_count": studio_only_count,
        "blocked": blocked,
    }


@app.post("/api/projects/{project_id}/manual-changes/prepare")
def prepare_manual_changes(project_id: str, req: ManualChangesRequest):
    """Stage saved editor differences for the normal Approval Desk workflow."""
    db = get_project_db(project_id)
    candidates = build_manual_change_candidates(db, req.scene_numbers or None)
    if not candidates:
        return {
            "project_id": project_id,
            "status": "no_changes",
            "candidates": [],
            "proposals": [],
        }

    existing_manual = {
        (
            proposal["scene_number"],
            proposal["original_verbatim_text"],
            proposal["proposed_replacement_text"],
        )
        for proposal in db.staged_proposals()
        if proposal.get("change_type") == "manual_editor_sync"
    }
    baseline = current_source_state(db)
    batch = db.create_approval_batch(
        "Author manual editor changes",
        ["manual_editor_sync"],
        baseline,
    )
    staged: list[dict] = []
    skipped: list[dict] = []
    project = db.project_info()
    candidates_for_mechanical_review: list[dict] = []
    for candidate in candidates:
        if candidate.get("safety_errors"):
            skipped.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "operation": "unsafe_scene_diff",
                "errors": list(candidate["safety_errors"]),
            })
            continue
        if project.get("source_root") and candidate.get("source_mode") == "import_baseline":
            skipped.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "operation": "studio_only",
                "errors": [
                    "This saved edit is based on an intake baseline and has no linked Markdown file in the configured Git checkout. Link or import the material into the current source checkout before preparing it for Git review."
                ],
            })
            continue
        scene = db.get_scene(candidate["scene_number"])
        if scene is None:
            continue
        for hunk in candidate["hunks"]:
            candidates_for_mechanical_review.append({
                "id": f"manual-{len(candidates_for_mechanical_review)}",
                "candidate": candidate,
                "scene": scene,
                "hunk": hunk,
            })

    mechanical: dict = {"provider": None, "model": None, "fallback_reasons": [], "items": {}}
    if candidates_for_mechanical_review:
        # These replacements are the author's already-saved editor text. Do
        # not send it through an AI copy editor: that would mutate the exact
        # change the author is asking to review and can block the desk on a
        # provider timeout. AI-generated proposals still use the mechanical
        # gate in the normal approval-batch path.
        mechanical = {
            "provider": "author-editor",
            "model": None,
            "fallback_reasons": [],
            "items": {
                item["id"]: {
                    "replacement_text": item["hunk"]["replacement"],
                    "issues": [],
                    "changed": False,
                }
                for item in candidates_for_mechanical_review
            },
        }

    for item in candidates_for_mechanical_review:
        candidate = item["candidate"]
        scene = item["scene"]
        hunk = item["hunk"]
        reviewed = mechanical["items"][item["id"]]
        replacement = reviewed["replacement_text"]
        corrected_key = (candidate["scene_number"], hunk["original"], replacement)
        if corrected_key in existing_manual:
            skipped.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "operation": hunk["operation"],
                "errors": ["This mechanically corrected editor change is already staged in the Approval Desk."],
            })
            continue
        if not replacement.strip() or hunk["original"] == replacement:
            skipped.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "operation": hunk["operation"],
                "errors": ["The mechanical pass produced an empty or unchanged text span."],
            })
            continue
        validation = validate_candidate(
            project,
            scene,
            hunk["original"],
            replacement,
            allow_multiline=True,
        )
        validation["mechanical_review"] = {
            "provider": mechanical["provider"],
            "model": mechanical["model"],
            "fallback_reasons": mechanical["fallback_reasons"],
            "issues": reviewed["issues"],
            "changed": reviewed["changed"],
            "applied_correction": reviewed["changed"],
            "author_saved_replacement_text": hunk["replacement"],
            "reviewed_replacement_text": reviewed["replacement_text"],
            "note": "Mechanical corrections were applied before staging; the saved editor text is retained for provenance.",
        }
        if reviewed["issues"]:
            validation["warnings"].extend(
                f"Mechanical pass applied: {issue['description']}" for issue in reviewed["issues"]
            )
        # A reintaked project may intentionally have no Git checkout yet. In
        # that case the imported revision is still a valid baseline and the
        # exact editor diff belongs in the Approval Desk as a Studio-only
        # proposal. Git-backed projects retain the stricter source-writeback
        # requirement.
        source_writeback_unavailable = not validation.get("writeback", {}).get("available")
        source_less_reintake = not project.get("source_root") and candidate.get("source_mode") == "import_baseline"
        if validation["errors"] or (source_writeback_unavailable and not source_less_reintake):
            skipped.append({
                "scene_number": candidate["scene_number"],
                "source_path": candidate["source_path"],
                "operation": hunk["operation"],
            "errors": validation["errors"] or [
                validation.get("writeback", {}).get("reason", "Markdown write-back is unavailable.")
            ],
            })
            continue
        staged.append(db.create_staged_proposal(
            batch_id=batch["batch_id"],
            scene_number=candidate["scene_number"],
            proposed_by_persona_id="author-editor",
            proposed_by_name="Author editor",
            change_type="manual_editor_sync",
            original_verbatim_text=hunk["original"],
            proposed_replacement_text=replacement,
            editorial_rationale=(
                "Saved manually in the Literary Studio editor; review the exact source diff before applying."
            ),
            source_branch=baseline.get("branch"),
            source_commit_sha=baseline.get("commit_sha"),
            source_digest=baseline.get("source_digest"),
            source_scene_version=candidate["source_scene_version"],
            validation=validation,
        ))

    result = {
        "kind": "manual_editor_sync",
        "candidate_count": len(candidates),
        "staged_count": len(staged),
        "skipped": skipped,
    }
    db.update_approval_batch(
        batch["batch_id"],
        status="pending" if staged else "closed",
        result=result,
    )
    return {
        "project_id": project_id,
        "status": "staged" if staged else ("blocked" if skipped else "no_changes"),
        "batch": db.get_approval_batch(batch["batch_id"]),
        "candidates": candidates,
        "proposals": staged,
        "skipped": skipped,
        "blocked_count": len(skipped) if not staged else 0,
        "studio_only_count": sum(1 for item in skipped if item.get("operation") == "studio_only"),
    }


@app.post("/api/projects/{project_id}/manual-changes/push")
def push_manual_changes(project_id: str, req: DeskPushRequest):
    """Push the complete saved Studio desk into the linked Git branch.

    This is separate from proposal review: it is the explicit author action
    for making the current desk the Markdown source of truth. Safe scene
    replacements are committed as one source-only commit. Suspicious
    whole-scene erasures remain listed as blockers unless ``allow_unsafe`` is
    explicitly enabled.
    """
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to push the saved desk to Git.")
    db = get_project_db(project_id)
    try:
        result = push_desk_to_git(
            db,
            allow_unsafe=req.allow_unsafe,
            override_dirty=req.override_dirty,
        )
        if result.get("committed"):
            project = db.project_info()
            snapshot = build_snapshot(
                project["project_id"],
                project["title"],
                project["source_root"],
                project.get("source_subpath"),
            )
            output_dir = PROJECTS_ROOT / project["project_id"] / "source"
            snapshot_path, manifest_path = write_snapshot(snapshot, output_dir)
            result["project"] = db.update_source_metadata(
                source_root=snapshot["manifest"]["source_root"],
                source_subpath=snapshot["manifest"]["source_subpath"],
                branch=snapshot["manifest"]["branch"],
                commit_sha=snapshot["manifest"]["commit_sha"],
                source_digest=snapshot["manifest"]["source_digest"],
                snapshot_path=str(snapshot_path),
                manifest_path=str(manifest_path),
                working_tree_dirty=snapshot["manifest"]["working_tree_dirty"],
            )
        return result
    except (GovernanceError, SnapshotError) as exc:
        status = 409 if isinstance(exc, GovernanceError) else 400
        raise HTTPException(status_code=status, detail={"code": getattr(exc, "code", "snapshot_error"), "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to refresh source provenance: {exc}") from exc


@app.post("/api/projects/{project_id}/git-pull")
def pull_project_from_git(project_id: str, req: GitPullRequest | None = None):
    """Pull source Markdown changes from the linked Git worktree into Studio SQLite cache non-destructively."""
    db = get_project_db(project_id)
    req = req or GitPullRequest()
    try:
        result = pull_desk_from_git(
            db,
            pull_remote=req.pull_remote,
            override_dirty=req.override_dirty,
        )
        project = db.project_info()
        snapshot = build_snapshot(
            project["project_id"],
            project["title"],
            project["source_root"],
            project.get("source_subpath"),
        )
        output_dir = PROJECTS_ROOT / project["project_id"] / "source"
        snapshot_path, manifest_path = write_snapshot(snapshot, output_dir)
        result["project"] = db.update_source_metadata(
            source_root=snapshot["manifest"]["source_root"],
            source_subpath=snapshot["manifest"]["source_subpath"],
            branch=snapshot["manifest"]["branch"],
            commit_sha=snapshot["manifest"]["commit_sha"],
            source_digest=snapshot["manifest"]["source_digest"],
            snapshot_path=str(snapshot_path),
            manifest_path=str(manifest_path),
            working_tree_dirty=snapshot["manifest"]["working_tree_dirty"],
        )
        result["graph"] = db.rebuild_story_graph()
        return result
    except (GovernanceError, SnapshotError) as exc:
        status = 409 if isinstance(exc, GovernanceError) else 400
        raise HTTPException(status_code=status, detail={"code": getattr(exc, "code", "git_error"), "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to refresh source provenance: {exc}") from exc


@app.post("/api/projects/{project_id}/scenes/{scene_number}/direct-commit")
def direct_commit_scene(project_id: str, scene_number: int, req: DirectCommitRequest | None = None):
    """Author direct commit of a single scene directly to Git without proposal batching."""
    db = get_project_db(project_id)
    req = req or DirectCommitRequest()
    try:
        result = commit_scene_to_git(
            db,
            scene_number,
            message=req.message,
            override_dirty=req.override_dirty,
        )
        if result.get("committed"):
            project = db.project_info()
            snapshot = build_snapshot(
                project["project_id"],
                project["title"],
                project["source_root"],
                project.get("source_subpath"),
            )
            output_dir = PROJECTS_ROOT / project["project_id"] / "source"
            snapshot_path, manifest_path = write_snapshot(snapshot, output_dir)
            result["project"] = db.update_source_metadata(
                source_root=snapshot["manifest"]["source_root"],
                source_subpath=snapshot["manifest"]["source_subpath"],
                branch=snapshot["manifest"]["branch"],
                commit_sha=snapshot["manifest"]["commit_sha"],
                source_digest=snapshot["manifest"]["source_digest"],
                snapshot_path=str(snapshot_path),
                manifest_path=str(manifest_path),
                working_tree_dirty=snapshot["manifest"]["working_tree_dirty"],
            )
        return result
    except (GovernanceError, SnapshotError) as exc:
        status = 409 if isinstance(exc, GovernanceError) else 400
        raise HTTPException(status_code=status, detail={"code": getattr(exc, "code", "git_error"), "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to refresh source provenance: {exc}") from exc


@app.get("/api/projects/{project_id}/governance/commit-pending")
def get_pending_governance_commits(project_id: str):
    """Return reviewed source changes waiting for the author's branch decision.

    Historical commit failures are exposed separately so they cannot be
    mistaken for a currently dirty manuscript checkout.
    """
    return get_project_db(project_id).pending_commit_batches()


@app.get("/api/projects/{project_id}/governance/commit-failed")
def get_failed_governance_commits(project_id: str):
    """Return historical commit failures that need review, without blocking navigation."""
    return get_project_db(project_id).failed_commit_batches()


@app.post("/api/projects/{project_id}/governance/commit-failed/{batch_id}/reconcile")
def reconcile_failed_governance_commit(project_id: str, batch_id: str):
    """Reconcile a failed ledger entry already represented in clean source Git."""
    try:
        return reconcile_failed_commit_batch(get_project_db(project_id), batch_id)
    except GovernanceError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc


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


@app.post("/api/projects/{project_id}/scenes/{scene_number}/passes")
def run_scene_pass(project_id: str, scene_number: int, req: ScenePassRequest):
    """Run one explicit scene pass and stage its exact proposals for approval.

    These passes never edit the Studio scene or Markdown source directly. They
    use the normal source-pinned Approval Bridge so every suggested correction
    arrives in the Approval Desk with an anchor, rationale, and provenance.
    """
    db = get_project_db(project_id)
    scene = db.get_scene(scene_number)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found.")

    if req.pass_type == "mechanical":
        request_text = f"""
Run a conservative mechanical spelling and copy-edit pass on Scene {scene_number} only.
Find only clear errors in spelling, punctuation, capitalization, grammar agreement,
sentence-boundary mechanics, and accidental spacing. Preserve the author's voice,
diction, rhythm, dialect, intentional fragments, profanity, quotations, names,
technical terms, musical terms, and deliberate typography. Do not make stylistic,
developmental, continuity, or substantive changes. Create one exact anchored
proposal per genuine correction and create no proposal when the text is already
mechanically sound.
""".strip()
    else:
        request_text = f"""
Run a comprehensive, evidence-based craft pass on Scene {scene_number} only,
focused on AI-generated-sounding prose, tell-not-show shortcuts, and recurring
authorial habits. Look for generic filler, abstract summary where the scene needs
embodied or dramatic specificity, stock transitions, repetitive syntax, habitual
adverbs, over-explanation, false emphasis, and flattened emotional labeling.
Treat the author's heightened voice, theatricality, intimacy, humor, repetition,
fragments, and intentional exposition as protected unless the passage itself
provides evidence that the choice is weakening the scene. Do not genericize,
flatten, or rewrite the scene wholesale. Return only actionable, contiguous,
paragraph-local proposals, each with a minimal replacement when a replacement is
actually justified. Put uncertain observations in questions rather than staging
them as edits.
""".strip()

    try:
        batch = prepare_for_approval(
            db,
            request_text=request_text,
            scene_numbers=[scene_number],
            # Scene-level editorial passes must meet the high-quality floor:
            # Gemini when available, otherwise AGY. Never fall back to Ollama,
            # OpenRouter free tiers, or FModCLI for these judgments.
            allowed_providers=("gemini", "agy"),
            preferred_provider=os.getenv("LITERARY_STUDIO_EDITORIAL_PROVIDER", "auto"),
        )
    except ApprovalBridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "project_id": project_id,
        "scene_number": scene_number,
        "pass_type": req.pass_type,
        "pass_label": "Mechanical spelling and copy-edit" if req.pass_type == "mechanical" else "AI slop / tell / habit craft pass",
        **batch,
    }


@app.get("/api/projects/{project_id}/approval-batches/{batch_id}")
def get_approval_batch(project_id: str, batch_id: str):
    batch = get_project_db(project_id).get_approval_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Approval batch not found.")
    return batch


class CommitBatchRequest(BaseModel):
    # Explicit author override for a pre-existing dirty source file. The
    # governed batch's exact written file is still hash-checked before commit.
    override_dirty: bool = False


@app.post("/api/projects/{project_id}/approval-batches/{batch_id}/commit")
def commit_approval_batch(project_id: str, batch_id: str, req: CommitBatchRequest | None = None):
    """Commit a batch's written-back source files now (retry after a failure, or commit early)."""
    try:
        return commit_batch(
            get_project_db(project_id),
            batch_id,
            override_dirty=bool(req and req.override_dirty),
        )
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
    # Explicitly allow an exact anchored write into a dirty source file. This
    # preserves unrelated worktree text but defers commit for author review.
    allow_dirty_source: bool = False
    # The web Approval Desk defers the batch commit until the author chooses
    # whether to commit it while leaving the desk. Other clients retain the
    # original auto-commit behavior unless they opt in.
    defer_commit: bool = False
    # Optional author edits submitted together with approval. They are
    # revalidated against the pinned scene before any write-back occurs.
    original_verbatim_text: str | None = None
    proposed_replacement_text: str | None = None
    editorial_rationale: str | None = None
    change_type: str | None = None


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
    retry with the explicit dirty-worktree apply action.
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

    edited_fields = {
        "original_verbatim_text": req.original_verbatim_text,
        "proposed_replacement_text": req.proposed_replacement_text,
        "editorial_rationale": req.editorial_rationale,
        "change_type": req.change_type,
    }
    if req.action == "accept" and any(value is not None for value in edited_fields.values()):
        scene = db.get_scene(int(proposal["scene_number"]))
        if scene is None:
            raise _review_error(409, "scene_missing", "The proposal scene no longer exists.")
        original = (req.original_verbatim_text if req.original_verbatim_text is not None else proposal["original_verbatim_text"]).strip()
        replacement = req.proposed_replacement_text if req.proposed_replacement_text is not None else proposal["proposed_replacement_text"]
        rationale = (req.editorial_rationale if req.editorial_rationale is not None else proposal["editorial_rationale"]).strip()
        change_type = (req.change_type if req.change_type is not None else proposal["change_type"]).strip()
        if not original or not replacement.strip() or not rationale or not change_type:
            raise _review_error(400, "invalid_proposal", "Edited proposals require an anchor, replacement, rationale, and change type.")
        validation = validate_candidate(
            db.project_info(),
            scene,
            original,
            replacement,
            allow_multiline=(proposal.get("change_type") == "manual_editor_sync" or change_type == "manual_editor_sync"),
        )
        if validation["errors"]:
            raise _review_error(
                400,
                "invalid_proposal",
                "The manual proposal edits failed governance validation.",
                validation=validation,
            )
        try:
            proposal = db.update_staged_proposal(
                proposal_id,
                original_verbatim_text=original,
                proposed_replacement_text=replacement,
                editorial_rationale=rationale,
                change_type=change_type,
                validation=validation,
            )
        except (KeyError, ValueError) as exc:
            raise _review_error(409, "proposal_update_failed", str(exc)) from exc

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
                write = write_back(
                    db,
                    proposal,
                    scene,
                    # An explicit author approval permits an exact anchored
                    # replacement in a dirty worktree. The write preserves
                    # unrelated text; commit handling remains separate.
                    allow_dirty=True,
                )
            except GovernanceError as exc:
                raise _review_error(
                    409,
                    "writeback_blocked",
                    str(exc),
                    reason=exc.code,
                    dirty_source_available=exc.code == "source_dirty",
                ) from exc
            writeback_status = "source_and_studio_dirty" if write.get("dirty_before") else "source_and_studio"
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
    if req.defer_commit and batch_id:
        current_batch = db.get_approval_batch(batch_id)
        if current_batch and not any(item.get("status") == "staged" for item in current_batch.get("proposals", [])):
            if (current_batch.get("writeback") or {}).get("files"):
                db.update_approval_batch(batch_id, status="awaiting_commit")
                commit = {
                    "committed": False,
                    "reason": "awaiting_author_commit",
                    "branch": (current_batch.get("baseline") or {}).get("branch"),
                }
            else:
                db.update_approval_batch(batch_id, status="closed")
                commit = {"committed": False, "reason": "batch_closed_no_source_changes"}
        else:
            commit = None
    else:
        commit = finalize_batch_if_done(db, batch_id)

    if req.action == "reject":
        message = "Proposal rejected."
    elif write:
        message = f"Accepted and written to {write['path']}."
        if write.get("dirty_before"):
            message += " The file was already dirty; commit is deferred so unrelated work is not committed silently."
    else:
        message = "Accepted in the Studio copy only; the Markdown source was not changed."
    if commit and commit.get("committed") and not commit.get("already"):
        message += f" Batch committed as {commit['commit_sha'][:8]} on {commit.get('branch')}."
    elif commit and commit.get("error"):
        message += f" Batch commit failed: {commit['error']['message']}"
    elif commit and commit.get("reason") == "awaiting_author_commit":
        message += " Changes are waiting for your branch decision."
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
