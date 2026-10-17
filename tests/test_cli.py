"""Operator config validation does not require GitHub or a live T3 server."""

import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path

from planetry.__main__ import load_config


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


if __name__ == '__main__':
    unittest.main()
