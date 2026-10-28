"""Regression tests for the local HTTP MCP transport.

Run from backend/: venv/bin/python -m unittest -v test_mcp_http_server
"""
import json
import os
import unittest
from unittest.mock import patch

from mcp_http_server import _origin_allowed, _valid_token, dispatch
from mcp_server import TOOLS


class AuthorizationTests(unittest.TestCase):
    def test_missing_secret_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_valid_token("Bearer example"))

    def test_short_secret_fails_closed(self):
        with patch.dict(os.environ, {"LITERARY_STUDIO_MCP_TOKEN": "short"}):
            self.assertFalse(_valid_token("Bearer short"))

    def test_valid_bearer(self):
        secret = "a" * 64
        with patch.dict(os.environ, {"LITERARY_STUDIO_MCP_TOKEN": secret}):
            self.assertTrue(_valid_token("Bearer " + secret))
            self.assertFalse(_valid_token("Bearer " + ("b" * 64)))
            self.assertFalse(_valid_token("Basic " + secret))
            self.assertFalse(_valid_token(None))

    def test_origin_denied_unless_explicitly_allowed(self):
        with patch.dict(os.environ, {"LITERARY_STUDIO_MCP_ALLOWED_ORIGINS": ""}):
            self.assertTrue(_origin_allowed(None))
            self.assertFalse(_origin_allowed("https://attacker.example"))
        with patch.dict(os.environ, {"LITERARY_STUDIO_MCP_ALLOWED_ORIGINS": "https://studio.example"}):
            self.assertTrue(_origin_allowed("https://studio.example"))
            self.assertFalse(_origin_allowed("https://other.example"))


class ProtocolTests(unittest.TestCase):
    def test_negotiates_supported_protocol(self):
        response = dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        })
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")

    def test_reuses_stdio_catalogue(self):
        response = dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual(response["result"]["tools"], TOOLS)

    def test_notifications_do_not_produce_response(self):
        self.assertIsNone(dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_invalid_request(self):
        self.assertEqual(dispatch([])["error"]["code"], -32600)
        self.assertEqual(dispatch({"method": "ping", "id": 2})["error"]["code"], -32600)

    def test_remote_catalogue_cannot_approve_manuscript_changes(self):
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("studio_prepare_for_approval", names)
        self.assertIn("studio_read_scene", names)
        self.assertNotIn("studio_review_proposal", names)
        self.assertNotIn("studio_accept_proposal", names)
        self.assertNotIn("studio_apply_edit", names)
        self.assertNotIn("studio_commit", names)



class HttpEndpointTests(unittest.TestCase):
    def _request(self, secret, authorization=None):
        import asyncio
        from mcp_http_server import app
        body = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}).encode()
        headers = [(b"content-type", b"application/json"), (b"accept", b"application/json, text/event-stream")]
        if authorization is not None:
            headers.append((b"authorization", authorization.encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "http", "method": "POST", "path": "/mcp",
            "raw_path": b"/mcp", "query_string": b"", "root_path": "",
            "headers": headers, "server": ("127.0.0.1", 8794),
            "client": ("127.0.0.1", 10000),
        }
        events = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            events.append(message)

        with patch.dict(os.environ, {"LITERARY_STUDIO_MCP_TOKEN": secret}):
            asyncio.run(app(scope, receive, send))
        status = next(e["status"] for e in events if e["type"] == "http.response.start")
        response = b"".join(e.get("body", b"") for e in events if e["type"] == "http.response.body")
        return status, response

    def test_http_denies_missing_bearer(self):
        status, _ = self._request("a" * 64)
        self.assertEqual(status, 401)

    def test_http_serves_existing_tools_with_bearer(self):
        secret = "a" * 64
        status, body = self._request(secret, "Bearer " + secret)
        self.assertEqual(status, 200)
        self.assertIn("studio_read_scene", body.decode())


if __name__ == "__main__":
    unittest.main()
