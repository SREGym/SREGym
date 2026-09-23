# Local validation record

Executed on the supplied CloudLab node, 2026-09-22. This is prototype validation,
not a published benchmark or a statistical estimate of model reliability.

## Reference recoveries

Each run passed a healthy baseline, received the incident, and used the reference
recovery. Final grading transferred actual leadership through all three voters.

| Local artifact run | Tier / mode | Seed | Final successful joins | Failed / incorrect | Result |
| --- | --- | ---: | ---: | --- | --- |
| `roblox-check` | small / historical | 0 | 300 | 0 / 0 | Pass |
| `roblox-scaled` | scaled / historical | 7 | 900 | 0 / 0 | Pass |
| `roblox-intervention` | small / intervention | 42 | 280 | 0 / 0 | Pass |

All three preserved the complete player table and incurred zero origin overloads.
These counts describe each final observation window, not all traffic in the run.

## Agent smoke evaluation

`roblox-codex` used the installed Codex CLI **0.155.1**, which selected
**gpt-6-astra**. The agent finished in approximately **302 seconds**. Its independent
final grade passed all seven predicates: 300 successful joins, zero failed or
incorrect joins, preserved player data, zero origin overloads, full admission,
a sufficient sustained window, and a three-voter cluster exercised across leaders.

One earlier startup attempt lacked the CLI's code-mode companion. That harness
failure is retained separately and excluded from the successful trial. The
launcher now mounts the companion when available. Native session metadata,
usage, commands, and the actual briefing are retained alongside the grade.

The trial used the initial operational briefing. Additional observational
alerts/tickets/chat/runbook commands were subsequently smoke-tested on
`roblox-demo`. Run a fresh, version-frozen campaign before drawing comparisons
between models or tiers. No difficulty or reliability claim follows from this
single successful agent trial.

## Three-trial scaled Codex campaign

Three fresh serial scaled/historical episodes used seed 7, gpt-6-astra, identical
source hashes, and a 600-second agent budget. All three grades were valid and
passed all seven predicates, with no timeouts.

| Trial | Agent elapsed | Final successful / failed joins | Failed joins during agent response |
| --- | --- | --- | --- |
| 1 | 326s | 900 / 0 | 0 |
| 2 | 403s | 840 / 0 | 2 |
| 3 | 288s | 839 / 1 | 0 |

There were no incorrect responses, player-data changes, or origin overloads.
The grader permits up to 2% failed joins, so trial 3's final transient failure
passes. Central modeled repairs were issued in 34–49 seconds. This exposes a
substantial difficulty and fidelity gap; the prototype does not meet the
ultra-long-horizon objective. These repeated fixed-seed trials are not a scaling
study or a general reliability estimate.

The [full report](../../../results/roblox-consul/codex-three-trials/REPORT.md)
contains timing definitions, source verification, and links to all traces.
The [gap assessment](../../../docs/roblox-long-horizon-gap.md) describes the
required replacement architecture. All campaign stacks were cleaned up.

## Automated checks

**28 fast tests passed. All 3 Docker integration scenarios passed** in approximately
441 seconds. The scenarios cover small/historical, small/intervention, and
scaled/historical with independent fresh volumes. Ruff checks and formatting
checks passed.

## Negative checks

The Docker tests deliberately verify rejection of:

- Maintenance-only operation and untreated incident state.
- Snapshot restoration without fixing the control-plane defects.
- Persistent player-data corruption hidden behind correct cached responses.
- A cold-cache reconnect surge, even after availability is restored.
- Erasing that surge's safety violation by restarting the gateway.

Fast tests also cover stopped/insufficient traffic, too-short observation,
leader-specific recurrence, unknown configuration fields, run-path traversal,
and unavailable grading evidence. Toolbox checks confirmed absence of backend
DNS access, controller source, and the Docker socket. Unauthenticated access to
runner evidence returned HTTP 403.

Full artifacts remain under `results/roblox-consul/` on this node and are ignored
by Git. `roblox-demo` is left running with a freshly injected historical incident;
the other completed demonstration environments are torn down after export.
