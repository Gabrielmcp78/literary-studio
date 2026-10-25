# Governance: validation, staleness, write-back

Implemented in `backend/governance.py`; endpoints in `backend/main.py`.

## 1. Validation (at preparation)

A candidate proposal is staged only if all of these hold; otherwise it is
returned in the batch's `rejected` list with reasons:

| Check | Rule |
|---|---|
| Paragraph | The original text has no line break; the replacement adds no paragraph break. |
| Unique | The original appears exactly once in the scene's text. |
| Formatting | The original maps to the scene HTML exactly once (it does not straddle italics, bold, or a link). |
| Balance | `“”`, `()`, `[]` balance and straight-quote parity are unchanged by the replacement. |
| Prose law | No new **error** from FictionForce `prose_law.new_findings` (binary-negative, em-dash limit, French spacing). New **warnings** (fragment triple, interiority frame) are attached, not blocking. |

Each staged proposal stores `validation`: `errors`, `warnings`, `prose_law`,
`source_path`, and `writeback` (`available`, `path` or `reason`). Write-back is
available when the original also appears exactly once in the Markdown file.

## 2. Staleness (per passage)

`POST /api/projects/{id}/approval-batches/{batch}/check` and every accept
re-verify each staged proposal:

| Kind | Blocking | Meaning |
|---|---|---|
| `branch_changed` | yes | The checkout is no longer on the batch's branch. |
| `scene_missing` | yes | The scene was removed. |
| `passage_changed` | yes | The original no longer appears exactly once in the Studio scene. |
| `source_passage_changed` | yes | The file changed and the original is no longer unique in it. |
| `commit_moved` | no | New commits landed; passages were re-verified. |
| `file_changed_passage_intact` | no | The file changed elsewhere; this passage is fine. |

Accepting one proposal no longer invalidates its siblings in the same scene.

## 3. Write-back and commits

`POST /api/projects/{id}/governance/proposals/{proposal}/review`

Body: `{"action": "accept" | "reject", "studio_only": false}`
(`target_branch` is accepted and ignored.)

On accept, in order:

1. Re-check this proposal's passage (`409 stale` if blocked).
2. Unless `studio_only`, patch the Markdown file atomically. Refused with
   `409 writeback_blocked` (`reason`: `branch_changed`, `source_dirty`,
   `anchor_not_in_source`, `no_source`, `no_batch`) and `studio_only_available: true`.
   `source_dirty` means the file has uncommitted edits that are not this
   batch's own earlier write-backs.
3. Update the Studio scene (version + revision + telemetry). If this fails the
   file is restored.
4. When no proposals remain staged, commit the batch's files with
   `git commit --only` (other staged or unstaged work is untouched). Message:
   subject, request, one line per edit, then `Studio-Batch:` and
   `Studio-Proposal:` trailers.

Response: `new_status`, `writeback_status` (`source_and_studio` |
`studio_only`), `source_file`, `batch_commit`, `message`.

Batch status: `pending` → `committed` | `closed` (no source changes) |
`commit_failed` (reason in `writeback.commit_error`).

`POST /api/projects/{id}/approval-batches/{batch}/commit` retries a failed
commit or commits early.

## Other endpoints

- `GET /api/registry`: Studio projects with `aliases` and
  `fictionforce_project`, plus `fictionforce_only` projects.
- `GET /api/projects/{id}/governance/history?status=&limit=`: reviewed
  proposals, newest first, with `batch_request_text`.
- Any `{id}` accepts a Studio id, title, slug, or FictionForce name.

## Provenance

Each batch's `result.provenance` records the provider and model that ran,
`fallback_reasons` for skipped providers, `persona_ids`, `panel_id`, and
`prose_law` status.
