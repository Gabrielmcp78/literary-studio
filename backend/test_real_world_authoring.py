"""End-to-end real-world authoring and Git synchronization integration test.

Tests the live FastAPI endpoints against real Git repositories, real Markdown
manuscripts with Chapter 6 compound coordinates, external Git collision detection,
non-destructive pull with annotation persistence, and publisher export.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from main import app, get_project_db
from studio_db import StudioDB
from compiler_engine import _scene_heading_text, render_markdown_export


class TestRealWorldAuthoringWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="litstudio_realworld_")
        self.repo_dir = Path(self.temp_dir) / "novel_repo"
        self.repo_dir.mkdir(parents=True)

        # 1. Initialize real Git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Elena Vance"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "elena@author.org"], cwd=str(self.repo_dir), check=True, capture_output=True)

        # 2. Create realistic Chapter 6 with compound counterpoint headers
        self.manuscript_dir = self.repo_dir / "manuscript"
        self.manuscript_dir.mkdir()
        self.ch6_file = self.manuscript_dir / "chapter_06.md"
        self.ch6_initial_text = (
            "# 6 | Now Then\n\n"
            "### 6.1 SUBJECT | Buffalo, NY | Present Day\n\n"
            "David tightened the tuning peg until the A-string whined against the bridge.\n"
            "Across the room, Celeste did not look up from her notebooks.\n\n"
            "### 6.2 COUNTERPOINT | Geneva, Switzerland | 1982\n\n"
            "The rain over Lake Geneva smelled of ozone and diesel exhaust.\n"
            "Thirty-four years earlier, his father had stood on the same stone jetty.\n"
        )
        self.ch6_file.write_text(self.ch6_initial_text, encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial manuscript commit: Chapter 6 dual-timeline"], cwd=str(self.repo_dir), check=True, capture_output=True)

        rev_parse = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.repo_dir), check=True, capture_output=True, text=True)
        self.initial_sha = rev_parse.stdout.strip()

        # 3. Initialize Studio project via real API
        from studio_db import PROJECTS_ROOT
        self.project_id = "real-novel-v1"
        project_dir = PROJECTS_ROOT / self.project_id
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        db_file = PROJECTS_ROOT / f"{self.project_id}.sqlite3"
        if db_file.exists():
            db_file.unlink()

        self.client = TestClient(app)
        create_resp = self.client.post(
            "/api/projects",
            json={
                "title": "Real World Novel",
                "project_id": self.project_id,
                "source_root": str(self.repo_dir),
                "branch": "main",
                "commit_sha": self.initial_sha,
                "source_digest": "sha256-initial",
            },
        )
        assert create_resp.status_code == 200, create_resp.text
        self.db = get_project_db(self.project_id)
        self.db.update_source_metadata(
            source_root=str(self.repo_dir),
            source_subpath="manuscript",
            branch="main",
            commit_sha=self.initial_sha,
            source_digest="sha256-initial",
            snapshot_path=str(self.repo_dir / "snapshot.json"),
            manifest_path=str(self.repo_dir / "manifest.json"),
            working_tree_dirty=False,
        )

        # 4. Import the repository scenes
        self.scenes = self.db.import_repository_scenes([
            {"path": "manuscript/chapter_06.md", "text": self.ch6_initial_text}
        ])

    def tearDown(self):
        from studio_db import PROJECTS_ROOT
        project_dir = PROJECTS_ROOT / self.project_id
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        db_file = PROJECTS_ROOT / f"{self.project_id}.sqlite3"
        if db_file.exists():
            db_file.unlink()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_end_to_end_authoring_cycle(self):
        # --- STEP 1: Verify Initial Hierarchy & Intake ---
        self.assertEqual(len(self.scenes), 2)
        s1 = self.scenes[0]
        s2 = self.scenes[1]

        # Chapter 6 coordinates must not be stripped or flattened
        self.assertEqual(s1["chapter_number"], 6)
        self.assertEqual(s1["scene_number_in_chapter"], 1)
        self.assertIn("SUBJECT", s1["title"])
        self.assertIn("Buffalo, NY", s1["title"])

        self.assertEqual(s2["chapter_number"], 6)
        self.assertEqual(s2["scene_number_in_chapter"], 2)
        self.assertIn("COUNTERPOINT", s2["title"])
        self.assertIn("Geneva, Switzerland", s2["title"])

        heading1 = _scene_heading_text(s1)
        heading2 = _scene_heading_text(s2)
        self.assertEqual(heading1, "6.1  SUBJECT | Buffalo, NY | Present Day")
        self.assertEqual(heading2, "6.2  COUNTERPOINT | Geneva, Switzerland | 1982")

        # --- STEP 2: Author Direct Commit ---
        # Author edits Scene 6.1 prose directly in studio
        new_prose_html = "<p>David adjusted the cello peg with deliberate, measured care. The cedar instrument hummed under his fingers.</p>"
        self.db.update_scene(s1["scene_number"], new_prose_html)

        # Author clicks "Direct Commit" in studio
        resp = self.client.post(
            f"/api/projects/{self.project_id}/scenes/{s1['scene_number']}/direct-commit",
            json={"message": "Author polish: enhance sensory detail in cello scene"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        commit_data = resp.json()
        self.assertTrue(commit_data["committed"])
        new_commit_sha = commit_data["commit_sha"]

        # Verify on disk in real git repo
        log_res = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=str(self.repo_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Author polish: enhance sensory detail in cello scene", log_res.stdout)

        # Verify the actual markdown file on disk:
        disk_md = self.ch6_file.read_text(encoding="utf-8")
        self.assertIn("### 6.1 SUBJECT | Buffalo, NY | Present Day", disk_md)
        self.assertIn("David adjusted the cello peg with deliberate, measured care", disk_md)
        self.assertIn("### 6.2 COUNTERPOINT | Geneva, Switzerland | 1982", disk_md)

        # --- STEP 3: Collision Detection (External Git Advance) ---
        # Author attaches an important margin annotation in Studio
        para_match = disk_md.split("\n\n")[2] # Scene 1 paragraph
        ann = self.db.create_annotation(
            scene_number=s1["scene_number"],
            paragraph_id=self.db.get_scene(s1["scene_number"])["content_html"].split('data-id="')[1].split('"')[0],
            quote_text="deliberate, measured care",
            body="Check historical resonance with father's hands in Chapter 14.",
        )
        self.assertTrue(ann["annotation_id"].startswith("ann_"))

        # External collaborator edits Chapter 6 directly in Git CLI
        external_edit = disk_md.replace(
            "Thirty-four years earlier, his father had stood on the same stone jetty.",
            "Thirty-four years earlier, his father had stood on the same stone jetty, clutching the leather dispatch case."
        )
        self.ch6_file.write_text(external_edit, encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "External collaborator: add dispatch case detail"],
            cwd=str(self.repo_dir),
            check=True,
            capture_output=True,
        )

        # Author in Studio makes a local edit to Scene 1
        self.db.update_scene(s1["scene_number"], "<p>David adjusted the cello peg with quiet patience.</p>")

        # Attempting desk push MUST be blocked to protect external commit!
        resp_push = self.client.post(
            f"/api/projects/{self.project_id}/manual-changes/push",
            json={"confirm": True}
        )
        self.assertEqual(resp_push.status_code, 409)
        self.assertEqual(resp_push.json()["detail"]["code"], "git_advanced")
        self.assertIn("newer commits", resp_push.json()["detail"]["message"])

        # --- STEP 4: Non-Destructive Git Pull ---
        resp_pull = self.client.post(
            f"/api/projects/{self.project_id}/git-pull",
            json={"pull_remote": False}
        )
        self.assertEqual(resp_pull.status_code, 200, resp_pull.text)
        pull_data = resp_pull.json()
        self.assertEqual(pull_data["status"], "pulled")

        # Verify Scene 2 in Studio now has the external collaborator's edit
        s2_updated = self.db.get_scene(s2["scene_number"])
        self.assertIn("clutching the leather dispatch case", s2_updated["content_html"])

        # CRITICAL: Verify author's annotation was NOT wiped out!
        annotations = self.db.list_annotations(s1["scene_number"])
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0]["body"], "Check historical resonance with father's hands in Chapter 14.")

        # --- STEP 5: Publisher Ready Export ---
        resp_export = self.client.post(
            f"/api/projects/{self.project_id}/compiler/export",
            json={"output_format": "markdown"}
        )
        self.assertEqual(resp_export.status_code, 200, resp_export.text)
        exported_markdown = resp_export.text

        # Verify no duplicate pipes or mangled headings in final publisher export
        self.assertIn("### 6.1  SUBJECT | Buffalo, NY | Present Day", exported_markdown)
        self.assertIn("### 6.2  COUNTERPOINT | Geneva, Switzerland | 1982", exported_markdown)
        self.assertNotIn("||", exported_markdown)
        self.assertNotIn("### 1 SUBJECT", exported_markdown)


if __name__ == "__main__":
    unittest.main()
