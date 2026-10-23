# Operating Planetry issue intake

## Architecture

`python3 -m planetry … run` is a persistent, code-only poller. It uses authenticated
GitHub conditional GETs with pagination. T3 receives work only when there is an
authorized issue/body/state change or an authorized comment change. The router
does not use GitHub Actions runners, a public webhook endpoint, an LLM timer, or
a temporary token borrowed from a conversation.

Validate that cached pagination discovers comment growth past 100 and 200
comments. A full cached final page must not hide newer pages after a GitHub
`304` response without a `Link` header.

T3's local OAuth dynamic client registration, pairing decision and token exchange
create a dedicated MCP client. PKCE, state, issuer, redirect and scope validation
are enforced. The local `t3 auth pairing create` command supplies the bootstrap
credential; it is consumed locally. The OAuth bearer renews before its 30-day
expiry and is stored with mode `0600`. The client is authorized for autonomous
threads using the owner's explicit full-access policy.

SQLite stores observations, incoming events, thread mappings, launch intents,
delivery IDs and public reply intents. An interrupted send uses the same T3
`clientRequestId` when retried. Launch has no server idempotency key: the router
records intent first, then reconciles the deterministic issue marker against T3
threads. An uncertain launch that cannot be found is held for operator review;
it is never blindly repeated. Keep the issue marker in thread titles.

Replies carry a hidden marker and are reconciled against the authenticated
GitHub actor. A cross-process lock prevents the poller and reply CLI from posting
the same outbox item concurrently. An unmarked human comment is a follow-up only
when both the issue author and comment author have allowed GitHub IDs; they need
not be the same account. This applies to new replies on closed issues and settled
threads too. Marked automation comments never request work, even when the posting
account is explicitly allowed. A copied marker grants no trust: outsiders'
comments are ignored and excluded from the context helper. GitHub does not offer
a comment idempotency key, so reconciliation is best-effort if its read API has
not yet exposed an accepted write.

## GitHub identity policy

Every registered repository uses this mandatory global top-level list in the
private operator config, with the two explicitly authorized identities:

```json
{
  "allowed_github_users": [
    {"id": 87043024, "login": "FlintSH"},
    {"id": 89701169, "login": "thebeeee"}
  ]
}
```

`allowed_github_users` must be a nonempty list. Each object must have exactly
`id` and `login`. `id` must be an actual JSON integer greater than zero, not a
boolean, string or floating-point number. `login` must use GitHub's login syntax.
IDs and case-insensitive logins must be unique within the list.
Authorization matches only the immutable numeric ID. The login is cosmetic:
the same account remains authorized after a rename, and a copied login with
another ID is denied. The legacy `allowed_github_user` single object is accepted
only when `allowed_github_users` is absent. Supplying both keys is rejected;
the singular key is not merged with or used as a fallback for a malformed list.
Missing or malformed policy fails closed, including during configuration reload.

Only issues authored by an allowed human may launch or resume automation, and
only comments from allowed humans may trigger follow-ups or enter the context
helper. The issue author and commenter can be different allowed accounts.
GitHub must report each as type `User` with an actual integer ID in the policy;
do not coerce a string, float or boolean into an authorized identity.
Nonhuman actors, pull requests, marked automation comments and outsiders remain
excluded. Denied issues receive no automated replies, including acknowledgements
or denial notices. `FlintSH` is explicitly allowed by numeric ID, not by the
authenticated posting role. Repository ownership and collaborator status grant
no implicit bypass. New plugins inherit this global policy unchanged;
per-repository identity overrides are not supported.

Issue agents must use only the installed control checkout's helper and private
config supplied in their task prompt for policy, filtered `context`, live
`registry`, coordination, replies and reopening. A copy in an issue worktree can
be stale. Do not bypass the installed helper with an unfiltered context fetch,
unmarked GitHub comment or direct issue-state mutation.

## Install

