"""Operator config validation does not require GitHub or a live T3 server."""

import copy
from contextlib import redirect_stdout
import io
import json
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from planetry.__main__ import load_config, main, refresh_router
from planetry.router import Router


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.control = self.root / 'control'
        self.control.mkdir()
        self.repo = 'FlintSH/velocity-network-chat'
        (self.control / 'repositories.json').write_text(json.dumps({
            'repositories': [{'name': self.repo}, {'name': 'FlintSH/network-inventory-sync'}]}))
        self.config_path = self.root / 'operator.json'
        self.config = {
            'control_root': str(self.control), 'state_dir': str(self.root / 'state'),
            'allowed_github_user': {'id': 1, 'login': 'thebeeee'},
            'poll_seconds': 60, 'max_active_threads': 1, 'max_new_threads_per_day': 20,
            'repositories': [{'name': self.repo, 'workspace': str(self.root / 'chat'),
                              'project_id': '12345678-abcd-1234-5678-123456789abc'}],
        }

    def load(self, config=None):
        self.config_path.write_text(json.dumps(self.config if config is None else config))
        return load_config(self.config_path)

    def test_valid_config_records_path_and_private_state_directory(self):
        loaded = self.load()
        self.assertEqual(str(self.config_path.resolve()), loaded['config_path'])
        self.assertEqual(0o700, stat.S_IMODE(Path(loaded['state_dir']).stat().st_mode))
        self.assertEqual({'id': 1, 'login': 'thebeeee'}, loaded['allowed_github_user'])

    def test_access_policy_is_required(self):
        del self.config['allowed_github_user']
        with self.assertRaises(ValueError):
            self.load()

    def test_access_policy_requires_an_actual_positive_integer_id(self):
        for user_id in (None, False, True, 0, -1, '1', 1.0, [], {}):
            with self.subTest(user_id=user_id):
                config = copy.deepcopy(self.config)
                config['allowed_github_user']['id'] = user_id
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_access_policy_requires_a_user_record_with_login_metadata(self):
        for policy in (None, True, 1, 'thebeeee', [], {}, {'login': 'thebeeee'},
                       {'id': 1}, {'id': 1, 'login': None}, {'id': 1, 'login': ''},
                       {'id': 1, 'login': 1}):
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    self.load(dict(self.config, allowed_github_user=policy))

    def test_plain_and_mcp_project_ids_are_supported(self):
        for project in ('12345678-abcd-1234-5678-123456789abc', 'mcp:12345678-abcd-1234-5678-123456789abc'):
            with self.subTest(project=project):
                self.config['repositories'][0]['project_id'] = project
                self.assertEqual(project, self.load()['repositories'][0]['project_id'])

    def test_relative_state_control_and_workspace_paths_are_rejected(self):
        for field in ('state_dir', 'control_root', 'workspace'):
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                if field == 'workspace':
                    config['repositories'][0][field] = 'relative/path'
                else:
                    config[field] = 'relative/path'
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_unregistered_duplicate_and_empty_repositories_are_rejected(self):
        variants = [[], [dict(self.config['repositories'][0], name='Elsewhere/unrelated')],
                    self.config['repositories'] * 2]
        for repos in variants:
            with self.subTest(repositories=repos):
                config = copy.deepcopy(self.config)
                config['repositories'] = repos
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_invalid_project_ids_are_rejected(self):
        for project in (None, 123, True, '', 'project', 'mcp:not-a-uuid',
                        '12345678-abcd-1234-5678-123456789abc-extra', '../escape'):
            with self.subTest(project=project):
                config = copy.deepcopy(self.config)
                config['repositories'][0]['project_id'] = project
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_numeric_limits_require_actual_integers_within_bounds(self):
        for field, minimum, maximum in (('poll_seconds', 30, 3600),
                                        ('max_active_threads', 1, 4),
                                        ('max_new_threads_per_day', 1, 100)):
            for value in (False, True, None, str(minimum), float(minimum), minimum - 1, maximum + 1):
                with self.subTest(field=field, value=value):
                    config = dict(self.config, **{field: value})
                    with self.assertRaises(ValueError):
                        self.load(config)
            for value in (minimum, maximum):
                with self.subTest(field=field, boundary=value):
                    self.assertEqual(value, self.load(dict(self.config, **{field: value}))[field])

    def test_optional_numeric_limits_use_defaults(self):
        for field in ('poll_seconds', 'max_active_threads', 'max_new_threads_per_day'):
            del self.config[field]
        self.assertEqual(self.repo, self.load()['repositories'][0]['name'])

    def test_refresh_adopts_new_repository_without_restarting_clients(self):
        initial = self.load()
        store, github, t3 = object(), object(), object()
        router = SimpleNamespace(config=initial, repos={self.repo: initial['repositories'][0]},
                                 store=store, github=github, t3=t3)
        new_repo = {'name': 'FlintSH/network-inventory-sync',
                    'workspace': str(self.root / 'inventory'),
                    'project_id': '22222222-2222-2222-2222-222222222222'}
        self.config['repositories'].append(new_repo)
        self.config_path.write_text(json.dumps(self.config))
        refresh_router(router)
        self.assertEqual(new_repo, router.repos[new_repo['name']])
        self.assertIs(store, router.store)
        self.assertIs(github, router.github)
        self.assertIs(t3, router.t3)

    def test_refresh_adopts_replacement_user_id_without_restarting_clients(self):
        initial = self.load()
        store, github, t3 = object(), object(), object()
        router = SimpleNamespace(config=initial, repos={self.repo: initial['repositories'][0]},
                                 store=store, github=github, t3=t3)
        self.config['allowed_github_user'] = {'id': 2, 'login': 'replacement-user'}
        self.config_path.write_text(json.dumps(self.config))
        refresh_router(router)
        self.assertEqual({'id': 2, 'login': 'replacement-user'}, router.config['allowed_github_user'])
        self.assertIs(store, router.store)
        self.assertIs(github, router.github)
        self.assertIs(t3, router.t3)

    def test_refresh_failure_preserves_last_known_good_registry(self):
        initial = self.load()
        repos = {self.repo: initial['repositories'][0]}
        router = SimpleNamespace(config=initial, repos=repos)
        for change in [dict(repositories=[]), dict(state_dir=str(self.root / 'other-state')),
                       dict(t3_endpoint='http://127.0.0.1:9999/mcp'),
                       dict(allowed_github_user={'id': True, 'login': 'thebeeee'})]:
            with self.subTest(change=change):
                modified = dict(self.config, **change)
                self.config_path.write_text(json.dumps(modified))
                with self.assertRaises(ValueError):
                    refresh_router(router)
                self.assertIs(initial, router.config)
                self.assertIs(repos, router.repos)
        self.config_path.write_text('{broken json')
        with self.assertRaises(ValueError):
            refresh_router(router)
        self.assertIs(initial, router.config)
        self.assertIs(repos, router.repos)

    def test_once_does_not_poll_when_access_policy_becomes_invalid_on_disk(self):
        missing_policy = dict(self.config)
        del missing_policy['allowed_github_user']
        invalid_files = [
            ('missing policy', json.dumps(missing_policy)),
            ('null policy', json.dumps(dict(self.config, allowed_github_user=None))),
            ('string user ID', json.dumps(dict(
                self.config, allowed_github_user={'id': '1', 'login': 'thebeeee'}))),
            ('malformed JSON', '{broken json'),
        ]
        for case, contents in invalid_files:
            with self.subTest(case=case):
                self.load()
                tick = Mock()

                def make_router(config, github, t3, store):
                    self.config_path.write_text(contents)
                    return SimpleNamespace(config=config, repos={self.repo: config['repositories'][0]}, tick=tick)

                with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'once']), \
                        patch('planetry.__main__.GitHub'), patch('planetry.__main__.T3'), \
                        patch('planetry.__main__.Store'), \
                        patch('planetry.__main__.Router', side_effect=make_router):
                    try:
                        result = main()
                    except ValueError:
                        result = 1
                tick.assert_not_called()
                self.assertNotEqual(0, result)

    def test_registry_command_reads_latest_shared_plugin_context_without_network(self):
        self.load()
        context_path = self.control / 'docs/plugins/example.json'
        context_path.parent.mkdir(parents=True)
        context = {'role': 'An automatically onboarded new plugin', 'integration': 'Shares existing contracts'}
        context_path.write_text(json.dumps(context))
        manifest_path = self.control / 'repositories.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['repositories'][0]['context'] = 'docs/plugins/example.json'
        manifest_path.write_text(json.dumps(manifest))
        output = io.StringIO()
        with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'registry']), \
                patch('planetry.__main__.GitHub') as github, patch('planetry.__main__.T3') as t3, \
                redirect_stdout(output):
            self.assertEqual(0, main())
        result = json.loads(output.getvalue())
        self.assertEqual([{'repository': self.repo, 'context': context}], result['plugin_contexts'])
        self.assertEqual(self.config['repositories'], result['repositories'])
        github.assert_not_called()
        t3.assert_not_called()

    def test_registry_command_rejects_context_path_outside_shared_contexts(self):
        self.load()
        manifest_path = self.control / 'repositories.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['repositories'][0]['context'] = '../operator.json'
        manifest_path.write_text(json.dumps(manifest))
        with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'registry']):
            with self.assertRaisesRegex(ValueError, 'escapes'):
                main()

    def test_issue_prompt_requires_live_context_and_new_plugin_onboarding(self):
        config = self.load()
        router = Router(config, None, None)
        self.addCleanup(router.store.db.close)
        issue = {'repo': self.repo, 'number': 7, 'payload': json.dumps({
            'title': 'Please add a plugin', 'body': 'A new gameplay feature', 'state': 'open',
            'user': {'id': 1, 'login': 'renamed-requester', 'type': 'User'}})}
        prompt = router.prompt(issue, [])
        self.assertIn('At the start of EVERY turn', prompt)
        self.assertIn(' registry\n', prompt)
        self.assertIn(str(self.control / 'docs/NEW_PLUGIN.md'), prompt)
        self.assertIn('only a snapshot', prompt)
        self.assertIn('onboarding helper', prompt)
        self.assertIn(f'reopen --repo {self.repo} --issue 7 --comment COMMENT_ID', prompt)
        self.assertIn('Answer guidance-only questions while closed', prompt)
        self.assertIn('Respect cancellation or a later closure', prompt)

    def test_check_reports_normalized_plural_policy_with_both_accounts(self):
        del self.config['allowed_github_user']
        self.config['allowed_github_users'] = [
            {'id': 87043024, 'login': 'FlintSH'}, {'id': 89701169, 'login': 'thebeeee'}]
        self.load()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'check']), \
                patch('planetry.__main__.GitHub') as github, patch('planetry.__main__.T3') as t3, \
                redirect_stdout(output):
            github.return_value.me.return_value = {'id': 87043024, 'login': 'FlintSH'}
            t3.return_value.call.return_value = {'runtimeMode': 'full-access'}
            self.assertEqual(0, main())
        result = json.loads(output.getvalue())
        self.assertEqual(self.config['allowed_github_users'], result['allowed_github_users'])
        self.assertNotIn('allowed_github_user', result)


