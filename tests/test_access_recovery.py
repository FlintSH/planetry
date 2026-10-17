"""Access enforcement preserves authorized work across polling and launch recovery."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from planetry.github import GitHubError
from planetry.router import Router
from planetry.store import digest
from test_router import FakeGitHub, FakeT3, REPO, issue


class AccessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = {
            'allowed_github_user': {'id': 901, 'login': 'person-901'},
            'state_dir': str(root / 'state'), 'control_root': str(root / 'control'),
            'config_path': str(root / 'operator.json'), 'max_active_threads': 1,
            'max_new_threads_per_day': 20,
            'repositories': [{'name': REPO, 'workspace': str(root / 'chat'), 'project_id': 'chat'}],
        }
        self.github, self.t3 = FakeGitHub(), FakeT3()
        self.router = Router(self.config, self.github, self.t3)
        self.addCleanup(lambda: self.router.store.db.close())

    def restart(self):
        self.router.store.db.close()
        self.router = Router(self.config, self.github, self.t3)

    def start_issue(self):
        self.github.add_issue(issue())
        self.router.tick()
        self.t3.settle()

    def legacy_comment(self, comment):
        # Model an already observed pre-policy event. Its login is display
        # metadata, and only a fresh response for this exact ID can verify it.
        with self.router.store.db:
            self.router.store.add_event(REPO, 1, 'comment', {
                'id': comment['id'], 'author': comment['user']['login'],
                'body': comment['body'], 'updated_at': comment['updated_at'],
            })
            self.router.store.observe(f'comment:{comment["id"]}', digest(comment['body']))
        return self.router.store.events(REPO, 1)[-1]

    def event_row(self, event_id):
        return dict(self.router.store.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone())

    def cache_delivery(self):
        events = self.router.store.events(REPO, 1)
        return self.router.store.save_delivery(
            REPO, 1, events, self.router.prompt(self.router.store.issue(REPO, 1), events))

    def fail_repository_poll(self):
        self.github.issue_errors[REPO] = GitHubError('GitHub request failed (HTTP 503)')

    def restore_repository_poll(self):
        del self.github.issue_errors[REPO]
        self.router.store.clear_failure('repository:' + REPO)

    def assert_unknown_pending(self, event_id, delivery):
        event = self.event_row(event_id)
        self.assertEqual('pending', event['state'])
        self.assertNotIn('author_id', json.loads(event['payload']))
        self.assertEqual(delivery, self.router.store.pending_delivery(REPO, 1))
        self.assertEqual({}, self.t3.sent)

    def test_failed_poll_preserves_legacy_update_until_fresh_identity_verification(self):
        self.start_issue()
        comment = self.github.add_comment(1, 'Authorized legacy update after an unavailable poll')
        # A rename must not prevent numeric identity verification.
        comment['user']['login'] = 'renamed-account'
        event = self.legacy_comment(comment)
        delivery = self.cache_delivery()
        self.fail_repository_poll()
        self.restart()

        self.router.tick()
        self.assertTrue(self.router.degraded)
        self.assert_unknown_pending(event['id'], delivery)
        self.assertEqual('completed', self.t3.threads['thread-1']['status'])
        self.restart()
        self.router.tick()  # Durable repository backoff also leaves it pending.
        self.assert_unknown_pending(event['id'], delivery)

        self.restore_repository_poll()
        self.restart()
        self.router.tick()
        self.restart()
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual(1, sum(name == 't3_thread_send' for name, _ in self.t3.calls))
        sent = next(iter(self.t3.sent.values()))
        self.assertEqual(delivery['key'], sent['clientRequestId'])
        self.assertEqual('thread-1', sent['threadId'])
        self.assertEqual(1, sent['message'].count(comment['body']))
        self.assertEqual(901, json.loads(self.event_row(event['id'])['payload'])['author_id'])
        self.assertEqual('delivered', self.event_row(event['id'])['state'])
        self.assertEqual('thread-1', self.router.store.issue(REPO, 1)['thread_id'])
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertIsNone(self.router.store.pending_delivery(REPO, 1))

    def test_legacy_outsider_copying_allowed_login_is_ignored_after_verification(self):
        self.start_issue()
        outsider = self.github.add_comment(1, 'OUTSIDER legacy update must never wake the thread', author=9999)
        outsider['user']['login'] = self.config['allowed_github_user']['login']
        event = self.legacy_comment(outsider)
        delivery = self.cache_delivery()
        self.fail_repository_poll()
        self.restart()

        self.router.tick()
        self.assert_unknown_pending(event['id'], delivery)
        self.restore_repository_poll()
        self.restart()
        self.router.tick()
        self.restart()
        self.router.tick()
        stored = self.event_row(event['id'])
        self.assertEqual(9999, json.loads(stored['payload'])['author_id'])
        self.assertEqual('ignored', stored['state'])
        self.assertEqual({}, self.t3.sent)
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual('completed', self.t3.threads['thread-1']['status'])
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertIsNone(self.router.store.pending_delivery(REPO, 1))

    def assert_mixed_launch_recovery(self, *, legacy_intent=False, crash_during_link=False):
        self.github.add_issue(issue())
        initial = self.github.add_comment(1, 'Original authorized detail accepted in the launch')
        outsider = self.github.add_comment(1, 'OUTSIDER detail from the former open policy', author=9999)
        outsider['user']['login'] = self.config['allowed_github_user']['login']
        self.t3.lose_launch_response = True
        self.t3.defer_launch_visibility = True
        # Reproduce an actual accepted prompt under the former all-human
        # policy, then restore current authorization before recovery begins.
        with patch('planetry.router.allowed_user', return_value=True):
            with self.assertRaises(TimeoutError):
                self.router.tick()
        original_delivery = self.router.store.pending_delivery(REPO, 1)
        original_events = self.router.store.events(REPO, 1)
        denied_id = next(event['id'] for event in original_events
                         if json.loads(event['payload']).get('id') == outsider['id'])
        authorized_ids = {event['id'] for event in original_events} - {denied_id}
        self.assertEqual('intent', self.router.store.issue(REPO, 1)['launch_state'])
        if legacy_intent:
            # Old databases had an outstanding delivery but no metadata pin.
            with self.router.store.db:
                self.router.store.db.execute('DELETE FROM metadata WHERE key=?',
                                             (f'launch_delivery:{REPO}:1',))
        else:
            self.assertEqual(original_delivery['key'], self.router.store.meta(f'launch_delivery:{REPO}:1'))

        newer = self.github.add_comment(1, 'New authorized update after the uncertain launch')
        self.github.add_issue(issue(2))
        self.restart()
        self.router.tick()  # Filter the cached prompt while the accepted thread is hidden.
        newer_id = next(event['id'] for event in self.router.store.events(REPO, 1)
                        if json.loads(event['payload']).get('id') == newer['id'])
        self.assertEqual('ignored', self.event_row(denied_id)['state'])
        self.assertEqual(original_delivery['key'], self.router.store.meta(f'launch_delivery:{REPO}:1'))
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual({}, self.t3.sent)
        self.assertIsNone(self.router.store.issue(REPO, 2)['thread_id'])
        self.assertTrue(self.router.store.events(REPO, 2))

        self.restart()  # The original delivery must survive pruning and restart.
        self.t3.threads['thread-1']['hidden'] = False
        if crash_during_link:
            self.router.store.db.executescript("""
                CREATE TRIGGER injected_recovery_failure BEFORE UPDATE ON deliveries
                WHEN NEW.state = 'delivered'
                BEGIN SELECT RAISE(ABORT, 'injected recovery acknowledgement failure'); END;
            """)
            with self.assertRaises(sqlite3.DatabaseError):
                self.router.tick()
            self.restart()
            self.assertIsNone(self.router.store.issue(REPO, 1)['thread_id'])
            self.assertEqual('intent', self.router.store.issue(REPO, 1)['launch_state'])
            for event_id in authorized_ids | {newer_id}:
                self.assertEqual('pending', self.event_row(event_id)['state'])
            self.assertEqual('ignored', self.event_row(denied_id)['state'])
            self.assertEqual({}, self.t3.sent)
            self.router.store.db.execute('DROP TRIGGER injected_recovery_failure')
            self.restart()

        self.router.tick()
        mapping = self.router.store.issue(REPO, 1)
        self.assertEqual(('thread-1', 'run-1', 'linked'),
                         (mapping['thread_id'], mapping['last_run_id'], mapping['launch_state']))
        self.assertEqual({}, self.t3.sent)
        for event_id in authorized_ids:
            self.assertEqual('delivered', self.event_row(event_id)['state'])
        self.assertEqual('ignored', self.event_row(denied_id)['state'])
        self.assertEqual('pending', self.event_row(newer_id)['state'])

        self.restart()
        self.router.tick()
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual(1, sum(name == 't3_thread_send' for name, _ in self.t3.calls))
        sent = next(iter(self.t3.sent.values()))
        self.assertEqual('thread-1', sent['threadId'])
        self.assertNotEqual(original_delivery['key'], sent['clientRequestId'])
        self.assertEqual(1, sent['message'].count(newer['body']))
        self.assertNotIn(initial['body'], sent['message'])
        self.assertNotIn('OUTSIDER', sent['message'])
        self.assertEqual('delivered', self.event_row(newer_id)['state'])
        self.assertEqual('ignored', self.event_row(denied_id)['state'])
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertIsNone(self.router.store.pending_delivery(REPO, 1))
        self.assertEqual(1, self.config['max_active_threads'])
        self.assertIsNone(self.router.store.issue(REPO, 2)['thread_id'])
        self.assertTrue(self.router.store.events(REPO, 2))

    def test_pinned_mixed_launch_links_original_events_then_sends_newer_update(self):
        self.assert_mixed_launch_recovery()

    def test_legacy_mixed_launch_recovers_original_delivery_after_link_crash(self):
        self.assert_mixed_launch_recovery(legacy_intent=True, crash_during_link=True)

    def test_launch_links_before_waiting_for_unknown_newer_comment(self):
        self.github.add_issue(issue())
        self.t3.lose_launch_response = True
        self.t3.defer_launch_visibility = True
        with self.assertRaises(TimeoutError):
            self.router.tick()
        original = self.router.store.pending_delivery(REPO, 1)
        original_ids = set(json.loads(original['event_ids']))
        comment = self.github.add_comment(1, 'New legacy follow-up awaits numeric provenance')
        event = self.legacy_comment(comment)
        followup = self.router.store.save_delivery(REPO, 1, [event], 'Unverified cached follow-up')
        self.fail_repository_poll()
        self.restart()
        self.t3.threads['thread-1']['hidden'] = False

        self.router.tick()
        self.assertEqual('thread-1', self.router.store.issue(REPO, 1)['thread_id'])
        self.assertEqual('linked', self.router.store.issue(REPO, 1)['launch_state'])
        for event_id in original_ids:
            self.assertEqual('delivered', self.event_row(event_id)['state'])
        self.assert_unknown_pending(event['id'], followup)
        self.restart()
        self.router.tick()
        self.assert_unknown_pending(event['id'], followup)

        self.restore_repository_poll()
        self.restart()
        self.router.tick()
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual(1, sum(name == 't3_thread_send' for name, _ in self.t3.calls))
        sent = next(iter(self.t3.sent.values()))
        self.assertEqual(followup['key'], sent['clientRequestId'])
        self.assertEqual('thread-1', sent['threadId'])
        self.assertEqual(1, sent['message'].count(comment['body']))
        self.assertNotIn('Unverified cached follow-up', sent['message'])
        self.assertEqual('delivered', self.event_row(event['id'])['state'])
        self.assertEqual([], self.router.store.events(REPO, 1))


if __name__ == '__main__':
    unittest.main()