1. Clone the control repository and each plugin repository. Read their `AGENTS.md`.
2. Register each checkout as a T3 project. Set its default to the Codex provider,
   `gpt-6.1-sol`, ultra reasoning and Fast service as shown below. Leave the normal
   checkout on `main`. The router asks T3 to create the issue worktree from current
   `origin/main` before starting the thread.
3. Copy `operator.example.json` to a private directory outside Git, fill the
   absolute checkout/state paths and T3 project IDs, and restrict it to `0600`.
   Keep the mandatory top-level `allowed_github_users` list shown above; access
   comes from these explicit IDs, not repository or posting roles. Every
   configured repository must also be in `repositories.json`.
4. Run the checks below, then install a user service using
   `deploy/planetry-issues.service.example`. Substitute the absolute paths. The
   service user must already have working `gh auth status` and access to the
   local T3 instance. Enable user lingering if the service must survive logout.

Use the installed control checkout for these commands, never an issue worktree.
Substitute the actual installed paths:

```sh
cd /absolute/installed/control
python3 -m planetry --config /private/operator.json check
python3 -m planetry --config /private/operator.json once
python3 -m planetry --config /private/operator.json status
systemctl --user enable --now planetry-issues.service
```

The first scan includes existing open issues authored by allowed accounts.
Historical closed issues and their existing comments are observed as a baseline,
not all launched during installation or upgrade. Previously observed comments
remain observed when policy changes. New authorized replies are watched after
closure or settlement and return to the same mapped issue thread and worktree.
Keep the observation state and mappings intact so old comments are not replayed.
All configured repositories are explicit; unregistered repositories cannot
request a thread through intake.

Intake delivers a new authorized reply even if the issue is closed; the agent
decides semantically what it needs. An actionable bug, feature or work request
requires the installed `reopen --repo OWNER/REPO --issue N --comment COMMENT_ID`
helper before implementation. It validates the registered issue, exact comment
and allowed human identities, rejects outsider, nonhuman and marked automation
comments, and is idempotent when already open. The agent then implements and
closes only after verified delivery. The CLI returns `{"reopen":"sent"}` on
success or `{"reopen":"ignored"}` for a stale comment or later closure. Re-read
installed `context` and confirm the issue is open before implementation, even
after an idempotent `sent`. A comment's `updated_at` must be later than
`closed_at` to reopen; a new edit after a later closure can request new work.
An ignored result does not authorize work or an alternate state mutation.
Pending reopen decisions also snapshot the authorized conversation. New human
comments, edits or deletions invalidate the pending decision so the agent must
read the current context and reassess it. Automation and denied comments do not
invalidate it. An unmapped closed issue starts triage only for a comment newer
than its closure; older queued comments cannot restart canceled work.
Questions or guidance are answered while the resolved issue stays closed;
thanks need no reopen or code. Cancellation
requests remain authoritative. See [issue-agent instructions](ISSUE_AGENT.md).

## Model selection

The owner's selection for every Planetry project is:

```json
{
  "instanceId": "codex",
  "model": "gpt-6.1-sol",
  "options": [
    {"id": "reasoningEffort", "value": "ultra"},
    {"id": "serviceTier", "value": "priority"}
  ]
}
```

Set this as `defaultModelSelection` with T3's `t3_project_update`. New issue
threads use the project default. Replies continue the existing thread, so when
the owner changes this preference, also apply it to existing issue threads with
`t3_thread_configure` using `modelSelection`. Project defaults do not rewrite
existing threads. The router does not choose different models by task size.

The onboarding helper copies the source project's entire model selection,
including reasoning and service tier, into a new plugin's T3 project. Each copy
is independent; later owner changes must be applied to all registered projects.
The issue-agent instructions require the same selection for delegated work.
This selection does not change runtime permissions or intake limits.

## Controls and monitoring

