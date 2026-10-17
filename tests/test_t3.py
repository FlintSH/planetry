from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib import error, parse

from planetry.t3 import MCP_SCOPES, PROTOCOL_VERSION, T3, T3Error


class Response(io.BytesIO):
    def __init__(self, value=None, *, status=200, headers=None, raw=None):
        super().__init__(raw if raw is not None else json.dumps(value).encode())
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}


class T3Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.client = T3(Path(self.temporary.name))
        self.token = "private-bearer-do-not-log"
        self.client._save_credentials({
            "access_token": self.token, "expires_at": time.time() + 3 * 86400,
            "origin": self.client.origin, "scope": " ".join(MCP_SCOPES),
        })

    @staticmethod
    def initialized():
        return Response({"jsonrpc": "2.0", "id": 1,
                         "result": {"protocolVersion": PROTOCOL_VERSION}},
                        headers={"mcp-session-id": "test-session"})

    def test_json_handshake_and_session_headers_then_reuse(self):
        responses = [self.initialized(), Response(status=202),
                     Response({"jsonrpc": "2.0", "id": 3, "result": {
                         "structuredContent": {"projects": []}, "isError": False}}),
                     Response({"jsonrpc": "2.0", "id": 4, "result": {
                         "structuredContent": {"threads": []}}})]
        self.client._opener = Mock()
        self.client._opener.open.side_effect = responses
        self.assertEqual(self.client.call("t3_project_list", {}), {"projects": []})
        self.assertEqual(self.client.call("t3_thread_list", {}), {"threads": []})
        calls = self.client._opener.open.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(json.loads(calls[0].args[0].data)["method"], "initialize")
        headers = dict(calls[2].args[0].header_items())
        self.assertEqual(headers["Mcp-session-id"], "test-session")
        self.assertEqual(headers["Mcp-protocol-version"], PROTOCOL_VERSION)
        self.assertEqual(headers["Authorization"], "Bearer " + self.token)
        self.assertEqual(calls[2].kwargs["timeout"], 180)

    def test_sse_ignores_notifications_and_handles_multiline_data(self):
        response = Response(raw=(
            b": heartbeat\r\n\r\n"
            b"event: message\r\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notification\"}\r\n\r\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":9,\r\n"
            b"data: \"result\":{\"ok\":true}}\r\n\r\n"
        ), headers={"Content-Type": "text/event-stream"})
        self.assertEqual(self.client._read_rpc_response(response, 9)["result"], {"ok": True})

    def test_sse_final_event_without_blank_line_and_batch(self):
        response = Response(raw=b'data: [{"jsonrpc":"2.0","id":4,"result":7}]',
                            headers={"Content-Type": "text/event-stream"})
        self.assertEqual(self.client._read_rpc_response(response, 4)["result"], 7)

    def test_text_content_json_fallback(self):
        self.client._opener = Mock()
        self.client._opener.open.side_effect = [self.initialized(), Response(status=202),
            Response({"jsonrpc": "2.0", "id": 3, "result": {"content": [
                {"type": "text", "text": '{"threadId":"one"}'}]}})]
        self.assertEqual(self.client.call("t3_thread_launch", {}), {"threadId": "one"})

    def test_tool_timeout_is_ambiguous_and_never_retried(self):
        self.client._opener = Mock()
        self.client._opener.open.side_effect = [self.initialized(), Response(status=202),
                                               TimeoutError(self.token)]
        with self.assertRaises(T3Error) as raised:
            self.client.call("t3_thread_launch", {})
        self.assertTrue(raised.exception.ambiguous)
        self.assertNotIn(self.token, str(raised.exception))
        self.assertEqual(self.client._opener.open.call_count, 3)
        self.assertIsNone(self.client._session)

    def test_http_error_redacts_body_url_and_never_retries_tool(self):
        secret_url = "http://127.0.0.1:3773/mcp?secret=" + self.token
        self.client._opener = Mock()
        self.client._opener.open.side_effect = [self.initialized(), Response(status=202),
            error.HTTPError(secret_url, 500, self.token, {}, io.BytesIO(self.token.encode()))]
        with self.assertRaises(T3Error) as raised:
            self.client.call("t3_thread_launch", {})
        self.assertEqual(raised.exception.status, 500)
        self.assertTrue(raised.exception.ambiguous)
        self.assertNotIn(self.token, str(raised.exception))
        self.assertEqual(self.client._opener.open.call_count, 3)

    def test_expired_mcp_session_is_not_retried_and_next_call_reinitializes(self):
        self.client._opener = Mock()
        self.client._opener.open.side_effect = [self.initialized(), Response(status=202),
            error.HTTPError(self.client.endpoint, 404, "expired", {}, io.BytesIO()),
            Response({"jsonrpc": "2.0", "id": 4,
                      "result": {"protocolVersion": PROTOCOL_VERSION}},
                     headers={"mcp-session-id": "new-session"}), Response(status=202),
            Response({"jsonrpc": "2.0", "id": 6, "result": {"structuredContent": {"ok": True}}})]
        with self.assertRaises(T3Error):
            self.client.call("t3_thread_send", {})
        self.assertEqual(self.client._opener.open.call_count, 3)
        self.assertEqual(self.client.call("t3_thread_list", {}), {"ok": True})

    def test_rejected_auth_during_initialize_renews_before_tool_dispatch(self):
        self.client._opener = Mock()
        self.client._opener.open.side_effect = [
            error.HTTPError(self.client.endpoint, 401, "revoked", {}, io.BytesIO()),
            Response({"jsonrpc": "2.0", "id": 2,
                      "result": {"protocolVersion": PROTOCOL_VERSION}},
                     headers={"mcp-session-id": "new-session"}), Response(status=202),
            Response({"jsonrpc": "2.0", "id": 4, "result": {"structuredContent": {"ok": True}}})]
        fresh = {"access_token": "renewed", "expires_at": time.time() + 3 * 86400}
        with patch.object(self.client, "_renew_credentials", return_value=fresh) as renew:
            self.assertEqual(self.client.call("t3_thread_list", {}), {"ok": True})
        renew.assert_called_once()
        self.assertEqual(self.client._opener.open.call_count, 4)

    def test_auth_cache_permissions_and_proactive_refresh(self):
        self.assertEqual(stat.S_IMODE(self.client.auth_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.client.state_dir.stat().st_mode), 0o700)
        self.client._ensure_credentials()
        self.assertEqual(self.client._token, self.token)
        old = self.client._read_credentials()
        old["expires_at"] = time.time() + 3600
        self.client._save_credentials(old)
        self.client._token = None
        fresh = {**old, "expires_at": time.time() + 30 * 86400, "access_token": "renewed"}
        with patch.object(self.client, "_renew_credentials", return_value=fresh) as renew:
            self.client._ensure_credentials()
        renew.assert_called_once()
        self.assertEqual(self.client._token, "renewed")

    def test_oauth_uses_pairing_pkce_validates_origin_and_persists_privately(self):
        calls = []
        def post(path, payload, *, form=False):
            calls.append((path, payload, form))
            if path.endswith("register"):
                return {"client_id": "client-identity"}
            if path.endswith("decision"):
                authorization = payload["authorization"]
                self.assertEqual(payload["decision"]["access"], "full-access")
                return {"redirectTo": authorization["redirect_uri"] + "?" + parse.urlencode({
                    "code": "private-auth-code", "state": authorization["state"],
                    "iss": self.client.origin})}
            return {"access_token": "private-new-token", "token_type": "Bearer",
                    "expires_in": 30 * 86400, "scope": " ".join(MCP_SCOPES)}
        with patch.object(self.client, "_json_post", side_effect=post), patch.object(
                self.client, "_pairing_credential", return_value={"id": "pair-1", "credential": "private-pair"}):
            value = self.client._renew_credentials()
        self.assertEqual(value["access_token"], "private-new-token")
        authorization = calls[1][1]["authorization"]
        verifier = calls[2][1]["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(authorization["code_challenge"], challenge)
        self.assertTrue(calls[2][2])
        self.assertEqual(json.loads(self.client.auth_path.read_text())["access_token"], "private-new-token")
        self.assertEqual(stat.S_IMODE(self.client.auth_path.stat().st_mode), 0o600)

    def test_oauth_rejects_wrong_redirect_without_token_exchange(self):
        with (patch.object(self.client, "_json_post", side_effect=[{"client_id": "id"}, {
                "redirectTo": "https://attacker.invalid/callback?code=private-code"}]) as post,
                patch.object(self.client, "_pairing_credential", return_value={
                    "id": "pair", "credential": "secret"})):
            with self.assertRaises(T3Error) as raised:
                self.client._renew_credentials()
        self.assertNotIn("private-code", str(raised.exception))
        self.assertEqual(post.call_count, 2)

    def test_cli_failure_does_not_expose_output(self):
        completed = Mock(returncode=1, stdout=self.token, stderr=self.token)
        with patch("planetry.t3.subprocess.run", return_value=completed):
            with self.assertRaises(T3Error) as raised:
                self.client._pairing_credential()
        self.assertNotIn(self.token, str(raised.exception))

    def test_credential_file_symlink_rejected(self):
        actual = self.client.state_dir / "actual-secret"
        self.client.auth_path.rename(actual)
        self.client.auth_path.symlink_to(actual)
        with self.assertRaises(T3Error):
            self.client._read_credentials()

    def test_external_endpoints_rejected(self):
        for endpoint in ["https://example.com/mcp", "http://user:secret@localhost/mcp",
                         "http://127.0.0.1:3773/mcp?token=secret"]:
            with self.assertRaises(ValueError):
                T3(self.client.state_dir, endpoint)

    def test_rpc_and_tool_errors_redact_untrusted_messages(self):
        for result in [{"error": {"code": -32602, "message": self.token}},
                       {"result": {"isError": True, "content": [{"type": "text", "text": self.token}]}}]:
            with self.subTest(result_kind=list(result)):
                self.client._session = "test-session"
                self.client._protocol = PROTOCOL_VERSION
                self.client._token = self.token
                self.client._expires_at = time.time() + 3 * 86400
                rpc_id = self.client._request_id + 1
                self.client._opener = Mock()
                self.client._opener.open.return_value = Response({"jsonrpc": "2.0", "id": rpc_id, **result})
                with self.assertRaises(T3Error) as raised:
                    self.client.call("t3_thread_launch", {})
                self.assertNotIn(self.token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
