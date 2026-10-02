"""The sandbox seam (docs/hosted-sandbox-isolation-spec.md §5).

Everything a model or a repository drives — every tool call, test or build
script, and in hosted mode the `claude` binary itself — goes through a
`SandboxSession`. Tool mode maps a session onto the existing clone directory
(`local.LocalPassthroughSandbox`, byte-for-byte today's behavior); hosted mode
sends it to a separate sandbox pod (`remote.RemoteSandbox`).

Deliberately imports nothing: `sandbox/server/` runs in an image that carries
none of the orchestrator, and may import only `sandbox.base` (spec §10.1).
"""
