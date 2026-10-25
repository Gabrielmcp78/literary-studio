"""Stdio MCP bridge for IDE and desktop-agent integrations.

The bridge talks only to the local Literary Studio FastAPI service. It never
edits a Git checkout or scene text; its only write is preparing source-pinned
governance proposals, which wait in the Approval Desk for the author.
Accepting or rejecting proposals is deliberately not exposed here: the desk
is the author's gate.

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0.
Spec: https://modelcontextprotocol.io/specification/2024-11-05

Imports (all Python standard library, no extra dependencies):
- json: encode/decode JSON-RPC messages and API payloads.
  https://docs.python.org/3/library/json.html
- os: read LITERARY_STUDIO_API_URL / LITERARY_STUDIO_TIMEOUT overrides.
  https://docs.python.org/3/library/os.html
- sys: stdio transport for MCP messages and stderr diagnostics.
  https://docs.python.org/3/library/sys.html
- traceback: log unexpected failures to stderr without killing the server.
  https://docs.python.org/3/library/traceback.html
- html.parser.HTMLParser: convert stored scene HTML to paragraph-addressed
  plain text so agents can cite paragraph ids.
  https://docs.python.org/3/library/html.parser.html
- urllib.parse.quote: percent-encode ids before they enter URL paths; an
  unencoded space previously crashed the whole server.
  https://docs.python.org/3/library/urllib.parse.html#urllib.parse.quote
- urllib.request / urllib.error: HTTP client for the local API.
  https://docs.python.org/3/library/urllib.request.html
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SERVER_VERSION = "1.1.0"
API_BASE = os.getenv("LITERARY_STUDIO_API_URL", "http://127.0.0.1:8000").rstrip("/")
# Proposal preparation calls the configured AI provider and can run long.
API_TIMEOUT = float(os.getenv("LITERARY_STUDIO_TIMEOUT", "600"))

_PROJECT = {"project_id": {"type": "string", "description": "Literary Studio project id (see studio_list_projects)."}}
_BATCH = {"batch_id": {"type": "string", "description": "Approval batch id returned by studio_prepare_for_approval."}}

TOOLS = [
    {
        "name": "studio_list_projects",
        "description": "List Literary Studio projects with their ids, titles, source branch, commit, and whether the source checkout has uncommitted changes. Call this first to get a valid project_id.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "studio_get_project",
        "description": "Get one project's source pinning: root, branch, commit, digest, snapshot paths, and dirty state.",
        "inputSchema": {"type": "object", "properties": dict(_PROJECT), "required": ["project_id"]},
    },
    {
        "name": "studio_get_story_map",
        "description": "Chapter-by-chapter scene map: scene numbers, titles, source paths, word counts, pressure scores, and tracked entities. Use it to choose chapter_numbers or scene_numbers. Pressure is a navigation signal, not an editorial verdict.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_PROJECT,
                "chapter_numbers": {"type": "array", "items": {"type": "integer"}, "description": "Optional filter."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "studio_read_scene",
        "description": "Read one scene as plain text, one paragraph per line prefixed with its paragraph id, plus version and word count. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {**_PROJECT, "scene_number": {"type": "integer"}},
            "required": ["project_id", "scene_number"],
        },
    },
    {
        "name": "studio_list_personas",
        "description": "List editorial personas and panels available to studio_prepare_for_approval (persona_ids). Returns ids, names, categories, and one-line taste summaries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_PROJECT,
                "category": {"type": "string", "description": "Optional filter, e.g. prestige_editor, literary_agent, craft_agent."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "studio_list_annotations",
        "description": "List the author's margin annotations on a scene.",
        "inputSchema": {
            "type": "object",
            "properties": {**_PROJECT, "scene_number": {"type": "integer"}},
            "required": ["project_id", "scene_number"],
        },
    },
    {
        "name": "studio_prepare_for_approval",
        "description": "Prepare an author's requested manuscript changes as source-pinned governance proposals in the Approval Desk. Does not edit prose or Git; the author approves in Literary Studio.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_PROJECT,
                "request_text": {"type": "string", "description": "The author's requested changes."},
                "scene_numbers": {"type": "array", "items": {"type": "integer"}},
                "chapter_numbers": {"type": "array", "items": {"type": "integer"}},
                "include": {"type": "array", "items": {"type": "string"}, "description": "Context lenses, e.g. plot, character, subtext, causality, documentation."},
                "persona_ids": {"type": "array", "items": {"type": "string"}, "description": "From studio_list_personas."},
            },
            "required": ["project_id", "request_text"],
        },
    },
    {
        "name": "studio_list_pending_proposals",
        "description": "List staged manuscript proposals waiting in a project's Approval Desk.",
        "inputSchema": {"type": "object", "properties": dict(_PROJECT), "required": ["project_id"]},
    },
    {
        "name": "studio_get_approval_batch",
        "description": "Retrieve a prepared approval batch, its baseline, questions, and proposals.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT, **_BATCH}, "required": ["project_id", "batch_id"]},
    },
    {
        "name": "studio_check_approval_conflicts",
        "description": "Check whether manual editing or Git changes made a prepared approval batch stale.",
        "inputSchema": {"type": "object", "properties": {**_PROJECT, **_BATCH}, "required": ["project_id", "batch_id"]},
    },
]


class ToolError(Exception):
    """A failure the calling agent should see and can act on (bad id, API down, 4xx)."""


def _seg(value: object) -> str:
    """Percent-encode one URL path segment.

    Parameters: value - any id; converted to str.
    Behavior: encodes every reserved character, including '/' and spaces.
    Failure: none; empty input yields an empty segment (callers validate first).
    """
    return quote(str(value), safe="")


def _request(method: str, path: str, payload: dict | None = None) -> object:
    """Call the Literary Studio API and return decoded JSON.

    Parameters: method - HTTP verb; path - already-encoded path beginning with '/';
    payload - optional JSON body.
    Behavior: sends JSON, returns parsed JSON (dict or list).
    Failure: raises ToolError for HTTP errors (with the API's detail), an
    unreachable service, or a non-JSON response.
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=API_TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, AttributeError):
            detail = raw
        hint = " Call studio_list_projects for valid ids." if exc.code == 404 and "Project" in str(detail) else ""
        raise ToolError(f"Literary Studio API returned {exc.code}: {detail}{hint}") from exc
    except URLError as exc:
        raise ToolError(
            f"Literary Studio API is unavailable at {API_BASE}: {exc.reason}. Start the FastAPI service on port 8000."
        ) from exc
    except TimeoutError as exc:
        raise ToolError(f"Literary Studio API timed out after {API_TIMEOUT:.0f}s.") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Literary Studio API returned non-JSON: {body[:200]}") from exc


