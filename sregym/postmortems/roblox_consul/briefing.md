You are on call for a small gaming platform. A service-discovery incident has
made the platform unavailable. External traffic is now on the maintenance page.
Restore correct player joins and full admission without losing player data or
overloading the origin. Recovery must remain stable across Consul leader changes.

You have a shell with the `ops` command. Start with `ops help`. Arguments are JSON:
`ops profile '{"node":"consul-1"}'`. You can also use curl against
http://control:8080/ops and http://gateway:8080. There is no host Docker access.

The platform uses three Consul voters, two Redis cache workers, and PostgreSQL.
Internal routing clients continue using Consul when player traffic is blocked.
A small scheduler keeps cache allocations in Consul KV. Worker advertisements
can be stale. Cache deployment generation is independent of restored KV state.
The secrets and telemetry paths depend on Consul; local profiles and logs remain
available when aggregate monitoring fails. Profiles identify their modeled origin.

Operational notes:
- `config` reads settings; `config` with `changes` updates only those settings.
- `raft`, `profile`, `transfer`, and `compact` support Consul maintenance.
  Compaction is a laboratory maintenance model and requires a follower.
- `workers`, `drain`, `kv-get`, and `reconcile` support cache deployment.
- `snapshot-save`/`snapshot-restore` operate on actual Consul snapshots. Restore
  replaces logical cluster state; verify the consequences before reopening.
- `cache-warm` accepts `offset` and `batch` (1..20). It uses the same origin read
  budget as players. Inspect gateway metrics and limit concurrent warming.
- `admission_percent` is the percentage of player-ID cohorts allowed in, similar
  to DNS steering. Check actual joins, cache behavior, and origin errors as you
  increase admission. Traffic arrives continuously, including after you finish.
- `alerts`, `tickets`, `chat`, and `runbook` expose local simulated response surfaces.
- `status-update` records a response update in the local incident room. `expert` offers bounded consultation
  on `consul`, `scheduler`, or `cache` (six calls per incident).

Useful example: `curl -s http://gateway:8080/metrics`. The process health endpoint
only says the process is running; it does not establish user-facing recovery.
