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
from unittest.mock import patch

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

    def test_refresh_failure_preserves_last_known_good_registry(self):
        initial = self.load()
        repos = {self.repo: initial['repositories'][0]}
        router = SimpleNamespace(config=initial, repos=repos)
        for change in [dict(repositories=[]), dict(state_dir=str(self.root / 'other-state')),
                       dict(t3_endpoint='http://127.0.0.1:9999/mcp')]:
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
            'title': 'Please add a plugin', 'body': 'A new gameplay feature', 'state': 'open'})}
        prompt = router.prompt(issue, [])
        self.assertIn('At the start of EVERY turn', prompt)
        self.assertIn(' registry\n', prompt)
        self.assertIn(str(self.control / 'docs/NEW_PLUGIN.md'), prompt)
        self.assertIn('only a snapshot', prompt)
        self.assertIn('onboarding helper', prompt)


if __name__ == '__main__':
    unittest.main()
