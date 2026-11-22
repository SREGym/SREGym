# Claude Code + Radar MCP

SREGym client for Claude Code with [Radar](https://github.com/skyhook-io/radar) as its only path to the
cluster. Radar is an open-source Kubernetes tool: it keeps a live model of the cluster (resources,
ownership and topology, recent changes, logs, events, detected issues) and exposes it as MCP tools,
including tools that change resources and run commands in containers.

The client is a thin layer over [`clients/claudecode`](../claudecode). It reuses that driver (conductor
handshake, submission, results) and swaps in an agent class that starts the same Claude Code CLI with
Radar connected. Anything not listed under
[Differences from `claudecode`](#differences-from-claudecode) is identical.

## Differences from `claudecode`

Every departure from the stock client, so a result can be read against the `claudecode` entries:

| | `claudecode` | `claudecode_radar` |
|---|---|---|
| Cluster access | kubeconfig, `kubectl`, egress to the API proxy and SREGym's MCP server | None of these. The registration sets `cluster_access: false`, and the client deletes `kubectl` from the container at start. Radar's MCP tools are the only route to the cluster |
| Task instruction | SREGym's | SREGym's, with one line changed: "You have access to kubectl commands to inspect and modify resources in namespace(s): …" names Radar's MCP tools (`mcp__radar__*`) instead, since the container has no kubectl |
| MCP servers | none | Radar, `--mcp-config` + `--strict-mcp-config` (no other servers) |
| System prompt | CLI default | CLI default + `--append-system-prompt` with [`routing.md`](routing.md): this environment has no kubectl, and which Radar tool covers exec, changes, deletes and waits. How to use Radar's tools comes from Radar itself, as its MCP server `instructions` |
| Allowed tools | stock list | stock list + `mcp__radar`, `ToolSearch` (Radar's tools are deferred), `Monitor`, `Task*`, MCP resource readers |
| Permission mode | CLI default | `--permission-mode dontAsk`: exactly the allowlist runs, no prompts and no auto-mode classifier |
| Preflight | model call | Radar's MCP `initialize` from inside the container, then the stock model call |

Notes:

- **Prompt content.** `routing.md` and the changed tool line are the only client-added text. Neither
  names an SREGym application, problem, fault type, oracle or rubric (a test checks `routing.md`).
- **Why `dontAsk`.** On some accounts the CLI starts in auto mode, where a permission classifier can
  refuse a fix (it denied deleting a rogue mutating webhook as "modifying shared resources"). `dontAsk`
  runs the allowlist the stock client already passes, plus the tools above.

## Cluster access (`cluster_access: false`)

A registration field, default `true` (no change for other agents). It needs filtered internet access
(the default), because the filtered egress boundary is what withholds the routes below; with
`--internet-access open` the run refuses to start. With `false`:

- the agent container gets no kubeconfig mount and no `KUBECONFIG`;
- the container's egress proxy gets no rule for the Kubernetes API proxy or for SREGym's MCP server
  (whose kubectl tools would be a second route to the cluster); the conductor API and the model
  provider stay reachable.

The client refuses to start if a kubeconfig is still present, so a registration without the field fails
loudly instead of handing the agent a working kubeconfig.

## Architecture

Radar runs on the host, not in the agent container. It reads the cluster through a long-lived
instance of SREGym's own filtering proxy ([`radar_view_proxy.py`](radar_view_proxy.py)), so it sees the
same cluster view as an agent's `kubectl` would: hidden namespaces and labels, Chaos Mesh objects and
filtered-mode write restrictions all apply.

```
agent container                        host
  claude ──MCP/HTTP──▶ RADAR_MCP_URL ──▶ radar ──▶ radar_view_proxy ──▶ API server
```

## Prerequisites

1. **Radar** (a release binary from [skyhook-io/radar](https://github.com/skyhook-io/radar/releases)).
   Record its version with each result; the preflight prints it.
2. **The view proxy**, started from the repository root with the benchmark cluster's kubeconfig:

   ```bash
   uv run python -m clients.claudecode_radar.radar_view_proxy --kubeconfig-out /tmp/radar-view.kubeconfig
   ```

3. **A fresh Radar per problem**, with its exec and run tools enabled. Radar remembers deleted objects
   for 15 minutes and diffs a re-created object against them. SREGym redeploys the application for
   every problem, so a Radar that outlives a problem shows the next one "recreated with changes"
   against the previous problem's objects, history a `kubectl` agent does not have. Restart Radar with
   a new, empty timeline database before each problem:

   ```bash
   RADAR_MCP_DISABLE_LOCALHOST_PROTECTION=1 kubectl-radar \
     --kubeconfig /tmp/radar-view.kubeconfig --no-browser --mcp-exec-tools \
     --port 9283 --listen-address <address reachable from the container> \
     --timeline-db "$(mktemp -d)/timeline.db"
   ```

   Wait for the MCP endpoint to answer and about 20 s for Radar's caches to sync before starting the
   problem. `--mcp-exec-tools` enables `exec_pod`, which the agent needs for in-container checks.
   `RADAR_MCP_DISABLE_LOCALHOST_PROTECTION=1` lets the container connect with
   `Host: host.docker.internal` (MCP's DNS-rebinding check otherwise answers 403).
4. **Endpoint access in filtered mode.** Allow the container to reach Radar:
   `--allow-agent-endpoint http://host.docker.internal:9283`.

## Running

```bash
uv run main.py --suite sregym-lite --agent claudecode_radar \
  --model anthropic/claude-opus-5-5 --reasoning-effort medium \
  --allow-agent-endpoint http://host.docker.internal:9283 --force-build
```

The agent image carries the client code, so the first run (and any run after a change under
`clients/`) needs `--force-build`; later runs can drop it. Credentials are
the stock client's: `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `RADAR_MCP_URL` | `http://host.docker.internal:9283/mcp` | Radar's MCP endpoint as the container sees it. Set it in the `claudecode_radar` entry's `kickoff_env` in `agents.yaml`. |

Everything else (`AGENT_MODEL_ID`, `AGENT_REASONING_EFFORT`, `MAX_THINKING_TOKENS`, credentials) is read
exactly as by `clients/claudecode`.

## Artifacts

The stock client's artifacts (`claude-code.txt` stream, `sessions/`, results JSON, ATIF
`trajectory.json`; `sregym/traces` reads this client's output as `claudecode`), plus
`radar-mcp-config.json`, the MCP configuration passed to the CLI. Radar's tool calls and responses are
in the transcript like any other tool call.