class ReopenCliTests(unittest.TestCase):
    setUp = ConfigTests.setUp
    load = ConfigTests.load

    def test_reopen_passes_integer_issue_and_comment_to_helper_and_prints_result(self):
        self.load()
        for result in ('sent', 'ignored'):
            with self.subTest(result=result):
                output = io.StringIO()
                with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'reopen',
                                               '--repo', self.repo, '--issue', '7', '--comment', '456']), \
                        patch('planetry.__main__.GitHub') as github, patch('planetry.__main__.T3') as t3, \
                        patch('planetry.__main__.Store') as store, \
                        patch('planetry.__main__.Router') as factory, redirect_stdout(output):
                    router = factory.return_value
                    router.repos = {self.repo: self.config['repositories'][0]}
                    router.reopen.return_value = result
                    self.assertEqual(0, main())
                router.reopen.assert_called_once_with(self.repo, 7, 456)
                self.assertIs(type(router.reopen.call_args.args[1]), int)
                self.assertIs(type(router.reopen.call_args.args[2]), int)
                self.assertEqual({'reopen': result}, json.loads(output.getvalue()))
                factory.assert_called_once_with(ANY, github.return_value,
                                                t3.return_value, store.return_value)
                router.reply.assert_not_called()
                router.tick.assert_not_called()
                t3.return_value.call.assert_not_called()
                github.return_value.reopen_issue.assert_not_called()

    def test_reopen_requires_an_explicit_numeric_comment_argument(self):
        for suffix in ([], ['--comment', 'not-a-number']):
            with self.subTest(suffix=suffix):
                with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'reopen',
                                               '--repo', self.repo, '--issue', '7', *suffix]), \
                        patch('planetry.__main__.GitHub') as github, \
                        patch('planetry.__main__.T3') as t3, patch('sys.stderr', io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main()
                self.assertEqual(2, raised.exception.code)
                github.assert_not_called()
                t3.assert_not_called()

    def test_reopen_rejects_unknown_repository_or_invalid_issue_before_helper(self):
        self.load()
        for repo, number in (('Other/repository', '7'), (self.repo, '0'), (self.repo, '-1')):
            with self.subTest(repo=repo, number=number):
                with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'reopen',
                                               '--repo', repo, '--issue', number, '--comment', '456']), \
                        patch('planetry.__main__.GitHub'), patch('planetry.__main__.T3'), \
                        patch('planetry.__main__.Store'), patch('planetry.__main__.Router') as factory:
                    router = factory.return_value
                    router.repos = {self.repo: self.config['repositories'][0]}
                    with self.assertRaises(ValueError):
                        main()
                    router.reopen.assert_not_called()


