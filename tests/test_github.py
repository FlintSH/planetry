"""Offline contract tests for GitHub pagination, credentials and write outcomes."""

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from planetry.github import AmbiguousMutationError, GitHub, GitHubError, RetryableError, _SafeRedirect


SECRET = "test-token-never-log-this"
API = "https://api.github.com"


def headers(values=None):
    result = Message()
    for key, value in (values or {}).items():
        result[key] = value
    return result


class Response(BytesIO):
    def __init__(self, body, status=200, response_headers=None, *, raw=False):
        super().__init__(body if raw else json.dumps(body).encode())
        self.status = status
        self.headers = headers(response_headers)


class FakeOpen:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.replies:
            raise AssertionError("Unexpected extra API request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def http_error(status, body=None, response_headers=None):
    return HTTPError(
        API + "/private?must_not_be_logged=" + SECRET,
        status,
        SECRET,
        headers(response_headers),
        BytesIO(json.dumps(body or {"message": SECRET}).encode()),
    )


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)

    def client(self, *replies, **kwargs):
        opener = FakeOpen(*replies)
        return GitHub(self.state_dir, token_provider=lambda: SECRET, opener=opener, **kwargs), opener

    def test_cached_first_page_still_fetches_changed_second_page(self):
        first = "/repos/FlintSH/planetry/issues?per_page=100"
        second = API + first + "&page=2"
        client, opener = self.client(
            Response([{"id": 1}], response_headers={"ETag": '"one"', "Link": f'<{second}>; rel="next"'}),
            Response([{"id": 2}], response_headers={"ETag": '"two"'}),
            http_error(304),
            Response([{"id": 2}, {"id": 3}], response_headers={"ETag": '"three"'}),
        )
        self.assertEqual(client.get_pages(first), [{"id": 1}, {"id": 2}])
        self.assertEqual(client.get_pages(first), [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual([r.full_url for r in opener.requests], [API + first, second] * 2)
        self.assertEqual(opener.requests[2].get_header("If-none-match"), '"one"')
        self.assertEqual(opener.requests[3].get_header("If-none-match"), '"two"')

    def test_cache_survives_a_new_client_and_does_not_contain_token(self):
        client, _ = self.client(Response({"id": 123}, response_headers={"ETag": '"who"'}))
        self.assertEqual(client.me(), {"id": 123})
        second_client, second_opener = self.client(http_error(304))
        self.assertEqual(second_client.me(), {"id": 123})
        self.assertEqual(second_opener.requests[0].get_header("If-none-match"), '"who"')
        cache_files = list((self.state_dir / "github-cache").glob("*.json"))
        self.assertEqual(len(cache_files), 1)
        self.assertNotIn(SECRET, cache_files[0].read_text())
        self.assertEqual(cache_files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state_dir / "github-cache").stat().st_mode & 0o777, 0o700)

    def test_relative_pagination_and_quoted_relation(self):
        client, opener = self.client(
            Response([{"id": 1}], response_headers={"Link": '<?page=2>; type="application/json"; rel="next last"'}),
            Response([{"id": 2}]),
        )
        self.assertEqual(client.get_comments("FlintSH/planetry", 4), [{"id": 1}, {"id": 2}])
        self.assertEqual(opener.requests[1].full_url, API + "/repos/FlintSH/planetry/issues/4/comments?page=2")

    def test_hostile_pagination_cannot_send_credentials_elsewhere(self):
        for target in (
            "https://attacker.invalid/steal", "//attacker.invalid/steal", "http://api.github.com/steal",
            "https://user@api.github.com/steal", "https://api.github.com:444/steal",
        ):
            with self.subTest(target=target):
                client, opener = self.client(Response([], response_headers={"Link": f'<{target}>; rel="next"'}))
                with self.assertRaises(GitHubError):
                    client.get_pages("/repos/FlintSH/planetry/issues")
                self.assertEqual(len(opener.requests), 1)

    def test_redirect_handler_rejects_foreign_host_before_following(self):
        client, _ = self.client()
        redirect = _SafeRedirect(client._validate_url)
        request = Request(API + "/old", headers={"Authorization": "Bearer " + SECRET})
        with self.assertRaises(GitHubError):
            redirect.redirect_request(request, None, 302, "Found", {}, "https://attacker.invalid/steal")
        new_request = redirect.redirect_request(request, None, 301, "Moved", {}, API + "/new")
        self.assertEqual(new_request.full_url, API + "/new")
        self.assertEqual(new_request.get_header("Authorization"), "Bearer " + SECRET)

    def test_redirect_handler_never_replays_mutations(self):
        client, _ = self.client()
        request = Request(API + "/old", data=b"{}", method="POST")
        with self.assertRaises(GitHubError):
            _SafeRedirect(client._validate_url).redirect_request(request, None, 302, "Moved", {}, API + "/new")

    def test_repeating_pagination_is_bounded(self):
        path = "/repos/FlintSH/planetry/issues"
        client, opener = self.client(Response([], response_headers={"Link": f'<{API + path}>; rel="next"'}))
        with self.assertRaisesRegex(GitHubError, "pagination repeated"):
            client.get_pages(path)
        self.assertEqual(len(opener.requests), 1)

    def test_repo_and_issue_number_validation_occurs_before_network(self):
        client, opener = self.client()
        for repo in ("owner", "owner/repo/more", "../repo", "owner/..", "https://bad/repo", "owner/repo?url=bad", "owner/repo\n"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                client.list_issues(repo)
        for number in (0, -1, "1/../../user", True, 1.5):
            with self.subTest(number=number), self.assertRaises(ValueError):
                client.get_issue("FlintSH/planetry", number)
            with self.subTest(reopen_number=number), self.assertRaises(ValueError):
                client.reopen_issue("FlintSH/planetry", number)
        with self.assertRaises(ValueError):
            client.reopen_issue("owner/repo/extra", 1)
        self.assertEqual(opener.requests, [])

    def test_explicit_api_path_cannot_change_origin(self):
        client, opener = self.client()
        for path in ("https://attacker.invalid/x", "//attacker.invalid/x", "http://api.github.com/x", "/../private", "/x#fragment", "/x\r\nAuthorization:bad"):
            with self.subTest(path=path), self.assertRaises(GitHubError):
                client.get_pages(path)
        self.assertEqual(opener.requests, [])

    def test_enterprise_api_base_path_and_pagination(self):
        client, opener = self.client(
            Response([{"id": 1}], response_headers={"Link": '</api/v3/repos/owner/repo/issues?page=2>; rel="next"'}),
            Response([{"id": 2}]),
            api_url="https://github.example/api/v3",
        )
        self.assertEqual(client.list_issues("owner/repo"), [{"id": 1}, {"id": 2}])
        self.assertTrue(opener.requests[0].full_url.startswith("https://github.example/api/v3/repos/"))
        with self.assertRaises(GitHubError):
            client.get_pages("https://github.example/admin")

    def test_rate_limit_and_server_errors_have_sanitized_backoff(self):
        cases = (
            (429, {}, 60),
            (403, {"Retry-After": "180"}, 180),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1400"}, 401),
            (503, {}, 60),
        )
        for status, response_headers, expected in cases:
            with self.subTest(status=status, headers=response_headers):
                client, _ = self.client(http_error(status, response_headers=response_headers))
                with patch("planetry.github.time.time", return_value=1000), self.assertRaises(RetryableError) as raised:
                    client.me()
                self.assertEqual(raised.exception.retry_after, expected)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertFalse(raised.exception.mutation_ambiguous)

    def test_secondary_rate_limit_detected_without_headers(self):
        client, _ = self.client(http_error(403, {"message": "You have exceeded a secondary rate limit " + SECRET}))
        with self.assertRaises(RetryableError) as raised:
            client.me()
        self.assertNotIn(SECRET, str(raised.exception))

    def test_permission_error_is_not_treated_as_rate_limit(self):
        client, _ = self.client(http_error(403))
        with self.assertRaises(GitHubError) as raised:
            client.me()
        self.assertNotIsInstance(raised.exception, RetryableError)
        self.assertEqual(str(raised.exception), "GitHub request failed (HTTP 403)")

    def test_read_timeout_retryable_write_timeout_requires_reconciliation(self):
        for operation, expected in (
            (lambda client: client.me(), RetryableError),
            (lambda client: client.post_comment("FlintSH/planetry", 1, "hello"), AmbiguousMutationError),
            (lambda client: client.close_issue("FlintSH/planetry", 1), AmbiguousMutationError),
            (lambda client: client.reopen_issue("FlintSH/planetry", 1), AmbiguousMutationError),
        ):
            with self.subTest(expected=expected):
                client, opener = self.client(URLError(SECRET))
                with self.assertRaises(expected) as raised:
                    operation(client)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertEqual(len(opener.requests), 1)

    def test_http_500_write_is_marked_ambiguous_and_not_retried(self):
        client, opener = self.client(http_error(500))
        with self.assertRaises(RetryableError) as raised:
            client.post_comment("FlintSH/planetry", 1, "hello")
        self.assertTrue(raised.exception.mutation_ambiguous)
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_json_does_not_leak_body_and_write_is_ambiguous(self):
        for write in (False, True):
            with self.subTest(write=write):
                client, _ = self.client(Response(SECRET.encode(), raw=True))
                with self.assertRaises(AmbiguousMutationError if write else GitHubError) as raised:
                    if write:
                        client.post_comment("FlintSH/planetry", 1, "hello")
                    else:
                        client.me()
                self.assertNotIn(SECRET, str(raised.exception))

    def test_uncached_304_is_not_an_empty_success(self):
        client, _ = self.client(http_error(304))
        with self.assertRaisesRegex(GitHubError, "uncached"):
            client.list_issues("FlintSH/planetry")

    def test_invalid_cache_is_ignored(self):
        client, opener = self.client(Response({"id": 123}))
        client._cache_path(API + "/user").write_text('{"version":')
        self.assertEqual(client.me()["id"], 123)
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))

    def test_authentication_errors_do_not_include_provider_output(self):
        def bad_token_provider():
            raise RuntimeError(SECRET)
        opener = FakeOpen()
        client = GitHub(self.state_dir, token_provider=bad_token_provider, opener=opener)
        with self.assertRaises(GitHubError) as raised:
            client.me()
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertEqual(opener.requests, [])

    def test_public_methods_use_exact_paths_and_json_writes(self):
        comment = "A quote: \". Literal shell: $(no) `no`\nAnother line."
        client, opener = self.client(
            Response([]), Response([]), Response({"number": 42}),
            Response({"id": 9}, 201), Response({"number": 42, "state": "closed"}),
            Response({"number": 42, "state": "open"}),
        )
        self.assertEqual(client.list_issues("FlintSH/planetry"), [])
        self.assertEqual(client.list_comments("FlintSH/planetry"), [])
        self.assertEqual(client.get_issue("FlintSH/planetry", 42), {"number": 42})
        self.assertEqual(client.post_comment("FlintSH/planetry", 42, comment), {"id": 9})
        self.assertEqual(client.close_issue("FlintSH/planetry", 42)["state"], "closed")
        self.assertEqual(client.reopen_issue("FlintSH/planetry", 42), {"number": 42, "state": "open"})
        self.assertEqual(opener.requests[0].full_url, API + "/repos/FlintSH/planetry/issues?state=all&sort=updated&direction=asc&per_page=100")
        self.assertEqual(opener.requests[1].full_url, API + "/repos/FlintSH/planetry/issues/comments?sort=updated&direction=asc&per_page=100")
        self.assertEqual(opener.requests[2].full_url, API + "/repos/FlintSH/planetry/issues/42")
        self.assertEqual(opener.requests[3].method, "POST")
        self.assertEqual(json.loads(opener.requests[3].data), {"body": comment})
        self.assertEqual(opener.requests[4].method, "PATCH")
        self.assertEqual(json.loads(opener.requests[4].data), {"state": "closed"})
        self.assertEqual(opener.requests[5].full_url, API + "/repos/FlintSH/planetry/issues/42")
        self.assertEqual(opener.requests[5].method, "PATCH")
        self.assertEqual(json.loads(opener.requests[5].data), {"state": "open"})
        self.assertTrue(all(timeout == 30 for timeout in opener.timeouts))

    def test_onboarding_repository_and_published_main_have_fixed_paths(self):
        sha = 'a' * 40
        client, opener = self.client(Response({'full_name': 'FlintSH/planetry-test'}), Response({'sha': sha}))
        self.assertEqual(client.get_repo('FlintSH/planetry-test')['full_name'], 'FlintSH/planetry-test')
        self.assertEqual(client.get_commit('FlintSH/planetry-test')['sha'], sha)
        self.assertEqual([request.full_url for request in opener.requests], [
            API + '/repos/FlintSH/planetry-test', API + '/repos/FlintSH/planetry-test/commits/main'])
        self.assertTrue(all(request.get_method() == 'GET' for request in opener.requests))

    def test_onboarding_methods_reject_noncanonical_inputs_before_requests(self):
        client, opener = self.client()
        sha = 'a' * 40
        for method in (client.get_repo, client.get_commit, client.list_releases,
                       lambda repo: client.successful_runs(repo, sha)):
            for repo in ('owner/repo/extra', 'owner/repo?redirect=1', 'https://attacker.invalid/repo', 'owner/..'):
                with self.subTest(method=method, repo=repo), self.assertRaises(ValueError):
                    method(repo)
        for ref in ('feature/new', '../main', 'main?redirect=1', sha, None):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                client.get_commit('FlintSH/planetry-test', ref)
        for invalid_sha in ('a' * 39, 'a' * 41, 'A' * 40, 'g' * 40, 'a' * 40 + '&branch=other', None, 123):
            with self.subTest(sha=invalid_sha), self.assertRaises(ValueError):
                client.successful_runs('FlintSH/planetry-test', invalid_sha)
        self.assertEqual(opener.requests, [])

    def test_release_listing_uses_all_pages_with_cached_pagination(self):
        path = '/repos/FlintSH/planetry-test/releases?per_page=100'
        second = API + path + '&page=2'
        client, opener = self.client(
            Response([{'id': 1, 'draft': True}], response_headers={'ETag': '"releases-one"', 'Link': f'<{second}>; rel="next"'}),
            Response([{'id': 2, 'draft': False}], response_headers={'ETag': '"releases-two"'}),
            http_error(304), http_error(304),
        )
        expected = [{'id': 1, 'draft': True}, {'id': 2, 'draft': False}]
        self.assertEqual(client.list_releases('FlintSH/planetry-test'), expected)
        self.assertEqual(client.list_releases('FlintSH/planetry-test'), expected)
        self.assertEqual([request.full_url for request in opener.requests], [API + path, second] * 2)

    def test_successful_runs_require_exact_release_workflow_push_and_main_commit(self):
        sha = 'a' * 40
        base = {'head_sha': sha, 'head_branch': 'main', 'conclusion': 'success',
                'status': 'completed', 'event': 'push', 'path': '.github/workflows/release.yml'}
        valid = [dict(base, id=1), dict(base, id=2, path='.github/workflows/release.yaml')]
        invalid = [dict(base, **change) for change in (
            {'head_sha': 'b' * 40}, {'head_branch': 'feature'}, {'conclusion': 'failure'},
            {'conclusion': 'neutral'}, {'status': 'in_progress'}, {'event': 'pull_request'},
            {'event': 'pull_request_target'}, {'event': 'workflow_dispatch'},
            {'path': '.github/workflows/lint.yml'}, {'path': '.github/workflows/release.yml@refs/heads/feature'},
            {'path': '.github/workflows/RELEASE.yml'}, {'path': None},
        )]
        missing_path = dict(base)
        del missing_path['path']
        client, opener = self.client(Response({'workflow_runs': invalid + [missing_path] + valid}))
        self.assertEqual(client.successful_runs('FlintSH/planetry-test', sha), valid)
        self.assertEqual(opener.requests[0].full_url,
            API + f'/repos/FlintSH/planetry-test/actions/runs?branch=main&head_sha={sha}&status=success&per_page=100')

    def test_successful_release_run_on_later_page_is_not_hidden_by_cache(self):
        sha = 'c' * 40
        path = f'/repos/FlintSH/planetry-test/actions/runs?branch=main&head_sha={sha}&status=success&per_page=100'
        run = {'id': 9, 'head_sha': sha, 'head_branch': 'main', 'conclusion': 'success',
               'status': 'completed', 'event': 'push', 'path': '.github/workflows/release.yml'}
        client, opener = self.client(
            Response({'workflow_runs': []}, response_headers={'ETag': '"runs-one"', 'Link': '<?page=2>; rel="next"'}),
            Response({'workflow_runs': [run]}, response_headers={'ETag': '"runs-two"'}),
            http_error(304), http_error(304),
        )
        self.assertEqual(client.successful_runs('FlintSH/planetry-test', sha), [run])
        self.assertEqual(client.successful_runs('FlintSH/planetry-test', sha), [run])
        self.assertEqual(opener.requests[0].full_url, API + path)
        self.assertEqual(opener.requests[1].full_url, API + '/repos/FlintSH/planetry-test/actions/runs?page=2')
        self.assertEqual(opener.requests[3].get_header('If-none-match'), '"runs-two"')

    def test_workflow_pagination_rejects_foreign_host_and_cycles(self):
        sha = 'a' * 40
        path = f'/repos/FlintSH/planetry-test/actions/runs?branch=main&head_sha={sha}&status=success&per_page=100'
        for target in ('https://attacker.invalid/runs', API + path):
            with self.subTest(target=target):
                client, opener = self.client(Response({'workflow_runs': []}, response_headers={'Link': f'<{target}>; rel="next"'}))
                with self.assertRaises(GitHubError):
                    client.successful_runs('FlintSH/planetry-test', sha)
                self.assertEqual(len(opener.requests), 1)

    def test_malformed_workflow_response_cannot_approve_onboarding(self):
        for payload in ({}, {'workflow_runs': None}, {'workflow_runs': {}}, {'workflow_runs': [None]}, []):
            with self.subTest(payload=payload):
                client, _ = self.client(Response(payload))
                with self.assertRaises(GitHubError):
                    client.successful_runs('FlintSH/planetry-test', 'a' * 40)

    def test_lightweight_tag_resolves_actual_commit_with_encoded_name(self):
        tag = 'releases/v1.0+build#2'
        sha = 'a' * 40
        client, opener = self.client(Response({'ref': 'refs/tags/' + tag,
                                               'object': {'type': 'commit', 'sha': sha}}))
        self.assertEqual(client.tag_commit('FlintSH/planetry-test', tag), sha)
        self.assertEqual(opener.requests[0].full_url,
            API + '/repos/FlintSH/planetry-test/git/ref/tags/releases%2Fv1.0%2Bbuild%232')
        self.assertEqual(len(opener.requests), 1)

    def test_nested_annotated_tags_are_peeled_to_the_commit(self):
        first, second, commit = '1' * 40, '2' * 40, '3' * 40
        client, opener = self.client(
            Response({'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': first}}),
            Response({'sha': first, 'object': {'type': 'tag', 'sha': second}}),
            Response({'sha': second, 'object': {'type': 'commit', 'sha': commit}}),
        )
        self.assertEqual(client.tag_commit('FlintSH/planetry-test', 'v1'), commit)
        self.assertEqual([request.full_url for request in opener.requests], [
            API + '/repos/FlintSH/planetry-test/git/ref/tags/v1',
            API + '/repos/FlintSH/planetry-test/git/tags/' + first,
            API + '/repos/FlintSH/planetry-test/git/tags/' + second])

    def test_invalid_tag_names_are_rejected_before_requests(self):
        client, opener = self.client()
        for tag in (None, 1, '', '../heads/main', 'a..b', '.hidden', 'v1/.hidden', 'v1//tag',
                    'v1/', 'v1.', 'v1.lock', 'v1?query=1', 'a b', 'v1\n', 'v1@{next}', 'v1\\tag'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                client.tag_commit('FlintSH/planetry-test', tag)
        with self.assertRaises(ValueError):
            client.tag_commit('FlintSH/repo/extra', 'v1')
        self.assertEqual(opener.requests, [])

    def test_wrong_tag_reference_or_noncommit_target_is_rejected(self):
        for payload in (
            {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'a' * 40}},
            {'ref': 'refs/tags/other', 'object': {'type': 'commit', 'sha': 'a' * 40}},
            {'ref': 'refs/tags/v1', 'object': {'type': 'tree', 'sha': 'a' * 40}},
            {'ref': 'refs/tags/v1', 'object': {'type': 'blob', 'sha': 'a' * 40}},
            {'ref': 'refs/tags/v1', 'object': {'type': 'commit', 'sha': 'short'}},
            {'ref': 'refs/tags/v1', 'object': None},
        ):
            with self.subTest(payload=payload):
                client, _ = self.client(Response(payload))
                with self.assertRaises(GitHubError):
                    client.tag_commit('FlintSH/planetry-test', 'v1')

    def test_annotated_tag_identity_and_cycles_are_checked(self):
        sha = 'a' * 40
        for annotated in (
            {'sha': 'b' * 40, 'object': {'type': 'commit', 'sha': 'c' * 40}},
            {'sha': sha, 'object': {'type': 'tag', 'sha': sha}},
        ):
            with self.subTest(annotated=annotated):
                client, opener = self.client(
                    Response({'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': sha}}),
                    Response(annotated),
                )
                with self.assertRaises(GitHubError):
                    client.tag_commit('FlintSH/planetry-test', 'v1')
                self.assertEqual(len(opener.requests), 2)

    def test_annotated_tag_depth_is_bounded_at_eight(self):
        chain = [f'{n:040x}' for n in range(1, 10)]
        replies = [Response({'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': chain[0]}})]
        replies.extend(Response({'sha': chain[index], 'object': {'type': 'tag', 'sha': chain[index + 1]}})
                       for index in range(8))
        client, opener = self.client(*replies)
        with self.assertRaisesRegex(GitHubError, 'exceeded its limit'):
            client.tag_commit('FlintSH/planetry-test', 'v1')
        self.assertEqual(len(opener.requests), 9)


if __name__ == "__main__":
    unittest.main()
