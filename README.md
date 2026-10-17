# PlanetRy

Shared context and development automation for the PlanetRy Minecraft network.

**Open an issue. The development agent replies there, works on the request,
tests changes and publishes them. You do not need T3 Code or access to the VM.**

- [Chat, join/leave messages and teleport requests](https://github.com/FlintSH/velocity-network-chat/issues/new/choose)
- [Inventory sync and MariaDB](https://github.com/FlintSH/network-inventory-sync/issues/new/choose)
- [General PlanetRy questions and requests](https://github.com/FlintSH/planetry/issues/new/choose)

Describe what happened, what you wanted to happen and which servers were
involved. Screenshots and redacted errors help. Never post passwords, tokens or
database connection strings. Reply to the agent's questions in the same issue.
Reopen a resolved issue if you need another code change.

## How it works

1. A small service checks the registered GitHub repositories every 60 seconds.
   Empty checks do not run an AI model.
2. A new issue starts its own T3 conversation and isolated repository worktree.
   The agent reads the [whole system context](docs/SYSTEM.md) and current code.
3. Comments, edits and reopened issues return to that same conversation. The
   agent asks questions and posts progress directly in the GitHub issue.
4. Relevant changes are tested, committed and pushed to `main`. The agent checks
   the existing release pipeline and replies with the result and download link.

The owner has enabled requests from **all human issue authors**, not just
collaborators. No approval label or special mention is needed. Bot-created issues
and pull requests do not trigger this service. Agent comments are identified as
PlanetRy automation, even though the current installation posts through Flint's
GitHub account.

The initial limits are one active issue thread and 20 new threads per UTC day.
Additional work stays queued; these limits are configurable. Follow-up comments
reuse their existing thread. A long build can delay another issue. Closing an
issue stops queued work and tells an active agent to stop; it cannot undo a push
that already happened.

Publishing a release does **not** install it on the Minecraft network. The
network operator controls production servers and player data.

## Repositories

| Repository | Responsibility |
| --- | --- |
| [velocity-network-chat](https://github.com/FlintSH/velocity-network-chat) | Velocity chat, presence, player lookup and teleport requests, plus the Paper teleport bridge |
| [network-inventory-sync](https://github.com/FlintSH/network-inventory-sync) | Paper/Purpur inventory sync through MariaDB or PostgreSQL |
| This repository | System context, issue forms, repository registry and issue routing |

The plugins remain separate repositories with their own build and release
pipelines. [`repositories.json`](repositories.json) is the shared inventory.
Adding a future plugin does not require rebuilding the router; register its
repository and T3 project as described in the [operator guide](docs/OPERATIONS.md).

## Development

Python 3.12+, GitHub CLI authenticated for the registered repositories, and a
running local T3 Code installation are required. There are no Python package
dependencies. The transport was verified against T3 Code 0.1.0's OAuth pairing
flow and MCP protocol `2025-06-18`.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q planetry
```

Runtime state and credentials belong outside Git. See
[`operator.example.json`](operator.example.json), the
[operator guide](docs/OPERATIONS.md), and the
[issue agent instructions](docs/ISSUE_AGENT.md).
