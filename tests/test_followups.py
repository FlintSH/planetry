"""Offline follow-up routing and the agent's durable reopen decision."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from planetry.github import AmbiguousMutationError, RetryableError
from planetry.router import MARKER, Router
from test_router import FakeGitHub, FakeT3, REPO, issue


FLINT = {'id': 87043024, 'login': 'FlintSH'}
BEE = {'id': 89701169, 'login': 'thebeeee'}
CLOSED_AT = '2026-10-20T12:00:00Z'
FOLLOWUP_AT = '2026-10-20T13:00:00Z'
LATER_CLOSED_AT = '2026-10-20T14:00:00Z'


class FollowupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = {
            'allowed_github_users': [dict(FLINT), dict(BEE)],
            'state_dir': str(root / 'state'), 'control_root': str(root / 'control'),
            'config_path': str(root / 'operator.json'), 'max_active_threads': 3,
            'max_new_threads_per_day': 20,
            'repositories': [{'name': REPO, 'workspace': str(root / 'chat'), 'project_id': 'chat'}],
        }
        self.github = FakeGitHub()
        self.github.actor_id = FLINT['id']
        self.t3 = FakeT3()
        self.router = Router(self.config, self.github, self.t3)
        self.addCleanup(lambda: self.router.store.db.close())

    def restart(self):
        self.router.store.db.close()
        self.router = Router(self.config, self.github, self.t3)

    def add_issue(self, number=1, *, author=BEE, state='open'):
        request = issue(number, author=author['id'], state=state)
        request['user']['login'] = author['login']
        request['closed_at'] = CLOSED_AT if state == 'closed' else None
        self.github.add_issue(request)

    def add_comment(self, body='Please fix the regression.', *, number=1, author=BEE,
                    updated_at=FOLLOWUP_AT):
        comment = self.github.add_comment(number, body, author=author['id'])
        comment['user']['login'] = author['login']
        comment['updated_at'] = updated_at
        return comment

    def start_closed_mapped_issue(self):
        self.add_issue()
        self.router.tick()
        self.github.issues[REPO][1]['closed_at'] = CLOSED_AT
        self.router.reply(REPO, 1, 'Delivered and verified.', close=True)
        self.t3.settle()
        self.router.tick()
        self.assertEqual({}, self.t3.sent)
        self.assertEqual([], self.router.store.events(REPO, 1))

    def defer_reopen(self, comment):
        # Simulate a safe-to-retry write failure after its durable decision exists.
        with patch.object(self.github, 'reopen_issue', side_effect=RetryableError('Unavailable')):
            with self.assertRaises(RetryableError):
                self.router.reopen(REPO, 1, comment['id'])
        pending = self.router.store.pending_reopens()
        self.assertEqual(1, len(pending))
        self.assertEqual([], self.github.reopens)
        return pending[0]

    def reopen_state(self, key):
        return self.router.store.db.execute('SELECT state FROM reopens WHERE key=?', (key,)).fetchone()[0]

    def test_both_accounts_author_issues_and_cross_account_comments(self):
        self.add_issue(1, author=FLINT)
        self.add_issue(2, author=BEE)
        self.router.tick()
        self.assertEqual(2, len(self.t3.launches))
        first = self.add_comment('Bee follows up on Flint\'s issue.', number=1, author=BEE)
        second = self.add_comment('Flint follows up on Bee\'s issue.', number=2, author=FLINT)
        self.router.tick()
        self.assertEqual(2, len(self.t3.sent))
        for number, comment in ((1, first), (2, second)):
            with self.subTest(number=number):
                thread_id = self.router.store.issue(REPO, number)['thread_id']
                sent = next(v for v in self.t3.sent.values() if v['threadId'] == thread_id)
                self.assertIn(comment['body'], sent['message'])
                self.assertIn(str(comment['id']), sent['message'])
        self.assertEqual(2, len(self.t3.launches))

    def test_allowed_posting_actor_manual_input_routes_but_marked_replies_do_not_after_restart(self):
        self.add_issue()
        self.router.tick()
        self.router.tick()
        self.assertEqual({}, self.t3.sent)
        before = self.add_comment('Manual owner follow-up.', author=FLINT)
        self.router.tick()
        self.assertEqual(1, len(self.t3.sent))
        self.assertIn(before['body'], self.t3.sent_messages()[0])

        self.restart()
        self.t3.settle()
        self.add_comment('Automatic result. ' + MARKER + 'saved -->', author=FLINT)
        self.router.tick()
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual('completed', self.t3.threads['thread-1']['status'])
        after = self.add_comment('Another manual owner follow-up.', author=FLINT)
        self.router.tick()
        self.router.tick()
        self.assertEqual(2, len(self.t3.sent))
        self.assertIn(after['body'], self.t3.sent_messages()[-1])
        self.assertNotIn('Automatic result.', self.t3.sent_messages()[-1])
        self.assertEqual([], self.router.store.events(REPO, 1))

    def test_malformed_accepted_followup_reserves_capacity_until_reconciled(self):
        self.add_issue(1)
        self.add_issue(2)
        self.router.tick()
        self.t3.settle('thread-1')
        self.t3.settle('thread-2')
        self.config['max_active_threads'] = 1
        self.add_comment(number=1)
        self.add_comment(number=2)
        call = self.t3.call

        def malformed(name, args):
            result = call(name, args)
            return {} if name == 't3_thread_send' else result

        with patch.object(self.t3, 'call', side_effect=malformed):
            self.router.tick()
        self.assertEqual(1, len(self.t3.sent))
        self.assertTrue(self.router.store.events(REPO, 1))
        self.assertTrue(self.router.store.events(REPO, 2))
        self.router.store.clear_failure(f'issue:{REPO}:1')
        self.restart()
        # The accepted run remains active and the exact delivery key is reused.
        self.router.tick()
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertTrue(self.router.store.events(REPO, 2))
        self.t3.settle('thread-1')
        self.router.tick()
        self.assertEqual(2, len(self.t3.sent))
        self.assertEqual([], self.router.store.events(REPO, 2))

    def test_settled_filter_is_explicit_and_followup_resumes_the_same_thread(self):
        self.add_issue()
        self.router.tick()
        self.t3.settle()
        self.assertEqual([], self.t3.call('t3_thread_list', {'projectId': 'chat', 'settled': False})['threads'])
        self.t3.calls.clear()
        comment = self.add_comment()
        self.restart()
        self.router.tick()
        lists = [args for name, args in self.t3.calls if name == 't3_thread_list']
        self.assertEqual({False, True}, {args['settled'] for args in lists})
        self.assertTrue(all(args['includeSubagents'] is False for args in lists))
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))
        sent = next(iter(self.t3.sent.values()))
        self.assertEqual('thread-1', sent['threadId'])
        self.assertEqual('auto', sent['mode'])
        self.assertIn(comment['body'], sent['message'])
        self.assertEqual('thread-1', self.router.store.issue(REPO, 1)['thread_id'])

    def test_historical_closed_issue_baselines_comments_then_new_reply_launches_triage(self):
        self.add_issue(state='closed')
        self.add_comment('Historical resolved request.', updated_at='2026-10-20T11:00:00Z')
        self.router.tick()
        self.restart()
        self.router.tick()
        self.assertEqual([], self.t3.launches)
        self.assertEqual([], self.github.posts)
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertIsNone(self.router.store.issue(REPO, 1)['thread_id'])

        comment = self.add_comment('A new failure in the latest release.', author=FLINT)
        self.router.tick()
        self.restart()
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        prompt = self.t3.launches[0]['message']
        self.assertIn(comment['body'], prompt)
        self.assertNotIn('Historical resolved request.', prompt)
        self.assertIn('"state": "closed"', prompt)
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual([], self.github.reopens)
        self.assertEqual({}, self.t3.sent)

    def test_edit_to_baselined_closed_comment_is_new_triage_input(self):
        self.add_issue(state='closed')
        comment = self.add_comment('Historical request.', updated_at='2026-10-20T11:00:00Z')
        self.router.tick()
        self.restart()
        comment['body'] = 'Updated: this is a regression on the new release.'
        comment['updated_at'] = FOLLOWUP_AT
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        self.assertIn(comment['body'], self.t3.launches[0]['message'])
        self.assertEqual([], self.github.reopens)

    def test_closed_mapped_followup_waits_for_agent_reopen_and_does_not_feed_back(self):
        self.start_closed_mapped_issue()
        comment = self.add_comment('The original bug is back; please fix it.', author=FLINT)
        self.router.tick()
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual('thread-1', next(iter(self.t3.sent.values()))['threadId'])
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual([], self.github.reopens)

        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.assertEqual([(REPO, 1)], self.github.reopens)
        self.assertEqual('open', self.github.get_issue(REPO, 1)['state'])
        self.t3.settle()
        self.restart()
        self.router.tick()
        self.router.tick()
        self.assertEqual(1, len(self.t3.sent), 'The helper\'s own state change must not resume its thread again')
        self.assertEqual('completed', self.t3.threads['thread-1']['status'])
        self.assertEqual([], self.router.store.events(REPO, 1))
        self.assertEqual([], self.router.store.pending_reopens())

    def test_question_and_thanks_are_triaged_while_issue_remains_closed(self):
        self.start_closed_mapped_issue()
        posts = copy.deepcopy(self.github.posts)
        for body in ('Where is the new configuration setting?', 'Thanks, that works now!'):
            with self.subTest(body=body):
                self.t3.settle()
                self.add_comment(body)
                self.router.tick()
                self.assertIn(body, self.t3.sent_messages()[-1])
                self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
                self.assertEqual([], self.github.reopens)
                self.assertEqual(posts, self.github.posts)
        self.assertEqual(2, len(self.t3.sent))
        self.assertEqual(1, len(self.t3.launches))

    def test_prompt_names_both_accounts_and_agent_semantic_reopen_policy(self):
        self.add_issue()
        self.router.tick()
        prompt = self.t3.launches[0]['message']
        for identity in (FLINT, BEE):
            self.assertIn('@' + identity['login'], prompt)
            self.assertIn(str(identity['id']), prompt)
        self.assertIn('Closed issues and settled T3 threads remain watched', prompt)
        self.assertIn(f'reopen --repo {REPO} --issue 1 --comment COMMENT_ID', prompt)
        self.assertIn('Answer guidance-only questions while closed', prompt)
        self.assertIn('thanks/acknowledgements need no', prompt)
        self.assertIn('Respect cancellation or a later closure', prompt)
        self.assertIn('Older messages with a different account policy', prompt)

    def test_reopen_rejects_unregistered_repository_and_invalid_comment_ids(self):
        for comment_id in (None, True, False, 0, -1, '101', 1.0):
            with self.subTest(comment_id=comment_id), self.assertRaises(ValueError):
                self.router.reopen(REPO, 1, comment_id)
        with self.assertRaises(ValueError):
            self.router.reopen('Other/repository', 1, 101)
        self.assertEqual([], self.github.reopens)
        self.assertEqual([], self.router.store.pending_reopens())

    def test_reopen_rejects_outsider_or_nonhuman_issue_authors_and_pull_requests(self):
        variants = [
            {'user': {'id': 9999, 'login': FLINT['login'], 'type': 'User'}},
            {'user': dict(FLINT, type='Bot')},
            {'pull_request': {'url': 'https://api.github.com/repos/' + REPO + '/pulls/1'}},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.add_issue(state='closed')
                self.github.issues[REPO][1].update(changes)
                comment = self.add_comment()
                with self.assertRaises(ValueError):
                    self.router.reopen(REPO, 1, comment['id'])
        self.assertEqual([], self.github.reopens)
        self.assertEqual([], self.router.store.pending_reopens())

    def test_reopen_rejects_missing_foreign_outsider_bot_and_marked_actor_comments(self):
        self.add_issue(state='closed')
        outsider = self.add_comment(author={'id': 9999, 'login': FLINT['login']})
        bot = self.add_comment()
        bot['user']['type'] = 'Bot'
        marked = self.add_comment('Automation ' + MARKER + 'saved -->', author=FLINT)
        foreign = self.add_comment(number=2)
        for comment_id in (999999, outsider['id'], bot['id'], marked['id'], foreign['id']):
            with self.subTest(comment_id=comment_id), self.assertRaises(ValueError):
                self.router.reopen(REPO, 1, comment_id)
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual([], self.github.reopens)
        self.assertEqual([], self.router.store.pending_reopens())

    def test_reopen_accepts_both_accounts_as_issue_and_cross_comment_authors(self):
        for number, author, commenter in ((1, FLINT, BEE), (2, BEE, FLINT)):
            with self.subTest(author=author['login'], commenter=commenter['login']):
                self.add_issue(number, author=author, state='closed')
                comment = self.add_comment(number=number, author=commenter)
                self.assertEqual('sent', self.router.reopen(REPO, number, comment['id']))
                self.assertEqual('open', self.github.get_issue(REPO, number)['state'])
        self.assertEqual([(REPO, 1), (REPO, 2)], self.github.reopens)

    def test_reopen_requires_comment_strictly_newer_than_closure(self):
        self.add_issue(state='closed')
        for timestamp in ('2026-10-20T11:00:00Z', CLOSED_AT, '2026-10-20T14:00:00+02:00'):
            with self.subTest(timestamp=timestamp):
                comment = self.add_comment(updated_at=timestamp)
                self.assertEqual('ignored', self.router.reopen(REPO, 1, comment['id']))
                self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
                self.assertEqual([], self.github.reopens)
        comment = self.add_comment(updated_at='2026-10-20T12:00:01Z')
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.assertEqual([(REPO, 1)], self.github.reopens)

    def test_already_open_reopen_is_idempotent_without_a_patch(self):
        self.add_issue()
        comment = self.add_comment(author=FLINT)
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.restart()
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.assertEqual([], self.github.reopens)
        self.assertEqual([], self.router.store.pending_reopens())

    def test_successful_reopen_is_deduplicated_across_restart_and_later_closure(self):
        self.add_issue(state='closed')
        comment = self.add_comment()
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.restart()
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.github.issues[REPO][1].update(state='closed', closed_at=LATER_CLOSED_AT)
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual([(REPO, 1)], self.github.reopens)
        count = self.router.store.db.execute('SELECT COUNT(*) FROM reopens').fetchone()[0]
        self.assertEqual(1, count)

    def test_genuine_comment_edit_creates_new_reopen_decision_after_later_closure(self):
        self.add_issue(state='closed')
        comment = self.add_comment()
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.github.issues[REPO][1].update(state='closed', closed_at=LATER_CLOSED_AT)
        comment['body'] = 'A separate regression after that delivery. Please fix it.'
        comment['updated_at'] = '2026-10-20T15:00:00Z'
        self.restart()
        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))
        self.assertEqual([(REPO, 1), (REPO, 1)], self.github.reopens)
        self.assertEqual('open', self.github.get_issue(REPO, 1)['state'])

    def test_lost_reopen_response_reconciles_before_intake_without_feedback(self):
        self.start_closed_mapped_issue()
        comment = self.add_comment()
        self.router.tick()
        self.github.lose_reopen_response = True
        with self.assertRaises(AmbiguousMutationError):
            self.router.reopen(REPO, 1, comment['id'])
        self.assertEqual(1, len(self.router.store.pending_reopens()))
        self.assertEqual('open', self.github.get_issue(REPO, 1)['state'])
        self.t3.settle()
        self.restart()
        self.router.tick()
        self.router.tick()
        self.assertEqual([(REPO, 1)], self.github.reopens)
        self.assertEqual([], self.router.store.pending_reopens())
        self.assertEqual(1, len(self.t3.sent))
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual('completed', self.t3.threads['thread-1']['status'])
        self.assertEqual([], self.router.store.events(REPO, 1))

    def test_pending_reopen_retries_after_definite_failure(self):
        self.start_closed_mapped_issue()
        comment = self.add_comment()
        self.router.tick()
        row = self.defer_reopen(comment)
        self.restart()
        self.router.tick()
        self.assertEqual('sent', self.reopen_state(row['key']))
        self.assertEqual([(REPO, 1)], self.github.reopens)
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(1, len(self.t3.sent))

    def test_later_authorized_cancellation_invalidates_pending_reopen_after_restart(self):
        self.start_closed_mapped_issue()
        comment = self.add_comment()
        self.router.tick()
        original = copy.deepcopy(comment)
        row = self.defer_reopen(comment)
        cancellation = self.add_comment('Never mind, cancel this request.', author=FLINT,
                                        updated_at='2026-10-20T13:30:00Z')

        self.restart()
        self.router.tick()
        self.router.tick()

        self.assertEqual(original, comment, 'Cancellation must be detected without editing the original request')
        self.assertEqual('ignored', self.reopen_state(row['key']))
        self.assertEqual([], self.router.store.pending_reopens())
        self.assertEqual([], self.github.reopens)
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual(1, len(self.t3.launches))
        self.assertEqual(2, len(self.t3.sent))
        self.assertIn(cancellation['body'], self.t3.sent_messages()[-1])

    def test_retriage_allows_fresh_decision_for_unchanged_original_comment(self):
        self.start_closed_mapped_issue()
        comment = self.add_comment()
        self.router.tick()
        original = copy.deepcopy(comment)
        stale = self.defer_reopen(comment)
        clarification = self.add_comment('Use the staging reproduction and fix this offline.', author=FLINT,
                                         updated_at='2026-10-20T13:30:00Z')
        self.restart()
        self.router.tick()
        self.assertEqual('ignored', self.reopen_state(stale['key']))
        self.assertIn(clarification['body'], self.t3.sent_messages()[-1])
        self.assertEqual([], self.github.reopens)

        self.assertEqual('sent', self.router.reopen(REPO, 1, comment['id']))

        decisions = [dict(row) for row in self.router.store.db.execute('SELECT * FROM reopens')]
        self.assertEqual(2, len(decisions))
        fresh = next(row for row in decisions if row['key'] != stale['key'])
        self.assertEqual(comment['id'], fresh['comment_id'])
        self.assertNotEqual(stale['fingerprint'], fresh['fingerprint'])
        self.assertEqual('sent', fresh['state'])
        self.assertEqual(original, comment)
        self.assertEqual([(REPO, 1)], self.github.reopens)
        self.assertEqual('open', self.github.get_issue(REPO, 1)['state'])

    def test_denied_bot_and_marked_comments_do_not_invalidate_pending_reopen(self):
        self.start_closed_mapped_issue()
        comment = self.add_comment()
        self.router.tick()
        row = self.defer_reopen(comment)
        self.add_comment('Outsider cancellation.', author={'id': 9999, 'login': FLINT['login']})
        bot = self.add_comment('Bot cancellation.')
        bot['user']['type'] = 'Bot'
        self.add_comment('Automatic update. ' + MARKER + 'saved -->', author=FLINT)

        self.restart()
        self.router.tick()

        self.assertEqual('sent', self.reopen_state(row['key']))
        self.assertEqual([], self.router.store.pending_reopens())
        self.assertEqual([(REPO, 1)], self.github.reopens)
        self.assertEqual('open', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual(1, len(self.t3.sent))

    def test_pending_reopen_does_not_override_later_closure_even_if_comment_is_newer(self):
        self.add_issue(state='closed')
        comment = self.add_comment(updated_at='2026-10-20T15:00:00Z')
        row = self.defer_reopen(comment)
        self.github.issues[REPO][1]['closed_at'] = LATER_CLOSED_AT
        self.restart()
        self.router.tick()
        self.assertEqual('ignored', self.reopen_state(row['key']))
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])
        self.assertEqual([], self.github.reopens)

    def test_lost_reopen_response_then_later_closure_does_not_reopen_again(self):
        self.add_issue(state='closed')
        comment = self.add_comment()
        self.github.lose_reopen_response = True
        with self.assertRaises(AmbiguousMutationError):
            self.router.reopen(REPO, 1, comment['id'])
        row = self.router.store.pending_reopens()[0]
        self.github.issues[REPO][1].update(state='closed', closed_at=LATER_CLOSED_AT)
        self.restart()
        self.router.tick()
        self.assertEqual('ignored', self.reopen_state(row['key']))
        self.assertEqual([(REPO, 1)], self.github.reopens)
        self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])

    def test_pending_reopen_is_cancelled_if_comment_body_timestamp_or_author_changes(self):
        self.add_issue(state='closed')
        for changes in ({'body': 'Never mind, cancel the request.'},
                        {'updated_at': '2026-10-20T15:00:00Z'},
                        {'user': dict(FLINT, type='User')}):
            with self.subTest(changes=changes):
                comment = self.add_comment()
                row = self.defer_reopen(comment)
                comment.update(changes)
                self.restart()
                self.router.tick()
                self.assertEqual('ignored', self.reopen_state(row['key']))
                self.assertEqual([], self.github.reopens)
                self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])

    def test_pending_reopen_revalidates_issue_and_comment_authorization(self):
        for target in ('issue', 'comment', 'policy', 'pull_request', 'automation', 'deleted'):
            with self.subTest(target=target):
                self.add_issue(state='closed')
                self.config['allowed_github_users'] = [dict(FLINT), dict(BEE)]
                comment = self.add_comment()
                row = self.defer_reopen(comment)
                if target == 'issue':
                    self.github.issues[REPO][1]['user'] = {'id': 9999, 'type': 'User'}
                elif target == 'comment':
                    comment['user'] = {'id': 9999, 'type': 'User'}
                elif target == 'policy':
                    self.config['allowed_github_users'] = [dict(FLINT)]
                elif target == 'pull_request':
                    self.github.issues[REPO][1]['pull_request'] = {}
                elif target == 'automation':
                    comment.update(body='Automation ' + MARKER + 'saved -->', user=dict(FLINT, type='User'))
                else:
                    self.github.comments[REPO].remove(comment)
                self.restart()
                self.router.tick()
                self.assertEqual('ignored', self.reopen_state(row['key']))
                self.assertEqual([], self.github.reopens)
                self.assertEqual('closed', self.github.get_issue(REPO, 1)['state'])

    def test_malformed_accepted_send_keeps_events_and_delivery_key_for_restart_retry(self):
        self.add_issue()
        self.router.tick()
        self.t3.settle()
        original_call = self.t3.call
        malformed = [
            {}, {'threadId': 'other-thread', 'runId': 'accepted'},
            {'threadId': 'thread-1'}, {'threadId': 'thread-1', 'runId': ''},
            {'threadId': 'thread-1', 'runId': 123},
        ]
        for index, response in enumerate(malformed):
            with self.subTest(response=response):
                comment = self.add_comment(f'Follow-up {index} must survive a malformed response.')

                def accepted_but_malformed(name, arguments):
                    result = original_call(name, arguments)
                    return response if name == 't3_thread_send' else result

                with patch.object(self.t3, 'call', side_effect=accepted_but_malformed):
                    self.router.tick()
                events = self.router.store.events(REPO, 1)
                self.assertEqual([comment['id']], [json.loads(e['payload'])['id'] for e in events])
                delivery = self.router.store.pending_delivery(REPO, 1)
                self.assertIsNotNone(delivery)
                self.assertEqual([e['id'] for e in events], json.loads(delivery['event_ids']))
                key = delivery['key']
                self.assertIn(key, self.t3.sent, 'The remote side accepted the send')
                self.assertEqual(index * 2 + 1, len(self.t3.sent))

                later = self.add_comment(f'Later comment {index} belongs to a separate delivery.')
                self.restart()
                # Advance only this issue's intentional failure backoff.
                self.router.store.clear_failure(f'issue:{REPO}:1')
                self.router.tick()
                sends = [args for name, args in self.t3.calls if name == 't3_thread_send'
                         and args['clientRequestId'] == key]
                self.assertEqual(2, len(sends))
                self.assertEqual(sends[0], sends[1])
                remaining = self.router.store.events(REPO, 1)
                self.assertEqual([later['id']], [json.loads(e['payload'])['id'] for e in remaining])
                self.assertIsNone(self.router.store.pending_delivery(REPO, 1))
                self.router.tick()
                self.assertEqual([], self.router.store.events(REPO, 1))
                self.assertIn(later['body'], self.t3.sent_messages()[-1])
                self.assertEqual((index + 1) * 2, len(self.t3.sent))
        self.assertEqual(1, len(self.t3.launches))


if __name__ == '__main__':
    unittest.main()
