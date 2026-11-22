## In this environment

Radar's MCP tools (`mcp__radar__*`) are your interface to the cluster. This environment has no
`kubectl` and no cluster credentials; Bash is available for everything else.

- Investigate with Radar's read tools.
- Run a command in a container (an HTTP call, a database or queue CLI, a `/metrics` endpoint):
  `exec_pod`, in an existing pod of the namespace you're testing from.
- Change: `patch_resource`, `apply_resource`, `manage_workload` (restart, scale, roll back).
  Delete: `delete_resource` (preview, then confirm with the token it returns).
- Wait for a rollout, a Job, a Pod or a deletion: `wait_for`.
