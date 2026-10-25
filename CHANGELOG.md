# Changelog

## 1.1.0 — 2026-10-24

### Fixed
- Accepting one proposal no longer makes the others in the same scene stale;
  staleness is now judged per passage.
- Editing an unrelated chapter no longer blocks a batch; per-file fingerprints
  plus passage checks replace the whole-manuscript digest.
- Proposals that drop a closing quotation mark, repeat elsewhere in the scene,
  cross paragraphs, or straddle italics are rejected before reaching the desk.
- Review no longer reports a fictional `main` branch.
- Telemetry no longer counts the manuscript import as words written
  (`imported_words` is reported separately).
- Provider provenance records the real model and why Gemini was skipped.
- MCP bridge: unencoded ids crashed the server; any error now returns a
  readable tool error and the server stays up.
- Alias URLs no longer write snapshots or PDF exports into the wrong folder.

### Added
- Write-back: accepted edits patch the Markdown source on the pinned branch;
  each batch becomes one commit with `Studio-Batch` / `Studio-Proposal`
  trailers. Files with the author's own uncommitted edits are never touched;
  a Studio-only accept is offered instead.
- Prose-law enforcement shared with FictionForce.
- Shared project registry with FictionForce (`GET /api/registry`); every
  endpoint accepts Studio ids, titles, slugs, or FictionForce names.
- Persona panels for preparation (`panel_id`), with per-editor attribution.
- Proposal history endpoint; batch summary, questions, and rejections persist.
- Paragraph `data-id`s on import, and a text-neutral backfill for existing
  scenes, so annotations can anchor anywhere.
- Approval Desk shows the original and replacement, warnings, and the
  write-back target; reloads the open scene after an accept; refuses to accept
  into a scene with unsaved edits.
- MCP bridge 1.2.0: `studio_list_projects`, `studio_get_project`,
  `studio_get_story_map`, `studio_read_scene`, `studio_list_personas`,
  `studio_list_annotations`, `studio_add_question`,
  `studio_get_proposal_history`; `panel_id` on preparation.
- README, governance reference, requirements.txt; the backend is under Git.
