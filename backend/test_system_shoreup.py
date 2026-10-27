"""Comprehensive tests for Literary Studio AI intake, multi-provider control,
manuscript chapter grounding, and Fiction Git Studio typesetting.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_provider import AIProviderUnavailable, AIResponse, generate_text
from compiler_engine import (
    DEFAULT_AUTHOR,
    FormatterUnavailable,
    _safe_filename,
    _compact_reader_layout,
    _inject_latex_provenance,
    _write_project_chapters,
    compile_shunn_pdf,
    list_format_profiles,
    render_markdown_export,
    select_export_scenes,
)
from graph_extractor import build_graph
from manuscript_context import (
    _chapter_from_message,
    _reference_from_message,
    build_interrogation_context,
    context_prompt,
)
from manual_sync import build_manual_change_candidates
from governance import GovernanceError, reconcile_failed_commit_batch
from desk_push import commit_scene_to_git, pull_desk_from_git, push_desk_to_git
from studio_db import (
    StudioDB,
    _markdown_scenes,
    is_project_local_scratch_path,
    is_repository_manuscript_path,
    load_semantic_style_registry,
)


class TestDatabaseConnectionLifecycle(unittest.TestCase):
    def test_database_connection_context_closes_connections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = StudioDB(Path(temp_dir) / "connection-lifecycle.sqlite3", "connection-lifecycle")
            with db._connect() as connection:
                connection.execute("SELECT 1")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_database_connection_enables_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = StudioDB(Path(temp_dir) / "wal-check.sqlite3", "wal-check")
            with db._connect() as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
                self.assertGreaterEqual(timeout, 5000)


class TestPathFiltering(unittest.TestCase):
    def test_scratch_namespace_is_never_repository_manuscript_source(self):
        scratch_paths = [
            "scratch/ideas.md",
            "manuscript/scratch/scene.md",
            "act-2/scratch/scene.md",
        ]
        for path in scratch_paths:
            self.assertTrue(is_project_local_scratch_path(path), path)
            self.assertFalse(is_repository_manuscript_path(path), path)

    def test_rejects_non_manuscript_directories(self):
        rejected_paths = [
            "worldbook/lore/ch01_history.md",
            "worldbook/editorial-history/editor-passes/act-1/agent_roundtable_ch01_ch02.md",
            "worldbook/editorial-history/editor-passes/act-3/sarah_mcgrath_pass.md",
            "worldbook/editorial-history/editor-passes/act-4/editorial_board_pass_part_four.md",
            "worldbook/editorial-history/editor-passes/act-6/complete_narrative_scaffolding_parts2_10.md",
            "outlines/detailed_plot_outline.md",
            "notes/chapter_notes.md",
            "reviews/chapter_10_assessment.md",
            "dossiers/character_profiles.md",
        ]
        for path in rejected_paths:
            self.assertFalse(
                is_repository_manuscript_path(path),
                f"Path should have been rejected as non-manuscript: {path}",
            )

    def test_accepts_clean_manuscript_paths(self):
        accepted_paths = [
            "manuscript/act-0/ch00-prologue.md",
            "manuscript/act-1/ch01-the-ivy-league.md",
            "manuscript/act-2/ch06-now-then.md",
            "manuscript/act-3/ch10-passions-principles.md",
            "manuscript/act-11/ch27-elegy.md",
            "manuscript/appendix/appendix-lexicon.md",
            "chapters/chapter-01.md",
        ]
        for path in accepted_paths:
            self.assertTrue(
                is_repository_manuscript_path(path),
                f"Path should have been accepted as manuscript: {path}",
            )


class TestRepositorySceneParsing(unittest.TestCase):
    def test_every_registered_style_has_a_print_contract(self):
        styles = load_semantic_style_registry()
        self.assertEqual(len(styles), 20)
        self.assertEqual(len({style["id"] for style in styles}), len(styles))
        self.assertTrue(all(isinstance(style.get("print"), dict) for style in styles))

    def test_chapter_without_scene_heading_or_break_is_one_scene(self):
        sections = _markdown_scenes(
            "manuscript/act-1/ch01-the-ivy-league.md",
            """---\ntitle: The Ivy League\n---\n# 1 | The Ivy League\n\n## Andante sostenuto\n\nOpening paragraph.\n\nSecond paragraph.\n""",
        )
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].scene_number_in_chapter, 1)
        self.assertEqual(sections[0].part_number, 1)
        self.assertEqual(sections[0].part_title, "Part 1")
        self.assertIn("Opening paragraph", sections[0].content_html)

    def test_semantic_scene_title_directives_define_boundaries(self):
        sections = _markdown_scenes(
            "manuscript/act-3/ch14-repair.md",
            """---\ntitle: Chapter 14\n---\n# 14 | Chapter 14\n\n<!-- literary-style: scene-title locked -->\n#### Arrival\n\nThe first scene.\n\n<!-- literary-style: scene-title locked -->\n#### The Turn\n\nThe second scene.\n""",
        )
        self.assertEqual([section.title for section in sections], ["Arrival", "The Turn"])
        self.assertEqual([section.scene_number_in_chapter for section in sections], [1, 2])
        self.assertNotIn("Arrival", sections[0].content_html)
        self.assertIn("The first scene", sections[0].content_html)


