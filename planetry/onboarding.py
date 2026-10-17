"""Register a delivered issue-created plugin without changing agent privileges."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime

from .access import allowed_user


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _git(workspace, *args, pass_fds=()):
    result = subprocess.run(['git', '-C', str(workspace), *args], capture_output=True,
                            text=True, timeout=120, pass_fds=pass_fds)
    if result.returncode:
        # Git stderr can contain authenticated URLs. Keep it out of public replies.
        raise RuntimeError('Git operation failed; inspect the onboarding checkout locally')
    return result.stdout.strip()


def origin_matches(origin, repo):
    return origin.casefold() in {f'https://github.com/{repo}.git'.casefold(),
                                f'https://github.com/{repo}'.casefold(),
                                f'git@github.com:{repo}.git'.casefold(),
                                f'ssh://git@github.com/{repo}.git'.casefold()}


class Onboarder:
    def __init__(self, config, github, t3):
        self.config, self.github, self.t3 = config, github, t3
        self.root = Path(config['control_root']).resolve()
        self.state = Path(config['state_dir'])

    def _source(self, repo, number):
        if type(number) is not int or number < 1:
            raise ValueError('Source issue number must be a positive integer')
        if repo not in {r['name'] for r in self.config['repositories']}:
            raise ValueError('Source repository is not registered')
        issue = self.github.get_issue(repo, number)
        if ('pull_request' in issue or issue.get('state') != 'open'
                or issue.get('user', {}).get('type') != 'User'
                or issue.get('number') != number
                or issue.get('html_url') != f'https://github.com/{repo}/issues/{number}'):
            raise ValueError('Onboarding requires an open human-authored Planetry issue')
        if not allowed_user(self.config, issue.get('user')):
            raise ValueError('Onboarding requires a source issue authored by the allowed GitHub user')
        return issue

    def _validate(self, repo, workspace, source_repo, source_issue):
        if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', repo):
            raise ValueError('Invalid repository name')
        source = self._source(source_repo, source_issue)
        target = self.github.get_repo(repo)
        if (target.get('full_name') != repo
                or target.get('owner', {}).get('login', '').casefold() != source_repo.split('/')[0].casefold()
                or target.get('private') is not False or target.get('archived')
                or target.get('fork') or target.get('disabled') or not target.get('has_issues')
                or target.get('default_branch') != 'main'):
            raise ValueError('Plugin must be a public main-branch repository owned by the Planetry owner')
        if datetime.fromisoformat(target['created_at'].replace('Z', '+00:00')) < datetime.fromisoformat(source['created_at'].replace('Z', '+00:00')):
            raise ValueError('Repository predates its source issue; unrelated repositories cannot be adopted')
        if not workspace.is_absolute() or workspace != workspace.resolve():
            raise ValueError('Use the canonical absolute plugin checkout path')
        if Path(_git(workspace, 'rev-parse', '--show-toplevel')).resolve() != workspace:
            raise ValueError('Workspace must be the repository root')
        if not origin_matches(_git(workspace, 'remote', 'get-url', 'origin'), repo):
            raise ValueError('Workspace origin does not match the requested GitHub repository')
        if _git(workspace, 'status', '--porcelain'):
            raise ValueError('Publish and clean the plugin checkout before onboarding')
        for registered in self.config['repositories']:
            path = Path(registered['workspace']).resolve()
            if registered['name'].casefold() != repo.casefold() and (
                    path == workspace or path in workspace.parents or workspace in path.parents):
                raise ValueError('Plugin checkout overlaps an existing registered workspace')
        head = _git(workspace, 'rev-parse', 'HEAD')
        if self.github.get_commit(repo, 'main')['sha'] != head:
            raise ValueError('Plugin checkout must match published main')
        descriptor_path = workspace / 'planetry-plugin.json'
        if any((workspace / p).is_symlink() for p in ('planetry-plugin.json', 'AGENTS.md', 'README.md')):
            raise ValueError('Plugin context files cannot be symlinks')
        descriptor = json.loads(descriptor_path.read_text())
        fields = {'role', 'platform', 'compatibility', 'integration', 'validation', 'source_issue'}
        if set(descriptor) != fields or any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in descriptor.values()):
            raise ValueError('Plugin descriptor needs the six documented nonempty text fields')
        if descriptor['source_issue'] != f'https://github.com/{source_repo}/issues/{source_issue}':
            raise ValueError('Plugin descriptor does not match the source issue')
        tracked = set(_git(workspace, 'ls-files').splitlines())
        if not {'AGENTS.md', 'README.md', 'planetry-plugin.json'} <= tracked:
            raise ValueError('Plugin must publish its context, README and agent instructions')
        if not any(p in tracked for p in ('.github/workflows/release.yml', '.github/workflows/release.yaml')):
            raise ValueError('Plugin needs its release-on-main workflow')
        if not any(p.startswith('.github/ISSUE_TEMPLATE/') for p in tracked):
            raise ValueError('Plugin needs its Planetry issue forms')
        agents = (workspace / 'AGENTS.md').read_text()
        if 'https://github.com/' + self.config.get('control_repository', 'FlintSH/planetry') not in agents:
            raise ValueError('Plugin agent instructions must link the shared Planetry context')
        if not self.github.successful_runs(repo, head):
            raise ValueError('A successful main CI run is required before onboarding')
        releases = [r for r in self.github.list_releases(repo)
                    if not r.get('draft') and r.get('target_commitish') == head]
        verified = False
        for release in releases:
            assets = release.get('assets', [])
            jars = [a for a in assets if a.get('name', '').endswith('.jar')
                    and not a['name'].endswith(('-sources.jar', '-javadoc.jar'))]
            sums = [a for a in assets if a.get('name') == 'SHA256SUMS']
            if jars and sums and all(a.get('state') == 'uploaded' and type(a.get('size')) is int
                                     and a['size'] > 0 for a in jars + sums):
                # GitHub ignores target_commitish when the tag already exists.
                # Check the actual tag object as well as release metadata.
                if self.github.tag_commit(repo, release['tag_name']) == head:
                    verified = True
                    break
        if not verified:
            raise ValueError('Publish the tested JAR and SHA256SUMS for this exact commit first')
        return descriptor

    def _project(self, repo, workspace, source_repo, journal, journal_path):
        projects, cursor = [], None
        while True:
            args = {'limit': 100}
            if cursor is not None:
                args['cursor'] = cursor
            page = self.t3.call('t3_project_list', args)
            projects.extend(page['projects'])
            cursor = page.get('nextCursor')
            if cursor is None:
                break
        matches = [p for p in projects if Path(p['workspaceRoot']).resolve() == workspace]
        if len(matches) > 1:
            raise ValueError('Multiple T3 projects match the plugin checkout')
        if matches:
            project = matches[0]
        else:
            if journal.get('project_intent'):
                raise RuntimeError('Uncertain T3 project creation; reconcile the project before retrying')
            journal['project_intent'] = True
            atomic_json(journal_path, journal)
            project = self.t3.call('t3_project_create', {'title': 'Planetry · ' + repo.split('/')[1],
                                                        'workspaceRoot': str(workspace)})
        if not isinstance(project.get('id'), str) or not re.fullmatch(
                r'(?:mcp:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', project['id']):
            raise ValueError('T3 returned an invalid project ID')
        source_id = next(r['project_id'] for r in self.config['repositories'] if r['name'] == source_repo)
        source = self.t3.call('t3_project_read', {'projectId': source_id})
        if not source.get('defaultModelSelection'):
            raise ValueError('Source T3 project needs a default model')
        self.t3.call('t3_project_update', {'projectId': project['id'], 'defaultThreadEnvMode': 'worktree',
                                         'defaultModelSelection': source['defaultModelSelection']})
        journal['project_id'] = project['id']
        atomic_json(journal_path, journal)
        return project['id']

    def _publish(self, repo, descriptor):
        from .coordination import resource_lock
        control_repo = self.config.get('control_repository', 'FlintSH/planetry')
        with resource_lock(self.config, 'publish', control_repo) as lock_fd:
            self._publish_locked(repo, descriptor, lock_fd=lock_fd)

    def _publish_locked(self, repo, descriptor, *, lock_fd):
        def git(*args):
            return _git(*args, pass_fds=(lock_fd,))

        # A new owned worktree on each attempt starts from actual remote main.
        # An earlier successful push is recognized by the desired files, so a
        # lost response never causes a second registration commit.
        if git(self.root, 'status', '--porcelain') or git(self.root, 'branch', '--show-current') != 'main':
            raise ValueError('Control checkout must be clean on main for safe context refresh')
        git(self.root, 'fetch', 'origin', 'main')
        git(self.root, 'merge', '--ff-only', 'origin/main')
        parent = self.state / 'onboarding-worktrees'
        parent.mkdir(mode=0o700, exist_ok=True)
        checkout = Path(tempfile.mkdtemp(prefix='context-', dir=parent))
        git(self.root, 'worktree', 'add', '--detach', str(checkout), 'origin/main')
        try:
            manifest_path = checkout / 'repositories.json'
            manifest = json.loads(manifest_path.read_text())
            context_path = 'docs/plugins/' + repo.split('/')[1] + '.json'
            entry = {'name': repo, 'role': descriptor['role'], 'branch': 'main', 'context': context_path}
            existing = [r for r in manifest['repositories'] if r['name'].casefold() == repo.casefold()]
            if existing and existing != [entry]:
                raise ValueError('Existing registration differs; resolve its context deliberately')
            if not existing:
                manifest['repositories'].append(entry)
            context = checkout / context_path
            context.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
            context.write_text(json.dumps(descriptor, indent=2, ensure_ascii=False) + '\n')
            git(checkout, 'add', 'repositories.json', context_path)
            if git(checkout, 'diff', '--cached', '--name-only'):
                git(checkout, 'commit', '-m', f'Register {repo} in Planetry system context')
                # No force push. A competing main update leaves this retryable.
                git(checkout, 'push', 'origin', 'HEAD:main')
            git(self.root, 'fetch', 'origin', 'main')
            git(self.root, 'merge', '--ff-only', 'origin/main')
        finally:
            # Keep failed/unpublished work recoverable for operator inspection.
            if not git(checkout, 'status', '--porcelain') and git(checkout, 'rev-parse', 'HEAD') == git(self.root, 'rev-parse', 'origin/main'):
                git(self.root, 'worktree', 'remove', str(checkout))

    def run(self, repo, workspace, source_repo, source_issue):
        workspace = Path(workspace)
        with (self.state / 'onboarding.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Reload inside the lock so simultaneous plugin additions are merged.
            from .__main__ import load_config
            fresh = load_config(self.config['config_path'])
            if any(fresh.get(k) != self.config.get(k) for k in ('state_dir', 'control_root', 't3_endpoint')):
                raise ValueError('Runtime configuration changed during onboarding; restart the command')
            self.config = fresh
            descriptor = self._validate(repo, workspace, source_repo, source_issue)
            key = hashlib.sha256(repo.casefold().encode()).hexdigest()
            journal_path = self.state / ('onboarding-' + key + '.json')
            identity = {'repo': repo, 'workspace': str(workspace), 'source': descriptor['source_issue']}
            journal = json.loads(journal_path.read_text()) if journal_path.exists() else identity.copy()
            if any(journal.get(k) != v for k, v in identity.items()):
                raise ValueError('Onboarding identity differs from its saved attempt')
            project_id = self._project(repo, workspace, source_repo, journal, journal_path)
            self._source(source_repo, source_issue)
            self._publish(repo, descriptor)
            self._source(source_repo, source_issue)
            # Public context is published and present locally before activation.
            current = load_config(self.config['config_path'])
            if any(current.get(k) != self.config.get(k) for k in ('state_dir', 'control_root', 't3_endpoint')):
                raise ValueError('Runtime configuration changed during onboarding; restart the command')
            entry = {'name': repo, 'workspace': str(workspace), 'project_id': project_id}
            existing = [r for r in current['repositories'] if r['name'].casefold() == repo.casefold()]
            if existing and existing != [entry]:
                raise ValueError('Private registration differs from the plugin project')
            if not existing:
                current['repositories'].append(entry)
            current.pop('config_path', None)
            atomic_json(Path(self.config['config_path']), current)
            journal['complete'] = True
            atomic_json(journal_path, journal)
            return entry
