"""Local T3 Code MCP client, using its supported OAuth and pairing endpoints.

Credentials live outside the checkout. No provider-session credentials or T3
database writes are used, and tool calls are never automatically retried: a
timed-out launch may already have created a thread.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib import error, parse, request


PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REFRESH_BEFORE_SECONDS = 24 * 60 * 60
MCP_SCOPES = (
    "orchestration:read",
    "orchestration:operate",
    "settings:write",
    "source-control:write",
)


class T3Error(RuntimeError):
    """A redacted transport failure; an ambiguous call must be reconciled."""

    def __init__(self, message: str, *, ambiguous: bool = False, status: int | None = None):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.status = status


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class T3:
    """A serialized client for the local T3 instance.

    ``call`` returns the tool's structuredContent (or JSON text fallback).
    ``close`` ends only the HTTP MCP session, never any T3 thread.
    """

    def __init__(self, state_dir: Path, endpoint: str = "http://127.0.0.1:3773/mcp", *,
                 timeout: float = 180, t3_command: str | None = None):
        parsed = parse.urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/mcp"):
            raise ValueError("T3 endpoint must be a loopback HTTP(S) /mcp URL")
        self.endpoint = endpoint
        self.origin = parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.auth_path = self.state_dir / "t3-auth.json"
        self.timeout = timeout
        self.t3_command = t3_command or shutil.which("t3") or str(Path.home() / ".local/bin/t3")
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._token: str | None = None
        self._expires_at = 0.0
        self._session: str | None = None
        self._protocol: str | None = None
        self._request_id = 0
        self._lock = threading.RLock()

    def _open(self, req: request.Request, *, ambiguous: bool = False):
        try:
            return self._opener.open(req, timeout=self.timeout)
        except error.HTTPError as exc:
            # Never include response bodies, authorization headers or URLs in errors.
            status = exc.code
            exc.close()
            raise T3Error(f"T3 HTTP request failed ({status})", status=status,
                          ambiguous=ambiguous and status >= 500) from None
        except (error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise T3Error("T3 connection failed or timed out", ambiguous=ambiguous) from None

    @staticmethod
    def _json_bytes(raw: bytes) -> Any:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise T3Error("T3 response exceeded the size limit")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise T3Error("T3 returned invalid JSON") from None

    def _json_post(self, path: str, payload: dict, *, form: bool = False) -> dict:
        encoded = (parse.urlencode(payload).encode() if form
                   else json.dumps(payload).encode())
        req = request.Request(self.origin + path, data=encoded, headers={
            "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
            "Accept": "application/json",
        })
        try:
            with self._open(req) as response:
                result = self._json_bytes(response.read(MAX_RESPONSE_BYTES + 1))
        except (OSError, http.client.HTTPException):
            raise T3Error("T3 OAuth response could not be read") from None
        if not isinstance(result, dict):
            raise T3Error("T3 OAuth response was not an object")
        return result

    def _read_credentials(self) -> dict | None:
        try:
            fd = os.open(self.auth_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise T3Error("T3 credential file must be owned by the service user")
                os.fchmod(handle.fileno(), 0o600)
                value = json.load(handle)
            if (isinstance(value, dict) and value.get("origin") == self.origin
                    and isinstance(value.get("access_token"), str)
                    and isinstance(value.get("expires_at"), (float, int))):
                return value
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            raise T3Error("T3 credential file could not be read safely") from None
        raise T3Error("T3 credential file has an invalid format or belongs to another endpoint")

    def _save_credentials(self, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".t3-auth-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.auth_path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _pairing_credential(self) -> dict:
        command = [self.t3_command, "auth", "pairing", "create", "--ttl", "5m",
                   "--label", "Planetry issue automation", "--json"]
        for scope in MCP_SCOPES:
            command.extend(["--scope", scope])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            value = json.loads(result.stdout) if result.returncode == 0 else None
            if (isinstance(value, dict) and isinstance(value.get("credential"), str)
                    and isinstance(value.get("id"), str)):
                return value
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        raise T3Error("T3 could not issue a local pairing credential")

    def _renew_credentials(self) -> dict:
        redirect = "http://127.0.0.1/planetry-oauth-callback"
        registration = self._json_post("/oauth/mcp/register", {
            "client_name": "Planetry issue automation",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
        })
        client_id = registration.get("client_id")
        if not isinstance(client_id, str):
            raise T3Error("T3 OAuth registration did not return a client ID")
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        authorization = {
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "resource": self.endpoint,
        }
        pairing = self._pairing_credential()
        consumed = False
        try:
            decision = self._json_post("/oauth/mcp/decision", {
                "authorization": authorization,
                "decision": {"_tag": "pairing-code", "access": "full-access",
                             "code": pairing["credential"]},
            })
            consumed = True
            target = decision.get("redirectTo")
            if not isinstance(target, str):
                raise T3Error("T3 OAuth approval did not return a redirect")
            parsed = parse.urlsplit(target)
            returned = parse.parse_qs(parsed.query)
            if (parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) != redirect
                    or parsed.fragment or returned.get("state") != [state]
                    or returned.get("iss") != [self.origin]
                    or len(returned.get("code", [])) != 1):
                raise T3Error("T3 OAuth approval response failed validation")
            result = self._json_post("/oauth/mcp/token", {
                "grant_type": "authorization_code", "code": returned["code"][0],
                "redirect_uri": redirect, "client_id": client_id,
                "code_verifier": verifier, "resource": self.endpoint,
            }, form=True)
            if (not isinstance(result.get("access_token"), str)
                    or not isinstance(result.get("expires_in"), (int, float))
                    or result["expires_in"] <= REFRESH_BEFORE_SECONDS
                    or result.get("token_type") != "Bearer"
                    or not set(MCP_SCOPES).issubset(str(result.get("scope", "")).split())):
                raise T3Error("T3 OAuth token response failed validation")
            credentials = {
                "access_token": result["access_token"],
                "expires_at": time.time() + result["expires_in"],
                "origin": self.origin,
                "scope": result["scope"],
            }
            self._save_credentials(credentials)
            return credentials
        finally:
            if not consumed:
                try:
                    subprocess.run([self.t3_command, "auth", "pairing", "revoke", pairing["id"]],
                                   capture_output=True, timeout=30, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass  # Unconsumed bootstrap credentials expire after five minutes.

    def _ensure_credentials(self, *, force: bool = False) -> None:
        if not force and self._token and self._expires_at > time.time() + REFRESH_BEFORE_SECONDS:
            return
        credentials = None if force else self._read_credentials()
        if not credentials or credentials["expires_at"] <= time.time() + REFRESH_BEFORE_SECONDS:
            credentials = self._renew_credentials()
        self._token = credentials["access_token"]
        self._expires_at = credentials["expires_at"]
        self._session = None
        self._protocol = None

    def _read_rpc_response(self, response, expected_id: int | None) -> dict | None:
        if response.status == 202:
            return None

        def matching(payload):
            envelopes = payload if isinstance(payload, list) else [payload]
            return next((item for item in envelopes if isinstance(item, dict)
                         and item.get("id") == expected_id and expected_id is not None), None)

        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return matching(self._json_bytes(raw)) if raw else None
        data: list[bytes] = []
        received = 0
        while True:
            line = response.readline(MAX_RESPONSE_BYTES + 1)
            received += len(line)
            if received > MAX_RESPONSE_BYTES:
                raise T3Error("T3 event stream exceeded the size limit")
            if not line or line in {b"\n", b"\r\n"}:
                if data:
                    found = matching(self._json_bytes(b"\n".join(data)))
                    if found is not None:
                        return found
                    data.clear()
                if not line:
                    return None
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" ").rstrip(b"\r\n"))

    def _rpc(self, method: str, params: dict | None = None, *, notification: bool = False,
             ambiguous: bool = False) -> Any:
        self._request_id += 1
        rpc_id = None if notification else self._request_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if rpc_id is not None:
            message["id"] = rpc_id
        if params is not None:
            message["params"] = params
        headers = {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + (self._token or ""),
        }
        if self._session:
            headers["mcp-session-id"] = self._session
        if self._protocol:
            headers["mcp-protocol-version"] = self._protocol
        req = request.Request(self.endpoint, data=json.dumps(message).encode(), headers=headers)
        try:
            with self._open(req, ambiguous=ambiguous) as response:
                self._session = response.headers.get("mcp-session-id") or self._session
                envelope = self._read_rpc_response(response, rpc_id)
        except T3Error as exc:
            if ambiguous:
                exc.ambiguous = exc.ambiguous or exc.status not in {400, 401, 403, 404, 405, 406}
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise T3Error("T3 response could not be read", ambiguous=ambiguous) from None
        if notification:
            return None
        if not envelope or envelope.get("jsonrpc") != "2.0":
            raise T3Error("T3 did not return the expected RPC response", ambiguous=ambiguous)
        if "error" in envelope:
            code = envelope["error"].get("code") if isinstance(envelope["error"], dict) else None
            code = code if isinstance(code, int) else None
            suffix = f" ({code})" if code is not None else ""
            raise T3Error("T3 RPC call was rejected" + suffix,
                          ambiguous=ambiguous and code not in {-32700, -32600, -32601, -32602})
        return envelope.get("result")

    def _initialize(self) -> None:
        for attempt in range(2):
            try:
                result = self._rpc("initialize", {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "planetry", "version": "0.1.0"},
                })
                break
            except T3Error as exc:
                if attempt == 0 and exc.status == 401:
                    self._ensure_credentials(force=True)
                else:
                    raise
        if not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL_VERSION:
            raise T3Error("T3 negotiated an unsupported MCP protocol")
        self._protocol = result["protocolVersion"]
        self._rpc("notifications/initialized", notification=True)

    def call(self, tool_name: str, arguments: dict) -> Any:
        with self._lock:
            self._ensure_credentials()
            if self._protocol is None:
                self._initialize()
            try:
                result = self._rpc("tools/call", {"name": tool_name, "arguments": arguments},
                                   ambiguous=True)
            except T3Error as exc:
                self._session = None
                self._protocol = None
                if exc.status == 401:
                    self._token = None
                    self._expires_at = 0
                    self.auth_path.unlink(missing_ok=True)
                raise
            if not isinstance(result, dict):
                raise T3Error("T3 tool returned an invalid result", ambiguous=True)
            if result.get("isError"):
                raise T3Error("T3 tool reported an error; inspect its T3 activity for details",
                              ambiguous=True)
            if "structuredContent" in result:
                return result["structuredContent"]
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        return json.loads(item["text"])
                    except (ValueError, KeyError, TypeError):
                        continue
            raise T3Error("T3 tool did not return structured data", ambiguous=True)

    def close(self) -> None:
        with self._lock:
            if self._session and self._token:
                headers = {"Authorization": "Bearer " + self._token,
                           "mcp-session-id": self._session,
                           "mcp-protocol-version": self._protocol or PROTOCOL_VERSION}
                try:
                    with self._open(request.Request(self.endpoint, method="DELETE", headers=headers)):
                        pass
                except T3Error:
                    pass
            self._session = None
            self._protocol = None
