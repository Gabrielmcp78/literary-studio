# Literary Studio MCP: isolated OAuth gateway

**Do not edit SecondBrain's running server on port 3456.** That process is
a SecondBrain-specific OAuth + MCP service, not a generic reverse proxy.

This gateway follows the already-working FictionForce auth pattern, using
its existing `oauth.js` implementation but **separate Studio OAuth token
files and scopes**.

Architecture:

```text
ChatGPT
  -> HTTPS Tailscale Funnel :443 /studio/mcp
  -> localhost:4344 (Studio OAuth gateway)
  -> localhost:8794/mcp (Studio bearer-protected HTTP adapter)
  -> localhost:8000 (Studio FastAPI)
  -> Literary Studio approval queue (author approves in localhost:3000)
```

Never expose 8794 or 8000 to Funnel. Leave port 8443 (tailnet-only
Studio web UI), SecondBrain (:3456), and FictionForce (:4343) untouched.

## Installation in an isolated worktree

```bash
cd /Volumes/Ready500/DEVELOPMENT/literary-studio
git fetch origin feature/studio-remote-mcp-transport
git worktree add --detach ../literary-studio-mcp-gateway-test origin/feature/studio-remote-mcp-transport
cd ../literary-studio-mcp-gateway-test/integrations/studio-mcp-gateway
npm install --no-audit --no-fund
npm run check
```

## Private upstream secret

The Python adapter on :8794 needs a stable private Bearer secret.
Do this only in a local terminal. Do not paste secret contents into chat.

```bash
umask 077
test -f "$HOME/.literary-studio-upstream-token" ||
  openssl rand -hex 32 > "$HOME/.literary-studio-upstream-token"
chmod 600 "$HOME/.literary-studio-upstream-token"
```

**Restart the existing Python adapter on :8794 with the same token.**
Inspect its PID and command before stopping the existing process.
For example, if its current process has been stopped safely:

```bash
cd /Volumes/Ready500/DEVELOPMENT/literary-studio-mcp-test/backend
export LITERARY_STUDIO_MCP_TOKEN="$(cat "$HOME/.literary-studio-upstream-token")"
./venv/bin/python mcp_http_server.py
```

If the test worktree has no `venv`, use the original managed Python:

```bash
/Volumes/Ready500/DEVELOPMENT/literary-studio/backend/venv/bin/python mcp_http_server.py
```

The Python service binds only to `127.0.0.1:8794`.

Test the adapter locally:

```bash
curl -i http://127.0.0.1:8794/mcp \
  -H "Authorization: Bearer $(cat "$HOME/.literary-studio-upstream-token")" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

This must return `200` with JSON and tools including `studio_list_projects`
and `studio_read_scene`. Never paste the Bearer token or verbose curl
headers into public locations.

## Start the OAuth gateway (separate terminal)

```bash
cd /Volumes/Ready500/DEVELOPMENT/literary-studio-mcp-gateway-test/integrations/studio-mcp-gateway
node index.mjs
```

Expected: `http://127.0.0.1:4344/studio/mcp` is listening.

The gateway requires `gm-mcp-hub/servers/fictionforce-mcp-server/oauth.js`
to be present and uses that established library. It creates new files
`~/.literary-studio-mcp-owner-token` and
`~/.literary-studio-mcp-oauth-tokens.json`. Those are independent of
FictionForce and SBrain credentials. The owner token is used to authorize
ChatGPT on Studio's own consent screen.

Check unauthenticated discovery on :4344:

```bash
curl -i http://127.0.0.1:4344/studio/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Expected: `200` JSON containing the Studio tools (OAuth allows discovery,
not privileged calls). A `tools/call` without OAuth must return `401`.
If tools/list returns `502`, the Python upstream or secret is wrong.

## Tailscale Funnel routes

**Only after** :4344 has passed the local tests, add path-specific Funnel
routes. These commands do not change existing paths when used with
`--set-path`; verify the status after each command.

```bash
tailscale funnel --bg --https=443 --set-path=/studio http://127.0.0.1:4344/studio
tailscale funnel --bg --https=443 --set-path=/.well-known/oauth-protected-resource/studio/mcp http://127.0.0.1:4344/.well-known/oauth-protected-resource/studio/mcp
tailscale funnel --bg --https=443 --set-path=/.well-known/oauth-authorization-server/studio http://127.0.0.1:4344/.well-known/oauth-authorization-server/studio
tailscale funnel status
```

If a version of Tailscale rejects these path mappings, stop and inspect
`tailscale funnel --help` rather than resetting the Funnel configuration.
**Never use `tailscale serve reset` or `tailscale funnel reset`** for
this deployment.

Public Studio resource:

`https://gabrielsmini.tail26536d.ts.net/studio/mcp`

At that URL:

- Unauthenticated `POST tools/list` returns Studio tool schemas as JSON.
- Unauthenticated `POST tools/call` returns OAuth challenge (401).
- Studio OAuth authorization lives under `/studio/oauth/...`.
- Studio's own `tools/call` requests are forwarded to the private Python
  adapter with the separate upstream token.
- The Studio toolset contains no acceptance, edit-application or commit tools.

Verify public discovery **before** reconnecting ChatGPT:

```bash
curl -i 'https://gabrielsmini.tail26536d.ts.net/studio/mcp' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

The response must be JSON (not the OpenClaw HTML page).
Only after that should the ChatGPT Studio connection be reconnected.

## Limitations

This is a draft integration. It has not been run against the live Mac gateway
or deployed to Funnel by the assistant. It is not a replacement for
Literary Studio's internal author-controlled Approval Desk.

Do not share any saved owner or upstream token in conversation or logs.
