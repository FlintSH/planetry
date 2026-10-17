"""Small, authenticated GitHub REST client used by the Planetry issue bridge.

No caller-controlled URL is ever allowed to move the Authorization header to a
different host. ETag responses retain their pagination metadata, so an unchanged
first page never hides a changed second page. Mutations are never retried here.
"""

from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from email.utils import parsedate_to_datetime


class GitHubError(RuntimeError):
    """A sanitized error safe to include in operator logs."""


class RetryableError(GitHubError):
    """Back off before the next request; reconcile ambiguous mutations first."""

    def __init__(
        self,
        message: str,
        retry_after: float = 60,
        *,
        mutation_ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))
        self.mutation_ambiguous = mutation_ambiguous


class AmbiguousMutationError(GitHubError):
    """The write may have succeeded. Read back its marker before retrying."""


_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}\Z")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_PAGES = 10_000


def _repository(repo: str) -> str:
    if not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo):
        raise ValueError("Repository must be an exact owner/name pair")
    if repo.split("/", 1)[1] in {".", ".."}:
        raise ValueError("Repository must be an exact owner/name pair")
    return repo


def _number(number: int) -> str:
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("Issue number must be a positive integer")
    return str(number)


def _gh_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise GitHubError("GitHub authentication unavailable; check gh auth status") from None


class _SafeRedirect(HTTPRedirectHandler):
    def __init__(self, validate: Callable[[str], str]) -> None:
        self._validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization when redirecting to any host.
        safe_url = self._validate(newurl)
        if req.get_method() not in {"GET", "HEAD"}:
            raise GitHubError("GitHub mutation redirect requires operator reconciliation")
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


