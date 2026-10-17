"""Disposable-process coordination tests; no GitHub or live T3 access."""

import copy
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from planetry.coordination import ClaimConflict, Coordination, NOTE_LIMIT, ResourceBusy, resource_lock
from planetry.store import Store


REPO = 'FlintSH/chat'
OTHER = 'FlintSH/inventory'
CTX = multiprocessing.get_context('fork')


def payload(number):
    return {'id': number + 100, 'number': number, 'title': f'Request {number}',
            'body': 'Fix it', 'state': 'open', 'user': {'id': 1, 'type': 'User'}}


def claim_worker(config, store_path, number, barrier, results):
    store = Store(store_path)
    coordination = Coordination(config, store)
    try:
        barrier.wait(timeout=5)
        try:
            coordination.claim(REPO, number, OTHER, ['src', f'uncontested-{number}'])
        except ClaimConflict as exc:
            results.put(('conflict', number, exc.as_dict()))
        else:
            results.put(('claimed', number, None))
    finally:
        coordination.close()
        store.db.close()


def guard_worker(config, store_path, resource, repo, marker, finish, cwd):
    store = Store(store_path)
    coordination = Coordination(config, store)
    try:
        command = [sys.executable, '-c', '''
import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
finish = pathlib.Path(sys.argv[2])
deadline = time.monotonic() + 15
while not finish.exists() and time.monotonic() < deadline:
    time.sleep(.01)
''', str(marker), str(finish)]
        coordination.run_locked(resource, repo, command, cwd=cwd)
    finally:
        coordination.close()
        store.db.close()