class ContextCliTests(unittest.TestCase):
    setUp = ConfigTests.setUp
    load = ConfigTests.load

    def issue(self, user=None, **extra):
        return dict({'id': 101, 'number': 7, 'title': 'Authorized request', 'body': 'Issue details',
                     'state': 'open', 'user': {'id': 1, 'login': 'renamed-requester', 'type': 'User'}
                     if user is None else user}, **extra)

    def comment(self, comment_id, user, body):
        return {'id': comment_id, 'body': body, 'user': user}

    def invoke_context(self, issue, comments=(), *, actor_id=777):
        self.load()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'context',
                                       '--repo', self.repo, '--issue', '7']), \
                patch('planetry.__main__.GitHub') as factory, \
                patch('planetry.__main__.T3') as t3, redirect_stdout(output):
            github = factory.return_value
            github.get_issue.return_value = issue
            github.get_comments.return_value = list(comments)
            github.me.return_value = {'id': actor_id, 'login': 'FlintSH', 'type': 'User'}
            code = main()
        self.assertEqual(0, code)
        github.post_comment.assert_not_called()
        github.close_issue.assert_not_called()
        t3.return_value.call.assert_not_called()
        return json.loads(output.getvalue())

    def test_context_allows_renamed_identity_and_only_its_human_comments(self):
        allowed = {'id': 1, 'login': 'another-rename', 'type': 'User'}
        comments = [
            self.comment(10, allowed, 'A legitimate follow-up'),
            self.comment(11, {'id': 2, 'login': 'thebeeee', 'type': 'User'}, 'Copied login request'),
            self.comment(12, {'id': 777, 'login': 'FlintSH', 'type': 'User'}, 'Owner request'),
            self.comment(13, allowed, 'Copied marker <!-- planetry-agent:unrelated -->'),
            self.comment(14, {'id': 1, 'login': 'thebeeee', 'type': 'Bot'}, 'Bot request'),
        ]
        result = self.invoke_context(self.issue(), comments)
        self.assertEqual('untrusted GitHub issue content', result['source'])
        self.assertEqual('renamed-requester', result['issue']['user']['login'])
        self.assertEqual([10, 13], [comment['id'] for comment in result['human_comments']])

    def test_context_discards_comments_with_missing_or_malformed_identity(self):
        invalid_users = [None, {}, [], 'thebeeee', {'id': True, 'type': 'User'},
                         {'id': '1', 'type': 'User'}, {'id': 1.0, 'type': 'User'},
                         {'id': 1}, {'id': 1, 'type': None}, {'id': 1, 'type': 'Bot'}]
        comments = [self.comment(index, user, 'Untrusted comment')
                    for index, user in enumerate(invalid_users, 1)]
        result = self.invoke_context(self.issue(), comments)
        self.assertEqual([], result['human_comments'])

    def test_context_excludes_actual_automation_reply_even_when_actor_is_allowed_user(self):
        user = {'id': 1, 'login': 'renamed-requester', 'type': 'User'}
        comments = [self.comment(10, user, 'Manual follow-up'),
                    self.comment(11, user, 'Automation reply <!-- planetry-agent:saved -->')]
        result = self.invoke_context(self.issue(), comments, actor_id=1)
        self.assertEqual([10], [comment['id'] for comment in result['human_comments']])

    def test_context_includes_both_accounts_and_filters_marked_posting_actor(self):
        del self.config['allowed_github_user']
        self.config['allowed_github_users'] = [
            {'id': 87043024, 'login': 'FlintSH'}, {'id': 89701169, 'login': 'thebeeee'}]
        flint = {'id': 87043024, 'login': 'renamed-owner', 'type': 'User'}
        bee = {'id': 89701169, 'login': 'renamed-requester', 'type': 'User'}
        comments = [self.comment(10, flint, 'A manual owner request'),
                    self.comment(11, bee, 'An authorized cross-account follow-up'),
                    self.comment(12, flint, 'Automation <!-- planetry-agent:saved -->'),
                    self.comment(13, {'id': 9999, 'login': 'FlintSH', 'type': 'User'}, 'Copied login')]
        for author in (flint, bee):
            with self.subTest(author=author):
                result = self.invoke_context(self.issue(author), comments, actor_id=87043024)
                self.assertEqual([10, 11], [comment['id'] for comment in result['human_comments']])

    def test_context_rejects_other_author_issue_before_fetching_comments(self):
        for user in ({'id': 2, 'login': 'thebeeee', 'type': 'User'},
                     {'id': 777, 'login': 'FlintSH', 'type': 'User'},
                     {'id': True, 'type': 'User'}, {'id': '1', 'type': 'User'},
                     {'id': 1, 'type': 'Bot'}, {}):
            with self.subTest(user=user):
                self.load()
                output = io.StringIO()
                with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'context',
                                               '--repo', self.repo, '--issue', '7']), \
                        patch('planetry.__main__.GitHub') as factory, \
                        patch('planetry.__main__.T3'), redirect_stdout(output):
                    github = factory.return_value
                    github.get_issue.return_value = self.issue(user)
                    with self.assertRaises(ValueError):
                        main()
                    github.get_comments.assert_not_called()
                    github.post_comment.assert_not_called()
                    github.close_issue.assert_not_called()
                self.assertEqual('', output.getvalue())

    def test_context_rejects_pull_requests_before_fetching_comments(self):
        self.load()
        with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'context',
                                       '--repo', self.repo, '--issue', '7']), \
                patch('planetry.__main__.GitHub') as factory, patch('planetry.__main__.T3'):
            github = factory.return_value
            github.get_issue.return_value = self.issue(pull_request={'url': 'https://api.github.com/pulls/7'})
            with self.assertRaises(ValueError):
                main()
            github.get_comments.assert_not_called()


if __name__ == '__main__':
    unittest.main()