class _ParagraphText(HTMLParser):
    """Collect top-level paragraphs and their data-id attributes from scene HTML."""

    BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[str, str]] = []
        self._id: str | None = None
        self._buf: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            if self._depth == 0:
                self._id = dict(attrs).get("data-id") or ""
                self._buf = []
            self._depth += 1
        elif tag == "br" and self._depth:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS and self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = "".join(self._buf).strip()
                if text:
                    self.paragraphs.append((self._id or "", text))

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def _scene_text(html: str) -> str:
    """Render scene HTML as '[paragraph-id] text' lines; ids may be empty."""
    parser = _ParagraphText()
    parser.feed(html or "")
    parser.close()
    return "\n\n".join(f"[{pid}] {text}" if pid else text for pid, text in parser.paragraphs)


def _require(arguments: dict, key: str, kind: type = str) -> object:
    """Fetch a required argument, coercing to kind.

    Failure: raises ToolError when missing, blank, or not coercible.
    """
    value = arguments.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolError(f"{key} is required")
    try:
        return kind(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{key} must be {kind.__name__}") from exc


_PROJECT_FIELDS = ("project_id", "title", "branch", "commit_sha", "working_tree_dirty", "source_root", "source_subpath", "created_at")


def call_tool(name: str, arguments: dict) -> object:
    """Dispatch one MCP tool call to the API.

    Parameters: name - tool name from TOOLS; arguments - tool input.
    Behavior: returns a JSON-serialisable result, trimmed for agent use where
    the raw endpoint is large (scene list, persona catalog).
    Failure: ToolError for bad input or API failures.
    """
    if name == "studio_list_projects":
        projects = _request("GET", "/api/projects")
        return {"projects": [{k: p.get(k) for k in _PROJECT_FIELDS} for p in projects]}

    pid = _seg(_require(arguments, "project_id"))
    base = f"/api/projects/{pid}"

    if name == "studio_get_project":
        return _request("GET", base)

    if name == "studio_get_story_map":
        data = _request("GET", f"{base}/story-intelligence")
        wanted = set(arguments.get("chapter_numbers") or [])
        chapters = []
        for chapter in data.get("chapters", []):
            if wanted and chapter.get("chapter_number") not in wanted:
                continue
            chapters.append({
                "chapter_number": chapter.get("chapter_number"),
                "title": chapter.get("title"),
                "scenes": [
                    {
                        "scene_number": s.get("scene_number"),
                        "title": s.get("title"),
                        "source_path": s.get("source_path"),
                        "word_count": s.get("word_count"),
                        "pressure_score": s.get("pressure_score"),
                        "entities": list(dict.fromkeys(e.get("label") for e in s.get("tracked_entities", []) if e.get("label"))),
                    }
                    for s in chapter.get("scenes", [])
                ],
            })
        return {
            "project_id": data.get("project_id"),
            "scene_count": data.get("scene_count"),
            "chapter_count": data.get("chapter_count"),
            "pressure_method": data.get("pressure_method"),
            "chapters": chapters,
        }

    if name == "studio_read_scene":
        number = _require(arguments, "scene_number", int)
        scene = _request("GET", f"{base}/scenes/{_seg(number)}")
        return {
            "scene_number": scene.get("scene_number"),
            "title": scene.get("title"),
            "chapter_number": scene.get("chapter_number"),
            "chapter_title": scene.get("chapter_title"),
            "source_path": scene.get("source_path"),
            "version": scene.get("version"),
            "word_count": scene.get("word_count"),
            "updated_at": scene.get("updated_at"),
            "text": _scene_text(scene.get("content_html", "")),
        }

    if name == "studio_list_personas":
        data = _request("GET", f"{base}/personas/catalog")
        category = str(arguments.get("category") or "").strip()
        personas = [
            {
                "persona_id": p.get("persona_id"),
                "name": p.get("name"),
                "category": p.get("category"),
                "title_or_archetype": p.get("title_or_archetype"),
                "organization_or_imprint": p.get("organization_or_imprint"),
                "taste_summary": (p.get("taste_summary") or "")[:240],
            }
            for p in data.get("personas", [])
            if not category or p.get("category") == category
        ]
        panels = [
            {k: panel.get(k) for k in ("panel_id", "name", "description", "persona_ids")}
            for panel in data.get("panels", [])
        ]
        return {"summary": data.get("summary"), "panels": panels, "personas": personas}

    if name == "studio_list_annotations":
        number = _require(arguments, "scene_number", int)
        return {"scene_number": number, "annotations": _request("GET", f"{base}/scenes/{_seg(number)}/annotations")}

    if name == "studio_prepare_for_approval":
        payload = {
            "request_text": _require(arguments, "request_text"),
            "scene_numbers": arguments.get("scene_numbers") or [],
            "chapter_numbers": arguments.get("chapter_numbers") or [],
            "include": arguments.get("include") or [],
            "persona_ids": arguments.get("persona_ids") or [],
        }
        return _request("POST", f"{base}/approval-batches", payload)

    if name == "studio_list_pending_proposals":
        return {"project_id": arguments["project_id"], "proposals": _request("GET", f"{base}/governance/proposals")}

    if name in ("studio_get_approval_batch", "studio_check_approval_conflicts"):
        bid = _seg(_require(arguments, "batch_id"))
        if name == "studio_get_approval_batch":
            return _request("GET", f"{base}/approval-batches/{bid}")
        return _request("POST", f"{base}/approval-batches/{bid}/check", {})

    raise ToolError(f"Unknown tool: {name}")


def _response(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(result: object, is_error: bool = False) -> dict:
    """Wrap a tool outcome per MCP: errors are results with isError so the agent can read them."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
    out: dict = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(result, dict) and not is_error:
        out["structuredContent"] = result
    return out


def handle(message: dict) -> dict | None:
    """Route one JSON-RPC message.

    Behavior: answers initialize, ping, tools/list, tools/call; ignores
    notifications. Tool failures return isError results, never exceptions.
    """
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return _response(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "literary-studio", "version": SERVER_VERSION},
        })
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            return _response(request_id, _tool_result(call_tool(str(params.get("name", "")), params.get("arguments") or {})))
        except ToolError as exc:
            return _response(request_id, _tool_result(str(exc), is_error=True))
        except Exception as exc:  # keep the server alive; log for the developer
            traceback.print_exc(file=sys.stderr)
            return _response(request_id, _tool_result(f"Internal bridge error: {type(exc).__name__}: {exc}", is_error=True))
    if request_id is None:
        return None
    return _error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF; one bad line never ends the loop."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            result = handle(json.loads(line))
        except json.JSONDecodeError as exc:
            result = _error(None, -32700, f"Parse error: {exc}")
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            result = _error(None, -32603, f"Internal error: {exc}")
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
