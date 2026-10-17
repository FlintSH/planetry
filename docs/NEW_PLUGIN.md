# Delivering a new plugin from an issue

The owner authorizes a new Planetry plugin repository when that is the right
solution to a registered issue. Complete the work and onboard it automatically;
do not ask the owner to register it manually. Keep the conversation in the
original issue until delivery is complete.

## Create and deliver

1. Read the live registry, shared system context and relevant plugin sources.
   Confirm whether the feature belongs in an existing plugin or needs a new one.
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
   frozen copy of the system map.
5. Commit `planetry-plugin.json` using the schema below. Write only public project
   information, never private filesystem paths, database credentials or tokens.
6. Add `.github/workflows/release.yml` (or `.yaml`). Every main commit must run
   relevant tests and publish the exact tested JARs with `SHA256SUMS` after success.
   Set the release's `target_commitish` to the exact full commit SHA, including for
   prereleases. Verify the run and released bytes before proceeding.

The new repository inherits all registered plugins' context through its agent
instructions and generated issue prompts. Publishing its descriptor into the
central registry gives the other plugins context about it as well.

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

The source issue must still be open, human-authored and in a registered repo.
The new repository must be public, non-forked, created after that issue, owned
by the same owner, have issues enabled and use `main`. The clean local checkout
must exactly match its published main. The helper verifies a successful push run
of its release workflow, the release's actual tag commit, and fully uploaded,
nonempty JAR and `SHA256SUMS` assets for that commit.

The helper then registers the T3 project with the source project's model, commits
and pushes its registry entry and `docs/plugins/<name>.json` context on control
main, and activates intake through an atomic private config update. The poller
picks it up on its next cycle without a restart. The control checkout must be
clean on main; work in issue worktrees so this remains safe.

Run `registry` to confirm the new entry and read the public control CI result.
Tell the author which repository and download to use, the installation steps and
tests completed. Link the new repository in the original issue, then close it
only after the plugin and onboarding are complete. Future issues can be opened
in the new plugin repository directly.

If interrupted, retry the same onboarding command. Do not create another repo or
T3 project, remove the journal, force-push, or overwrite conflicting registrations.
Onboarding does not grant permission to alter credentials, runtime access,
concurrency limits or unrelated projects.