class FakeT3:
    def __init__(self):
        self.pages = {}
        self.details = {}
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == 't3_thread_list':
            value = self.pages.get((args['projectId'], args.get('cursor')), {'threads': []})
        elif name == 't3_thread_read':
            value = self.details.get(args['threadId'], {'thread': None})
        else:
            raise AssertionError(name)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'state_dir': str(self.root / 'state'),
                       'control_root': str(self.root / 'control'),
                       'repositories': [{'name': REPO, 'project_id': 'chat'},
                                        {'name': OTHER, 'project_id': 'inventory'}]}
        self.store_path = self.root / 'state' / 'router.sqlite'
        self.store = Store(self.store_path)
        self.addCleanup(self.store.db.close)
        for number in range(1, 5):
            self.store.ingest_issue(REPO, payload(number))
        other_issue = payload(1)
        other_issue['id'] += 1000
        self.store.ingest_issue(OTHER, other_issue)
        self.coordination = Coordination(self.config, self.store)
        self.addCleanup(lambda: self.coordination.close())
        self.t3 = FakeT3()

    def mapped(self, number, thread_id, repo=REPO):
        self.store.update_issue(repo, number, thread_id=thread_id, launch_state='linked')

    def thread(self, thread_id, status='running', project='chat', **fields):
        return {'threadId': thread_id, 'projectId': project, 'status': status, **fields}

    def rows(self):
        return {(r['repo'], r['issue']): r for r in self.coordination.snapshot(self.t3)['issues']}

    def test_claims_normalize_are_idempotent_and_persist_separately(self):
        self.store.update_issue(REPO, 1, launch_state='intent')
        claims = self.coordination.claim(REPO, 1, OTHER, ['./src//main/', 'src/main'], 'Editing bridge')
        self.assertEqual(['src/main'], [c['path'] for c in claims])
        again = self.coordination.claim(REPO, 1, OTHER, ['src/main'])
        self.assertEqual(claims, again)
        self.coordination.close()
        self.coordination = Coordination(self.config, self.store)
        row = self.rows()[(REPO, 1)]
        self.assertEqual(claims, row['claims'])
        self.assertEqual('Editing bridge', row['note']['text'])
        self.assertEqual('pending_launch', row['status'])
        self.assertIsNone(row['active'])
        self.assertNotEqual(self.store_path, self.coordination.path)
        tables = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('claims', tables)
        self.assertNotIn('notes', tables)

    def test_overlaps_are_segment_aware_and_batch_conflict_rolls_back_note(self):
        self.mapped(1, 'owner-thread')
        self.coordination.claim(REPO, 1, OTHER, ['src/main'])
        for path in ('src', 'src/main', 'src/main/Bridge.java', '.'):
            with self.subTest(path=path), self.assertRaises(ClaimConflict) as raised:
                self.coordination.claim(REPO, 2, OTHER, ['free', path], 'Must roll back')
            conflict = raised.exception.as_dict()
            self.assertEqual('claim_conflict', conflict['error'])
            self.assertEqual('owner-thread', conflict['conflicts'][0]['thread_id'])
            self.assertEqual(1, conflict['conflicts'][0]['issue'])
        row = self.rows()[(REPO, 2)]
        self.assertEqual([], row['claims'])
        self.assertIsNone(row['note'])
        self.coordination.claim(REPO, 2, OTHER, ['src/main2', 'src-other'])
        # The same number in another repository is a distinct owner.
        with self.assertRaises(ClaimConflict):
            self.coordination.claim(OTHER, 1, OTHER, ['src/main'])
        # Claiming another target repository is independent.
        self.coordination.claim(REPO, 2, REPO, ['src'])

    def test_repo_wide_claim_conflicts_and_never_expires_with_thread_state(self):
        self.coordination.claim(REPO, 1, OTHER, ['.'])
        self.store.update_issue(REPO, 1, last_status='completed')
        self.coordination.db.execute('UPDATE claims SET claimed_at=0')
        self.coordination.db.commit()
        with self.assertRaises(ClaimConflict):
            self.coordination.claim(REPO, 2, OTHER, ['anything'])
        self.assertEqual(0, self.coordination.release(REPO, 1, OTHER, ['anything']))
        self.assertEqual(1, self.coordination.release(REPO, 1, OTHER, ['.']))
        self.coordination.claim(REPO, 2, OTHER, ['anything'])

    def test_invalid_paths_and_unknown_owners_are_rejected_without_partial_claims(self):
        bad_paths = ['', ' ', '/', '/tmp/a', '../a', 'a/../b', '.git', 'a/.git/config',
                     '.GIT/HEAD', 'C:/a', 'C:a', 'a\\b', 'a\x00b', 'a\nb']
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.coordination.claim(REPO, 1, OTHER, ['valid', path])
        for paths in ([], 'src', None):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.coordination.claim(REPO, 1, OTHER, paths)
        for repo, number, target in [('Else/repo', 1, OTHER), (REPO, 99, OTHER),
                                     (REPO, True, OTHER), (REPO, 1, 'Else/repo')]:
            with self.subTest(owner=(repo, number, target)), self.assertRaises(ValueError):
                self.coordination.claim(repo, number, target, ['src'])
        self.assertEqual([], self.rows()[(REPO, 1)]['claims'])

    def test_release_is_owned_and_supports_exact_subsets_and_target_or_all(self):
        self.coordination.claim(REPO, 1, OTHER, ['a', 'b'])
        self.coordination.claim(REPO, 1, REPO, ['c'])
        self.coordination.claim(REPO, 2, OTHER, ['d'])
        self.assertEqual(0, self.coordination.release(REPO, 2, OTHER, ['a']))
        self.assertEqual(1, self.coordination.release(REPO, 1, OTHER, ['./a']))
        self.assertEqual(1, self.coordination.release(REPO, 1, OTHER))
        self.assertEqual(1, self.coordination.release(REPO, 1))
        self.assertEqual(['d'], [r['path'] for r in self.rows()[(REPO, 2)]['claims']])
        with self.assertRaises(ValueError):
            self.coordination.release(REPO, 1, paths=['c'])
        with self.assertRaises(ValueError):
            self.coordination.release(REPO, 99)

    def test_notes_are_bounded_latest_plain_text_and_require_known_owner(self):
        self.coordination.note(REPO, 1, 'First')
        latest = self.coordination.note(REPO, 1, 'Handoff\nTests passed\tcontinue')
        self.assertEqual(latest, self.rows()[(REPO, 1)]['note'])
        for text in ('', ' ', 'x' * (NOTE_LIMIT + 1), 'secret\x1b[1m', 'bad\x7f'):
            with self.subTest(text=text[:15]), self.assertRaises(ValueError):
                self.coordination.note(REPO, 1, text)
        with self.assertRaises(ValueError):
            self.coordination.note(REPO, 99, 'Unknown')

    def test_concurrent_claims_have_one_winner_and_no_partial_loser(self):
        barrier, results = CTX.Barrier(2), CTX.Queue()
        processes = [CTX.Process(target=claim_worker, args=(self.config, self.store_path, n, barrier, results))
                     for n in (1, 2)]
        for process in processes:
            process.start()
            self.addCleanup(self.stop_process, process)
        outcomes = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(0, process.exitcode)
        self.assertEqual(['claimed', 'conflict'], sorted(o[0] for o in outcomes))
        winner = next(o[1] for o in outcomes if o[0] == 'claimed')
        loser = next(o for o in outcomes if o[0] == 'conflict')
        self.assertEqual(winner, loser[2]['conflicts'][0]['issue'])
        claims = list(self.coordination.db.execute('SELECT * FROM claims'))
        self.assertEqual(2, len(claims))
        self.assertEqual({winner}, {r['issue'] for r in claims})

    def test_snapshot_paginates_registered_projects_filters_manual_and_reads_metadata(self):
        self.mapped(1, 'mapped')
        self.mapped(2, 'hidden')
        self.store.ingest_issue('Unregistered/repo', payload(1))
        self.store.update_issue('Unregistered/repo', 1, thread_id='foreign')
        self.t3.pages[('chat', None)] = {'threads': [self.thread('manual')], 'nextCursor': 0}
        self.t3.pages[('chat', 0)] = {'threads': [self.thread('mapped', 'queued')]}
        self.t3.details['mapped'] = {'thread': self.thread('mapped', 'queued', branch='issue/1',
                                                         worktreePath='/worktrees/issue-1')}
        self.t3.details['hidden'] = {'thread': self.thread('hidden', 'completed', branch=None,
                                                         worktreePath=None)}
        snapshot = self.coordination.snapshot(self.t3)
        rows = {(r['repo'], r['issue']): r for r in snapshot['issues']}
        self.assertEqual(5, len(rows))
        self.assertNotIn(('Unregistered/repo', 1), rows)
        self.assertEqual({'chat', 'inventory'}, {a['projectId'] for n, a in self.t3.calls
                                                if n == 't3_thread_list'})
        self.assertTrue(any(a.get('cursor') == 0 for n, a in self.t3.calls if n == 't3_thread_list'))
        self.assertEqual('issue/1', rows[(REPO, 1)]['branch'])
        self.assertEqual('/worktrees/issue-1', rows[(REPO, 1)]['worktree'])
        self.assertTrue(rows[(REPO, 1)]['active'])
        self.assertFalse(rows[(REPO, 2)]['active'])
        self.assertEqual('queued', rows[(REPO, 3)]['status'])
        self.assertTrue(snapshot['fresh'])
        self.assertLessEqual(rows[(REPO, 1)]['observed_at'], snapshot['generated_at'])
        self.assertTrue(all(a['includeSubagents'] is False for n, a in self.t3.calls
                            if n == 't3_thread_list'))

    def test_snapshot_accepts_t3_page_project_without_project_id_on_rows(self):
        self.mapped(1, 'mapped')
        # Matches the installed T3 list schema, which identifies the project
        # once on the page, unlike the detailed thread-read result.
        self.t3.pages[('chat', None)] = {'projectId': 'chat', 'threads': [
            {'threadId': 'mapped', 'status': 'running'}], 'nextCursor': None}
        self.t3.details['mapped'] = {'thread': self.thread('mapped', branch='issue/1',
                                                         worktreePath='/worktrees/issue-1')}
        snapshot = self.coordination.snapshot(self.t3)
        self.assertTrue(snapshot['fresh'])
        self.assertEqual([], snapshot['errors'])
        row = next(r for r in snapshot['issues'] if r['repo'] == REPO and r['issue'] == 1)
        self.assertTrue(row['active'])
        self.assertEqual('/worktrees/issue-1', row['worktree'])
        self.t3.pages[('chat', None)]['projectId'] = 'wrong-project'
        self.assertFalse(self.coordination.snapshot(self.t3)['fresh'])

    def test_snapshot_missing_unreachable_and_unknown_are_not_inactive(self):
        for n, thread_id in enumerate(('missing', 'unreachable', 'new-status'), 1):
            self.mapped(n, thread_id)
            self.coordination.claim(REPO, n, REPO, [f'file-{n}'])
        self.t3.details['unreachable'] = TimeoutError('private transport detail')
        self.t3.pages[('chat', None)] = {'threads': [self.thread('new-status', 'unexpected',
                                                               branch=None, worktreePath=None)]}
        snapshot = self.coordination.snapshot(self.t3)
        rows = {r['issue']: r for r in snapshot['issues'] if r['repo'] == REPO}
        self.assertEqual('missing', rows[1]['status'])
        self.assertEqual('unreachable', rows[2]['status'])
        for n in (1, 2, 3):
            self.assertIsNone(rows[n]['active'])
            self.assertFalse(rows[n]['fresh'])
            self.assertEqual(1, len(rows[n]['claims']))
        self.assertFalse(snapshot['fresh'])
        self.assertNotIn('private transport detail', json.dumps(snapshot))
        with self.assertRaises(ClaimConflict):
            self.coordination.claim(REPO, 4, REPO, ['file-1'])

    def test_snapshot_partial_pagination_and_failed_detail_keep_known_activity(self):
        self.mapped(1, 'running')
        self.mapped(2, 'absent')
        self.t3.pages[('chat', None)] = {'threads': [self.thread('running')], 'nextCursor': 2}
        self.t3.pages[('chat', 2)] = TimeoutError()
        self.t3.details['running'] = OSError()
        self.t3.details['absent'] = OSError()
        snapshot = self.coordination.snapshot(self.t3)
        rows = {r['issue']: r for r in snapshot['issues'] if r['repo'] == REPO}
        self.assertTrue(rows[1]['active'])
        self.assertEqual('running', rows[1]['status'])
        self.assertFalse(rows[1]['fresh'])
        self.assertEqual('unreachable', rows[2]['status'])
        self.assertIsNone(rows[2]['active'])
        self.assertEqual(3, len(snapshot['errors']))

    def test_snapshot_cursor_cycle_and_wrong_identity_are_reported(self):
        self.mapped(1, 'mapped')
        self.t3.pages[('chat', None)] = {'threads': [], 'nextCursor': 1}
        self.t3.pages[('chat', 1)] = {'threads': [], 'nextCursor': 1}
        self.t3.details['mapped'] = {'thread': self.thread('mapped', project='unregistered')}
        snapshot = self.coordination.snapshot(self.t3)
        row = next(r for r in snapshot['issues'] if r['repo'] == REPO and r['issue'] == 1)
        self.assertEqual('unreachable', row['status'])
        self.assertIsNone(row['active'])
        self.assertEqual(2, len(snapshot['errors']))
        self.assertEqual(2, sum(n == 't3_thread_list' and a['projectId'] == 'chat'
                                for n, a in self.t3.calls))

    @staticmethod
    def stop_process(process):
        if process.is_alive():
            process.kill()
        process.join(timeout=5)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Disposable child did not reach expected state')

    def start_guard(self, resource='build', repo=None):
        marker, finish = self.root / 'ready', self.root / 'finish'
        process = CTX.Process(target=guard_worker, args=(self.config, self.store_path, resource,
                                                        repo, marker, finish, self.root))
        process.start()
        self.addCleanup(self.stop_process, process)
        self.addCleanup(lambda: finish.touch())
        self.wait_until(lambda: marker.exists() and marker.read_text().strip())
        pid = int(marker.read_text())
        self.addCleanup(self.stop_orphan, pid)
        return process, pid, finish

    @staticmethod
    def stop_orphan(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def test_build_guard_is_global_nonblocking_and_child_holds_lock_after_wrapper_killed(self):
        process, child_pid, finish = self.start_guard()
        argv = [sys.executable, '-c', 'raise SystemExit(7)']
        start = time.monotonic()
        with self.assertRaises(ResourceBusy) as raised:
            self.coordination.run_locked('build', OTHER, argv)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual('resource_busy', raised.exception.as_dict()['error'])
        process.kill()
        process.join(timeout=5)
        os.kill(child_pid, 0)
        with self.assertRaises(ResourceBusy):
            self.coordination.run_locked('build', None, argv)
        finish.touch()
        # Probe only the OS lock; the command itself is never retried.
        import fcntl
        lockpath = self.root / 'state' / 'coordination-locks' / 'build.lock'
        def available():
            with lockpath.open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True
        self.wait_until(available)
        self.assertEqual(7, self.coordination.run_locked('build', None, argv))
        self.assertTrue(lockpath.exists())

    def test_publish_guards_are_per_registered_repo_and_release_on_normal_exit(self):
        process, _, finish = self.start_guard('publish', REPO)
        argv = [sys.executable, '-c', 'raise SystemExit(0)']
        with self.assertRaises(ResourceBusy):
            self.coordination.run_locked('publish', REPO, argv)
        self.assertEqual(0, self.coordination.run_locked('publish', OTHER, argv))
        self.assertEqual(0, self.coordination.run_locked('build', None, argv))
        finish.touch()
        process.join(timeout=5)
        self.assertEqual(0, process.exitcode)
        self.assertEqual(0, self.coordination.run_locked('publish', REPO, argv))
        with self.assertRaises(ValueError):
            self.coordination.run_locked('publish', 'Unknown/repo', argv)
        with self.assertRaises(ValueError):
            self.coordination.run_locked('other-resource', None, argv)

    def test_standalone_resource_lock_shares_guard_and_releases_on_exception(self):
        argv = [sys.executable, '-c', 'pass']
        with self.assertRaisesRegex(RuntimeError, 'wrapper failed'):
            with resource_lock(self.config, 'publish', REPO) as fd:
                self.assertGreaterEqual(os.fstat(fd).st_size, 0)
                with self.assertRaises(ResourceBusy):
                    self.coordination.run_locked('publish', REPO, argv)
                raise RuntimeError('wrapper failed')
        self.assertEqual(0, self.coordination.run_locked('publish', REPO, argv))
        with self.assertRaises(ValueError), resource_lock(self.config, 'publish', 'Unknown/repo'):
            self.fail('Unregistered publication guard was acquired')
        fresh = {**self.config, 'state_dir': str(self.root / 'new-state')}
        with resource_lock(fresh, 'build') as fd:
            os.fstat(fd)

    def test_guard_preserves_caller_cwd_argv_exit_status_and_does_not_rerun(self):
        marker = self.root / 'argv-and-cwd'
        script = '''import json, os, pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_text(json.dumps([os.getcwd(), sys.argv[2:], p.exists()]))
raise SystemExit(13)
'''
        previous = os.getcwd()
        try:
            os.chdir(self.root)
            code = self.coordination.run_locked('build', None,
                [sys.executable, '-c', script, str(marker), '$(touch forbidden)', '; echo nope'])
        finally:
            os.chdir(previous)
        self.assertEqual(13, code)
        self.assertEqual([str(self.root), ['$(touch forbidden)', '; echo nope'], False],
                         json.loads(marker.read_text()))
        with self.assertRaises(FileNotFoundError):
            self.coordination.run_locked('build', None, [str(self.root / 'missing-executable')])
        self.assertEqual(0, self.coordination.run_locked('build', None,
                                                       [sys.executable, '-c', 'pass'], cwd=self.root))
        for command in ('echo no-shell', [], [None]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.coordination.run_locked('build', None, command)


if __name__ == '__main__':
    unittest.main()
