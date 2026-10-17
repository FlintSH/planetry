"""Internal issue visibility, durable path claims and subprocess resource guards.

Claims never expire: a stopped thread may still have uncommitted work. Only an
explicit release by its issue owner frees a path. This database is independent
of the intake Store, and none of these operations contact GitHub.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import ntpath
import os
from pathlib import Path
import sqlite3
import subprocess
import time


ACTIVE = {'preparing', 'queued', 'starting', 'running', 'waiting'}
INACTIVE = {'idle', 'completed', 'interrupted', 'failed', 'cancelled', 'rolled_back'}
NOTE_LIMIT = 4000


class CoordinationError(RuntimeError):
    code = 'coordination_error'

    def as_dict(self):
        return {'error': self.code, 'message': str(self)}


class ClaimConflict(CoordinationError):
    code = 'claim_conflict'

    def __init__(self, conflicts):
        self.conflicts = conflicts
        owners = sorted({f'{c["repo"]}#{c["issue"]}' for c in conflicts})
        super().__init__('Paths are already claimed by ' + ', '.join(owners))

    def as_dict(self):
        return {**super().as_dict(), 'conflicts': self.conflicts}


class ResourceBusy(CoordinationError):
    code = 'resource_busy'

    def __init__(self, resource, repo=None):
        self.resource, self.repo = resource, repo
        super().__init__(f'{resource} resource is busy' + (f' for {repo}' if repo else ''))

    def as_dict(self):
        return {**super().as_dict(), 'resource': self.resource, 'repo': self.repo}


@contextmanager
def resource_lock(config, resource, repo=None):
    """Acquire a nonblocking build/publish guard and yield its locked FD.

    Callers launching subprocesses should pass this FD via pass_fds to retain
    protection if their wrapper dies. Closing the FD releases the guard after
    all inheriting children exit; never explicitly unlock or unlink its file.
    """
    if resource not in {'build', 'publish'}:
        raise ValueError('Resource must be build or publish')
    if resource == 'publish' and (not isinstance(repo, str) or
            repo not in {r['name'] for r in config['repositories']}):
        raise ValueError('Repository is not registered')
    directory = Path(config['state_dir']) / 'coordination-locks'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = ('build' if resource == 'build' else
            'publish-' + hashlib.sha256(repo.encode()).hexdigest())
    fd = os.open(directory / (name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResourceBusy(resource, repo if resource == 'publish' else None) from None
        yield fd
    finally:
        # A child may still hold this open-file description after its wrapper
        # is killed. LOCK_UN would release the child's lock prematurely too.
        os.close(fd)


def _paths(paths):
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError('Paths must be a nonempty list of relative paths')
    result = set()
    for path in paths:
        if (not isinstance(path, str) or not path.strip() or len(path) > 4096
                or path.startswith('/') or '\\' in path or ntpath.splitdrive(path)[0]
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path)):
            raise ValueError('Claims require relative paths; use . for the whole repository')
        parts = path.split('/')
        if '..' in parts or any(p.casefold() == '.git' for p in parts):
            raise ValueError('Claims cannot contain .. or .git paths')
        result.add('/'.join(p for p in parts if p not in {'', '.'}) or '.')
    return sorted(result)


def _overlaps(left, right):
    return (left == '.' or right == '.' or left == right
            or left.startswith(right + '/') or right.startswith(left + '/'))


def _text(text, *, empty=False):
    if (not isinstance(text, str) or len(text) > NOTE_LIMIT
            or (not empty and not text.strip())
            or any((ord(c) < 32 and c not in '\n\t') or 127 <= ord(c) <= 159 for c in text)):
        raise ValueError(f'Work notes require plain text of 1–{NOTE_LIMIT} characters')
    return text


class Coordination:
    """Share one state_dir across processes and pass the existing intake Store.

    claim() returns the owner's claims for its target repository. note() returns
    {text, updated_at}; release() returns a removed-row count. snapshot() returns
    {generated_at, fresh, issues, errors}; activity is True, False, or None when
    unknown. run_locked() returns the child's integer exit status, without retry.
    """

    def __init__(self, config, store):
        self.config, self.store = config, store
        self.repos = {r['name']: r for r in config['repositories']}
        self.state_dir = Path(config['state_dir'])
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / 'coordination.sqlite'
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS claims (
                repo TEXT NOT NULL, issue INTEGER NOT NULL,
                target_repo TEXT NOT NULL, path TEXT NOT NULL, claimed_at REAL NOT NULL,
                PRIMARY KEY(repo, issue, target_repo, path));
            CREATE INDEX IF NOT EXISTS claims_target ON claims(target_repo);
            CREATE TABLE IF NOT EXISTS notes (
                repo TEXT NOT NULL, issue INTEGER NOT NULL,
                text TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(repo, issue));
        ''')

    def close(self):
        self.db.close()

    def _registered(self, repo):
        if not isinstance(repo, str) or repo not in self.repos:
            raise ValueError('Repository is not registered')

    def _owner(self, repo, issue):
        self._registered(repo)
        if type(issue) is not int or issue <= 0:
            raise ValueError('Issue must be a positive integer')
        row = self.store.issue(repo, issue)
        if row is None:
            raise ValueError('Issue is not known to Planetry intake')
        return row

    def _claims(self, repo, issue, target_repo=None):
        sql, args = 'SELECT * FROM claims WHERE repo=? AND issue=?', [repo, issue]
        if target_repo is not None:
            sql += ' AND target_repo=?'
            args.append(target_repo)
        return [dict(r) for r in self.db.execute(sql + ' ORDER BY target_repo,path', args)]

    def claim(self, repo, issue, target_repo, paths, note=''):
        self._owner(repo, issue)
        self._registered(target_repo)
        paths, note = _paths(paths), _text(note, empty=True)
        now = time.time()
        with self.db:
            # Serialize the read/check/write, not just each INSERT. Otherwise two
            # processes can both pass the overlap check before either inserts.
            self.db.execute('BEGIN IMMEDIATE')
            conflicts = []
            for row in self.db.execute('SELECT * FROM claims WHERE target_repo=? ORDER BY repo,issue,path',
                                       (target_repo,)):
                if (row['repo'], row['issue']) == (repo, issue):
                    continue
                requested = [p for p in paths if _overlaps(p, row['path'])]
                if requested:
                    owner = self.store.issue(row['repo'], row['issue'])
                    payload = json.loads(owner['payload']) if owner else {}
                    conflicts.append({**dict(row), 'requested_paths': requested,
                                      'title': payload.get('title'),
                                      'thread_id': owner['thread_id'] if owner else None})
            if conflicts:
                raise ClaimConflict(conflicts)
            self.db.executemany('INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?)',
                                [(repo, issue, target_repo, p, now) for p in paths])
            if note:
                self._write_note(repo, issue, note, now)
        return self._claims(repo, issue, target_repo)

    def _write_note(self, repo, issue, text, now):
        self.db.execute('''INSERT INTO notes VALUES (?,?,?,?)
            ON CONFLICT(repo,issue) DO UPDATE SET text=excluded.text,updated_at=excluded.updated_at''',
                        (repo, issue, text, now))

    def note(self, repo, issue, text):
        self._owner(repo, issue)
        text, now = _text(text), time.time()
        with self.db:
            self._write_note(repo, issue, text, now)
        return {'text': text, 'updated_at': now}

    def release(self, repo, issue, target_repo=None, paths=None):
        self._owner(repo, issue)
        if target_repo is not None:
            self._registered(target_repo)
        if paths is not None and target_repo is None:
            raise ValueError('Releasing specific paths requires target_repo')
        normalized = _paths(paths) if paths is not None else None
        sql, args = 'DELETE FROM claims WHERE repo=? AND issue=?', [repo, issue]
        if target_repo is not None:
            sql += ' AND target_repo=?'
            args.append(target_repo)
        if normalized is not None:
            # Only exact claims are released. Releasing a child cannot punch a
            # hole in a repo-wide claim; release its explicit . claim instead.
            sql += ' AND path IN (' + ','.join('?' for _ in normalized) + ')'
            args.extend(normalized)
        with self.db:
            return self.db.execute(sql, args).rowcount

    def run_locked(self, resource, repo, command, cwd=None):
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(a, (str, os.PathLike)) for a in command)):
            raise ValueError('Command must be a nonempty argv list')
        with resource_lock(self.config, resource, repo) as fd:
            # Child retains the guard if this wrapper is killed while waiting.
            return subprocess.run(command, cwd=cwd, shell=False, check=False,
                                  pass_fds=(fd,)).returncode

    def snapshot(self, t3):
        """Read live T3 state without exposing unrelated/manual project threads.

        Freshness is observation time, not proof that a claim owner is editing.
        Missing/unreachable threads retain their claims and unknown activity.
        No snapshot operation cancels, settles, resumes or launches a thread.
        """
        errors, projects = [], {}
        for project_id in sorted({r['project_id'] for r in self.repos.values()}):
            threads, observed, cursor, seen = {}, {}, None, set()
            complete = False
            while True:
                args = {'projectId': project_id, 'limit': 100, 'includeSubagents': False}
                if cursor is not None:
                    args['cursor'] = cursor
                try:
                    page = t3.call('t3_thread_list', args)
                    rows = page['threads']
                    # T3 identifies the project on the page; its list rows do
                    # not include projectId (thread_read details do).
                    if (page.get('projectId', project_id) != project_id
                            or not isinstance(rows, list) or any(not isinstance(r, dict)
                            or not isinstance(r.get('threadId'), str)
                            or r.get('projectId', project_id) != project_id for r in rows)):
                        raise ValueError('Malformed T3 thread page')
                    threads.update((r['threadId'], r) for r in rows)
                    observed.update((r['threadId'], time.time()) for r in rows)
                    cursor = page.get('nextCursor')
                    if cursor is None:
                        complete = True
                        break
                    if not isinstance(cursor, (int, str)) or cursor in seen:
                        raise ValueError('Repeated or invalid T3 page cursor')
                    seen.add(cursor)
                except Exception as exc:
                    errors.append({'project_id': project_id, 'error': type(exc).__name__})
                    break
            projects[project_id] = (threads, observed, complete)

        owners = {(r['repo'], r['number']): r for r in
                  self.store.mappings() + self.store.pending_issues()}
        for row in self.store.db.execute("SELECT * FROM issues WHERE launch_state='intent'"):
            owners[(row['repo'], row['number'])] = dict(row)
        # A durable claim or handoff remains visible even after intake drains
        # the event queue, including a launch whose mapping is not known yet.
        for row in self.db.execute('SELECT repo,issue FROM claims UNION SELECT repo,issue FROM notes'):
            owner = self.store.issue(row['repo'], row['issue'])
            if owner:
                owners[(row['repo'], row['issue'])] = owner
        claims = {}
        for row in self.db.execute('SELECT * FROM claims ORDER BY target_repo,path'):
            claims.setdefault((row['repo'], row['issue']), []).append(dict(row))
        notes = {(r['repo'], r['issue']): {'text': r['text'], 'updated_at': r['updated_at']}
                 for r in self.db.execute('SELECT * FROM notes')}

        result = []
        for (repo, number), owner in sorted(owners.items()):
            if repo not in self.repos:
                continue
            project_id = self.repos[repo]['project_id']
            threads, observed, complete = projects[project_id]
            thread_id = owner['thread_id']
            thread = threads.get(thread_id)
            observed_at = observed.get(thread_id)
            fresh, lookup = complete, None
            if thread_id and (thread is None or 'branch' not in thread or 'worktreePath' not in thread):
                try:
                    response = t3.call('t3_thread_read', {'threadId': thread_id, 'limit': 1})
                    if not isinstance(response, dict) or 'thread' not in response:
                        raise ValueError('Malformed T3 thread read')
                    detail = response['thread']
                    if detail is None:
                        lookup = 'missing'
                        fresh = False
                    elif (not isinstance(detail, dict) or detail.get('threadId') != thread_id
                          or detail.get('projectId') != project_id):
                        raise ValueError('Mapped T3 thread identity does not match registered project')
                    else:
                        thread = {**(thread or {}), **detail}
                        observed_at, fresh = time.time(), True
                except Exception as exc:
                    lookup = 'missing' if getattr(exc, 'status', None) == 404 else 'unreachable'
                    fresh = False
                    errors.append({'repo': repo, 'issue': number, 'thread_id': thread_id,
                                   'error': type(exc).__name__})
            if thread is not None:
                status = thread.get('status')
                if not isinstance(status, str):
                    status = 'unknown'
                active = True if status in ACTIVE else False if status in INACTIVE else None
                if active is None:
                    fresh = False
            elif thread_id:
                status, active, fresh = lookup or ('missing' if complete else 'unreachable'), None, False
            elif owner['launch_state'] == 'intent':
                status, active, fresh = 'pending_launch', None, False
            else:
                status, active = 'queued', False
            payload = json.loads(owner['payload'])
            result.append({'repo': repo, 'issue': number, 'title': payload.get('title', ''),
                           'issue_state': payload.get('state'), 'thread_id': thread_id,
                           'project_id': project_id, 'status': status, 'active': active,
                           'branch': thread.get('branch') if thread else None,
                           'worktree': thread.get('worktreePath') if thread else None,
                           'claims': claims.get((repo, number), []), 'note': notes.get((repo, number)),
                           'observed_at': observed_at, 'fresh': fresh,
                           'lookup': lookup, 'last_status': owner.get('last_status')})
        return {'generated_at': time.time(), 'fresh': not errors and all(r['fresh'] for r in result),
                'issues': result, 'errors': errors}
