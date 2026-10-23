"""Durable intake, delivery intents and idempotent reply outbox."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS observations (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS issues (
                repo TEXT NOT NULL, number INTEGER NOT NULL, github_id INTEGER NOT NULL,
                payload TEXT NOT NULL, thread_id TEXT, launch_title TEXT,
                launch_state TEXT NOT NULL DEFAULT 'new', launch_at REAL,
                last_run_id TEXT, last_status TEXT, failure_notified TEXT,
                PRIMARY KEY(repo, number));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
                event_ids TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS replies (
                key TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
                body TEXT NOT NULL, close_issue INTEGER NOT NULL, comment_id INTEGER,
                state TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reopens (
                key TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
                comment_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, closed_at TEXT,
                state TEXT NOT NULL DEFAULT 'pending');
        """)

    def meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    def retry_due(self, scope):
        failure = self.meta('backoff:' + scope)
        return not failure or failure['retry_at'] <= time.time()

    def record_failure(self, scope, error):
        previous = self.meta('backoff:' + scope, {})
        attempts = previous.get('attempts', 0) + 1
        now = time.time()
        self.set_meta('backoff:' + scope, {
            'scope': scope, 'error': type(error).__name__, 'attempts': attempts,
            'last_error_at': now, 'retry_at': now + min(3600, 60 * 2 ** min(attempts - 1, 6)),
        })

    def clear_failure(self, scope):
        with self.db:
            self.db.execute('DELETE FROM metadata WHERE key=?', ('backoff:' + scope,))

    def diagnostics(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT value FROM metadata WHERE key LIKE 'backoff:%' ORDER BY key")]

    def observe(self, key, fingerprint):
        row = self.db.execute('SELECT fingerprint FROM observations WHERE key=?', (key,)).fetchone()
        if row and row[0] == fingerprint:
            return False
        self.db.execute('INSERT OR REPLACE INTO observations VALUES (?,?)', (key, fingerprint))
        return True

    def issue(self, repo, number):
        row = self.db.execute('SELECT * FROM issues WHERE repo=? AND number=?', (repo, number)).fetchone()
        return dict(row) if row else None

    def ingest_issue(self, repo, issue, emit=True):
        if not isinstance(issue, dict) or not isinstance(issue.get('user'), dict):
            raise ValueError('Malformed GitHub issue author')
        if 'pull_request' in issue or issue.get('user', {}).get('type') == 'Bot':
            return
        if (type(issue.get('id')) is not int or issue['id'] <= 0
                or type(issue.get('number')) is not int or issue['number'] <= 0
                or issue.get('state') not in {'open', 'closed'}
                or not isinstance(issue.get('title'), str)
                or not (issue.get('body') is None or isinstance(issue['body'], str))):
            raise ValueError('Malformed GitHub issue payload')
        number = issue['number']
        old = self.issue(repo, number)
        content = {k: issue.get(k) for k in ('id', 'number', 'title', 'body', 'state')}
        with self.db:
            self.db.execute('''INSERT INTO issues(repo,number,github_id,payload) VALUES (?,?,?,?)
                ON CONFLICT(repo,number) DO UPDATE SET payload=excluded.payload''',
                (repo, number, issue['id'], json.dumps(issue)))
            if self.observe(f'issue:{issue["id"]}', digest(content)) and emit:
                if issue['state'] == 'open' or (old and (old['thread_id'] or old['launch_state'] == 'intent')):
                    self.add_event(repo, number, 'issue', content)

    def ingest_comment(self, repo, number, comment, actor_id, *, watch_closed=False):
        if not isinstance(comment, dict) or not isinstance(comment.get('user'), dict):
            raise ValueError('Malformed GitHub comment author')
        if comment.get('user', {}).get('type') == 'Bot':
            return
        if (type(comment.get('id')) is not int or comment['id'] <= 0
                or not isinstance(comment['user'].get('login'), str)
                or type(comment['user'].get('id')) is not int
                or not isinstance(comment.get('updated_at'), str)
                or not (comment.get('body') is None or isinstance(comment['body'], str))):
            raise ValueError('Malformed GitHub comment payload')
        body = comment.get('body') or ''
        if comment.get('user', {}).get('id') == actor_id and '<!-- planetry-agent:' in body:
            return
        issue = self.issue(repo, number)
        if not issue:
            return  # Pull request comments are absent from the issue table.
        self.verify_comment_author(repo, number, comment)
        with self.db:
            if self.observe(f'comment:{comment["id"]}', digest(body)):
                # Baseline historical comments on untracked closed issues once;
                # later comments/edits must be triaged even without a mapping.
                if json.loads(issue['payload'])['state'] == 'open' or issue['thread_id'] or watch_closed:
                    self.add_event(repo, number, 'comment', {
                        'id': comment['id'], 'author': comment['user']['login'],
                        'author_id': comment['user']['id'],
                        'body': body, 'updated_at': comment['updated_at']})

    def verify_comment_author(self, repo, number, comment):
        """Backfill legacy provenance from a fresh GitHub resource, never a login."""
        user = comment.get('user')
        if (type(comment.get('id')) is not int or not isinstance(user, dict)
                or user.get('type') != 'User' or type(user.get('id')) is not int
                or user['id'] <= 0):
            return
        with self.db:
            for old in self.db.execute("SELECT id,payload FROM events WHERE repo=? AND number=? AND kind='comment' AND state='pending'",
                                       (repo, number)).fetchall():
                content = json.loads(old['payload'])
                if content.get('id') == comment['id'] and 'author_id' not in content:
                    content['author_id'] = user['id']
                    self.db.execute('UPDATE events SET payload=? WHERE id=?', (json.dumps(content), old['id']))

    def add_event(self, repo, number, kind, payload):
        self.db.execute('INSERT INTO events(repo,number,kind,payload,created) VALUES (?,?,?,?,?)',
                        (repo, number, kind, json.dumps(payload), time.time()))

    def pending_issues(self):
        return [dict(r) for r in self.db.execute('''SELECT i.*, MIN(e.id) AS oldest_event
            FROM issues i JOIN events e USING(repo,number) WHERE e.state='pending'
            GROUP BY i.repo,i.number ORDER BY oldest_event''')]

    def events(self, repo, number):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE repo=? AND number=? AND state='pending' ORDER BY id", (repo, number))]

    def update_issue(self, repo, number, **fields):
        allowed = {'thread_id', 'launch_title', 'launch_state', 'launch_at', 'last_run_id', 'last_status', 'failure_notified'}
        if not fields.keys() <= allowed:
            raise ValueError('Invalid issue update')
        with self.db:
            self.db.execute('UPDATE issues SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE repo=? AND number=?',
                            (*fields.values(), repo, number))

    def save_delivery(self, repo, number, events, message):
        ids = [e['id'] for e in events]
        key = 'planetry:' + digest([repo, number, ids])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO deliveries(key,repo,number,event_ids,message) VALUES(?,?,?,?,?)',
                            (key, repo, number, json.dumps(ids), message))
        return dict(self.db.execute('SELECT * FROM deliveries WHERE key=?', (key,)).fetchone())

    def pending_delivery(self, repo, number):
        r = self.db.execute("SELECT * FROM deliveries WHERE repo=? AND number=? AND state='pending' ORDER BY rowid LIMIT 1",
                            (repo, number)).fetchone()
        return dict(r) if r else None

    def discard_events(self, repo, number, event_ids):
        """Remove denied input from the queue and invalidate prompts containing it."""
        event_ids = set(event_ids)
        if not event_ids:
            return
        with self.db:
            self.db.executemany("UPDATE events SET state='ignored' WHERE repo=? AND number=? AND id=? AND state='pending'",
                                [(repo, number, eid) for eid in event_ids])
            for row in self.db.execute("SELECT key,event_ids FROM deliveries WHERE repo=? AND number=? AND state='pending'",
                                       (repo, number)).fetchall():
                if event_ids.intersection(json.loads(row['event_ids'])):
                    self.db.execute("UPDATE deliveries SET state='ignored' WHERE key=?", (row['key'],))

    def refresh_delivery_message(self, key, message):
        with self.db:
            self.db.execute("UPDATE deliveries SET message=? WHERE key=? AND state='pending'", (message, key))

    def unlinked_intents(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM issues WHERE launch_state='intent' AND thread_id IS NULL")]

    def begin_launch(self, repo, number, title, delivery_key):
        # Pin exactly the prompt whose acceptance may become uncertain. Later
        # policy filtering or new comments must not change its event set.
        with self.db:
            self.db.execute("UPDATE issues SET launch_state='intent', launch_title=?, launch_at=? WHERE repo=? AND number=?",
                            (title, time.time(), repo, number))
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                            (f'launch_delivery:{repo}:{number}', json.dumps(delivery_key)))

    def launch_delivery(self, repo, number):
        key = self.meta(f'launch_delivery:{repo}:{number}')
        if key is None:
            # Pre-policy intents have one outstanding launch delivery. Pin it
            # before filtering can invalidate that original prompt.
            row = self.pending_delivery(repo, number)
            if row is None:
                raise ValueError('Launch intent has no recoverable delivery')
            key = row['key']
            self.set_meta(f'launch_delivery:{repo}:{number}', key)
        return key

    def complete_delivery(self, key):
        with self.db:
            self._complete_delivery(key)

    def _complete_delivery(self, key):
        row = self.db.execute('SELECT event_ids FROM deliveries WHERE key=?', (key,)).fetchone()
        if row is None:
            raise ValueError('Unknown delivery')
        for eid in json.loads(row[0]):
            self.db.execute("UPDATE events SET state='delivered' WHERE id=? AND state='pending'", (eid,))
        self.db.execute("UPDATE deliveries SET state='delivered' WHERE key=?", (key,))

    def link_launch(self, repo, number, thread_id, run_id, delivery_key):
        # A launch is not idempotent at the T3 API. Publishing its thread mapping
        # and consuming the prompt must therefore commit together locally.
        with self.db:
            delivery = self.db.execute('SELECT repo,number FROM deliveries WHERE key=?', (delivery_key,)).fetchone()
            if delivery is None or (delivery['repo'], delivery['number']) != (repo, number):
                raise ValueError('Launch delivery does not belong to this issue')
            updated = self.db.execute('''UPDATE issues SET thread_id=?, launch_state='linked', last_run_id=?
                WHERE repo=? AND number=?''', (thread_id, run_id, repo, number))
            if updated.rowcount != 1:
                raise ValueError('Unknown launch issue')
            self._complete_delivery(delivery_key)

    def prepare_reply(self, repo, number, body, close=False):
        key = digest([repo, number, body, close])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO replies(key,repo,number,body,close_issue) VALUES(?,?,?,?,?)',
                            (key, repo, number, body, int(close)))
        return dict(self.db.execute('SELECT * FROM replies WHERE key=?', (key,)).fetchone())

    def finish_reply(self, key, comment_id, state='sent'):
        with self.db:
            self.db.execute('UPDATE replies SET comment_id=?, state=? WHERE key=?', (comment_id, state, key))

    def pending_replies(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM replies WHERE state IN ('pending','commented')")]

    def mappings(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM issues WHERE thread_id IS NOT NULL')]

    def prepare_reopen(self, repo, number, comment_id, fingerprint, closed_at):
        key = digest(['reopen', repo, number, comment_id, fingerprint])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO reopens(key,repo,number,comment_id,fingerprint,closed_at) VALUES(?,?,?,?,?,?)',
                            (key, repo, number, comment_id, fingerprint, closed_at))
        return dict(self.db.execute('SELECT * FROM reopens WHERE key=?', (key,)).fetchone())

    def finish_reopen(self, key, state='sent'):
        with self.db:
            self.db.execute('UPDATE reopens SET state=? WHERE key=?', (state, key))

    def pending_reopens(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM reopens WHERE state='pending'")]
