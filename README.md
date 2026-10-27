# Literary Studio

A local editorial studio for novel manuscripts kept in Git. It imports a
manuscript from a Markdown checkout, gives you an editor, story map, persona
panels and interrogation chat, and routes every AI-suggested change through an
**Approval Desk** that you control. Approved changes are written back to your
Markdown on the pinned branch and committed, one commit per batch.

## Architecture

```
 IDE / desktop agent ──MCP (stdio)──▶ backend/mcp_server.py ─┐
                                                              │ HTTP (127.0.0.1:8000)
 Browser ── Next.js frontend (:3000) ─────────────────────────┤
                                                              ▼
                                       FastAPI  backend/main.py
             ┌───────────────┬──────────────┬─────────────────┼────────────────┐
     project_manager   approval_bridge   governance        studio_db       ai_provider
     (registry, ids)   (prepare batch)   (validate,        (per-project    (Gemini → AGY →
                                          stale checks,     SQLite)          Apple FM → Ollama)
                                          write-back, Git)
                                                │
                                 your manuscript Git checkout (Markdown)
                                 FictionForce prose_law.py (shared rules)
```

- **One SQLite file per project** under `build_artifacts/projects/`. Scenes are
  stored as HTML rendered from the Markdown source, with paragraph `data-id`s.
- **The Approval Desk is the only gate.** Agents can prepare proposals and ask
  questions; only the author accepts or rejects. See `docs/GOVERNANCE.md`.
- **Shared registry.** Studio ids, titles, and FictionForce project names all
  resolve to the same project (matched by manuscript root).

## Setup

```bash
cd backend
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# Copy .env.example to .env and enter GEMINI_API_KEY there.
venv/bin/uvicorn main:app --reload --port 8000

cd ../frontend
npm install
npm run dev          # http://localhost:3000
```

Register the MCP bridge with Claude Desktop or an IDE: see
`docs/IDE_APPROVAL_BRIDGE.md`. Restart the client after changing the bridge.

## Configuration

### Launch from a macOS icon

Double-click `Literary Studio.app` in this repository, or drag it to the Dock.
It starts the backend and frontend from the checked-out environment, waits for
both health checks, and opens `http://127.0.0.1:3000/`. No IDE or interactive
terminal is required. Runtime logs and PID files are kept in `.runtime/`.

The app reads provider keys from `backend/.env`; that file remains local and is
never copied into the bundle. To stop the services without opening a terminal,
run `scripts/stop_literary_studio.zsh` from a Finder shortcut or use Activity
Monitor for the two processes.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | unset | Enables Gemini, the primary provider. Without it the chain falls back to AGY. |
| `OPENROUTER_API_KEY`, `LITERARY_STUDIO_OPENROUTER_MODEL` | unset, `nvidia/nemotron-3-ultra-550b-a55b:free` | OpenRouter provider and model; used after Gemini and before Ollama. |
| `LITERARY_STUDIO_APPROVAL_MODEL` | `gemini-3.1-pro-preview` | Model for approval preparation. |
| `LITERARY_STUDIO_AGY_BIN`, `LITERARY_STUDIO_AGY_MODEL` | `agy` on PATH, `claude-opus-5-5-high` | AGY fallback and model selection. |
| `LITERARY_STUDIO_OLLAMA_MODEL`, `LITERARY_STUDIO_OLLAMA_NUM_CTX` | `gpt-oss:120b-cloud`, `131072` | Ollama model and context size; preferred after Gemini. |
| `LITERARY_STUDIO_CARD_CONTEXT_CHARS` | `200000` | Maximum supporting-card context; manuscript prose is not truncated by this setting. |
| `LITERARY_STUDIO_ROLEPLAY_HISTORY_CHARS` | `64000` | Recent roleplay-memory budget; durable history remains stored. |
| `FICTIONFORCE_ROOT` | `/Volumes/Ready500/DEVELOPMENT/fiction-git-system` | Where prose law is loaded from. |
| `FICTIONFORCE_CONFIG` | `$FICTIONFORCE_ROOT/fiction.config.json` | Registry join with FictionForce. |
| `LITERARY_STUDIO_PROSE_LAW` | `on` | `off` disables prose-law enforcement. |
| `LITERARY_STUDIO_API_URL`, `LITERARY_STUDIO_TIMEOUT` | `http://127.0.0.1:8000`, `600` | MCP bridge target and timeout (s). |

## External dependencies

- Git on PATH (snapshots, write-back, commits). Your repo's own hooks and
  identity are respected; write-back commits use your `user.name`/`user.email`.
- Optional: FictionForce checkout (prose law, registry), AGY CLI, Apple
  FoundationModels via FModCLI, Ollama.
- `docker-compose.yml` (Postgres/pgvector, Neo4j) is not used by the current
  backend.

## Usage flow

1. Create a project and snapshot a checkout:
   `POST /api/projects` then `POST /api/projects/{id}/source-snapshot`
   with `source_root` and `source_subpath`.
2. Prepare changes from an agent (`studio_prepare_for_approval`, optionally
   with `panel_id`) or the terminal (`backend/approval_cli.py`).
3. Review on the Approval Desk: each card shows the original, the replacement,
   warnings, and whether accepting writes to your Markdown.
4. When the batch's last proposal is reviewed, accepted edits become one
   commit on the pinned branch with `Studio-Batch` / `Studio-Proposal` trailers.

## Data safety

- Write-back never touches a file with your own uncommitted edits; it offers a
  Studio-only accept instead.
- Back up `build_artifacts/projects/*.sqlite3`; they are runtime state and are
  not in Git.
- New scratch pages are stored under
  `build_artifacts/projects/<project-id>/scratch/`, which is ignored runtime
  state outside the linked manuscript checkout. To make prose canonical, copy
  or move it into an actual chapter Markdown file; chapter-linked files remain
  subject to the normal source and Approval Desk workflow.
- Governance also provides an explicit `Override dirty-tree guard` action. It
  requires a second confirmation and commits only files changed by the saved
  Studio desk; unrelated dirty files remain untouched. If an approved batch's
  source file was already dirty, the Approval Desk offers the same override,
  with a warning that same-file edits will be included. These actions create
  local commits on the current branch; they do not silently push to a remote
  or merge branches.
