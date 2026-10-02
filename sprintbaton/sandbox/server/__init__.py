"""The sandbox service (hosted-sandbox-isolation spec §4, §10).

Runs in its own pod, holding no deployment secret — only the worker<->sandbox
shared token. It manages sessions (one per task: work trees, the sandbox-side
git copy, scratch/state dirs), launches runs as per-run bubblewrap sandboxes,
relays each run's egress to the worker's broker, and moves files through an
explicit transfer API: no volume is ever shared with the worker (invariant 2).

Import rule (spec §10.1, enforced by a test): modules in this package import
the standard library and `sprintbaton.sandbox.base` — nothing else from
`sprintbaton`. The sandbox image therefore carries no orchestrator, resolver,
or credential code.
"""