class TestCompilerSelection(unittest.TestCase):
    def setUp(self):
        self.scenes = [
            {"scene_number": 1, "part_number": 1, "chapter_number": 1, "source_path": "manuscript/ch01.md"},
            {"scene_number": 2, "part_number": 1, "chapter_number": 1, "source_path": "manuscript/ch01.md"},
            {"scene_number": 3, "part_number": 1, "chapter_number": 2, "source_path": "manuscript/ch02.md"},
            {"scene_number": 4, "part_number": 2, "chapter_number": 3, "source_path": "manuscript/ch03.md"},
            {"scene_number": 5, "part_number": None, "chapter_number": None, "source_path": "scratch/notes.md"},
        ]

    def test_part_selection_includes_all_descendant_chapters_and_scenes(self):
        selected = select_export_scenes(self.scenes, part_numbers=[1])
        self.assertEqual([scene["scene_number"] for scene in selected], [1, 2, 3])

    def test_chapter_selection_includes_all_descendant_scenes(self):
        selected = select_export_scenes(self.scenes, chapter_numbers=[1])
        self.assertEqual([scene["scene_number"] for scene in selected], [1, 2])

    def test_explicit_scene_selection_and_empty_selection(self):
        selected = select_export_scenes(self.scenes, scene_numbers=[4])
        self.assertEqual([scene["scene_number"] for scene in selected], [4])
        all_scenes = select_export_scenes(self.scenes)
        self.assertEqual([scene["scene_number"] for scene in all_scenes], [1, 2, 3, 4])

    def test_markdown_export_contains_selected_hierarchy(self):
        selected = select_export_scenes(
            [
                {
                    **self.scenes[0],
                    "title": "Opening",
                    "part_title": "Part One",
                    "chapter_title": "Chapter One",
                    "content_html": "<p>Opening prose.</p>",
                }
            ]
        )
        markdown = render_markdown_export(selected, "Test Manuscript", "Author")
        self.assertIn('title: "Test Manuscript"', markdown)
        self.assertIn("# Part One", markdown)
        self.assertIn("## Chapter 1 - Chapter One", markdown)
        self.assertIn("Opening prose.", markdown)

    def test_markdown_export_uses_named_scene_heading_without_redundant_break(self):
        markdown = render_markdown_export(
            [
                {
                    "scene_number": 1,
                    "scene_number_in_chapter": 1,
                    "title": "SUBJECT | Buffalo, NY | Present Day",
                    "part_number": 1,
                    "part_title": "Part One",
                    "chapter_number": 1,
                    "chapter_title": "Chapter One",
                    "content_html": "<p>First scene.</p>",
                },
                {
                    "scene_number": 2,
                    "scene_number_in_chapter": 2,
                    "title": "ANSWER | MIT | April 11, 2005",
                    "part_number": 1,
                    "part_title": "Part One",
                    "chapter_number": 1,
                    "chapter_title": "Chapter One",
                    "content_html": "<p>Second scene.</p>",
                },
            ],
            "Test Manuscript",
            "Author",
        )
        self.assertIn("### 1.1  SUBJECT | Buffalo, NY | Present Day", markdown)
        self.assertIn("### 1.2  ANSWER | MIT | April 11, 2005", markdown)
        self.assertNotIn("First scene.\n\n* * *\n\n### 1.2", markdown)

    def test_typesetter_source_emits_named_scene_heading_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _write_project_chapters(
                [
                    {
                        "scene_number": 8,
                        "scene_number_in_chapter": 1,
                        "title": "SUBJECT | Buffalo, NY | Present Day",
                        "part_number": 2,
                        "part_title": "Part Two",
                        "chapter_number": 6,
                        "chapter_title": "act-2 / 6 | Now Then",
                        "source_path": "manuscript/act-2/ch06-now-then.md",
                        "content_html": "<p>Opening prose.</p>",
                    }
                ],
                Path(temp_dir),
            )
            source = paths[0].read_text(encoding="utf-8")

        self.assertIn("### 6.1  SUBJECT | Buffalo, NY | Present Day", source)
        self.assertLess(source.index("### 6.1  SUBJECT"), source.index("Opening prose."))


