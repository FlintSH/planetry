"""Exercise the installed coordination command boundary without external writes."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from planetry.__main__ import main
from planetry.coordination import resource_lock
from planetry.onboarding import Onboarder
from planetry.store import Store
import test_cli


class WorkCliTests(unittest.TestCase):
    setUp = test_cli.ConfigTests.setUp
    load = test_cli.ConfigTests.load

    def prepare(self):
        config = self.load()
        store = Store(Path(config['state_dir']) / 'router.sqlite')
        for number in (1, 2):
            store.ingest_issue(self.repo, {
                'id': number, 'number': number, 'title': 'Local fixture', 'body': '',
                'state': 'open', 'user': {'id': 1, 'type': 'User'}})
        store.db.close()
        return config

    def invoke(self, *arguments):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['planetry', '--config', str(self.config_path), 'work', *arguments]), \
                patch('planetry.__main__.GitHub') as github, redirect_stdout(output):
            code = main()
        github.assert_not_called()
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def test_claim_note_conflict_and_owner_release_across_cli_process_state(self):
        self.prepare()
        note = self.root / 'note.txt'
        note.write_text('Implementing mail; shared config integration pending.', encoding='utf-8')
        base = ['--repo', self.repo, '--target-repo', self.repo]
        code, claims = self.invoke('claim', *base, '--issue', '1', '--path', 'src/mail',
                                   '--note-file', str(note))
        self.assertEqual(0, code)
        self.assertEqual('src/mail', claims[0]['path'])
        code, conflict = self.invoke('claim', *base, '--issue', '2', '--path', 'src/mail/Inbox.java')
        self.assertEqual(75, code)
        self.assertEqual('claim_conflict', conflict['error'])
        self.assertEqual(1, conflict['conflicts'][0]['issue'])
        code, removed = self.invoke('release', '--repo', self.repo, '--issue', '2')
        self.assertEqual((0, 0), (code, removed))
        code, removed = self.invoke('release', '--repo', self.repo, '--issue', '1')
        self.assertEqual((0, 1), (code, removed))
        code, saved = self.invoke('note', '--repo', self.repo, '--issue', '1', '--note-file', str(note))
        self.assertEqual(0, code)
        self.assertEqual(note.read_text(), saved['text'])

    def test_list_uses_only_read_calls_and_closes_transport(self):
        self.prepare()
        with patch('planetry.__main__.T3') as factory:
            factory.return_value.call.return_value = {'threads': [], 'nextCursor': None}
            code, board = self.invoke('list')
            self.assertEqual(0, code)
            self.assertEqual([1, 2], [r['issue'] for r in board['issues']])
            self.assertTrue(all(r['status'] == 'queued' for r in board['issues']))
            factory.return_value.close.assert_called_once()
            self.assertTrue(all(c.args[0] == 't3_thread_list' for c in factory.return_value.call.call_args_list))

    def test_run_preserves_literal_argv_cwd_and_exit_status(self):
        self.prepare()
        marker = self.root / 'argv.json'
        literal = 'literal $(do-not-run) ; `do-not-run`'
        script = 'import json,os,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getcwd(),sys.argv[2]]));sys.exit(7)'
        code, _ = self.invoke('run', '--resource', 'build', '--cwd', str(self.root), '--',
                              sys.executable, '-c', script, str(marker), literal)
        self.assertEqual(7, code)
        self.assertEqual([str(self.root), literal], json.loads(marker.read_text()))

    def test_busy_run_reports_conflict_without_executing_child(self):
        config = self.prepare()
        marker = self.root / 'must-not-exist'
        with resource_lock(config, 'build'):
            code, result = self.invoke('run', '--resource', 'build', '--cwd', str(self.root), '--',
                                      sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(marker))
        self.assertEqual(75, code)
        self.assertEqual('resource_busy', result['error'])
        self.assertFalse(marker.exists())

    def test_onboarding_obeys_same_control_publication_guard(self):
        config = self.prepare()
        config['control_repository'] = self.repo
        onboard = Onboarder(config, None, None)
        from planetry.coordination import ResourceBusy
        with resource_lock(config, 'publish', self.repo), patch.object(onboard, '_publish_locked') as publish:
            with self.assertRaises(ResourceBusy):
                onboard._publish('FlintSH/new-plugin', {})
            publish.assert_not_called()
        with patch.object(onboard, '_publish_locked') as publish:
            onboard._publish('FlintSH/new-plugin', {})
            publish.assert_called_once()

    def test_prompt_requires_live_board_owner_and_shared_guards(self):
        config = self.prepare()
        from planetry.router import Router
        router = Router(config, None, None)
        try:
            prompt = router.prompt(router.store.issue(self.repo, 1), [])
        finally:
            router.store.db.close()
        self.assertIn('work list', prompt)
        self.assertIn(f'--repo {self.repo} --issue 1', prompt)
        self.assertIn('docs/COORDINATION.md', prompt)
        self.assertIn('publish lock', prompt)


if __name__ == '__main__':
    unittest.main()
