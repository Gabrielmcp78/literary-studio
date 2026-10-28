# Remote Literary Studio MCP transport

This feature adds a **local** Streamable HTTP transport to the existing
`backend/mcp_server.py` tools without changing the stdio bridge or the
Literary Studio approval path.

## Security boundary

- Bind ONLY to `127.0.0.1` (default port `8794`).
- A dedicated, unpredictable `LITERARY_STUDIO_MCP_TOKEN` of at least 32
  characters is mandatory. The service fails closed without it.
- Do **not** expose port `8794` with Tailscale Funnel or an unauthenticated
  reverse proxy.
- The existing public OAuth gateway must authenticate the remote client,
  authorize Studio access, **replace** the client's Authorization header
  with the private upstream token, and only then proxy to the local transport.
  A raw Tailscale Serve path proxy alone does not provide that OAuth step.
- This transport does not issue OAuth tokens or provide OAuth discovery
  metadata. Those remain the responsibility of the existing gateway.
- The bridge exposes the existing 12 Studio tools: reads, margin questions,
  source-pinned proposal preparation, and conflict checks. **No proposal
  acceptance, Git write-back, or commits** are exposed through MCP.
- The `/api` FastAPI service at `127.0.0.1:8000` stays private.

## Local installation (Mac mini)

Use your **local** checkout after inspecting the proposed branch changes.
Do not pull this feature directly into your potentially dirty `main` worktree.

```bash
cd /Volumes/Ready500/DEVELOPMENT/literary-studio
git fetch origin feature/studio-remote-mcp-transport
git worktree add ../literary-studio-mcp-test origin/feature/studio-remote-mcp-transport
cd ../literary-studio-mcp-test/backend
# Existing managed virtualenv may live in the original checkout:
PYTHON=/Volumes/Ready500/DEVELOPMENT/literary-studio/backend/venv/bin/python
"$PYTHON" -m unittest -v test_mcp_http_server
export LITERARY_STUDIO_MCP_TOKEN="$(openssl rand -hex 32)"
"$PYTHON" mcp_http_server.py
```

The backend FastAPI service at port 8000 must already be running; the transport
uses the same `LITERARY_STUDIO_API_URL` setting as the stdio bridge.

For a **second** terminal, provide the same private token without pasting it
into chats or saving it in the source checkout, then:

```bash
curl -i http://127.0.0.1:8794/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $LITERARY_STUDIO_MCP_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

The expected response lists `studio_list_projects`,
`studio_get_project`, `studio_get_story_map`, `studio_read_scene`,
`studio_prepare_for_approval` and the remaining bridge tools.

Without Authorization, the endpoint must return **401**.

## Existing gateway integration (NOT automatically configured)

1. Leave the current Funnel routes (`/mcp`, `/fiction`, OAuth metadata,
   and the `8443` tailnet-only Studio UI) unchanged.
2. Configure a distinct OAuth-protected public resource such as
   `/studio/mcp` on the existing gateway.
3. After validating the inbound token and permissions, have the gateway
   replace the inbound Authorization header with
   `Bearer $LITERARY_STUDIO_MCP_TOKEN` and proxy to
   `http://127.0.0.1:8794/mcp`. This must be performed by an
   authenticated gateway with header rewriting, **not** by a plain
   `tailscale serve` mapping.
4. Publish accurate OAuth protected-resource metadata for the new Studio
   resource and use the same established OAuth issuance/authorization flow.
5. Register the resulting MCP URL as a **separate Literary Studio connector**
   in ChatGPT, with user approval for connection.
6. Verify, in order: auth rejection, `initialize`, `tools/list`,
   `studio_list_projects`, `studio_get_project`, `studio_read_scene`,
   `studio_list_pending_proposals` and stale-source protection.
7. Stage no proposals without an explicit user request; approvals remain
   in Literary Studio at `http://localhost:3000`.

**Never assert the remote connector is operational until all tests pass.**

## Interface notes

- `POST /mcp` and `POST /studio/mcp`: sessionless JSON-RPC 2.0.
- `GET /mcp`: 405, because this server does not provide SSE subscriptions.
- `GET /health`: requires the same bearer token.
- Supported request protocol versions: `2024-11-05`, `2025-03-26`,
  `2025-06-18`. Tools use the existing Literary Studio stdio bridge.
- Browser Origins are rejected unless listed in
  `LITERARY_STUDIO_MCP_ALLOWED_ORIGINS` (comma separated).
- The ingress should impose its own request size, rate and duration limits,
  especially for AI proposal-preparation calls.
