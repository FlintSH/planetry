# Planetry

Shared context and development automation for the Planetry Minecraft network.

**Open an issue from the authorized GitHub account (`thebeeee`, ID `89701169`).
The development agent replies there, works on the request, tests changes and
publishes them. You do not need T3 Code or access to the VM.**

- [Chat, join/leave messages and teleport requests](https://github.com/FlintSH/velocity-network-chat/issues/new/choose)
- [Inventory sync and MariaDB](https://github.com/FlintSH/network-inventory-sync/issues/new/choose)
- [General Planetry questions and requests](https://github.com/FlintSH/planetry/issues/new/choose)

Describe what happened, what you wanted to happen and which servers were
involved. Screenshots and redacted errors help. Never post passwords, tokens or
database connection strings. Reply to the agent's questions in the same issue.
Reopen a resolved issue if you need another code change.

## How it works

1. A small service checks the registered GitHub repositories every 60 seconds.
   Empty checks do not run an AI model.
2. A new issue from the authorized account starts its own T3 conversation and
   isolated repository worktree. The agent reads the
   [whole system context](docs/SYSTEM.md) and current code.
3. For an authorized issue, comments from the allowed account, edits and reopen
   events return to that same conversation. The agent asks questions and posts
   progress in the GitHub issue.
4. Relevant changes are tested, committed and pushed to `main`. The agent checks
   the existing release pipeline and replies with the result and download link.

Only GitHub account ID **89701169**, currently displayed as `thebeeee`, may trigger
issue automation. The mandatory global `allowed_github_user` policy matches the
immutable ID; its login is cosmetic. Renaming that account keeps access, while a
copied login with another ID is denied. Missing or malformed policy fails closed.
Only that account's issues and comments are accepted. Other comments are ignored
and excluded from the context helper; denied issues receive no automated replies.
`FlintSH`, repository owners and collaborators have no implicit bypass. No
approval label or special mention is needed for authorized requests. Bot-created
issues and pull requests do not trigger this service. Agent comments are
identified as Planetry automation, even though the current installation posts
through Flint's GitHub account.

The configured limits are three active issue threads and 20 new threads per UTC
day. Each issue has its own worktree. Agents share a private work board and claim
files to coordinate overlapping changes. Heavy builds run one at a time, and
publishing is serialized per repository. Additional work stays queued; these
limits are configurable. Authorized follow-up comments reuse their existing
thread. Closing an authorized issue stops queued work and tells an active agent
to stop; it cannot undo a push that already happened. Trusted internal agent
coordination remains available, and existing authorized threads are preserved.

Publishing a release does **not** install it on the Minecraft network. The
network operator controls production servers and player data.

For the proposed `dev` → `main` workflow for shared server files and updates
on restart, see the [server configuration deployment plan](docs/SERVER_CONFIG_PLAN.md).
This is a design proposal; no server updater is installed.

## Repositories

| Repository | Responsibility |
| --- | --- |
| [velocity-network-chat](https://github.com/FlintSH/velocity-network-chat) | Velocity chat, presence, player lookup and teleport requests, plus the Paper teleport bridge |
| [network-inventory-sync](https://github.com/FlintSH/network-inventory-sync) | Paper/Purpur inventory sync through MariaDB or PostgreSQL |
| This repository | System context, issue forms, repository registry and issue routing |

The plugins remain separate repositories with their own build and release
pipelines. [`repositories.json`](repositories.json) is the shared inventory.
When an authorized issue needs a new plugin, the agent creates its repository and
release pipeline and adds it to issue intake and shared system context.
Future authorized issues in any registered repository can then work across that
plugin too. New plugins inherit the same global identity policy, with no
per-repository override. See [new plugin delivery](docs/NEW_PLUGIN.md).

## Development

Python 3.12+, GitHub CLI authenticated for the registered repositories, and a
running local T3 Code installation are required. There are no Python package
dependencies. The transport was verified against T3 Code 0.1.0's OAuth pairing
flow and MCP protocol `2025-06-18`.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q planetry
```

Runtime state and credentials belong outside Git. The private operator config
must include the global `allowed_github_user` object shown in the example, with
an actual positive integer `id` and a valid GitHub display `login`. See
[`operator.example.json`](operator.example.json), the
[operator guide](docs/OPERATIONS.md), and the
[issue agent instructions](docs/ISSUE_AGENT.md). The
[coordination guide](docs/COORDINATION.md) explains claims, handoffs and resource guards.
