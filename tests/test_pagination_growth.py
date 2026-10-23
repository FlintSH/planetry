"""Offline regressions for new pages behind an unchanged terminal page body."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from planetry.github import GitHub, GitHubError
from test_github import API, SECRET, FakeOpen, Response, http_error


def records(start, stop):
    return [{"id": number} for number in range(start, stop)]


class GrowingPages:
    """GitHub-like ETags cover page bodies, but not newly added next links."""

    def __init__(self, path, page_size, total):
        self.path = path
        self.page_size = page_size
        self.total = total
        self.requests = []

    def page_url(self, page):
        if page == 1:
            return API + self.path
        return API + self.path + ("&" if "?" in self.path else "?") + f"page={page}"

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        page = int(parse_qs(urlsplit(request.full_url).query).get("page", ["1"])[0])
        if request.full_url != self.page_url(page):
            raise AssertionError("Unexpected pagination URL")
        start = (page - 1) * self.page_size + 1
        payload = records(start, min(start + self.page_size, self.total + 1))
        etag = f'"page-{page}-items-{len(payload)}"'
        if request.get_header("If-none-match") == etag:
            # Real GitHub can omit Link on 304 even when pagination grew.
            raise http_error(304)
        response_headers = {"ETag": etag}
        if page * self.page_size < self.total:
            response_headers["Link"] = f'<{self.page_url(page + 1)}>; rel="next"'
        return Response(payload, response_headers=response_headers)


class PaginationGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)

    def client(self, opener, state_dir=None):
        return GitHub(state_dir or self.state_dir, token_provider=lambda: SECRET, opener=opener)

    def test_comment_growth_past_exact_boundaries_survives_cached_304s_and_restart(self):
        path = "/repos/FlintSH/planetry/issues/42/comments?per_page=100"
        for initial, total in ((100, 101), (100, 156), (200, 201), (200, 256)):
            for restart in (False, True):
                with self.subTest(initial=initial, total=total, restart=restart):
                    state_dir = self.state_dir / f"{initial}-{total}-{restart}"
                    server = GrowingPages(path, 100, initial)
                    client = self.client(server, state_dir)
                    self.assertEqual(client.get_comments("FlintSH/planetry", 42), records(1, initial + 1))
                    old_pages = initial // 100
                    terminal_url = server.page_url(old_pages)
                    cached = json.loads(client._cache_path(terminal_url).read_text())
                    self.assertEqual(len(cached["payload"]), 100)
                    self.assertIsNone(cached["next_url"])

                    server.total = total
                    if restart:
                        client = self.client(server, state_dir)
                    expected = records(1, total + 1)
                    self.assertEqual(client.get_comments("FlintSH/planetry", 42), expected)
                    second_poll = server.requests[old_pages:]
                    self.assertEqual([request.full_url for request in second_poll],
                                     [server.page_url(page) for page in range(1, old_pages + 2)])
                    for request in second_poll[:-2]:
                        self.assertIsNotNone(request.get_header("If-none-match"))
                    self.assertIsNone(second_poll[-2].get_header("If-none-match"))
                    self.assertIsNone(second_poll[-1].get_header("If-none-match"))

                    # The new link is persisted even though the old body/ETag did not change.
                    cached = json.loads(client._cache_path(terminal_url).read_text())
                    self.assertEqual(cached["next_url"], server.page_url(old_pages + 1))
                    before = len(server.requests)
                    result = client.get_comments("FlintSH/planetry", 42)
                    self.assertEqual(result, expected)
                    repeated = server.requests[before:]
                    self.assertEqual(len(repeated), old_pages + 1)
                    self.assertTrue(all(request.get_header("If-none-match") for request in repeated))
                    self.assertEqual(len({item["id"] for item in result}), total)

    def test_general_list_growth_uses_default_or_explicit_page_size(self):
        for query, page_size in (
            ("", 30), ("?per_page=1", 1), ("?per_page=7", 7),
            ("?per_page=100", 100), ("?per_page=%32", 2),
            ("?per%5Fpage=2", 2), ("?filter=per_page%3D1", 30),
        ):
            with self.subTest(query=query):
                path = "/repos/FlintSH/planetry/releases" + query
                server = GrowingPages(path, page_size, page_size)
                client = self.client(server, self.state_dir / str(page_size) / query.encode().hex())
                self.assertEqual(client.get_pages(path), records(1, page_size + 1))
                server.total += 1
                self.assertEqual(client.get_pages(path), records(1, page_size + 2))
                self.assertIsNone(server.requests[1].get_header("If-none-match"))
                self.assertEqual(len(server.requests), 3)

    def test_empty_and_partial_terminal_pages_keep_conditional_requests(self):
        for query, counts in (("", (0, 1, 29)), ("?per_page=7", (0, 1, 6)),
                              ("?per_page=100", (0, 30, 99)), ("?per_page=invalid", (0,))):
            for count in counts:
                with self.subTest(query=query, count=count):
                    path = "/repos/FlintSH/planetry/releases" + query
                    payload = records(1, count + 1)
                    opener = FakeOpen(Response(payload, response_headers={"ETag": '"partial"'}),
                                      http_error(304), http_error(304))
                    client = self.client(opener, self.state_dir / str(count) / query.encode().hex())
                    for _ in range(3):
                        self.assertEqual(client.get_pages(path), payload)
                    self.assertEqual(len(opener.requests), 3)
                    self.assertEqual([request.get_header("If-none-match") for request in opener.requests],
                                     [None, '"partial"', '"partial"'])

    def test_unknown_page_size_revalidates_nonempty_terminal_without_guessing_urls(self):
        for query in ("per_page=", "per_page=0", "per_page=-1", "per_page=101",
                      "per_page=1.5", "per_page=invalid", "per_page=100&per_page=1",
                      "per_page=" + "9" * 5000):
            with self.subTest(query=query[:80]):
                path = "/repos/FlintSH/planetry/releases?" + query
                payload = records(1, 3)
                opener = FakeOpen(*(Response(payload, response_headers={"ETag": '"same"'}) for _ in range(2)))
                # Each subtest starts without a cache, including the oversized query.
                with tempfile.TemporaryDirectory(dir=self.state_dir) as state_dir:
                    client = self.client(opener, Path(state_dir))
                    self.assertEqual(client.get_pages(path), payload)
                    self.assertEqual(client.get_pages(path), payload)
                self.assertEqual([request.full_url for request in opener.requests], [API + path] * 2)
                self.assertTrue(all(request.get_header("If-none-match") is None for request in opener.requests))

    def test_full_terminal_without_server_link_stops_after_one_request_per_poll(self):
        path = "/repos/FlintSH/planetry/issues?per_page=100"
        payload = records(1, 101)
        opener = FakeOpen(*(Response(payload, response_headers={"ETag": '"unchanged"'}) for _ in range(3)))
        client = self.client(opener)
        for _ in range(3):
            self.assertEqual(client.get_pages(path), payload)
        self.assertEqual([request.full_url for request in opener.requests], [API + path] * 3)
        self.assertTrue(all(request.get_header("If-none-match") is None for request in opener.requests))

    def test_revalidated_terminal_page_still_rejects_untrusted_next_host(self):
        path = "/repos/FlintSH/planetry/issues?per_page=100"
        payload = records(1, 101)
        opener = FakeOpen(
            Response(payload, response_headers={"ETag": '"unchanged"'}),
            Response(payload, response_headers={"ETag": '"unchanged"',
                     "Link": '<https://attacker.invalid/comments?page=2>; rel="next"'}),
        )
        client = self.client(opener)
        self.assertEqual(client.get_pages(path), payload)
        with self.assertRaises(GitHubError):
            client.get_pages(path)
        self.assertEqual([request.full_url for request in opener.requests], [API + path] * 2)

    def test_newly_discovered_pages_remain_bounded(self):
        path = "/repos/FlintSH/planetry/issues?per_page=100"
        server = GrowingPages(path, 100, 200)
        client = self.client(server)
        self.assertEqual(client.get_pages(path), records(1, 201))
        server.total = 201
        with patch("planetry.github._MAX_PAGES", 2):
            with self.assertRaisesRegex(GitHubError, "exceeded its page limit"):
                client.get_pages(path)
        self.assertEqual([request.full_url for request in server.requests],
                         [server.page_url(1), server.page_url(2)] * 2)


if __name__ == "__main__":
    unittest.main()
