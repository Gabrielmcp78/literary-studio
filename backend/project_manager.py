"""Physical project isolation and the shared project registry for Literary Studio.

Each project owns a separate SQLite file. The API must resolve a project
through this manager before it can access manuscript state.

The registry joins Studio projects with FictionForce projects (from
fiction.config.json) by manuscript root, so either system's name resolves:
"String Theory", "string-theory", "String Theories" and "string-theories"
all open the same Studio project.

Imports
- json: read FictionForce's fiction.config.json. https://docs.python.org/3/library/json.html
- os: FICTIONFORCE_CONFIG override. https://docs.python.org/3/library/os.html
- re: id validation and slugging. https://docs.python.org/3/library/re.html
- pathlib.Path: root comparison. https://docs.python.org/3/library/pathlib.html
- threading.RLock: serialise registry access. https://docs.python.org/3/library/threading.html#rlock-objects
- uuid.uuid4: collision suffixes for new ids. https://docs.python.org/3/library/uuid.html
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from studio_db import PROJECTS_ROOT, StudioDB


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FICTION_GIT_ROOT = PROJECT_ROOT.parent / "fiction-git-system"

PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
FICTIONFORCE_CONFIG = Path(
    os.getenv("FICTIONFORCE_CONFIG") or str(DEFAULT_FICTION_GIT_ROOT / "fiction.config.json")
)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _normalise_root(value: str | None, base: Path | None = None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    try:
        return path.resolve()
    except OSError:
        return None


def live_source_state(project: dict[str, Any]) -> dict[str, Any]:
    """Read branch, commit, and dirty state from the configured checkout.

    ``project_meta`` stores the provenance captured at intake, but Git can
    change afterward (including untracked files). Project listings must not
    present that historical snapshot as the current worktree state.
    """
    root = _normalise_root(project.get("source_root"))
    if root is None or not root.is_dir():
        return {}
    try:
        branch = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or None
        commit_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    return {
        "branch": branch,
        "commit_sha": commit_sha,
        "working_tree_dirty": bool(status),
    }


def fictionforce_projects() -> dict[str, list[Path]]:
    """FictionForce project name -> resolved manuscript roots. Empty if unreadable."""
    try:
        config = json.loads(FICTIONFORCE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    base = FICTIONFORCE_CONFIG.parent
    projects: dict[str, list[Path]] = {}
    for name, entry in (config.get("projects") or {}).items():
        roots = [_normalise_root(root, base) for root in (entry or {}).get("manuscript_roots", [])]
        projects[name] = [root for root in roots if root is not None]
    return projects


class ProjectNotFound(KeyError):
    pass


class ProjectManager:
    def __init__(self, root: str | Path = PROJECTS_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _validate_id(self, project_id: str) -> str:
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ProjectNotFound(project_id)
        return project_id

    def _path(self, project_id: str) -> Path:
        return self.root / f"{self._validate_id(project_id)}.sqlite3"

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            projects = []
            for path in sorted(self.root.glob("*.sqlite3")):
                project_id = path.stem
                if not PROJECT_ID_PATTERN.fullmatch(project_id):
                    continue
                try:
                    project = StudioDB(path, project_id).project_info()
                    project.update(live_source_state(project))
                    projects.append(project)
                except (KeyError, OSError):
                    continue
            return projects

    def ensure_default_project(self) -> dict[str, Any]:
        projects = self.list_projects()
        if projects:
            return projects[0]
        return self.create_project("Untitled manuscript", project_id="untitled-manuscript")

    def create_project(
        self,
        title: str,
        project_id: str | None = None,
        source_root: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        source_digest: str | None = None,
    ) -> dict[str, Any]:
        clean_title = title.strip() or "Untitled manuscript"
        candidate = project_id or re.sub(r"[^a-z0-9]+", "-", clean_title.casefold()).strip("-")
        candidate = candidate[:48] or "manuscript"
        if not PROJECT_ID_PATTERN.fullmatch(candidate):
            raise ValueError("Project ID must contain lowercase letters, numbers, and hyphens.")
        path = self._path(candidate)
        if path.exists():
            candidate = f"{candidate}-{uuid4().hex[:8]}"
            path = self._path(candidate)

        db = StudioDB(path, candidate, clean_title)
        with db._lock, db._connect() as connection:
            connection.execute(
                """UPDATE project_meta
                   SET title = ?, source_root = ?, branch = ?, commit_sha = ?, source_digest = ?
                   WHERE project_id = ?""",
                (clean_title, source_root, branch, commit_sha, source_digest, candidate),
            )
        return db.project_info()

    def registry(self) -> dict[str, Any]:
        """Studio projects joined with FictionForce projects by manuscript root.

        Returns {"projects": [...], "fictionforce_only": [...]}, where each
        Studio project carries ``aliases`` and ``fictionforce_project``.
        """
        fiction = fictionforce_projects()
        claimed: set[str] = set()
        joined: list[dict[str, Any]] = []
        for project in self.list_projects():
            source = _normalise_root(project.get("source_root"))
            manuscript = _normalise_root(project.get("source_subpath"), source) if source else None
            match = None
            for name, roots in fiction.items():
                if any(root in {source, manuscript} for root in roots):
                    match = name
                    break
            if match:
                claimed.add(match)
            aliases = list(dict.fromkeys(
                alias for alias in (project.get("title"), match, slugify(match) if match else None)
                if alias and alias != project["project_id"]
            ))
            joined.append({**project, "aliases": aliases, "fictionforce_project": match})
        orphans = [
            {"fictionforce_project": name, "manuscript_roots": [str(root) for root in roots], "studio_project_id": None}
            for name, roots in fiction.items()
            if name not in claimed
        ]
        return {"projects": joined, "fictionforce_only": orphans}

    def resolve(self, value: str) -> str:
        """Resolve a Studio id, title, slug, or FictionForce name to a Studio project id.

        Failure: ProjectNotFound when nothing matches, or the match is ambiguous.
        """
        raw = (value or "").strip()
        if PROJECT_ID_PATTERN.fullmatch(raw) and self._path(raw).exists():
            return raw
        wanted = {raw.casefold(), slugify(raw)}
        matches = {
            project["project_id"]
            for project in self.registry()["projects"]
            if wanted & {project["project_id"], *(a.casefold() for a in project["aliases"]),
                         *(slugify(a) for a in project["aliases"])}
        }
        if len(matches) == 1:
            return matches.pop()
        raise ProjectNotFound(raw)

    def open(self, project_id: str) -> StudioDB:
        path = self._path(project_id)
        if not path.exists():
            raise ProjectNotFound(project_id)
        db = StudioDB(path, project_id)
        try:
            if db.project_info()["project_id"] != project_id:
                raise ProjectNotFound(project_id)
        except KeyError as exc:
            raise ProjectNotFound(project_id) from exc
        return db