class TestSourceRepairRoster(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = StudioDB(Path(self.temp_dir.name) / "repair.sqlite3", "repair-project", "Repair Project")
        from studio_db import ImportedSection
        self.db._persist_imported_sections([
            ImportedSection(
                title="Old One",
                content_html="<p>First.</p>",
                chapter_number=14,
                chapter_title="Chapter 14",
                scene_number_in_chapter=1,
                source_path="manuscript/act-5/ch14.md",
            ),
            ImportedSection(
                title="Old Two",
                content_html="<p>Second.</p>",
                chapter_number=14,
                chapter_title="Chapter 14",
                scene_number_in_chapter=2,
                source_path="manuscript/act-5/ch14.md",
            ),
        ])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_recompile_replaces_file_roster_and_removes_deleted_scene(self):
        from studio_db import ImportedSection
        scenes, removed = self.db.replace_imported_file(
            "manuscript/act-5/ch14.md",
            [ImportedSection(
                title="Only Scene",
                content_html="<p>Repaired.</p>",
                chapter_number=14,
                chapter_title="Chapter 14",
                scene_number_in_chapter=1,
                source_path="manuscript/act-5/ch14.md",
            )],
        )
        self.assertEqual(len(scenes), 1)
        self.assertEqual(removed, [2])
        self.assertEqual([scene["title"] for scene in self.db.list_scenes()], ["Only Scene"])
        self.assertIn("Repaired", self.db.list_scenes()[0]["content_html"])


class TestStoryGraph(unittest.TestCase):
    def test_next_scene_edges_follow_ordered_roster_when_ids_have_gaps(self):
        nodes, edges = build_graph([
            {"scene_number": 49, "title": "First", "chapter_title": "Chapter 14", "scene_number_in_chapter": 1, "content_text": ""},
            {"scene_number": 123, "title": "Second", "chapter_title": "Chapter 14", "scene_number_in_chapter": 2, "content_text": ""},
        ])
        node_ids = {node["node_id"] for node in nodes}
        self.assertIn(("scene:49", "scene:123", "NEXT_SCENE"), {
            (edge["source_id"], edge["target_id"], edge["relationship"])
            for edge in edges
        })
        self.assertTrue(all(edge["source_id"] in node_ids and edge["target_id"] in node_ids for edge in edges))


class TestMessageParsing(unittest.TestCase):
    def test_chapter_from_message(self):
        self.assertEqual(_chapter_from_message("read chapter 10 please"), 10)
        self.assertEqual(_chapter_from_message("how is ch 2 looking?"), 2)
        self.assertEqual(_chapter_from_message("look at ch. 3"), 3)
        self.assertEqual(_chapter_from_message("Chapter ten was great"), 10)
        self.assertEqual(_chapter_from_message("review this chapter", current_chapter=5), 5)
        self.assertEqual(_chapter_from_message("look at the prologue"), 0)
        self.assertIsNone(_chapter_from_message("hello there"))

    def test_reference_from_message(self):
        self.assertEqual(_reference_from_message("chapter 1 scene 2"), (1, 2))
        self.assertEqual(_reference_from_message("ch. 4 sc. 1"), (4, 1))
        self.assertEqual(_reference_from_message("ch 10, sc 3"), (10, 3))
        self.assertEqual(_reference_from_message("chapter one scene two"), (1, 2))


class TestManuscriptContextGrounding(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_project.sqlite3"
        self.db = StudioDB(self.db_path, "test-project", "Test Project")
        # Insert two chapters with scenes
        from studio_db import ImportedSection
        self.db._persist_imported_sections([
            ImportedSection(
                title="Opening Scene",
                content_html="<p>David poured a pint of milk on the counter.</p>",
                chapter_number=1,
                chapter_title="Chapter 1: The Ivy League",
                scene_number_in_chapter=1,
            ),
            ImportedSection(
                title="Second Scene",
                content_html="<p>Cold air rushed over the balcony rail.</p>",
                chapter_number=1,
                chapter_title="Chapter 1: The Ivy League",
                scene_number_in_chapter=2,
            ),
            ImportedSection(
                title="Lecture Hall",
                content_html="<p>The hall was packed with doctoral students.</p>",
                chapter_number=2,
                chapter_title="Chapter 2: The Lecture",
                scene_number_in_chapter=1,
            ),
        ])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_context_includes_full_chapter_prose_by_default(self):
        ctx = build_interrogation_context(
            self.db,
            current_scene_number=1,
            current_screen_text="<p>Live edits on David and the milk.</p>",
            target_scope="screen",
            user_message="How is the pacing in this opening?",
        )
        self.assertEqual(ctx["target_scope"], "chapter")
        self.assertEqual(ctx["target"]["chapter_number"], 1)
        self.assertEqual(len(ctx["target"]["scenes"]), 2)
        # Verify active scene has live editor text
        self.assertTrue(ctx["target"]["scenes"][0]["is_active_scene"])
        self.assertIn("Live edits on David", ctx["target"]["scenes"][0]["text"])

        # Format prompt and verify structure
        prompt = context_prompt(ctx)
        self.assertIn("MANUSCRIPT PROSE", prompt)
        self.assertIn("Chapter 1: The Ivy League", prompt)
        self.assertIn("[ACTIVE EDITOR SCENE IN FOCUS]", prompt)
        self.assertIn("Cold air rushed over the balcony", prompt)
        # Story graph must be at the bottom
        graph_pos = prompt.find("STORY GRAPH")
        prose_pos = prompt.find("MANUSCRIPT PROSE")
        self.assertGreater(graph_pos, prose_pos, "Story graph must appear after manuscript prose")

    def test_context_resolves_named_chapter(self):
        ctx = build_interrogation_context(
            self.db,
            current_scene_number=1,
            current_screen_text="",
            target_scope="screen",
            user_message="Tell me what you think of chapter 2",
        )
        self.assertEqual(ctx["target"]["chapter_number"], 2)
        self.assertEqual(len(ctx["target"]["scenes"]), 1)
        self.assertIn("The hall was packed with doctoral students", ctx["target"]["scenes"][0]["text"])


class TestProviderRouting(unittest.TestCase):
    def test_preferred_provider_routing(self):
        mock_response = AIResponse(text="Hello from test provider", provider="mocked", model="test-model")
        with patch("ai_provider._run_openrouter", return_value=mock_response) as mock_or:
            res = generate_text("test prompt", preferred_provider="openrouter")
            self.assertEqual(res.text, "Hello from test provider")
            mock_or.assert_called_once()

    def test_editorial_provider_floor_excludes_lower_fallbacks(self):
        mock_response = AIResponse(text="High quality response", provider="agy", model="claude-opus")
        with patch("ai_provider._run_agy", return_value=mock_response) as mock_agy, patch(
            "ai_provider._run_ollama", side_effect=AssertionError("Ollama must not run for editorial passes"),
        ) as mock_ollama:
            result = generate_text(
                "editorial pass",
                preferred_provider="agy",
                allowed_providers=("gemini", "agy"),
            )
        self.assertEqual(result.provider, "agy")
        mock_agy.assert_called_once()
        mock_ollama.assert_not_called()


class TestCompilerEngine(unittest.TestCase):
    def test_list_format_profiles(self):
        profiles = list_format_profiles()
        self.assertIsInstance(profiles, list)
        profile_ids = [p["id"] for p in profiles]
        self.assertTrue("trade_paperback_prh" in profile_ids or "manuscript_submission" in profile_ids)

    def test_list_format_profiles_fallback_when_typesetter_absent(self):
        with patch("compiler_engine._load_typesetter", side_effect=FormatterUnavailable("simulated absent")):
            profiles = list_format_profiles()
            self.assertTrue(len(profiles) >= 2)
            self.assertEqual(profiles[0]["id"], "manuscript_submission")

    def test_markdown_export_carries_full_book_provenance(self):
        markdown = render_markdown_export(
            [{
                "scene_number": 1,
                "part_number": 1,
                "part_title": "Part One",
                "chapter_number": 1,
                "chapter_title": "Chapter 1",
                "title": "Opening",
                "content_html": "<p>Opening prose.</p>",
                "source_path": "manuscript/ch01.md",
            }],
            "Test Manuscript",
            DEFAULT_AUTHOR,
            provenance={
                "generated_at": "2026-10-27T01:02:03+00:00",
                "branch": "main",
                "commit_sha": "abcdef1234567890",
            },
        )
        self.assertIn('author: "Gabriel McPherson"', markdown)
        self.assertIn('git_branch: "main"', markdown)
        self.assertIn('git_commit: "abcdef1234567890"', markdown)
        self.assertIn('page_numbering: "full-book"', markdown)

    def test_latex_provenance_overrides_empty_pages_and_starts_at_absolute_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_path = Path(temp_dir) / "export.tex"
            tex_path.write_text(
                "\\documentclass{book}\n\\usepackage{fancyhdr}\n"
                "\\begin{document}\n\\mainmatter\n"
                "<!-- literary-studio-scene: 17 | Opening -->\\par\n",
                encoding="utf-8",
            )
            _inject_latex_provenance(
                tex_path,
                {
                    "author": DEFAULT_AUTHOR,
                    "generated_at": "2026-10-27T01:02:03+00:00",
                    "branch": "main",
                    "commit_sha": "abcdef1234567890",
                },
                page_start=42,
                add_scene_labels=True,
            )
            rendered = tex_path.read_text(encoding="utf-8")
            self.assertIn(r"\fancypagestyle{empty}", rendered)
            self.assertIn(r"\setcounter{page}{42}", rendered)
            self.assertIn(r"\label{literary-studio-scene-17}", rendered)
            self.assertIn("Gabriel McPherson", rendered)
            self.assertIn(r"\fontsize{7}{8}\selectfont", rendered)

    def test_reader_bridge_removes_internal_scene_markers_and_print_padding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_path = Path(temp_dir) / "reader.tex"
            tex_path.write_text(
                "\\clearpage\n"
                "\\makeatletter\n"
                "\\ifodd\\c@page\n"
                "  \\thispagestyle{empty}\\null\\clearpage\n"
                "  \\thispagestyle{empty}\\null\\clearpage\n"
                "\\else\n"
                "  \\thispagestyle{empty}\\null\\clearpage\n"
                "\\fi\n"
                "\\makeatother\n"
                "PART ONE\n"
                "\\clearpage\n"
                "\\thispagestyle{empty}\\null\\clearpage\n"
                "<!-- literary-studio-scene: 17 | Opening -->\\par\n",
                encoding="utf-8",
            )
            _compact_reader_layout(tex_path)
            _inject_latex_provenance(
                tex_path,
                {"author": DEFAULT_AUTHOR, "generated_at": "now", "branch": "main", "commit_sha": "abc"},
            )
            rendered = tex_path.read_text(encoding="utf-8")
            self.assertNotIn("literary-studio-scene", rendered)
            self.assertNotIn("\\ifodd\\c@page", rendered)
            self.assertNotIn(r"\null\clearpage", rendered)
            self.assertIn("PART ONE", rendered)


class TestRoleplayGuardrails(unittest.TestCase):
    def test_plot_recap_drift_is_flagged_but_focused_analysis_is_not(self):
        from main import _roleplay_response_needs_rewrite

        recap = " ".join(
            [
                "The chapter opens with David arriving at the hall.",
                "Then he sees Celeste. Later they discuss the experiment.",
                "After that he leaves the room. Next he returns to the apartment.",
                "Meanwhile the conversation turns toward the cello.",
                "Finally the scene ends with him deciding to continue.",
            ]
            * 12
        )
        focused = (
            "The important pressure is not what happens next but how David keeps converting intimacy "
            "into an experiment. The clipped syntax makes that defense audible, while Celeste's pauses "
            "create a counter-rhythm the scene never explains away. I would keep the ambiguity and test "
            "one sharper physical detail at the moment he refuses her question."
        )
        self.assertTrue(_roleplay_response_needs_rewrite(recap))
        self.assertFalse(_roleplay_response_needs_rewrite(focused))


class TestEvaluationFocus(unittest.TestCase):
    def test_chapter_focus_assembles_all_scenes_and_marks_active_scene(self):
        from main import _evaluation_material

        with tempfile.TemporaryDirectory() as temp_dir:
            db = StudioDB(Path(temp_dir) / "evaluation-focus.sqlite3", "evaluation-focus", "Evaluation Focus")
            active = db.create_scene(
                title="First movement",
                content_html="<p>Live first movement.</p>",
                chapter_number=4,
                chapter_title="Chapter 4",
                scene_number_in_chapter=1,
            )
            db.create_scene(
                title="Second movement",
                content_html="<p>Second movement.</p>",
                chapter_number=4,
                chapter_title="Chapter 4",
                scene_number_in_chapter=2,
            )
            material, context, label, chapter_number = _evaluation_material(
                db,
                active,
                focus_scope="chapter",
                current_screen_text="Live first movement.",
            )
            self.assertEqual(label, "Chapter 4: Chapter 4")
            self.assertEqual(chapter_number, 4)
            self.assertEqual(context["target_scope"], "chapter")
            self.assertIn("Live first movement.", material)
            self.assertIn("Second movement.", material)


class TestSceneCreationAndScratchFiles(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_scratch.sqlite3"
        self.db = StudioDB(self.db_path, "test-scratch", "Test Scratch Project")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_scratch_scene_blank_page(self):
        scene = self.db.create_scene(
            title="My Blank Scratchpad",
            content_html="<p></p>",
            is_scratch=True,
        )
        self.assertIsNotNone(scene)
        self.assertEqual(scene["title"], "My Blank Scratchpad")
        self.assertIsNone(scene["chapter_number"])
        self.assertEqual(scene["chapter_title"], "Scratch & Drafts")
        self.assertEqual(scene["source_path"], "scratch/my-blank-scratchpad.md")

        # Verify it was saved to disk
        expected_disk_file = Path(self.temp_dir.name) / "test-scratch" / scene["source_path"]
        self.assertTrue(expected_disk_file.exists(), f"Scratch file should be created on disk at {expected_disk_file}")
        self.assertFalse((Path(self.temp_dir.name) / "test-scratch" / "manuscript" / "scratch").exists())

        # Update / autosave with blank or empty content
        updated = self.db.update_scene(scene["scene_number"], "<p></p>", "Blank save")
        self.assertEqual(updated["version"], 2)

        # Write actual prose to the scratchpad
        updated2 = self.db.update_scene(scene["scene_number"], "<p>First burst of inspiration.</p>", "Wrote prose")
        self.assertEqual(updated2["version"], 3)
        self.assertIn("First burst of inspiration.", expected_disk_file.read_text(encoding="utf-8"))

    def test_scratch_stays_outside_linked_git_checkout(self):
        checkout = Path(self.temp_dir.name) / "checkout"
        checkout.mkdir()
        self.db.update_source_metadata(
            source_root=str(checkout),
            source_subpath="manuscript",
            branch="main",
            commit_sha="abc123",
            source_digest="digest",
            snapshot_path="snapshot.md",
            manifest_path="manifest.json",
            working_tree_dirty=False,
        )
        scene = self.db.create_scene(title="Private scratch", is_scratch=True)
        self.assertEqual(scene["source_path"], "scratch/private-scratch.md")
        self.assertTrue((Path(self.temp_dir.name) / "test-scratch" / scene["source_path"]).exists())
        self.assertFalse((checkout / "manuscript" / "scratch" / "private-scratch.md").exists())

    def test_create_scene_in_existing_chapter(self):
        s1 = self.db.create_scene(
            title="Opening",
            content_html="<p>Beginning.</p>",
            chapter_number=1,
            chapter_title="Chapter 1: The Start",
        )
        self.assertEqual(s1["scene_number_in_chapter"], 1)

        s2 = self.db.create_scene(
            title="Continuing",
            content_html="<p>Next part.</p>",
            chapter_number=1,
        )
        self.assertEqual(s2["scene_number_in_chapter"], 2)
        self.assertEqual(s2["chapter_title"], "Chapter 1: The Start")

    def test_metadata_update_is_taxonomy_only(self):
        scene = self.db.create_scene(
            title="Opening",
            content_html="<p>Canonical prose.</p>",
            chapter_number=1,
            chapter_title="Chapter 1: The Start",
            scene_number_in_chapter=1,
            part_number=1,
            part_title="Part One",
        )
        updated = self.db.update_scene_metadata(
            scene["scene_number"],
            title="The Threshold",
            chapter_number=1,
            chapter_title="The Start",
            scene_number_in_chapter=1,
            part_number=1,
            part_title="Part One",
        )
        self.assertEqual(updated["title"], "The Threshold")
        self.assertEqual(updated["part_title"], "Part One")
        self.assertIn("Canonical prose.", updated["content_html"])
        self.assertEqual(updated["version"], scene["version"])


class TestProjectEditorSettingsAndStoryAnchors(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = StudioDB(Path(self.temp_dir.name) / "settings.sqlite3", "settings-project", "Settings Project")
        from studio_db import ImportedSection
        self.db._persist_imported_sections([
            ImportedSection(
                title="Opening",
                content_html="<p>Dr. David Lang enters the room.</p><p>Arthur Lang left a watch behind.</p>",
                chapter_number=1,
                chapter_title="Chapter 1",
                scene_number_in_chapter=1,
            )
        ])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_editor_settings_are_project_scoped_and_normalised(self):
        saved = self.db.update_editor_settings({
            "font_family": "palatino",
            "font_size": 26,
            "line_height": 2.1,
            "paragraph_spacing": "airy",
        })
        self.assertEqual(saved["font_family"], "palatino")
        self.assertEqual(saved["font_size"], 26)
        self.assertEqual(self.db.get_editor_settings(), saved)
        self.assertEqual(self.db.project_info()["editor_settings"], saved)

    def test_story_tracks_include_paragraph_anchors(self):
        self.db.rebuild_story_graph()
        intelligence = self.db.story_intelligence()
        track = next(track for track in intelligence["tracks"] if track["label"] == "David Lang")
        self.assertEqual(track["appearances"], [1])
        self.assertEqual(track["appearance_anchors"][0]["paragraph_index"], 0)
        self.assertIn("David Lang enters", track["appearance_anchors"][0]["quote_text"])


class TestManualChangeCandidates(unittest.TestCase):
    def test_live_source_comparison_uses_scene_span(self):
        source_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(source_root, ignore_errors=True))
        source_path = source_root / "chapter.md"
        source_path.write_text(
            "# Chapter 1\n\n## Scene 1 — Opening\n\nOriginal prose.\n",
            encoding="utf-8",
        )
        scene = {
            "scene_number": 1,
            "title": "Opening",
            "chapter_number": 1,
            "scene_number_in_chapter": 1,
            "source_path": "chapter.md",
            "version": 2,
            "updated_at": "2026-10-26T00:00:00Z",
            "content_html": "<p>Revised prose.</p>",
        }
        db = MagicMock()
        db.project_info.return_value = {"source_root": str(source_root)}
        db.list_scenes.return_value = [scene]
        db.scene_history.return_value = [{"source": "editor", "version": 2}]
        with patch("manual_sync.source_file", return_value=(source_path, "chapter.md")):
            candidates = build_manual_change_candidates(db)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["hunks"][0]["original"], "Original prose.")
        self.assertEqual(candidates[0]["hunks"][0]["replacement"], "Revised prose.")

    def test_reconcile_normalises_project_prefixed_scene_path(self):
        source_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(source_root, ignore_errors=True))
        source_path = source_root / "manuscript" / "chapter.md"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("Clean replacement.\n", encoding="utf-8")

        db = MagicMock()
        db.get_approval_batch.return_value = {
            "status": "commit_failed",
            "baseline": {"branch": "main"},
            "writeback": {"files": {"manuscript/chapter.md": "sha"}},
            "proposals": [
                {
                    "status": "accepted",
                    "scene_number": 1,
                    "proposed_replacement_text": "Clean replacement.",
                }
            ],
        }
        db.get_scene.return_value = {
            "source_path": "string-theories-v11/manuscript/chapter.md"
        }
        db.project_info.return_value = {
            "project_id": "string-theories-v11",
            "source_root": str(source_root),
            "source_subpath": "manuscript",
        }
        with patch(
            "governance.source_worktree_status",
            return_value={
                "source_root": str(source_root.resolve()),
                "branch": "main",
                "commit_sha": "abc123",
                "working_tree_dirty": False,
            },
        ), patch(
            "governance.source_file",
            return_value=(source_path, "manuscript/chapter.md"),
        ):
            result = reconcile_failed_commit_batch(db, "batch-failed")

        self.assertTrue(result["reconciled"])
        self.assertEqual(result["commit_sha"], "abc123")
        db.update_approval_batch.assert_called_once()


class TestGitEngineAndSynchronization(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.temp_dir, ignore_errors=True))
        self.repo_dir = Path(self.temp_dir) / "test_repo"
        self.repo_dir.mkdir(parents=True)
        # Initialize a real git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Author"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "author@test.com"], cwd=str(self.repo_dir), check=True, capture_output=True)

        self.manuscript_dir = self.repo_dir / "manuscript"
        self.manuscript_dir.mkdir()
        self.chapter_file = self.manuscript_dir / "chapter_01.md"
        self.chapter_file.write_text(
            "# 1 | The Awakening\n\n### 1.1 First Dawn\n\nMorning light cracked across the frozen basin.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial manuscript commit"], cwd=str(self.repo_dir), check=True, capture_output=True)

        rev_parse = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.repo_dir), check=True, capture_output=True, text=True)
        self.initial_sha = rev_parse.stdout.strip()

        # Initialize StudioDB
        self.db_path = Path(self.temp_dir) / "studio.sqlite"
        self.db = StudioDB(str(self.db_path), project_id="git-test-proj")
        self.db.update_source_metadata(
            source_root=str(self.repo_dir),
            source_subpath="manuscript",
            branch="main",
            commit_sha=self.initial_sha,
            source_digest="digest-test",
            snapshot_path="snap.json",
            manifest_path="manifest.json",
            working_tree_dirty=False,
        )
        # Import repository
        self.scenes = self.db.import_repository_scenes([
            {"path": "manuscript/chapter_01.md", "text": self.chapter_file.read_text(encoding="utf-8")}
        ])

    def test_git_advanced_detection_blocks_desk_push(self):
        import subprocess
        self.assertEqual(len(self.scenes), 1)
        scene = self.scenes[0]

        # Author in Studio modifies scene in DB
        self.db.update_scene(scene["scene_number"], "<p>Morning light broke violently across the frozen basin.</p>")

        # Meanwhile, an external commit is made to the git repo touching chapter_01.md
        self.chapter_file.write_text(
            "# 1 | The Awakening\n\n### 1.1 First Dawn\n\nExternal collaborator edit.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "commit", "-am", "External author commit"], cwd=str(self.repo_dir), check=True, capture_output=True)

        # Attempting desk_push should raise GovernanceError with code 'git_advanced'
        with self.assertRaises(GovernanceError) as ctx:
            push_desk_to_git(self.db)
        self.assertEqual(ctx.exception.code, "git_advanced")
        self.assertIn("newer commits", str(ctx.exception))

    def test_git_pull_updates_scenes_non_destructively(self):
        import re
        import subprocess
        scene = self.scenes[0]
        # Find the block id assigned to the paragraph in content_html
        match = re.search(r'data-id="([^"]+)"', scene["content_html"])
        paragraph_id = match.group(1) if match else "p-1"
        if not match:
            # If no data-id, update scene with an id
            paragraph_id = "test-para-id"
            self.db.update_scene(scene["scene_number"], f'<p data-id="{paragraph_id}">Morning light cracked across the frozen basin.</p>')

        # Attach an annotation to scene 1
        self.db.create_annotation(
            scene_number=scene["scene_number"],
            paragraph_id=paragraph_id,
            quote_text="frozen basin",
            body="Check historical resonance here.",
        )
        annotations_before = self.db.list_annotations(scene["scene_number"])
        self.assertEqual(len(annotations_before), 1)

        # External author makes a commit
        self.chapter_file.write_text(
            "# 1 | The Awakening\n\n### 1.1 First Dawn\n\nExternal updated prose pulled from remote branch.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "commit", "-am", "External branch update"], cwd=str(self.repo_dir), check=True, capture_output=True)

        # Pull from git
        result = pull_desk_from_git(self.db)
        self.assertEqual(result["status"], "pulled")
        self.assertEqual(result["total_scenes"], 1)

        # Verify scene was updated in DB
        updated_scene = self.db.get_scene(scene["scene_number"])
        self.assertIn("External updated prose pulled from remote branch.", updated_scene["content_html"])

        # Verify annotation was NOT wiped out
        annotations_after = self.db.list_annotations(scene["scene_number"])
        self.assertEqual(len(annotations_after), 1)
        self.assertEqual(annotations_after[0]["body"], "Check historical resonance here.")

        # Verify revision history has git_pull entry
        history = self.db.scene_history(scene["scene_number"])
        self.assertTrue(any(rev.get("source") == "git_pull" for rev in history))

    def test_author_direct_commit(self):
        import subprocess
        scene = self.scenes[0]
        self.db.update_scene(scene["scene_number"], "<p>Author typed new direct prose in studio.</p>")

        result = commit_scene_to_git(self.db, scene["scene_number"], message="Author direct commit test")
        self.assertTrue(result["committed"])
        self.assertEqual(result["scene_number"], scene["scene_number"])

        # Check git commit log
        log = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=str(self.repo_dir), check=True, capture_output=True, text=True)
        self.assertIn("Author direct commit test", log.stdout)

        # Check file contents on disk
        disk_content = self.chapter_file.read_text(encoding="utf-8")
        self.assertIn("Author typed new direct prose in studio.", disk_content)


if __name__ == "__main__":
    unittest.main()
