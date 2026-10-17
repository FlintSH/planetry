# Operating PlanetRy issue intake

## Architecture

`python3 -m planetry … run` is a persistent, code-only poller. It uses authenticated
GitHub conditional GETs with pagination. T3 receives work only when there is an
issue/body/state change or a human comment change. The router does not use GitHub
Actions runners, a public webhook endpoint, an LLM timer, or a temporary token
borrowed from a conversation.

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
the same outbox item concurrently. An unmarked human comment from the owner's
account is still a follow-up. A marker copied by another author is still human
input. GitHub does not offer a comment idempotency key, so reconciliation is
best-effort if its read API has not yet exposed an accepted write.

## Install

1. Clone the control repository and each plugin repository. Read their `AGENTS.md`.
2. Register each checkout as a T3 project. Set its default model/provider, and
   leave the normal checkout on `main`. The router asks T3 to create the issue
   worktree from current `origin/main` before starting the thread.
3. Copy `operator.example.json` to a private directory outside Git, fill the
   absolute checkout/state paths and T3 project IDs, and restrict it to `0600`.
   Every configured repository must also be in `repositories.json`.
4. Run the checks below, then install a user service using
   `deploy/planetry-issues.service.example`. Substitute the absolute paths. The
   service user must already have working `gh auth status` and access to the
   local T3 instance. Enable user lingering if the service must survive logout.

```sh
python3 -m planetry --config /private/operator.json check
python3 -m planetry --config /private/operator.json once
python3 -m planetry --config /private/operator.json status
systemctl --user enable --now planetry-issues.service
```

The first scan includes existing open human issues. Older closed issues are
observed but do not launch work until reopened. All configured repositories are
explicit; unregistered repositories cannot request a thread through intake.

## Controls and monitoring

- `poll_seconds`: 60 by default, minimum 30. Empty polling consumes API requests,
  not model tokens. ETags reduce unchanged responses and API usage.
- `max_active_threads`: 1 initially. Outstanding uncertain launches reserve a
  slot as well. Keep one heavy Minecraft build active on a small VM.
- `max_new_threads_per_day`: 20 initially, per UTC day. Additional new issues
  remain queued. This is a launch limit, not a total model-token budget; comments
  in an existing thread can still consume tokens.
- `systemctl --user stop planetry-issues` pauses intake. It does not cancel an
  already running T3 conversation. Stop that conversation in T3 if required.
- `systemctl --user status planetry-issues` and `journalctl --user -u
  planetry-issues` show service health. The CLI `status` command shows the last
  successful poll, queue and private thread mappings. Do not post its output to
  public issues.

The service backs off on transport/rate-limit failures. Events remain in SQLite.
A provider run that fails/interruption is reported in the issue; a new human
reply resumes the same thread. An agent needing clarification posts its question
and ends the turn, releasing the development slot.

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

## Adding a plugin

Plugins created to fulfill registered PlanetRy issues use the automatic
[`onboard` helper](NEW_PLUGIN.md). It validates the source issue, canonical repo
and checkout, published main, successful CI, and release assets. It registers or
reuses a T3 project, publishes the shared registry/context in a dedicated control
worktree, fast-forwards the clean control checkout, then atomically appends the
private configuration. All subsequent issue prompts load the live registry.

The service reloads validated repository additions before every poll, preserving
existing thread mappings and queue state. No restart or copied webhook secret
is needed. Invalid configuration leaves the last known good registry active;
changes to runtime paths or the T3 endpoint still require an operator restart.

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

The router restricts which repositories enter the queue. Agents are instructed
to accept PlanetRy development requests while refusing issue text that tries to
change their policy, expose secrets, access unrelated projects or deploy to the
live Minecraft network. Their responses must use the marked reply helper.

These are task-scope instructions, **not an OS security sandbox**. This deployment
runs agents under the existing development account with its GitHub access. A
public issue can consume that account's model allowance and lead to a tested
commit on main. For stronger credential and filesystem separation, move this
service and T3 into a dedicated PlanetRy VM/account and use a GitHub App or
credentials restricted to the registered repositories. The any-author policy is
intentional; changing it requires the owner's instruction.

Agent changes to plugin code follow main CI and the existing release pipeline.
Changes to this service require its own tests and an operator-controlled service
restart. New plugin registration through the helper is authorized; an issue still
cannot rewrite its own permissions, authentication or other operational policy.
