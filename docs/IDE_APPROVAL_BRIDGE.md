# IDE approval bridge

Literary Studio exposes author-controlled proposal preparation for a local IDE
or desktop agent. The bridge reads the selected project's imported manuscript,
asks the configured AI provider to turn an author request into exact proposals,
and places only source-pinned proposals in the Approval Desk. It does not edit
the Git checkout.

## MCP configuration

Start Literary Studio's FastAPI service on port 8000, then register this stdio
server with the IDE or desktop agent that supports MCP:

```json
{
  "mcpServers": {
    "literary-studio": {
      "command": "/Volumes/Ready500/DEVELOPMENT/literary-studio/backend/venv/bin/python",
      "args": [
        "/Volumes/Ready500/DEVELOPMENT/literary-studio/backend/mcp_server.py"
      ],
      "env": {
        "LITERARY_STUDIO_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

The available tools (bridge v1.2.0) are:

Read-only orientation:

- `studio_list_projects`: project ids, aliases (including FictionForce names), branch, commit, dirty state. Call first. Any tool's `project_id` accepts an id, title, or FictionForce name.
- `studio_get_project`: full source pinning for one project.
- `studio_get_story_map`: chapters → scenes with word counts, pressure scores, tracked entities.
- `studio_read_scene`: one scene as plain text, each paragraph prefixed with its paragraph id.
- `studio_list_personas`: personas and panels usable as `persona_ids`.
- `studio_list_annotations`: the author's margin notes on a scene.
- `studio_get_proposal_history`: what the author accepted and rejected, to learn their taste.

Author-facing, non-editing:

- `studio_add_question`: leave a margin question on one paragraph when a change needs the author's decision.

Approval workflow:

- `studio_prepare_for_approval`: prepare validated proposals from an author request; `panel_id` runs a persona panel in one batch.
- `studio_list_pending_proposals`: list the project's staged proposals.
- `studio_get_approval_batch`: retrieve a batch and its evidence baseline.
- `studio_check_approval_conflicts`: verify that manual/Git edits have not made a batch stale.

Accepting or rejecting proposals is intentionally not exposed: the Approval
Desk is the author's gate. Validation, staleness, and write-back rules are in
`docs/GOVERNANCE.md`.

Errors (unknown project, API down, bad input) come back as MCP tool results with
`isError: true` and a readable message; the server stays running. Set
`LITERARY_STUDIO_TIMEOUT` (seconds, default 600) for long preparation runs.

Example agent request:

> In project `string-theories`, prepare the requested continuity and subtext
> changes for Chapters 13–16. Include plot, character, subtext, causality, and
> documentation context. Do not edit the manuscript.

The agent should call `studio_prepare_for_approval` with `chapter_numbers: [13,
14, 15, 16]` and the requested `include` values.

## Terminal fallback

```bash
backend/venv/bin/python backend/approval_cli.py string-theories \
  "Prepare continuity and subtext changes for approval." \
  --chapters 13 14 15 16 \
  --include plot character subtext causality documentation
```

The response contains an `approval_batch_id`, baseline branch/commit/digest,
questions, and staged proposal IDs. Review and approve proposals in Literary
Studio. If you edit a scene or the source checkout after preparation, approval
is blocked until the proposal is re-prepared or explicitly reconciled.
