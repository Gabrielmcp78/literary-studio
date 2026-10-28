/**
 * Literary Studio's public OAuth-protected MCP facade.
 *
 * This process is deliberately separate from SecondBrain (3456) and
 * FictionForce (4343). The Studio FastAPI service (8000) is NEVER exposed.
 *
 * Imports the already-installed, established FictionForce OAuth module,
 * but uses isolated Studio OAuth tokens and a distinct resource URI.
 */
import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const HOST = '127.0.0.1';
const PORT = Number.parseInt(process.env.STUDIO_GATEWAY_PORT || '4344', 10);
const BASE = '/studio';
const PUBLIC_ORIGIN = (process.env.STUDIO_PUBLIC_ORIGIN || 'https://gabrielsmini.tail26536d.ts.net').replace(/\/+$/, '');
const UPSTREAM = 'http://127.0.0.1:8794/mcp';
const HOME = process.env.HOME;
const FF_DIR = process.env.FICTIONFORCE_MCP_DIR ||
  '/Volumes/Ready500/DEVELOPMENT/gm-mcp-hub/servers/fictionforce-mcp-server';
const OAUTH_MODULE = path.join(FF_DIR, 'oauth.js');

if (!HOME) throw new Error('HOME is required for persistent OAuth secrets');
if (!fs.existsSync(OAUTH_MODULE)) {
  throw new Error(`FictionForce OAuth module not found: ${OAUTH_MODULE}`);
}

const upstreamTokenFile = process.env.STUDIO_UPSTREAM_TOKEN_FILE ||
  path.join(HOME, '.literary-studio-upstream-token');
const upstreamToken = fs.readFileSync(upstreamTokenFile, 'utf8').trim();
if (upstreamToken.length < 32) {
  throw new Error('Studio upstream token must be at least 32 characters');
}

const { createOAuth } = await import(pathToFileURL(OAUTH_MODULE).href);
const app = express();
app.disable('x-powered-by');
app.use(express.json({ limit: '1mb' }));

// Do not reuse SecondBrain or FictionForce credentials or OAuth token stores.
const oauth = await createOAuth({
  publicOrigin: PUBLIC_ORIGIN,
  basePath: BASE,
  mcpPath: `${BASE}/mcp`,
  scopes: ['studio:read', 'studio:propose'],
  displayName: 'Literary Studio',
  consentBlurb: 'Read Literary Studio projects, source-pinned scenes and editorial notes; prepare author-reviewable proposals. This connection cannot approve, apply or commit manuscript changes.',
  staticToken: process.env.STUDIO_OWNER_TOKEN,
  tokenFile: path.join(HOME, '.literary-studio-mcp-owner-token'),
  accessTokensFile: path.join(HOME, '.literary-studio-mcp-oauth-tokens.json'),
});
app.use(oauth.router);

// The incoming OAuth token must never be forwarded to the upstream Python MCP.
// Only this gateway supplies the private upstream bearer credential.
app.post(`${BASE}/mcp`, oauth.requireAuthForMcp, async (req, res) => {
  try {
    const headers = {
      'authorization': `Bearer ${upstreamToken}`,
      'content-type': 'application/json',
      'accept': 'application/json, text/event-stream',
    };
    if (req.get('mcp-protocol-version')) {
      headers['mcp-protocol-version'] = req.get('mcp-protocol-version');
    }
    const upstream = await fetch(UPSTREAM, {
      method: 'POST',
      headers,
      body: JSON.stringify(req.body),
      signal: AbortSignal.timeout(600000),
    });
    res.status(upstream.status);
    if (upstream.headers.get('content-type')) {
      res.set('Content-Type', upstream.headers.get('content-type'));
    }
    res.set('Cache-Control', 'no-store');
    res.send(Buffer.from(await upstream.arrayBuffer()));
  } catch (err) {
    console.error('[Studio MCP] upstream request failed:', err.message);
    if (!res.headersSent) res.status(502).json({
      jsonrpc: '2.0', id: req.body?.id ?? null,
      error: { code: -32000, message: 'Literary Studio upstream unavailable' },
    });
  }
});

// No SSE subscriptions: the local Studio bridge is sessionless JSON-RPC.
app.get(`${BASE}/mcp`, oauth.requireAuth, (_req, res) => {
  res.set('Allow', 'POST').status(405).end();
});
app.delete(`${BASE}/mcp`, oauth.requireAuth, (_req, res) => {
  res.set('Allow', 'POST').status(405).end();
});
app.get(`${BASE}/health`, oauth.requireAuth, (_req, res) => {
  res.json({ status: 'ok', server: 'literary-studio-mcp-gateway',
    upstream: '127.0.0.1:8794', approval: 'author-only' });
});

app.listen(PORT, HOST, () => {
  console.log(`Literary Studio OAuth MCP gateway: http://${HOST}:${PORT}${BASE}/mcp`);
  console.log(`Public resource after Funnel configuration: ${PUBLIC_ORIGIN}${BASE}/mcp`);
});
