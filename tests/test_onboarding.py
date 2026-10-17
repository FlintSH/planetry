"""New-plugin onboarding uses disposable Git repositories and fake APIs only."""

import copy
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from planetry.github import GitHubError
from planetry.onboarding import Onboarder


SOURCE = 'FlintSH/planetry'
PLUGIN = 'FlintSH/new-planetry-plugin'
SOURCE_PROJECT = '11111111-1111-1111-1111-111111111111'
PLUGIN_PROJECT = '22222222-2222-2222-2222-222222222222'
ISSUE_URL = f'https://github.com/{SOURCE}/issues/7'


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    def __init__(self, sha):
        self.repo = {
            'id': 2200, 'full_name': PLUGIN, 'name': PLUGIN.split('/')[1],
            'owner': {'login': 'FlintSH', 'id': 77}, 'private': False,
            'fork': False, 'archived': False, 'disabled': False,
            'has_issues': True, 'default_branch': 'main',
            'created_at': '2026-10-17T10:01:00Z',
        }
        self.issue = {
            'id': 10007, 'number': 7, 'state': 'open',
            'url': f'https://api.github.com/repos/{SOURCE}/issues/7',
            'html_url': ISSUE_URL,
            'repository_url': f'https://api.github.com/repos/{SOURCE}',
            'created_at': '2026-10-17T10:00:00Z',
            'user': {'id': 901, 'login': 'requester', 'type': 'User'},
        }
        self.sha = sha
        self.tag_sha = sha
        self.tag_error = None
        self.releases = [{
            'draft': False, 'prerelease': True, 'tag_name': 'build-1',
            'target_commitish': sha,
            'assets': [{'name': 'new-plugin.jar', 'state': 'uploaded', 'size': 1000},
                       {'name': 'SHA256SUMS', 'state': 'uploaded', 'size': 80}],
        }]
        self.runs = [{'status': 'completed', 'conclusion': 'success', 'head_sha': sha}]

    def get_repo(self, repo):
        return copy.deepcopy(self.repo)

    def get_issue(self, repo, number):
        return copy.deepcopy(self.issue)

    def get_commit(self, repo, ref):
        return {'sha': self.sha}

    def list_releases(self, repo):
        return copy.deepcopy(self.releases)

    def successful_runs(self, repo, sha):
        return copy.deepcopy(self.runs)

    def tag_commit(self, repo, tag):
        if self.tag_error:
            raise self.tag_error
        return self.tag_sha


