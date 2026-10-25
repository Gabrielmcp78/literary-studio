"""Physical project isolation for Literary Studio.

Each project owns a separate SQLite file. The API must resolve a project
through this manager before it can access manuscript state.
"""

from __future__ import annotations

import re
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from studio_db import PROJECTS_ROOT, StudioDB


PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


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
                    projects.append(StudioDB(path, project_id).project_info())
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