- `poll_seconds`: 60 by default, minimum 30. Empty polling consumes API requests,
  not model tokens. ETags reduce unchanged responses and API usage.
- `max_active_threads`: 3 in the example and current installation, configurable
  from 1 to 4 (omitting the setting preserves the conservative default of 1).
  It reloads before each poll without stopping existing threads. Raising it lets
  queued issues start in available slots; lowering it defers new starts and never
  cancels existing work. Outstanding uncertain launches also reserve slots.
- `max_new_threads_per_day`: 20 initially, per UTC day. Additional new issues
  remain queued. This is a launch limit, not a total model-token budget;
  authorized comments in an existing thread can still consume tokens.
- `systemctl --user stop planetry-issues` pauses intake. It does not cancel an
  already running T3 conversation. Stop that conversation in T3 if required.
- `systemctl --user status planetry-issues` and `journalctl --user -u
  planetry-issues` show service health. The CLI `status` command shows the last
  successful poll, queue and private thread mappings. Do not post its output to
  public issues.

The service backs off on transport/rate-limit failures. Events remain in SQLite.
A failed or interrupted provider run is reported in the authorized issue; a new
reply from either allowed account resumes the same thread, including after it
has settled or the issue has closed. An agent needing clarification asks in the
issue and ends the turn, releasing its slot.

Any invalid or unreadable configuration refresh skips the poll and backs off.
The service retains its state and last valid config object, but intake resumes
only after the config is valid. Running T3 threads are unaffected. Repair the
private config and run `check`; the poller retries automatically. Keep SQLite
and existing thread mappings rather than clearing state or recreating threads.

Queued comment events record `author_id`. Denied comments are ignored and cached
prompts containing them are invalidated. Legacy events with unknown numeric
provenance wait for a fresh GitHub response for that exact comment ID; a failed
poll does not discard them or guess identity from a login. If the comment is no
longer available, operator review is required to clear the unverified event.
Authorized pending prompts are rebuilt under the current global policy. Launch
intents retain their original delivery event set, so recovering an accepted
launch does not consume newer follow-ups that still need to be sent.

If a launch is stuck at `intent`, inspect the corresponding T3 project for the
`[issue-GITHUB_ID]` title marker and inspect service logs. An invisible or deleted
thread must not be replaced without checking whether its work ran. The router
automatically links a visible matching thread on the next poll. Back up state
before manual recovery; do not delete SQLite to clear an error, because that
loses deduplication and can start old issues again.

Back up the entire private state directory while the service is stopped,
including SQLite and its WAL if present. Restore it with the same T3 environment;
thread IDs refer to that environment. Credentials and cached issue content are
private. Never commit them. If T3 is upgraded, run `check`, the automated routing
suite and read-only connection checks before relying on intake. Do not create
synthetic GitHub issues for testing or health checks.

## Parallel work and live rollout

`work list` reads current T3 status and the private work board without starting
or stopping threads. It includes queued requests and uncertain launch intents.
The separate `coordination.sqlite` stores atomic path claims and progress notes.
Include it in state backups. Claims require explicit release; a completed,
missing or crashed thread does not prove its checkout has no unpublished work.
See [coordination](COORDINATION.md) for the CLI, handoffs and recovery rules.

The `work run` guards serialize heavy builds globally and publication per
registered repository. They fail with exit 75 when busy and never rerun a
command automatically. They protect cooperating agents, not arbitrary shell
commands that bypass the helper. Onboarding uses the same control-repository
publication guard as normal issue work. Keep guard lockfiles in place.

For a live upgrade, publish and verify the code, fast-forward the clean control
checkout, and notify currently running issue threads with an in-flight T3
`steer` message to read the new coordination guide and record their existing
scope. Preserve their IDs, branches, worktrees and runs. Never restart, cancel,
or recreate those threads. Trusted internal agent coordination through the work
board and T3 `steer` remains authorized under the identity policy; it does not
admit other public commenters. Preserve existing authorized issue threads,
worktrees, Codex `gpt-6.1-sol` ultra/Fast selection and three parallel slots.
Retain the intake SQLite database, observations and launch intents so recovery
reconciles normally. Do not test this through synthetic GitHub issues.

