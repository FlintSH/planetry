# GitHub-managed server files: proposed plan

For [issue #6](https://github.com/FlintSH/planetry/issues/6), use one private
configuration repository and a small updater that runs **before the Minecraft
process starts**. Edit shared files on `dev`; merge into `main` when ready.
Once checks pass, each server installs its applicable files on its next start.
Running servers keep their current files until then.

This document is the plan. The updater, configuration repository and startup
integration have not been built or installed. The existing Planetry issue
automation continues to publish plugin releases without deploying them.

## Everyday workflow

1. Edit a shared plugin configuration, menu, message or other managed file on
   `dev` using GitHub's editor or a local checkout. A development server can
   consume a separate tested `dev` channel; production never follows it.
2. Check the change on development, then merge `dev` into `main`.
3. GitHub Actions validates and packages that exact commit. A failed check
   leaves the previous deployment version selected.
4. Restart a server normally. Its startup wrapper checks the selected version,
   installs the files for that server while Minecraft is stopped, then starts
   Java. A restart before checks finish uses the previous selected version.
5. Check the startup result: server name, configuration commit, plugin versions
   and whether anything changed. No extra restart or in-game reload is needed.

A main merge makes an update available; it does not restart any server.
Servers restarted at different times can run different versions. Changes that
cannot tolerate that difference need the coordinated rollout described below.

## Repository and file ownership

Keep the configuration repository private. Make the shared settings the normal
editing surface, with explicit overrides for server roles and individual
servers. An example layout is:

```text
shared/backend/plugins/PlanetryCore/language.yml
shared/backend/plugins/ExampleMenus/menus/main.yml
shared/proxy/plugins/networkchat/language.properties
roles/survival/server.properties
servers/survival-1/settings.yml
servers/survival-2/settings.yml
deploy/managed-files.yml
deploy/plugin-versions.yml
```

These are proposed paths in the new repository. Only files supported by each
plugin's current version would be managed. `shared/backend` and `shared/proxy`
are separate targets. Apply a server's shared files, then its role overrides,
then its individual overrides, using a fixed, validated order. File overrides
replace the whole file; no implicit YAML or Properties merge.

`managed-files.yml` explicitly maps each source to a relative destination and
the servers receiving it. A folder such as a menu definition directory can be
enrolled after checking its contents. Do not mirror the whole `plugins` folder
or the whole server root. Reject duplicate destinations, unknown server names,
absolute paths, traversal, links and destinations outside the approved roots.
The operator also sets a local destination allowlist; a bundle cannot expand
it or manage the startup wrapper, credentials or updater state.

Each destination has one owner. Changes made directly to a managed file are
reported as drift; normal startup refuses to overwrite them until the operator
has saved them into Git or deliberately discarded them. Unmanaged files stay
untouched. Plugins that rewrite their own configurations need a tested
normalization rule or must remain unmanaged in the first version.
An unmanaged destination collision also stops the update. Initial enrollment
uses a reviewed dry run and backup to adopt existing files as a known baseline.

Deletion is explicit: deleting a source file alone fails validation. A removal
entry may delete only a previously managed destination whose content still
matches its recorded version. Back it up first. Removing ownership without
deleting the file is a separate operation. No recursive cleanup or broad
`--delete` synchronization.

## Secrets and persistent data

Keep database credentials, forwarding/teleport shared secrets, GitHub access
credentials and plugin licence keys outside Git and release bundles, even in a
private repository. Each server has a local settings/secret file that the
updater reads without printing its contents. Git templates contain only
explicit placeholders; missing values refuse startup. Render using the target
file format's escaping rules, then validate the final file. Rendered files and
their backups need restricted access too.

This also handles mixed files: for example, a managed Inventory Sync template
can hold shared options while its unique `server-id` and database credentials
come from local settings. Core also needs a unique backend ID. For a
server-specific file without secrets, such as `server.properties`, a Git
override is sufficient. CI validates templates;
only the server can validate its final rendered configuration.

Never enroll worlds, player inventories, UUID/player caches, permission or
economy databases, SQLite files, recovery journals, mail/progress data, logs or
backups as configuration. In particular, preserve Inventory Sync's recovery
journals and Core's local persistent storage. Database table prefixes, sync
groups, storage engines and field profiles are migration-sensitive settings;
an ordinary config merge must reject changes to them until a separate offline
migration has been planned. Configuration rollback does not roll back player
data or database schemas.

## Plugin JARs and tested bundles

Keep plugin source in its existing repository. `plugin-versions.yml` pins an
exact tested release, asset name and SHA-256 for each managed JAR. Reuse the
existing Planetry plugin releases and their `SHA256SUMS`; do not compile Java
on game servers or download whichever release happens to be newest at boot.
Updating a plugin means changing that pin on `dev`, testing, then merging.
Licensed third-party JARs need an approved private source and distribution
rights; they are not automatically copied into GitHub.

Velocity receives the Network Chat proxy JAR. Paper/Purpur backends receive
the Network Chat bridge, Inventory Sync and Core JARs where configured.
Validate each server's platform, Minecraft/Java requirements and dependency
versions before publication and before startup. The
[system context](SYSTEM.md) and plugin READMEs define those requirements.
Upgrading the server engine or Java itself is a separate operation.

After all checks pass, publish a versioned bundle containing the rendered
non-secret inputs, pinned JARs where redistribution is allowed, and a manifest
of source commit, target servers, destinations, compatibility requirements,
file sizes and hashes. Publish the exact tested bytes. Use durable versioned
release assets rather than depending on expiring Actions artifacts.
[GitHub documents artifact expiry here.](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)

Treat bundle IDs as immutable and reject changed bytes for an existing ID.
The selected `main` channel points to one verified bundle, not a moving Git
checkout. Serialize channel publication and check commit ancestry under that
guard so a slow older workflow cannot replace a newer selection. GitHub's
[concurrency controls](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
alone do not guarantee execution in commit order.

The server has read-only access to that dedicated configuration repository and
approved asset sources. Use repository-scoped credentials provisioned locally
by the operator; never give a game server write access or reuse the development
agent's credentials. GitHub supports
[read-only, repository-specific deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
for Git access; release downloads need an appropriately scoped supported
authentication method too. HTTPS/authenticated source selection establishes
trust; hashes check file integrity, not who authored a bundle.

## Startup and recovery contract

Run the updater outside Minecraft, under the server's process supervisor or
hosting panel. A Minecraft plugin loads too late to safely replace every other
plugin and its configuration. On systemd, a failing
[`ExecStartPre`](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#ExecStartPre=)
can prevent Java from starting. A panel needs an equivalent supported startup
wrapper; an installation script that only runs when creating a server is
insufficient. The wrapper must preserve console input, stop signals and Java's
exit status. Verify the actual host's hook before choosing that adapter.

The updater must perform this sequence:

1. Acquire a per-server lock held through the Minecraft process lifetime, so
   another start cannot modify a running server. All start/restart routes must
   use that wrapper. Confirm the previous Java process has stopped.
2. Read the selected bundle ID once. Download into staging outside live
   destinations, with timeouts, size limits and bounded retries. Verify the
   trusted manifest, hashes, archive paths, disk space and compatibility;
   reject unsafe archive members before extraction. Never run bundle scripts.
3. Build the complete server-specific file set, render local settings and
   secrets, validate it, and check for drift. An unchanged verified bundle is
   a no-op. Refuse symlink destinations or parent directories too.
4. Save a versioned backup of every managed file being replaced or deleted,
   including whether a destination was originally absent. Write a durable
   installation journal before changing live paths.
5. Install using staged files and per-file atomic replacement on the same
   filesystem. Remove a superseded managed JAR explicitly, preventing two
   versions from loading. Several replacements are not one atomic transaction:
   keep Java stopped until every operation and the final verification pass.
6. Record the installed bundle and retain the journal until the installation
   is durably complete. On interruption, restore the full previous managed set
   before permitting boot. An uncertain or failed restore blocks startup.
7. Start Java and record a separate healthy version only after checking server
   readiness, enabled required plugins and their storage connections; a
   listening port alone is insufficient. Player-specific inventory readiness
   remains the plugins' normal session check. A failed candidate is marked
   and not repeatedly installed by automatic restart loops.

The default on download, validation or installation failure is to leave the
previous files intact (or restore them) and refuse startup with a redacted
error. An operator can explicitly pin and start a previously verified local
bundle during a GitHub outage. First installation has no such fallback.
Do not silently start a partial or incompatible installation.

For rollback, pin the previous bundle so the next restart does not immediately
reinstall the bad one, then restore its managed files while stopped. Do not
automatically launch older plugins after a candidate has run: it may have
changed persistent data. Check downgrade compatibility and handle any data
recovery separately. Keep at least the current and previous healthy bundles
and backups; set a disk budget and refuse updates if safe backups cannot fit.

## Rollout and implementation work

Ordinary message/menu edits can roll out on individual restarts. Changes to
Inventory Sync compatibility, shared database settings, proxy/backend secrets
or paired plugin protocols need a maintenance window. Pin one bundle for that
cohort, drain players, stop all affected processes and prevent joining or
transfers until the full cohort reports the expected version and is healthy.
The updater does not take over crashed inventory ownership or run recovery
commands. Those remain explicit operator procedures.

Build this in four stages:

1. Inventory the desired files on a non-production copy. Classify each as
   managed configuration, local secret, persistent data or excluded file.
   Confirm the hosting panel/process supervisor, restart entry points, server
   IDs/roles and available runtime. These facts are needed before implementing
   the startup adapter; no credentials need to be posted in an issue.
2. Create the private configuration repository, manifest format and CI
   validator. Add template/format checks, secret scanning, compatibility checks
   and deterministic bundle publication. Limit who can merge production
   configuration and protect the publication workflow.
3. Implement the updater with `validate`, `plan`, `apply`, `status` and
   `rollback` operations, a durable recovery journal and a supervisor adapter.
   A dry-run plan must show changed/deleted destinations and versions without
   exposing secret values. Use a read-only download identity and no arbitrary
   repository-provided shell commands.
4. Test on staging, then let the network operator enroll production servers
   and startup hooks. Automatic deployments begin only after that separate
   setup. Building or releasing this tool does not authorize live installation.

Acceptance tests must demonstrate: `dev` isolation; successful main publication;
failed and out-of-order CI; role and server overrides; secret rendering;
unchanged restart; drift and explicit deletion; wrong-platform JAR rejection;
hash failures; unsafe paths/archives; interrupted download; disk exhaustion;
concurrent starts; interruption after each install operation; full restoration
including previously absent files; GitHub outage/offline pinning; failed plugin
startup; downgrade refusal; and a multi-server rollout without mixed incompatible
versions. Verify the actual panel restart route as well as the test wrapper.

The result should be one place to edit shared server files, a clear tested
version to deploy, and updates that take effect at the next controlled start
without copying player data or secrets between servers.
