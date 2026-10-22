# Planetry system context

Planetry is a Minecraft network being developed by Flint's friend. She uses
GitHub issues to describe problems and request features in ordinary language.
Flint owns the repositories and the development environment. The issue agent is
authorized to investigate, implement, test, commit to `main`, push and follow the
release pipeline without asking Flint to relay messages or approve each fix.

This document is a map of the system, not a replacement for each repository's
current source, `AGENTS.md`, configuration defaults and release workflow. Read
those files before changing behavior. Discover the current versions and release
status from GitHub; do not assume an example version here is the latest.

## Repositories and deployment boundaries

| Repository | Purpose | Installed on |
| --- | --- | --- |
| [velocity-network-chat](https://github.com/FlintSH/velocity-network-chat) | PlanetryVelocity: network chat, player lookup, teleports and PlanetryItems grants with PlanetryVelocity-Backend | Main JAR on Velocity; companion JAR on participating Paper backends |
| [network-inventory-sync](https://github.com/FlintSH/network-inventory-sync) | Shared player inventories, database ownership, snapshots and recovery | Every participating Paper/Purpur backend |
| [planetry](https://github.com/FlintSH/planetry) | Shared context, repository registry and GitHub issue automation | Development environment; never the Minecraft server |

An issue can require coordinated changes to several registered plugins or a new
Planetry plugin in its own public repository. The owner authorizes the agent to
create, test, publish and automatically onboard that new plugin. Follow
[new plugin delivery](NEW_PLUGIN.md). The helper registers its T3 project and
issue intake and publishes its system context. Public issue text cannot adopt
unrelated existing repositories or change access credentials or execution policy.

Read the live `registry` command at the start of every issue turn. The table
above describes the original plugins; `repositories.json` and linked
`docs/plugins/*.json` records include plugins added later, with platform versions,
integration contracts and validation commands. New plugins receive this same
whole-system context. Do not rely on the registry snapshot in an older message.

## PlanetryVelocity

The downloads and plugin display names are **PlanetryVelocity** and **PlanetryVelocity-Backend**. The repository remains `velocity-network-chat`; the proxy retains its `networkchat` internal ID/settings path and existing permissions. See the [live plugin context](plugins/planetry-velocity.json) for current grant and compatibility contracts. Replace old JARs during upgrade. The backend copies an absent new config from the legacy NetworkChatBridge directory without overwriting either config.

- Designed for several backend servers behind one Velocity proxy. There is no
  distributed proxy directory or Redis transport. Cross-server does not mean
  cross-proxy.
- The proxy JAR targets Java 17 bytecode and the Velocity 3.1.1 API. CI covers
  Velocity 3.1.1 / Java 17, 3.4.0 / Java 21 and 4.2.0 / Java 25. A proxy may need
  a newer Java runtime than the plugin's bytecode requires.
- Ordinary modern signed chat needs a compatible SignedVelocity installation
  on the proxy and participating backends, plus explicit backend acknowledgement
  in Network Chat configuration. `/g`, one-off `/sc` and `/msg` do not rely on
  ordinary-chat capture. Do not cancel modern signed chat unsafely.
- Chat groups partition public routing. Staff chat spans groups. Private chat
  can respect groups. Permissions are checked on Velocity, not only on Paper.
  Optional PAPIProxyBridge / PlaceholderAPI integration supplies placeholders.
- Player-facing text and chat formats live in `plugins/networkchat/language.properties`.
  The first load imports legacy templates from `config.properties` and
  `teleport.properties` without rewriting those files. Language entries take
  precedence. Chat text reloads with `/networkchat reload`; `teleport.message.*`
  still requires a proxy restart. Format modes remain in the settings files.
- Join announcements wait until the backend connection is ready. Excluded auth
  servers delay the announcement until an eligible backend is reached. Hidden
  players and inherited `networkchat.hidden` permission affect visibility.
  Consult `docs/PRESENCE.md` before diagnosing missing joins/leaves.
- `/whereami` reports the player's backend. `/find <player>` requires the
  proxy permission `networkchat.find` by default.
- `/tpa` (alias `/tpask`), `/tpaccept`, `/tpdeny` and `/tpcancel` implement player
  requests. `/tp <player>` is a staff self-teleport requiring
  `networkchat.teleport.staff` by default.
- Exact location teleports require `PlanetryVelocity-Backend-*.jar` on each
  participating Paper/Purpur **26.1.2 / Java 25** backend. The bridge and proxy
  use the same private shared secret. The proxy's separate
  `plugins/networkchat/teleport.properties` and the bridge's
  `plugins/PlanetryVelocity-Backend/config.yml` require restart after changes.
- The bridge authenticates messages, limits replay and expiry, loads destination
  chunks asynchronously, and rechecks sessions before a cancellable teleport.
  Preserve these checks. A server switch may succeed even if the later location
  teleport is rejected; report that outcome accurately.

Build layout: `formatting`, `teleport-protocol`, `plugin`, plus `paper-bridge`
under the `paper` Maven profile. Read `.github/workflows/release.yml` for the
current build matrix. Relevant commands include `mvn -B -ntp clean verify` and,
with Java 25, `mvn -B -ntp -Ppaper clean verify`. The documented real-runtime
checks are `scripts/smoke.py` and `scripts/bridge-smoke.py`.

## Inventory Sync

- Targets Paper/Purpur **26.1.2 / Java 25**. It is not a proxy, Spigot, Fabric or
  Folia plugin. Servers in a sync group must use the same exact Minecraft version,
  field profile and item definitions/plugins/datapacks.
- Supports PostgreSQL 14+ and MariaDB 10.11+. PostgreSQL is the default engine;
  MariaDB uses `database.type: mariadb`, normally port 3306. Both JDBC drivers
  ship inside the plugin. Do not silently remove MariaDB support: it is a stated
  requirement for Planetry.
- Each backend needs a unique `server-id`. A sync group shares a database,
  table prefix, compatible profile and inventory authority. Defaults refuse
  login until the placeholder server ID is replaced.
- Storage/hotbar, armour, offhand, ender chest, experience and selected slot sync
  by default. Health and hunger are optional. Location, worlds, economy,
  permissions, gamemode and open containers are not inventory-sync features.
- Database row ownership, random session tokens and expected revisions fence
  writes. Snapshot, history and release happen transactionally. Ownership does
  not expire automatically; a crashed server requires explicit recovery.
- Capture/apply runs on the server thread. Database work is asynchronous. A
  player is frozen while loading or when ownership verification is stale.
  Never bypass these checks to make a teleport, login or recovery appear faster.
- Local recovery journals can preserve a failed final save. Recovery commands
  are console-only and require the owning backend to be stopped and the player
  offline. A historical inventory cannot reverse external trades, world changes
  or dropped items. Do not claim crash-proof duplication prevention.
- Changing database engine is an offline data migration. Changing one config
  value does not migrate inventories. Never use the production database for tests.

Read the README, configuration defaults, `docs/ACCEPTANCE.md` and
`.github/workflows/release.yml`. The build is `mvn -B -ntp clean verify` on Java
25. Database tests need an isolated database configured through the documented
`INVSYNC_TEST_*` variables. CI covers PostgreSQL and MariaDB, then runs
`scripts/smoke.py` against real Paper using the same packaged JAR. Storage
changes must retain transaction, fencing, rollback and compatibility coverage
for both engines. TLS or packaging changes also need their relevant checks.

## PlanetryCore

[PlanetryCore](https://github.com/FlintSH/planetry-core) provides persistent game
levels, rewards, XP display, placeholders and network mail on Paper/Purpur
26.1.2 / Java 25. Read its [live plugin context](plugins/planetry-core.json) and
README for setup and recovery. Shared mail uses Core's MariaDB/PostgreSQL database
and authenticated forwarded UUIDs across participating backends. `/mail` claims
saved gifts; `planetry.admin.sendmail` gates individual/offline and all-online
sends. Mail console commands execute on the claiming backend after a durable
claim and Inventory Sync readiness. Uncertain effects require operator review
and are never retried automatically. No proxy update is required for mail.
Player/staff text uses MiniMessage files in `lang/` with English/German defaults
and a configurable language selection. Hotbar shortcuts use only Nexo IDs
`quest_book` and `novalink` by default; unavailable items wait without a vanilla
material fallback. See the plugin README for upgrade migration and configuration.

## Integration between plugins

The teleport bridge consults `NetworkInventorySync.isReady(UUID)` immediately
before moving a player when Inventory Sync is installed. It waits for initial
inventory application and fresh ownership verification. An installed but
disabled/incompatible Inventory Sync must not be treated as absent. Bridge
configuration can also require Inventory Sync to be installed.

`isReady` is a main-thread check for the current bound online session, not a
reservation that stays valid on a later tick. Changes to this method, login
gating, plugin identity or teleport timing need tests in both repositories.
Use authenticated UUID forwarding consistently across backends.

## Delivery and communication

Do not open pull requests for routine issue work. Work in isolated checkouts,
validate the change, then publish a normal fast-forward update to `main`.
Coordinate cross-repository changes and update setup instructions when matching
plugin versions are needed. Never overwrite unrelated work or force-push main.

Both plugin repositories publish a uniquely versioned prerelease from passing
`main` CI. The release uses the exact tested artifacts and includes
`SHA256SUMS`; Inventory Sync also publishes the bundled MariaDB driver's source
JAR. Verify the workflow for the pushed commit and its released files before
telling the issue author that the download is ready.

Tests with controlled players in real server runtimes do not prove real-client
transfers or compatibility with the production plugin set. Report the tests
actually run and any remaining staging checks. Shipping a release does not
authorize installing it on the live network or touching live player data.

Reply in the GitHub issue with the result, download link, any changed setup and
clear next steps. The issue author should not need access to T3, this VM or a
programming tool to follow the conversation. See [issue-agent instructions](ISSUE_AGENT.md).

## Network item commands

PlanetryVelocity routes `/pitems give <player> <id> [amount]` and `/planetryitems give` to the online recipient's current backend, requiring `planetry.admin.items` on Velocity. Names complete across this proxy and item IDs come from the selected backend catalog. Other subcommands and backend console grants remain backend-local. Matching PlanetryVelocity-Backend and PlanetryItems versions are required for the grant API. Each backend still loads its own recursive `plugins/PlanetryItems/items/` definitions and language; proxy distribution is not provided.

Items use the existing teleport shared secret/server allowlist independently of teleport enablement, plus restart-only `items.properties` and backend `items-enabled`. Authenticated short-lived packets are durably reserved before mutation, and Inventory Sync readiness is rechecked. Full inventories reject atomically. Missing acknowledgements mean unconfirmed delivery; no automatic retry occurs. Preserve the replay journal, inspect the recipient inventory before manual retries, and validate real clients/two-backend transport on staging.