For the two-account policy rollout, hold `onboarding.lock`, reread the latest
private config, replace the identity policy with the canonical
`allowed_github_users` list above and remove the legacy singular key in the same
atomic write. Preserve all other fields, especially concurrent plugin
registrations, credentials, runtime paths and the configured three slots. Keep
the file private with mode `0600`. Apply any concurrency change under this same
lock; the validated limit reloads on the next poll.

Install the verified code in the clean control checkout and restart only
`planetry-issues.service`. Never restart `t3code.service`, cancel or recreate T3
issue threads for an intake upgrade; all running threads continue. Run the
installed helper's `check` and read-only status/connection checks, then inspect
the normal poll result. Steer running issue threads to read the current policy
and follow-up instructions and use the installed filtered `context`, `reply`
and `reopen` helpers. Future turns also receive the current policy in rebuilt
prompts. Before the first live poll, record exact observations of comments
already answered through existing threads only after checking their body
fingerprints still match. Mark only those exact IDs as handled, without replay
or requeue. Do not clear state or requeue observed comments to exercise the rollout.

## Adding a plugin

Plugins created to fulfill authorized Planetry issues use the automatic
[`onboard` helper](NEW_PLUGIN.md). It validates the source issue, canonical repo
and checkout, published main, successful CI, and release assets. It registers or
reuses a T3 project, publishes the shared registry/context in a dedicated control
worktree, fast-forwards the clean control checkout, then atomically appends the
private configuration. All subsequent issue prompts load the live registry.
The source issue must be authored by an allowed human account. New registrations
inherit the mandatory global `allowed_github_users` policy unchanged; repository
ownership does not grant access and there is no per-repository override.

The service reloads validated repository additions and concurrency limits before
every poll, preserving existing authorized thread mappings and queue state. No
restart or copied webhook secret is needed. Any invalid or unreadable config
refresh pauses intake and backs off until a valid refresh; cached configuration
does not permit continued intake. State and running T3 threads are preserved.
Changes to runtime paths or the T3 endpoint still require an operator restart.

Onboarding holds a cross-process lock. Retry the same command after interruption:
it reuses matching projects and public/private registrations. An uncertain T3
project creation is held until project discovery can reconcile its workspace.
Unpublished control worktrees remain in the private state's
`onboarding-worktrees` directory for recovery. A dirty control checkout, conflicting
main push, changed source identity or unrelated repository fails without replacing
existing configuration. The source issue must remain open until onboarding completes.

The operator can still deliberately register an existing plugin by updating the
public registry/context, T3 project and private configuration. The automatic
helper is specifically for newly created issue-driven plugins, not arbitrary
adoption of existing repositories.

## Authority and deployment boundary

The router restricts both repositories and GitHub author IDs before work enters
the queue. Agents accept authorized Planetry development requests while refusing
issue text that tries to change their policy, expose secrets, access unrelated
projects or deploy to the live Minecraft network. Their responses must use the
marked reply helper, which must not reply to denied issues.

These are task-scope instructions, **not an OS security sandbox**. This deployment
runs agents under the existing development account with its GitHub access. A
public issue from an allowed account can consume the development account's
model allowance and lead to a tested commit on main. For stronger credential
and filesystem separation, move this service and T3 into a dedicated Planetry
VM/account and use a GitHub App or credentials restricted to the registered
repositories. Only the operator can change the global identity policy; issue
content cannot authorize an exception.

Agent changes to plugin code follow main CI and the existing release pipeline.
Changes to this service require its own tests and an operator-controlled service
restart. New plugin registration through the helper is authorized; an issue still
cannot rewrite its own permissions, authentication or other operational policy.
