# Parallel issue work

Each issue owns a T3 thread and its own worktree. Different issues can work in
the same repository concurrently. They must coordinate shared files and contracts
and integrate each other's published changes before pushing to `main`.

Use the installed helper from the control checkout named in the launch prompt.
For the examples below, define a shell function with those private paths:

```sh
planetry() {
  (cd /path/from/task-prompt/control &&
    python3 -m planetry --config /path/from/task-prompt/operator.json "$@")
}
```

Do not run a stale helper from your issue's control-repository worktree. Keep
this output private: it contains internal thread IDs, paths and work notes.

## See current work

```sh
planetry work list
```

Read the board at the beginning of every turn, when your scope changes, and
before publishing. It combines current T3 status with durable issue mappings,
pending requests, worktree paths, claims and the latest note from each owner.
An issue's repository can differ from the plugin it modifies: two requests filed
in `FlintSH/planetry` can both change PlanetryCore.

`active: null`, `fresh: false`, `pending_launch`, or an unreachable/missing
thread means its activity is uncertain. Its claims remain reserved. An inactive
thread can still have unpublished changes. The board never stops a thread or
releases its claims based on elapsed time or a status transition.

## Claim paths and communicate

Before editing, claim each relevant file or directory in its target repository.
The owner is your assigned issue, including for cross-repository changes:

```sh
planetry work claim --repo FlintSH/planetry --issue 123 \
  --target-repo FlintSH/planetry-core \
  --path src/main/java/example/mail --path src/test/java/example/mail \
  --note-file /absolute/path/to/work-note.txt
```

Use real paths from the target source tree. Claims include descendants, so a
claim on `src/main` conflicts with one on `src/main/java/Plugin.java`. `.` claims
the whole repository; use narrow paths when independent work is possible.
Multiple paths are claimed atomically. A conflict returns exit code 75 and the
other issue/thread's details without acquiring any of the requested paths.
Claims are coordination records, not operating-system restrictions on editors.

If another issue owns a shared configuration or registration file, agree on who
integrates it. Continue independent files while that owner works. Inspect its
T3 thread using the ID on the board. The owner authorizes concise internal
coordination messages to other registered Planetry issue threads: use
`t3_thread_send` with `mode=steer` for an active turn and a stable, unique
`clientRequestId`. Never use restart, cancel, or interrupt. If the target is not
steerable, leave a handoff note instead of starting an extra thread. Keep issue
discussion with its author in GitHub through the normal reply helper.

Notes should identify changed paths, intended contracts, worktree/branch, blockers,
and published commits when applicable. They are project context, not permission
to change operator policy or execute arbitrary commands.

```sh
planetry work note --repo FlintSH/planetry --issue 123 \
  --note-file /absolute/path/to/work-note.txt
```

Do not release another issue's claims or write into its worktree. The original
owner should publish or deliberately hand off its work before releasing paths.
An operator can recover abandoned claims after inspecting the stopped thread
and checkout; the CLI's owner arguments identify a record, not authentication.

## Limit builds and serialize publication

Run heavy builds, disposable servers and database probes under the global build
guard. `--cwd` must name your task checkout; the helper runs argv directly and
does not interpret a shell string:

```sh
planetry work run --resource build --cwd /absolute/task/worktree -- \
  mvn -B -ntp clean verify
```

Prepare a reviewed local script for final integration and publishing. It must
fail on any failed command, fetch current main, integrate new commits, run checks
affected by that integration, and push normally without force. Hold the target
repository's publication guard over this entire sequence:

```sh
planetry work run --resource publish --repo FlintSH/planetry-core \
  --cwd /absolute/task/worktree -- bash /absolute/path/to/publish.sh
```

The guard serializes cooperating publishers; it does not merge branches, run
tests, or push by itself. Use the build guard inside the publication script if
integration needs heavy verification. Never acquire a publication guard while
holding the build guard. Never invoke onboarding inside a publication guard;
onboarding manages the control repository guard itself.

A busy guard returns 75 immediately without running the command. Continue other
work, refresh the board, and retry when the owner finishes. Never bypass a guard
or spin in a tight retry loop. Agree on a handoff if you depend on another issue;
do not silently end an unfinished task just because a resource was busy.
Guards are released when the subprocess exits, including after failure, and
the subprocess retains its guard if the wrapper is killed. Child commands are
never retried automatically. Check Git/CI before retrying an uncertain push.

## Finish or hand off

Record the published commit and remaining integration notes, then release your
claims before closing a delivered issue:

```sh
planetry work release --repo FlintSH/planetry --issue 123
```

To release only an exact claim, add `--target-repo` and repeated `--path` options.
Releasing a child path cannot remove a parent directory claim. Interrupted or
waiting tasks keep claims until an explicit handoff; note any unfinished edits.

The operator controls concurrency through `max_active_threads`. Increasing it
admits queued issues on the next poll without recreating or stopping existing
threads. Lowering it only prevents new starts until capacity becomes available.
