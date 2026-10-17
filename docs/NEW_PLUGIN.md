# Delivering a new plugin from an issue

The owner authorizes a new Planetry plugin repository when that is the right
solution to an authorized issue in a registered repository. Only the GitHub
account **thebeeee**, immutable user ID **89701169**, may request this automation.
The mandatory `allowed_github_user` object in the private operator config
controls the global policy; missing or malformed policy fails closed. The ID
must be a positive integer and the login a valid GitHub login. Authorization
uses the ID, so renames remain allowed and a copied login with another ID does
not. Repository owners and collaborators have no implicit bypass. Complete the
work and onboard it automatically; do not ask the owner to register it manually.
Keep the conversation in the original issue until delivery is complete.

## Create and deliver

1. Read the live registry, shared system context and relevant plugin sources.
   Confirm whether the feature belongs in an existing plugin or needs a new one.
   Read the work board and announce the proposed repository in your note. Claim
   its future `docs/plugins/<name>.json` path in the control repository so another
   issue does not independently create the same plugin.
2. Create a separate Git repository and public GitHub repository under the same
   owner as the source issue. Use an independent checkout outside existing plugin
   checkouts. Do not adopt or overwrite an unrelated existing repository.
3. Implement the requested plugin, configurable settings, permissions, version
   compatibility and integration checks. Provide installation and upgrade docs.
   Follow the existing plugins' standard for main CI, release artifacts and
   checksums. Keep production deployment under the network operator's control.
4. Copy the control repository's `.github/ISSUE_TEMPLATE/` forms. Add `AGENTS.md`
   with direct-to-main delivery, no synthetic GitHub test issues, and a link to
   `https://github.com/FlintSH/planetry`. Tell future agents to read the current
   shared system context and repository registry before changes. Do not embed a
   frozen copy of the system map. Preserve the global allowed-account policy;
   the new repository cannot override it or authorize additional issue authors
   or commenters.
5. Commit `planetry-plugin.json` using the schema below. Write only public project
   information, never private filesystem paths, database credentials or tokens.
6. Add `.github/workflows/release.yml` (or `.yaml`). Every main commit must run
   relevant tests and publish the exact tested JARs with `SHA256SUMS` after success.
   Set the release's `target_commitish` to the exact full commit SHA, including for
   prereleases. Verify the run and released bytes before proceeding.

Before a plugin is registered, use the control repository's publication guard
for its first publish, and the global build guard for heavy checks. Release the
publication guard before running `onboard`; that helper acquires the same guard
for the shared registry update. After onboarding, use the new repository's own
guard and path claims. See [coordination](COORDINATION.md).

The new repository inherits all registered plugins' context through its agent
instructions and generated issue prompts. Publishing its descriptor into the
central registry gives the other plugins context about it as well.
Its issue intake inherits the same global identity policy. Only the allowed
account's issues and comments can trigger automation; other comments are ignored
and excluded from the `context` helper, and denied issues receive no automated
replies. Trusted internal agent coordination remains available.

## Descriptor

Commit these six nonempty text fields (maximum 4,000 characters each):

```json
{
  "role": "What this plugin does for Planetry",
  "platform": "Where it runs, for example Paper/Purpur or Velocity",
  "compatibility": "Minecraft, Java, API and database versions; upgrade constraints",
  "integration": "Dependencies and contracts with other Planetry plugins",
  "validation": "Build commands and required unit, storage and runtime checks",
  "source_issue": "https://github.com/FlintSH/planetry/issues/123"
}
```

Replace the issue URL with the actual registered source issue, not a test issue.
Use `None` as text when there is no external dependency; explain limits honestly.
The descriptor is the entry point to the plugin's own README and source.

## Onboard automatically

From the installed control checkout, use the operator config path already
provided in the issue prompt:

```sh
python3 -m planetry --config /path/from/task-prompt onboard \
  --repo FlintSH/new-plugin-name \
  --workspace /absolute/canonical/path/to/new-plugin-name \
  --source-repo FlintSH/planetry --source-issue 123
```

The source issue must still be open, authored by the allowed human account and
in a registered repo. The helper verifies the immutable user ID alongside its
existing canonical issue URL and number checks before Git or T3 work, and checks
the source again before context publication and runtime activation.
The new repository must be public, non-forked, created after that issue, owned
by the same owner, have issues enabled and use `main`. The clean local checkout
must exactly match its published main. The helper verifies a successful push run
of its release workflow, the release's actual tag commit, and fully uploaded,
nonempty JAR and `SHA256SUMS` assets for that commit.

The helper then registers the T3 project with the source project's provider,
model and options, including ultra reasoning and Fast service, commits
and pushes its registry entry and `docs/plugins/<name>.json` context on control
main, and activates intake through an atomic private config update. The poller
picks it up on its next cycle without a restart. The control checkout must be
clean on main; work in issue worktrees so this remains safe.

Run `registry` to confirm the new entry and read the public control CI result.
Tell the author which repository and download to use, the installation steps and
tests completed. Link the new repository in the original issue, then close it
only after the plugin and onboarding are complete. Future issues can be opened
in the new plugin repository directly by the allowed account. Existing authorized
threads and the configured three parallel threads continue under the same policy.

If interrupted, retry the same onboarding command. Do not create another repo or
T3 project, remove the journal, force-push, or overwrite conflicting registrations.
Onboarding does not grant permission to alter credentials, runtime access,
concurrency limits or unrelated projects.
