"""Parallel issue routing preserves running work and durable capacity limits."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from planetry.__main__ import load_config, refresh_router
from planetry.router import Router
from test_router import FakeGitHub, FakeT3, OTHER_REPO, REPO, issue


class ParallelRouterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        control = self.root / 'control'
        control.mkdir()
        (control / 'repositories.json').write_text(json.dumps({
            'repositories': [{'name': REPO}, {'name': OTHER_REPO}]}))
        self.config_path = self.root / 'operator.json'
        self.config = {
            'allowed_github_user': {'id': 901, 'login': 'person-901'},
            'state_dir': str(self.root / 'state'), 'control_root': str(control),
            'max_active_threads': 1, 'max_new_threads_per_day': 20,
            'repositories': [{
                'name': REPO, 'workspace': str(self.root / 'chat'),
                'project_id': '12345678-abcd-1234-5678-123456789abc'}],
        }
        self.write_config()
        self.github, self.t3 = FakeGitHub(), FakeT3()
        self.router = Router(load_config(self.config_path), self.github, self.t3)
        self.addCleanup(lambda: self.router.store.db.close())

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def refresh_limit(self, limit):
        self.config['max_active_threads'] = limit
        self.write_config()
        refresh_router(self.router)

    def restart(self):
        self.router.store.db.close()
        self.router = Router(load_config(self.config_path), self.github, self.t3)

    def identity(self, number, repo=REPO):
        row = self.router.store.issue(repo, number)
        return row['thread_id'], row['last_run_id']

    def assert_queued(self, *numbers):
        for number in numbers:
            with self.subTest(queued_issue=number):
                self.assertIsNone(self.router.store.issue(REPO, number)['thread_id'])
                self.assertTrue(self.router.store.events(REPO, number))

    def test_live_raise_to_three_preserves_running_identity_and_drains_queue(self):
        manual = {
            'threadId': 'manual-thread', 'latestRunId': 'manual-run',
            'title': 'Existing operator work', 'status': 'running',
            'projectId': self.config['repositories'][0]['project_id'],
        }
        self.t3.threads[manual['threadId']] = copy.deepcopy(manual)
        for number in range(1, 6):
            self.github.add_issue(issue(number))
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        original_identity = self.identity(1)
        original_thread = copy.deepcopy(self.t3.threads['thread-1'])
        store, connection = self.router.store, self.router.store.db

        self.refresh_limit(3)
        self.router.tick()
        self.assertIs(store, self.router.store)
        self.assertIs(connection, self.router.store.db)
        self.assertIs(self.github, self.router.github)
        self.assertIs(self.t3, self.router.t3)
        self.assertEqual(original_identity, self.identity(1))
        self.assertEqual(original_thread, self.t3.threads['thread-1'])
        self.assertEqual(manual, self.t3.threads['manual-thread'])
        self.assertEqual(3, len(self.t3.launches))
        self.assertEqual({'thread-1', 'thread-2', 'thread-3'},
                         {self.identity(number)[0] for number in (1, 2, 3)})
        self.assertEqual({self.config['repositories'][0]['project_id']},
                         {launch['projectId'] for launch in self.t3.launches})
        self.assert_queued(4, 5)

        self.t3.settle('thread-2')
        self.router.tick()
        self.assertEqual(('thread-4', 'run-4'), self.identity(4))
        self.assert_queued(5)
        self.t3.settle('thread-3')
        self.router.tick()
        self.router.tick()
        self.assertEqual(('thread-5', 'run-5'), self.identity(5))
        self.assertEqual(5, len(self.t3.launches))
        self.assertEqual(original_identity, self.identity(1))
        self.assertEqual(original_thread, self.t3.threads['thread-1'])
        self.assertEqual(manual, self.t3.threads['manual-thread'])
        self.assertEqual({}, self.t3.sent)
        self.assertEqual([], self.router.store.pending_issues())

    def test_parallel_worktree_branches_use_global_github_ids(self):
        self.config['repositories'].append({
            'name': OTHER_REPO, 'workspace': str(self.root / 'inventory'),
            'project_id': '22222222-2222-2222-2222-222222222222'})
        self.refresh_limit(3)
        requests = [(REPO, 7, 900001), (REPO, 8, 900099), (OTHER_REPO, 7, 900123)]
        for repo, number, github_id in requests:
            value = issue(number)
            value['id'] = github_id
            self.github.add_issue(value, repo=repo)
        self.router.tick()
        self.assertEqual(3, len(self.t3.launches))
        branches = set()
        for repo, number, github_id in requests:
            with self.subTest(repo=repo, number=number):
                row = self.router.store.issue(repo, number)
                launch = next(args for args in self.t3.launches
                              if args['title'] == row['launch_title'])
                strategy = launch['workspaceStrategy']
                self.assertEqual({
                    'type': 'worktree', 'baseRef': 'main', 'startFromOrigin': True,
                    'branch': f'planetry/issue-{github_id}'}, strategy)
                self.assertEqual(self.router.repos[repo]['project_id'], launch['projectId'])
                branches.add(strategy['branch'])
        self.assertEqual(3, len(branches))
        self.assertNotEqual(self.identity(7, REPO), self.identity(7, OTHER_REPO))

    def test_followups_dedupe_and_reuse_or_reserve_an_active_slot(self):
        self.refresh_limit(3)
        for number in (1, 2, 3):
            self.github.add_issue(issue(number))
        self.router.tick()
        self.t3.settle('thread-1')
        self.github.add_issue(issue(4))
        self.router.tick()
        self.assertEqual(('thread-4', 'run-4'), self.identity(4))

        self.github.add_comment(1, 'Resume the finished issue when a slot opens.')
        self.github.add_comment(2, 'Extra detail for the issue already running.')
        self.router.tick()
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual('thread-2', next(iter(self.t3.sent.values()))['threadId'])
        self.assertIn('Extra detail for the issue already running.', self.t3.sent_messages()[0])
        self.assertEqual([], self.router.store.events(REPO, 2))
        waiting_events = self.router.store.events(REPO, 1)
        self.assertEqual(1, len(waiting_events))
        waiting_delivery = self.router.store.pending_delivery(REPO, 1)
        active_sends = copy.deepcopy(self.t3.sent)

        self.github.add_issue(issue(5))
        self.router.tick()
        self.restart()
        self.router.tick()
        self.assertEqual(active_sends, self.t3.sent)
        self.assertEqual(waiting_events, self.router.store.events(REPO, 1))
        self.assertEqual(waiting_delivery, self.router.store.pending_delivery(REPO, 1))
        self.assertEqual(4, len(self.t3.launches))
        self.assert_queued(5)

        self.t3.settle('thread-3')
        self.router.tick()
        self.router.tick()
        self.assertEqual(2, len(self.t3.sent))
        resumed = self.t3.sent[waiting_delivery['key']]
        self.assertEqual('thread-1', resumed['threadId'])
        self.assertIn('Resume the finished issue', resumed['message'])
        self.assertEqual(('thread-1', 'followup-' + waiting_delivery['key']), self.identity(1))
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertIsNone(self.router.store.pending_delivery(REPO, 1))
        self.assertEqual(4, len(self.t3.launches))
        self.assert_queued(5)

        self.t3.settle('thread-4')
        self.router.tick()
        self.router.tick()
        self.assertEqual(('thread-5', 'run-5'), self.identity(5))
        self.assertEqual(5, len(self.t3.launches))
        self.assertEqual(2, len(self.t3.sent))
        self.assertEqual(2, sum(name == 't3_thread_send' for name, _ in self.t3.calls))

    def test_uncertain_launch_reserves_one_of_three_slots_across_restart(self):
        for number in range(1, 6):
            self.github.add_issue(issue(number))
        self.t3.lose_launch_response = True
        self.t3.defer_launch_visibility = True
        with self.assertRaises(TimeoutError):
            self.router.tick()
        self.assertEqual('intent', self.router.store.issue(REPO, 1)['launch_state'])
        self.t3.defer_launch_visibility = False
        self.refresh_limit(3)
        self.restart()
        self.router.tick()
        self.router.tick()
        self.assertEqual(3, len(self.t3.launches))
        self.assertEqual('intent', self.router.store.issue(REPO, 1)['launch_state'])
        self.assertEqual((None, None), self.identity(1))
        self.assertEqual(('thread-2', 'run-2'), self.identity(2))
        self.assertEqual(('thread-3', 'run-3'), self.identity(3))
        self.assert_queued(4, 5)

        self.restart()
        self.router.tick()
        self.assertEqual(3, len(self.t3.launches))
        self.t3.threads['thread-1']['hidden'] = False
        self.router.tick()
        self.assertEqual(('thread-1', 'run-1'), self.identity(1))
        self.assertEqual('linked', self.router.store.issue(REPO, 1)['launch_state'])
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertEqual(3, len(self.t3.launches))
        self.assert_queued(4, 5)
        self.t3.settle('thread-2')
        self.router.tick()
        self.assertEqual(('thread-4', 'run-4'), self.identity(4))
        self.assertEqual(('thread-1', 'run-1'), self.identity(1))
        self.assertEqual(4, len(self.t3.launches))
        self.assertEqual({}, self.t3.sent)
        self.assert_queued(5)

    def test_malformed_accepted_launch_reserves_capacity_in_the_same_tick(self):
        self.refresh_limit(3)
        for number in range(1, 5):
            self.github.add_issue(issue(number))
        original_call = self.t3.call

        def malformed_first_launch(name, arguments):
            result = original_call(name, arguments)
            if name == 't3_thread_launch' and len(self.t3.launches) == 1:
                return {}  # The launch was accepted but its identity is unusable.
            return result

        with patch.object(self.t3, 'call', side_effect=malformed_first_launch):
            self.router.tick()
        self.assertEqual('intent', self.router.store.issue(REPO, 1)['launch_state'])
        self.assertEqual(3, len(self.t3.launches),
                         'The new unresolved intent must consume capacity immediately')
        self.assert_queued(4)

    def test_null_or_invalid_launch_identity_preserves_intent_events_and_capacity(self):
        for invalid in (None, '', ' ', 42, []):
            with self.subTest(identity=invalid), tempfile.TemporaryDirectory() as state:
                config = dict(self.router.config, state_dir=state, max_active_threads=3)
                github, t3 = FakeGitHub(), FakeT3()
                router = Router(config, github, t3)
                try:
                    for number in range(1, 5):
                        github.add_issue(issue(number))
                    original_call = t3.call

                    def malformed(name, arguments):
                        result = original_call(name, arguments)
                        if name == 't3_thread_launch' and len(t3.launches) == 1:
                            return {'threadId': invalid}
                        return result

                    with patch.object(t3, 'call', side_effect=malformed):
                        router.tick()
                    first = router.store.issue(REPO, 1)
                    self.assertEqual('intent', first['launch_state'])
                    self.assertIsNone(first['thread_id'])
                    self.assertTrue(router.store.events(REPO, 1))
                    self.assertIsNotNone(router.store.pending_delivery(REPO, 1))
                    self.assertEqual(3, len(t3.launches))
                    self.assertIsNone(router.store.issue(REPO, 4)['thread_id'])
                    # A later successful observation links the accepted thread;
                    # it never launches a replacement or exceeds capacity.
                    router.store.clear_failure(f'issue:{REPO}:1')
                    router.tick()
                    self.assertEqual('thread-1', router.store.issue(REPO, 1)['thread_id'])
                    self.assertEqual('linked', router.store.issue(REPO, 1)['launch_state'])
                    self.assertEqual([], router.store.events(REPO, 1))
                    self.assertEqual(3, len(t3.launches))
                finally:
                    router.store.db.close()

    def test_lowering_limit_defers_starts_without_cancelling_running_threads(self):
        self.refresh_limit(3)
        for number in range(1, 5):
            self.github.add_issue(issue(number))
        self.router.tick()
        identities = {number: self.identity(number) for number in (1, 2, 3)}
        threads = copy.deepcopy(self.t3.threads)

        self.refresh_limit(1)
        self.router.tick()
        self.router.tick()
        self.assertEqual(identities, {number: self.identity(number) for number in (1, 2, 3)})
        self.assertEqual(threads, self.t3.threads)
        self.assertEqual(3, len(self.t3.launches))
        self.assert_queued(4)

        for thread_id in ('thread-1', 'thread-3'):
            self.t3.settle(thread_id)
            self.router.tick()
            self.assertEqual(3, len(self.t3.launches))
            self.assert_queued(4)
        self.github.add_comment(2, 'This update belongs to the run still active.')
        self.router.tick()
        self.assertEqual('thread-2', next(iter(self.t3.sent.values()))['threadId'])
        self.assertEqual('running', self.t3.threads['thread-2']['status'])
        self.assertEqual(3, len(self.t3.launches))
        self.assert_queued(4)

        self.t3.settle('thread-2')
        self.router.tick()
        self.router.tick()
        self.assertEqual(('thread-4', 'run-4'), self.identity(4))
        self.assertEqual(4, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual([], self.router.store.pending_issues())
        self.assertTrue(all(name in {'t3_thread_list', 't3_thread_launch', 't3_thread_send'}
                            for name, _ in self.t3.calls))


if __name__ == '__main__':
    unittest.main()