class FakeT3:
    def __init__(self):
        self.projects = []
        self.calls = []
        self.create_calls = 0
        self.page_size = 1
        self.lose_create_response = False
        self.hide_created_project = False

    def call(self, tool, args):
        self.calls.append((tool, copy.deepcopy(args)))
        if tool == 't3_project_list':
            cursor = int(args.get('cursor', 0))
            projects = [p for p in self.projects if not p.get('hidden')]
            stop = cursor + self.page_size
            return {'projects': copy.deepcopy(projects[cursor:stop]),
                    'nextCursor': stop if stop < len(projects) else None}
        if tool == 't3_project_create':
            self.create_calls += 1
            project = {'id': PLUGIN_PROJECT, 'projectId': PLUGIN_PROJECT,
                       'title': args['title'], 'workspaceRoot': args['workspaceRoot'],
                       'hidden': self.hide_created_project}
            self.projects.append(project)
            if self.lose_create_response:
                self.lose_create_response = False
                raise TimeoutError('Project created but response lost')
            return copy.deepcopy(project)
        if tool == 't3_project_read':
            return {'id': SOURCE_PROJECT, 'projectId': SOURCE_PROJECT,
                    'defaultModelSelection': {'provider': 'codex', 'model': 'gpt-6-astra'}}
        if tool == 't3_project_update':
            return {'id': args['projectId'], 'projectId': args['projectId']}
        raise AssertionError(f'Unexpected T3 call: {tool}')


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.control = self.root / 'control'
        self.control.mkdir()
        (self.control / 'repositories.json').write_text(json.dumps({
            'system': 'planetry', 'repositories': [
                {'name': SOURCE, 'role': 'System context and automation', 'branch': 'main'}]}))
        self.workspace = self.root / 'new-plugin'
        self.workspace.mkdir()
        git(self.workspace, 'init', '-b', 'main')
        git(self.workspace, 'config', 'user.email', 'tests@example.invalid')
        git(self.workspace, 'config', 'user.name', 'Planetry test fixture')
        git(self.workspace, 'remote', 'add', 'origin', f'https://github.com/{PLUGIN}.git')
        self.descriptor = {
            'role': 'New gameplay plugin', 'platform': 'Paper/Purpur',
            'compatibility': 'Minecraft 26.1.2 / Java 25',
            'integration': 'Uses existing inventory readiness before moving players.',
            'validation': 'Run mvn verify and disposable Paper probes.',
            'source_issue': ISSUE_URL,
        }
        (self.workspace / 'planetry-plugin.json').write_text(json.dumps(self.descriptor))
        (self.workspace / 'AGENTS.md').write_text(
            'Read https://github.com/FlintSH/planetry system context. Publish tested changes to main.\n')
        (self.workspace / 'README.md').write_text('Disposable plugin fixture, never published.\n')
        workflows = self.workspace / '.github/workflows'
        workflows.mkdir(parents=True)
        (workflows / 'release.yml').write_text('name: release\non: [push]\njobs: {}\n')
        forms = self.workspace / '.github/ISSUE_TEMPLATE'
        forms.mkdir()
        (forms / 'feature.yml').write_text('name: Request a feature\nbody: []\n')
        git(self.workspace, 'add', '.')
        git(self.workspace, 'commit', '-m', 'Prepare disposable plugin fixture')
        self.sha = git(self.workspace, 'rev-parse', 'HEAD')
        self.github = FakeGitHub(self.sha)
        self.t3 = FakeT3()
        self.config_path = self.root / 'operator.json'
        self.config = {
            'control_root': str(self.control), 'state_dir': str(self.root / 'state'),
            'config_path': str(self.config_path),
            'repositories': [{'name': SOURCE, 'workspace': str(self.control),
                              'project_id': SOURCE_PROJECT}],
        }
        self.config_path.write_text(json.dumps(self.config))
        (self.root / 'state').mkdir(mode=0o700)
        self.published = []

    def publish(self, repo, descriptor):
        self.published.append((repo, copy.deepcopy(descriptor)))
        path = self.control / 'repositories.json'
        manifest = json.loads(path.read_text())
        if not any(r['name'] == repo for r in manifest['repositories']):
            manifest['repositories'].append({
                'name': repo, 'role': descriptor['role'], 'branch': 'main',
                'context': f'docs/plugins/{repo.split("/")[1]}.json'})
        path.write_text(json.dumps(manifest))

    def run_onboarding(self):
        with patch.object(Onboarder, '_publish', side_effect=self.publish):
            return Onboarder(self.config, self.github, self.t3).run(
                PLUGIN, self.workspace, SOURCE, 7)

    def assert_rejected(self):
        with self.assertRaises((ValueError, RuntimeError)):
            self.run_onboarding()
        self.assertEqual([], self.published)
        self.assertEqual(0, self.t3.create_calls)
        self.assertEqual(1, len(json.loads(self.config_path.read_text())['repositories']))

    def test_onboarding_registers_context_project_and_private_config_once(self):
        first = self.run_onboarding()
        second = self.run_onboarding()
        self.assertEqual(first, second)
        self.assertEqual(PLUGIN, first['name'])
        self.assertEqual(str(self.workspace), first['workspace'])
        self.assertEqual(PLUGIN_PROJECT, first['project_id'])
        self.assertEqual(1, self.t3.create_calls)
        config = json.loads(self.config_path.read_text())
        self.assertEqual([SOURCE, PLUGIN], [r['name'] for r in config['repositories']])
        self.assertEqual(0o600, stat.S_IMODE(self.config_path.stat().st_mode))

    def test_existing_project_is_reused_across_list_pages(self):
        self.t3.projects = [
            {'id': SOURCE_PROJECT, 'projectId': SOURCE_PROJECT,
             'title': 'Control', 'workspaceRoot': str(self.control)},
            {'id': PLUGIN_PROJECT, 'projectId': PLUGIN_PROJECT,
             'title': 'Plugin', 'workspaceRoot': str(self.workspace)},
        ]
        result = self.run_onboarding()
        self.assertEqual(PLUGIN_PROJECT, result['project_id'])
        self.assertEqual(0, self.t3.create_calls)
        self.assertTrue(any(args.get('cursor') == 1 for tool, args in self.t3.calls
                            if tool == 't3_project_list'))

    def test_closed_source_issue_is_rejected(self):
        self.github.issue['state'] = 'closed'
        self.assert_rejected()

    def test_pull_request_source_is_rejected(self):
        self.github.issue['pull_request'] = {'url': 'https://api.github.com/example'}
        self.assert_rejected()

    def test_bot_authored_source_is_rejected(self):
        self.github.issue['user']['type'] = 'Bot'
        self.assert_rejected()

    def test_different_owner_is_rejected(self):
        self.github.repo['owner']['login'] = 'AnotherOwner'
        self.github.repo['full_name'] = 'AnotherOwner/new-planetry-plugin'
        self.assert_rejected()

    def test_preexisting_unrelated_repository_is_rejected(self):
        self.github.repo['created_at'] = '2026-10-16T10:00:00Z'
        self.assert_rejected()

    def test_private_archived_fork_or_disabled_issues_are_rejected(self):
        for key, value in [('private', True), ('archived', True), ('fork', True),
                           ('disabled', True), ('has_issues', False)]:
            with self.subTest(key=key):
                before = self.github.repo[key]
                self.github.repo[key] = value
                self.assert_rejected()
                self.github.repo[key] = before

    def test_transferred_or_renumbered_source_issue_is_rejected(self):
        for change in [dict(number=8), dict(html_url='https://github.com/Other/unrelated/issues/7'),
                       dict(html_url=f'https://github.com/{SOURCE}/issues/8'), dict(html_url=None)]:
            with self.subTest(change=change):
                before = copy.deepcopy(self.github.issue)
                self.github.issue.update(change)
                self.assert_rejected()
                self.github.issue = before

    def test_source_number_must_be_a_positive_actual_integer(self):
        with patch.object(self.github, 'get_issue', wraps=self.github.get_issue) as get_issue:
            for value in [False, True, 0, -1, '7', 7.0, None]:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, 'positive integer'):
                        Onboarder(self.config, self.github, self.t3).run(
                            PLUGIN, self.workspace, SOURCE, value)
            get_issue.assert_not_called()

    def test_symlinked_published_context_files_are_rejected(self):
        for name in ['AGENTS.md', 'README.md', 'planetry-plugin.json']:
            with self.subTest(name=name):
                path = self.workspace / name
                content = path.read_text()
                outside = self.root / ('outside-' + name)
                outside.write_text(content)
                path.unlink()
                path.symlink_to(outside)
                git(self.workspace, 'add', name)
                git(self.workspace, 'commit', '-m', 'Symlink fixture')
                self.github.sha = git(self.workspace, 'rev-parse', 'HEAD')
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    self.run_onboarding()
                path.unlink()
                path.write_text(content)
                git(self.workspace, 'add', name)
                git(self.workspace, 'commit', '-m', 'Restore regular fixture file')
        self.assertEqual([], self.published)
        self.assertEqual(0, self.t3.create_calls)

    def test_invalid_t3_project_id_prevents_publication_and_activation(self):
        for project_id in [None, '', 'invalid', '../escape', 123, True]:
            with self.subTest(project_id=project_id):
                self.t3.projects = [{'id': project_id, 'workspaceRoot': str(self.workspace),
                                     'title': 'Existing project'}]
                with self.assertRaisesRegex(ValueError, 'invalid project ID'):
                    self.run_onboarding()
        self.assertEqual([], self.published)
        self.assertEqual(1, len(json.loads(self.config_path.read_text())['repositories']))

    def test_wrong_origin_is_rejected(self):
        git(self.workspace, 'remote', 'set-url', 'origin', 'https://github.com/Other/other.git')
        self.assert_rejected()

    def test_dirty_workspace_is_rejected(self):
        (self.workspace / 'AGENTS.md').write_text('Uncommitted edits\n')
        self.assert_rejected()

    def test_unpublished_head_is_rejected(self):
        self.github.sha = 'a' * 40
        self.assert_rejected()

    def test_release_and_successful_main_workflow_are_required(self):
        for field, value in [('releases', []), ('runs', [])]:
            with self.subTest(field=field):
                old = getattr(self.github, field)
                setattr(self.github, field, value)
                self.assert_rejected()
                setattr(self.github, field, old)

    def test_release_must_reference_exact_commit_and_contain_artifacts(self):
        for change in [dict(target_commitish='main'), dict(draft=True),
                       dict(assets=[{'name': 'new-plugin.jar'}]),
                       dict(assets=[{'name': 'SHA256SUMS'}])]:
            with self.subTest(change=change):
                before = copy.deepcopy(self.github.releases)
                self.github.releases[0].update(change)
                self.assert_rejected()
                self.github.releases = before

    def test_actual_release_tag_must_resolve_to_published_main(self):
        self.github.tag_sha = 'a' * 40
        self.assert_rejected()

    def test_both_release_artifacts_must_be_uploaded_and_nonempty(self):
        for index in [0, 1]:
            for change in [dict(size=0), dict(size=-1), dict(size=None),
                           dict(state='new'), dict(state='starter'), dict(state=None)]:
                with self.subTest(asset=index, change=change):
                    before = copy.deepcopy(self.github.releases)
                    self.github.releases[0]['assets'][index].update(change)
                    self.assert_rejected()
                    self.github.releases = before

    def test_release_tag_lookup_failure_propagates_without_activation(self):
        self.github.tag_error = GitHubError('GitHub tag lookup unavailable')
        with self.assertRaisesRegex(GitHubError, 'tag lookup unavailable'):
            self.run_onboarding()
        self.assertEqual([], self.published)
        self.assertEqual(0, self.t3.create_calls)
        self.assertEqual(1, len(json.loads(self.config_path.read_text())['repositories']))

    def test_lost_project_create_response_reconciles_without_duplicate(self):
        self.t3.lose_create_response = True
        with self.assertRaises(TimeoutError):
            self.run_onboarding()
        self.assertEqual([], self.published)
        self.assertEqual(1, self.t3.create_calls)
        self.assertEqual(PLUGIN_PROJECT, self.run_onboarding()['project_id'])
        self.assertEqual(1, self.t3.create_calls)

    def test_uncertain_invisible_project_never_blindly_recreates(self):
        self.t3.lose_create_response = True
        self.t3.hide_created_project = True
        with self.assertRaises(TimeoutError):
            self.run_onboarding()
        with self.assertRaisesRegex(RuntimeError, 'Uncertain T3 project'):
            self.run_onboarding()
        self.assertEqual(1, self.t3.create_calls)
        self.assertEqual([], self.published)
        self.t3.projects[-1]['hidden'] = False
        self.assertEqual(PLUGIN_PROJECT, self.run_onboarding()['project_id'])
        self.assertEqual(1, self.t3.create_calls)

    def test_retry_after_context_publication_preserves_single_registration(self):
        def publish_then_lose_response(repo, descriptor):
            self.publish(repo, descriptor)
            raise RuntimeError('Context push accepted but response lost')

        with patch.object(Onboarder, '_publish', side_effect=publish_then_lose_response):
            with self.assertRaises(RuntimeError):
                Onboarder(self.config, self.github, self.t3).run(
                    PLUGIN, self.workspace, SOURCE, 7)
        self.assertEqual(1, len(json.loads(self.config_path.read_text())['repositories']))
        self.run_onboarding()
        self.assertEqual(1, self.t3.create_calls)
        manifest = json.loads((self.control / 'repositories.json').read_text())
        self.assertEqual([SOURCE, PLUGIN], [r['name'] for r in manifest['repositories']])
        self.assertEqual(2, len(json.loads(self.config_path.read_text())['repositories']))

    def test_source_closed_during_project_creation_prevents_publication(self):
        original_call = self.t3.call

        def close_source(tool, args):
            result = original_call(tool, args)
            if tool == 't3_project_create':
                self.github.issue['state'] = 'closed'
            return result

        with patch.object(self.t3, 'call', side_effect=close_source):
            with self.assertRaisesRegex(ValueError, 'open human-authored'):
                self.run_onboarding()
        self.assertEqual([], self.published)
        self.assertEqual(1, len(json.loads(self.config_path.read_text())['repositories']))

    def test_source_closed_after_publication_defers_runtime_activation(self):
        def publish_and_close(repo, descriptor):
            self.publish(repo, descriptor)
            self.github.issue['state'] = 'closed'

        with patch.object(Onboarder, '_publish', side_effect=publish_and_close):
            with self.assertRaisesRegex(ValueError, 'open human-authored'):
                Onboarder(self.config, self.github, self.t3).run(
                    PLUGIN, self.workspace, SOURCE, 7)
        self.assertEqual(1, len(self.published))
        self.assertEqual(1, len(json.loads(self.config_path.read_text())['repositories']))
        self.github.issue['state'] = 'open'
        self.run_onboarding()
        self.assertEqual(2, len(json.loads(self.config_path.read_text())['repositories']))
        self.assertEqual(1, self.t3.create_calls)

    def test_reloads_private_config_and_preserves_unrelated_addition(self):
        current = copy.deepcopy(self.config)
        other = {'name': 'FlintSH/another-plugin', 'workspace': str(self.root / 'other'),
                 'project_id': '33333333-3333-3333-3333-333333333333'}
        current['repositories'].append(other)
        self.config_path.write_text(json.dumps(current))
        manifest_path = self.control / 'repositories.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['repositories'].append({'name': other['name'], 'role': 'Another plugin', 'branch': 'main'})
        manifest_path.write_text(json.dumps(manifest))
        self.run_onboarding()
        saved = json.loads(self.config_path.read_text())
        self.assertEqual([SOURCE, other['name'], PLUGIN], [r['name'] for r in saved['repositories']])
        self.assertEqual(other, saved['repositories'][1])

    def test_nested_checkout_and_symlink_alias_are_rejected(self):
        for path in [self.workspace / '.github', self.root / 'plugin-link']:
            with self.subTest(path=path):
                if path.name == 'plugin-link':
                    path.symlink_to(self.workspace, target_is_directory=True)
                with patch.object(Onboarder, '_publish', side_effect=self.publish):
                    with self.assertRaises(ValueError):
                        Onboarder(self.config, self.github, self.t3).run(PLUGIN, path, SOURCE, 7)
        self.assertEqual(0, self.t3.create_calls)
        self.assertEqual([], self.published)

    def test_repeated_publish_uses_actual_bare_remote_without_duplicate_commit(self):
        # This repository never leaves the temporary directory. API clients are
        # still fakes, so no GitHub issues, repositories or T3 projects are made.
        remote = self.root / 'control.git'
        git(self.control, 'init', '-b', 'main')
        git(self.control, 'config', 'user.email', 'tests@example.invalid')
        git(self.control, 'config', 'user.name', 'Planetry test fixture')
        git(self.control, 'add', '.')
        git(self.control, 'commit', '-m', 'Initial local control registry')
        git(self.root, 'init', '--bare', str(remote))
        git(self.control, 'remote', 'add', 'origin', str(remote))
        git(self.control, 'push', '-u', 'origin', 'main')
        first = Onboarder(self.config, self.github, self.t3).run(
            PLUGIN, self.workspace, SOURCE, 7)
        count = git(self.control, 'rev-list', '--count', 'main')
        self.assertEqual('2', count)
        second = Onboarder(self.config, self.github, self.t3).run(
            PLUGIN, self.workspace, SOURCE, 7)
        self.assertEqual(first, second)
        self.assertEqual(count, git(self.control, 'rev-list', '--count', 'main'))
        self.assertEqual('', git(self.control, 'status', '--porcelain'))
        manifest = json.loads(git(remote, 'show', 'main:repositories.json'))
        registered = [r for r in manifest['repositories'] if r['name'] == PLUGIN]
        self.assertEqual(1, len(registered))
        descriptor = json.loads(git(remote, 'show', 'main:' + registered[0]['context']))
        self.assertEqual(self.descriptor, descriptor)
        self.assertTrue((self.control / registered[0]['context']).is_file())
        self.assertEqual(1, self.t3.create_calls)
        self.assertEqual(1, git(self.control, 'worktree', 'list', '--porcelain').count('worktree '))


if __name__ == '__main__':
    unittest.main()