class GitHub:
    """A bounded GitHub client. ``opener`` and ``token_provider`` allow testing.

    ``opener`` may be a urllib opener object or a callable accepting a Request
    and ``timeout``. A custom opener is responsible for safe redirect handling.
    """

    def __init__(
        self,
        state_dir: Path,
        api_url: str = "https://api.github.com",
        *,
        token_provider: Callable[[], str] | None = None,
        opener: Any = None,
        timeout: float = 30,
    ) -> None:
        try:
            parsed = urlsplit(api_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("GitHub API URL is invalid") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in api_url
            or any(ord(c) < 33 or ord(c) == 127 for c in api_url)
        ):
            raise ValueError("GitHub API URL must be an HTTPS origin or API base path")
        self.api_url = api_url.rstrip("/")
        self._origin = (parsed.scheme, parsed.hostname.lower(), port or 443)
        self._base_path = parsed.path.rstrip("/")
        self._token_provider = token_provider or _gh_token
        self._token: str | None = None
        if not 0 < timeout <= 60:
            raise ValueError("Request timeout must be between 0 and 60 seconds")
        self.timeout = timeout
        self._cache_dir = Path(state_dir) / "github-cache"
        self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._cache_dir.chmod(0o700)
        selected_opener = opener or build_opener(_SafeRedirect(self._validate_url))
        self._open = getattr(selected_opener, "open", selected_opener)

    def _validate_url(self, url: str) -> str:
        if not isinstance(url, str) or not url or "\\" in url or any(
            ord(c) < 33 or ord(c) == 127 for c in url
        ):
            raise GitHubError("GitHub API URL rejected")
        try:
            parts = urlsplit(url)
            origin = (parts.scheme, (parts.hostname or "").lower(), parts.port or 443)
        except ValueError:
            raise GitHubError("GitHub API URL rejected") from None
        if (
            origin != self._origin
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or not parts.path.startswith(self._base_path + "/")
            or any(segment in {".", ".."} for segment in parts.path.split("/"))
        ):
            raise GitHubError("GitHub API URL outside the configured API rejected")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))

    def _url(self, path: str) -> str:
        if not isinstance(path, str):
            raise GitHubError("GitHub API path rejected")
        if path.startswith("/") and not path.startswith("//"):
            return self._validate_url(self.api_url + path)
        if path.startswith("https://"):
            return self._validate_url(path)
        raise GitHubError("GitHub API path must be absolute within the configured API")

    def _auth_token(self) -> str:
        if self._token is None:
            try:
                token = self._token_provider()
            except Exception:
                raise GitHubError("GitHub authentication unavailable; check gh auth status") from None
            if not isinstance(token, str) or not token or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise GitHubError("GitHub authentication returned an invalid token")
            self._token = token
        return self._token

    def _cache_path(self, url: str) -> Path:
        return self._cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, url: str) -> dict[str, Any] | None:
        try:
            path = self._cache_path(url)
            if path.stat().st_size > _MAX_RESPONSE_BYTES * 2:
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(entry, dict)
                or entry.get("version") != 1
                or entry.get("url") != url
                or not isinstance(entry.get("etag"), str)
                or len(entry["etag"]) > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in entry["etag"])
                or "payload" not in entry
                or not (entry.get("next_url") is None or isinstance(entry.get("next_url"), str))
            ):
                return None
            return entry
        except (OSError, ValueError):
            return None

    def _write_cache(self, url: str, payload: Any, etag: str, next_url: str | None) -> None:
        if not etag or len(etag) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in etag):
            self._cache_path(url).unlink(missing_ok=True)
            return
        entry = {"version": 1, "url": url, "payload": payload, "etag": etag, "next_url": next_url}
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._cache_dir, delete=False) as file:
                temp_name = file.name
                json.dump(entry, file, separators=(",", ":"), ensure_ascii=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self._cache_path(url))
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def _next_url(self, header: str | None, current_url: str) -> str | None:
        if not header:
            return None
        for target, parameters in re.findall(r"<([^<>]+)>\s*([^,]*)", header):
            match = re.search(r'(?:^|;)\s*rel\s*=\s*(?:"([^"]*)"|([^;\s,]+))', parameters, re.I)
            if match and "next" in (match.group(1) or match.group(2)).split():
                return self._validate_url(urljoin(current_url, target))
        return None

    @staticmethod
    def _retry_delay(headers: Any, default: float = 60) -> float:
        delays = [default]
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                delays.append(float(retry_after))
            except (TypeError, ValueError):
                try:
                    delays.append(parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        if headers.get("X-RateLimit-Remaining") == "0":
            try:
                delays.append(float(headers.get("X-RateLimit-Reset", 0)) - time.time() + 1)
            except (TypeError, ValueError):
                pass
        return max(delay for delay in delays if math.isfinite(delay))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        url = self._url(path)
        cache = self._read_cache(url) if method == "GET" else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self._auth_token(),
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "planetry-issue-bridge/1",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=True).encode("utf-8")
        if cache:
            headers["If-None-Match"] = cache["etag"]
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(request, timeout=self.timeout) as response:
                status = response.status
                response_headers = response.headers
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            response_headers = error.headers or {}
            try:
                raw = error.read(_MAX_RESPONSE_BYTES + 1)
            except (OSError, ValueError, HTTPException):
                raw = b""
            finally:
                error.close()
        except (OSError, URLError, HTTPException):
            if method != "GET":
                raise AmbiguousMutationError("GitHub write outcome unknown; read back before retrying") from None
            raise RetryableError("GitHub request temporarily unavailable", 30) from None

        if status == 304:
            if method != "GET" or cache is None:
                raise GitHubError("GitHub returned an uncached conditional response")
            next_url = (
                self._next_url(response_headers.get("Link"), url)
                if response_headers.get("Link") is not None
                else cache.get("next_url")
            )
            if next_url is not None:
                next_url = self._validate_url(next_url)
            self._write_cache(url, cache["payload"], response_headers.get("ETag", cache["etag"]), next_url)
            return cache["payload"], next_url

        limited = status == 429 or (
            status == 403 and (
                response_headers.get("X-RateLimit-Remaining") == "0"
                or response_headers.get("Retry-After") is not None
                or b"rate limit" in raw.lower()
                or b"abuse detection" in raw.lower()
            )
        )
        if limited or status >= 500:
            raise RetryableError(
                "GitHub rate limit reached" if limited else f"GitHub temporarily unavailable (HTTP {status})",
                self._retry_delay(response_headers),
                mutation_ambiguous=method != "GET" and status >= 500,
            )
        if not 200 <= status < 300:
            raise GitHubError(f"GitHub request failed (HTTP {status})")
        if len(raw) > _MAX_RESPONSE_BYTES:
            if method != "GET":
                raise AmbiguousMutationError("GitHub write response too large; read back before retrying")
            raise GitHubError("GitHub response exceeds the supported size")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            if method != "GET":
                raise AmbiguousMutationError("GitHub write response invalid; read back before retrying") from None
            raise GitHubError("GitHub returned invalid JSON") from None
        next_url = self._next_url(response_headers.get("Link"), url)
        if method == "GET":
            self._write_cache(url, payload, response_headers.get("ETag", ""), next_url)
        return payload, next_url

    def get_pages(self, path: str) -> list[dict[str, Any]]:
        """Collect every page, including cached pages returned with HTTP 304."""
        current = self._url(path)
        seen = set()
        items = []
        while current:
            if current in seen or len(seen) >= _MAX_PAGES:
                raise GitHubError("GitHub pagination repeated or exceeded its page limit")
            seen.add(current)
            payload, current = self._request("GET", current)
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise GitHubError("GitHub returned an unexpected list response")
            items.extend(payload)
        return items

    def _object(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload, _ = self._request(method, path, body)
        if not isinstance(payload, dict):
            if method != "GET":
                raise AmbiguousMutationError("GitHub write response invalid; read back before retrying")
            raise GitHubError("GitHub returned an unexpected object response")
        return payload

    def list_issues(self, repo: str) -> list[dict[str, Any]]:
        return self.get_pages(f"/repos/{_repository(repo)}/issues?state=all&sort=updated&direction=asc&per_page=100")

    def list_comments(self, repo: str) -> list[dict[str, Any]]:
        return self.get_pages(f"/repos/{_repository(repo)}/issues/comments?sort=updated&direction=asc&per_page=100")

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._object("GET", f"/repos/{_repository(repo)}/issues/{_number(number)}")

    def get_repo(self, repo: str) -> dict[str, Any]:
        return self._object("GET", f"/repos/{_repository(repo)}")

    def get_commit(self, repo: str, ref: str = 'main') -> dict[str, Any]:
        if ref != 'main':
            raise ValueError('Onboarding reads published main only')
        return self._object("GET", f"/repos/{_repository(repo)}/commits/main")

    def list_releases(self, repo: str) -> list[dict[str, Any]]:
        return self.get_pages(f"/repos/{_repository(repo)}/releases?per_page=100")

    def tag_commit(self, repo: str, tag: str) -> str:
        """Resolve an actual Git tag, peeling at most eight annotated tags."""
        repo = _repository(repo)
        if (not isinstance(tag, str) or not tag or len(tag) > 1024
                or re.search(r'[\x00-\x20\x7f~^:?*\[\\]', tag)
                or '..' in tag or '@{' in tag or tag == '@' or tag.endswith('.')
                or any(not part or part.startswith('.') or part.endswith('.lock') for part in tag.split('/'))):
            raise ValueError('Expected a valid Git tag name')
        reference = self._object('GET', f'/repos/{repo}/git/ref/tags/{quote(tag, safe="")}')
        if reference.get('ref') != 'refs/tags/' + tag:
            raise GitHubError('GitHub returned an unexpected tag reference')
        obj = reference.get('object')
        seen = set()
        while True:
            if (not isinstance(obj, dict) or not isinstance(obj.get('sha'), str)
                    or not re.fullmatch(r'[0-9a-f]{40}', obj['sha'])):
                raise GitHubError('GitHub returned an invalid tag object')
            if obj.get('type') == 'commit':
                return obj['sha']
            if obj.get('type') != 'tag':
                raise GitHubError('GitHub tag does not resolve to a commit')
            if obj['sha'] in seen or len(seen) >= 8:
                raise GitHubError('GitHub annotated tag chain repeated or exceeded its limit')
            seen.add(obj['sha'])
            annotated = self._object('GET', f'/repos/{repo}/git/tags/{obj["sha"]}')
            if annotated.get('sha') != obj['sha']:
                raise GitHubError('GitHub returned an unexpected annotated tag')
            obj = annotated.get('object')

    def successful_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('Expected a complete commit SHA')
        current = self._url(f'/repos/{_repository(repo)}/actions/runs?branch=main&head_sha={sha}&status=success&per_page=100')
        seen, runs = set(), []
        while current:
            if current in seen or len(seen) >= _MAX_PAGES:
                raise GitHubError('GitHub workflow pagination repeated or exceeded its page limit')
            seen.add(current)
            data, current = self._request('GET', current)
            if (not isinstance(data, dict) or not isinstance(data.get('workflow_runs'), list)
                    or any(not isinstance(run, dict) for run in data['workflow_runs'])):
                raise GitHubError('GitHub returned an unexpected workflow response')
            # A green unrelated workflow (or a pull_request_target run on the
            # base branch) cannot stand in for the plugin's release-on-push run.
            runs.extend(run for run in data['workflow_runs']
                        if run.get('head_sha') == sha and run.get('head_branch') == 'main'
                        and run.get('conclusion') == 'success' and run.get('status') == 'completed'
                        and run.get('event') == 'push'
                        and run.get('path') in {'.github/workflows/release.yml', '.github/workflows/release.yaml'})
        return runs

    def get_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.get_pages(f"/repos/{_repository(repo)}/issues/{_number(number)}/comments?per_page=100")

    def me(self) -> dict[str, Any]:
        return self._object("GET", "/user")

    def post_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Comment body must be a non-empty string")
        return self._object("POST", f"/repos/{_repository(repo)}/issues/{_number(number)}/comments", {"body": body})

    def close_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._object("PATCH", f"/repos/{_repository(repo)}/issues/{_number(number)}", {"state": "closed"})
