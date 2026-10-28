"""Sessionless HTTP transport for the existing Literary Studio MCP bridge.

This is a LOCAL upstream for an authenticated OAuth gateway, not a public
FastAPI or an OAuth authorization server. Run on 127.0.0.1 only. Never route
Tailscale Funnel directly to this listener without an authenticated ingress.

The legacy stdio transport and its tool dispatcher remain unchanged.
"""
from __future__ import annotations

import hmac
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mcp_server import handle

app = FastAPI(
    title="Literary Studio MCP transport",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

MAX_BODY_BYTES = 1_048_576
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def _valid_token(authorization: str | None) -> bool:
    """Fail closed unless a sufficiently long secret is configured."""
    secret = os.environ.get("LITERARY_STUDIO_MCP_TOKEN", "")
    if len(secret) < 32 or not authorization:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    return (
        separator == " "
        and scheme.lower() == "bearer"
        and hmac.compare_digest(supplied, secret)
    )


def _json_error(status: int, code: int, message: str, request_id=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"jsonrpc": "2.0", "id": request_id,
                 "error": {"code": code, "message": message}},
    )


def _origin_allowed(origin: str | None) -> bool:
    """Origin-less nonbrowser MCP clients are allowed; browser origins opt in."""
    if origin is None:
        return True
    allowed = set(
        x.strip()
        for x in os.environ.get("LITERARY_STUDIO_MCP_ALLOWED_ORIGINS", "").split(",")
        if x.strip()
    )
    return origin in allowed


def dispatch(payload: object) -> dict | None:
    """Reuse the exact stdio tool catalogue and dispatcher."""
    if not isinstance(payload, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32600, "message": "Invalid Request"}}
    response = handle(payload)
    if payload.get("method") == "initialize" and isinstance(response, dict):
        requested = (payload.get("params") or {}).get("protocolVersion")
        if requested in SUPPORTED_PROTOCOLS:
            response["result"]["protocolVersion"] = requested
    return response


@app.get("/health")
async def health(request: Request):
    if not _valid_token(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return {"status": "ok", "transport": "streamable-http", "approval": "studio-only"}


@app.post("/mcp")
@app.post("/studio/mcp")
async def mcp(request: Request):
    if not _valid_token(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    if not _origin_allowed(request.headers.get("origin")):
        return _json_error(403, -32000, "Origin not allowed")
    if "application/json" not in request.headers.get("content-type", "").lower():
        return _json_error(415, -32600, "Expected application/json")
    if "application/json" not in request.headers.get("accept", "application/json").lower() and "*/*" not in request.headers.get("accept", ""):
        return _json_error(406, -32600, "Accept must include application/json")
    if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > MAX_BODY_BYTES:
        return _json_error(413, -32600, "MCP request too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _json_error(413, -32600, "MCP request too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_error(400, -32700, "Parse error")
    result = dispatch(payload)
    if result is None:
        return Response(status_code=202)
    return JSONResponse(content=result)


@app.get("/mcp")
@app.get("/studio/mcp")
async def unsupported_stream():
    return Response(status_code=405, headers={"Allow": "POST"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LITERARY_STUDIO_MCP_PORT", "8794")))
