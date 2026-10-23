import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import time

from .github import GitHub, RetryableError
from .router import Router, MARKER
from .store import Store
from .t3 import T3
from .access import allowed_user, require_policy


def load_config(path):
    path = Path(path).expanduser().resolve()
    config = json.loads(path.read_text())
    require_policy(config)
    config['config_path'] = str(path)
    for key in ('state_dir', 'control_root'):
        if not Path(config[key]).is_absolute():
            raise ValueError(f'{key} must be absolute')
    manifest = json.loads((Path(config['control_root']) / 'repositories.json').read_text())
    allowed = {repo['name'] for repo in manifest['repositories']}
    if not config['repositories']:
        raise ValueError('At least one repository must be registered')
    seen = set()
    for repo in config['repositories']:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo['name']) or repo['name'] not in allowed:
            raise ValueError('Repository missing from trusted manifest')
        if repo['name'] in seen:
            raise ValueError('Duplicate repository')
        seen.add(repo['name'])
        if not Path(repo['workspace']).is_absolute() or not isinstance(repo['project_id'], str) or not re.fullmatch(
                r'(?:mcp:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', repo['project_id']):
            raise ValueError('Each repository needs an absolute workspace and T3 project ID')
    if type(config.get('poll_seconds', 60)) is not int or not 30 <= config.get('poll_seconds', 60) <= 3600:
        raise ValueError('poll_seconds must be between 30 and 3600')
    if type(config.get('max_active_threads', 1)) is not int or not 1 <= config.get('max_active_threads', 1) <= 4:
        raise ValueError('max_active_threads must be between 1 and 4')
    if type(config.get('max_new_threads_per_day', 20)) is not int or not 1 <= config.get('max_new_threads_per_day', 20) <= 100:
        raise ValueError('max_new_threads_per_day must be between 1 and 100')
    state = Path(config['state_dir'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    return config


def refresh_router(router):
    """Hot-load validated additions while preserving process-bound clients/state."""
    fresh = load_config(router.config['config_path'])
    for key in ('state_dir', 'control_root', 't3_endpoint'):
        if fresh.get(key) != router.config.get(key):
            raise ValueError('Changing runtime paths or T3 endpoint requires a service restart')
    router.config = fresh
    router.repos = {r['name']: r for r in fresh['repositories']}


def main():
    parser = argparse.ArgumentParser(description='Planetry GitHub issue bridge')
    parser.add_argument('--config', required=True, help='Private operator JSON path')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'once', 'status', 'check'):
        commands.add_parser(name)
    commands.add_parser('registry', help='Read the current repository map and plugin context')
    work = commands.add_parser('work', help='Coordinate parallel issue work privately')
    work_commands = work.add_subparsers(dest='work_command', required=True)
    work_commands.add_parser('list', help='Show live issue threads, worktrees, claims and notes')
    for name in ('claim', 'note', 'release'):
        child = work_commands.add_parser(name)
        child.add_argument('--repo', required=True, help='Repository containing the assigned issue')
        child.add_argument('--issue', required=True, type=int)
        if name != 'note':
            child.add_argument('--target-repo', required=name == 'claim', help='Repository being edited')
            child.add_argument('--path', action='append', required=name == 'claim', dest='paths',
                               help='Repository-relative file or directory; repeat for multiple paths')
        if name in ('claim', 'note'):
            child.add_argument('--note-file', required=name == 'note', help='Private UTF-8 progress/handoff file')
    run = work_commands.add_parser('run', help='Run one command under a build or publication lock')
    run.add_argument('--resource', required=True, choices=('build', 'publish'))
    run.add_argument('--repo', help='Registered target repository; required for publish')
    run.add_argument('--cwd', required=True, help='Task worktree to run the command in')
    run.add_argument('argv', nargs=argparse.REMAINDER, help='Command argv after --; no implicit shell')
    onboard = commands.add_parser('onboard', help='Register a delivered plugin created from a Planetry issue')
    for flag in ('repo', 'workspace', 'source-repo'):
        onboard.add_argument('--' + flag, required=True)
    onboard.add_argument('--source-issue', required=True, type=int)
    for name in ('reply', 'context', 'reopen'):
        child = commands.add_parser(name)
        child.add_argument('--repo', required=True)
        child.add_argument('--issue', required=True, type=int)
        if name == 'reply':
            child.add_argument('--body-file', required=True)
            child.add_argument('--close', action='store_true')
        if name == 'reopen':
            child.add_argument('--comment', required=True, type=int)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(args.config)
    state = Path(config['state_dir'])
    store = Store(state / 'router.sqlite')
    if args.command == 'work':
        return work_command(args, config, store)
    if args.command == 'registry':
        manifest = json.loads((Path(config['control_root']) / 'repositories.json').read_text())
        contexts = []
        for repo in manifest['repositories']:
            if 'context' in repo:
                path = (Path(config['control_root']) / repo['context']).resolve()
                if not path.is_relative_to(Path(config['control_root']).resolve() / 'docs/plugins'):
                    raise ValueError('Plugin context path escapes the shared context directory')
                contexts.append({'repository': repo['name'], 'context': json.loads(path.read_text())})
        print(json.dumps({'repositories': config['repositories'], 'system': manifest,
                          'plugin_contexts': contexts}, indent=2, ensure_ascii=False))
        return 0
    if args.command == 'status':
        print(json.dumps({'last_poll': store.meta('last_poll'),
            'last_successful_poll': store.meta('last_successful_poll'),
            'diagnostics': store.diagnostics(),
            'pending_issues': [{'repo': r['repo'], 'number': r['number'], 'launch_state': r['launch_state']}
                               for r in store.pending_issues()],
            'threads': [{'repo': r['repo'], 'number': r['number'], 'thread_id': r['thread_id']}
                        for r in store.mappings()]}, indent=2))
        return 0
    github = GitHub(state / 'github')
    t3 = T3(state, endpoint=config.get('t3_endpoint', 'http://127.0.0.1:3773/mcp'))
    if args.command == 'onboard':
        from .onboarding import Onboarder
        print(json.dumps(Onboarder(config, github, t3).run(
            args.repo, Path(args.workspace), args.source_repo, args.source_issue), indent=2))
        return 0
    router = Router(config, github, t3, store)
    if args.command in ('reply', 'context', 'reopen'):
        if args.repo not in router.repos or args.issue < 1:
            raise ValueError('Unknown repository or invalid issue number')
        if args.command == 'reply':
            result = router.reply(args.repo, args.issue, Path(args.body_file).read_text(), args.close)
            print(json.dumps({'comment_id': result}))
        elif args.command == 'reopen':
            result = router.reopen(args.repo, args.issue, args.comment)
            print(json.dumps({'reopen': result}))
        else:
            issue = github.get_issue(args.repo, args.issue)
            if 'pull_request' in issue:
                raise ValueError('This is a pull request, not a development issue')
            if not router.authorized_issue(issue):
                raise ValueError('Issue author is not authorized for Planetry automation')
            comments = [c for c in github.get_comments(args.repo, args.issue)
                        if isinstance(c, dict) and allowed_user(config, c.get('user'))
                        and not (c['user']['id'] == router.actor() and MARKER in (c.get('body') or ''))]
            print(json.dumps({'source': 'untrusted GitHub issue content', 'issue': issue,
                              'human_comments': comments}, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'check':
        capabilities = t3.call('orchestrator_capabilities', {})
        print(json.dumps({'github_actor': github.me()['login'],
            'allowed_github_users': require_policy(config),
            't3_runtime_mode': capabilities['runtimeMode'],
            'repositories': list(router.repos)}, indent=2))
        if capabilities['runtimeMode'] != 'full-access':
            raise ValueError('T3 client is not authorized for autonomous development')
        return 0
    # Serializes pollers, not the reply helper used by the running issue agent.
    with (state / 'poller.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Planetry poller is already running') from None
        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        failures = 0
        while not stopped:
            delay = config.get('poll_seconds', 60)
            try:
                try:
                    refresh_router(router)
                except (ValueError, KeyError, TypeError, OSError):
                    logging.error('Configuration refresh rejected; intake paused until policy is valid')
                    # A missing/malformed identity restriction must never fall
                    # back to a previously broader authorization policy.
                    raise
                delay = router.config.get('poll_seconds', 60)
                router.tick()
                failures = 0
            except Exception as exc:
                failures += 1
                # Do not log raw provider/HTTP payloads, issue text or credentials.
                logging.error('Poll failed (%s); durable events retained', type(exc).__name__)
                delay = min(3600, delay * 2 ** min(failures, 5))
                if isinstance(exc, RetryableError):
                    delay = max(delay, exc.retry_after)
                if args.command == 'once':
                    return 1
            if args.command == 'once':
                return 0
            deadline = time.monotonic() + delay
            while not stopped and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    return 0


def work_command(args, config, store):
    from .coordination import Coordination, CoordinationError
    board = Coordination(config, store)
    t3 = None
    try:
        if args.work_command == 'list':
            t3 = T3(Path(config['state_dir']), endpoint=config.get('t3_endpoint', 'http://127.0.0.1:3773/mcp'))
            result = board.snapshot(t3)
        elif args.work_command == 'claim':
            note = Path(args.note_file).read_text(encoding='utf-8') if args.note_file else ''
            result = board.claim(args.repo, args.issue, args.target_repo, args.paths, note=note)
        elif args.work_command == 'note':
            result = board.note(args.repo, args.issue, Path(args.note_file).read_text(encoding='utf-8'))
        elif args.work_command == 'release':
            result = board.release(args.repo, args.issue, target_repo=args.target_repo, paths=args.paths)
        else:
            argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
            return board.run_locked(args.resource, args.repo, argv, cwd=args.cwd)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CoordinationError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False, indent=2))
        return 75  # Temporary contention: no requested command or claim ran.
    finally:
        if t3:
            t3.close()
        board.close()
        store.db.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Detailed state remains local; generic error types cannot accidentally
        # expose tokens in a subprocess exception or an HTTP response body.
        print(f'Planetry command failed ({type(error).__name__}). Check configuration and service status.', file=sys.stderr)
        sys.exit(1)
