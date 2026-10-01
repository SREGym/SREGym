# Incident Arena task contracts (vendored)

One directory per task of [abundant-ai/incident-arena](https://github.com/abundant-ai/incident-arena)
(commit `fba011e451653c4058c2dc80a97c5d260c036872`, Apache-2.0; see `LICENSE-incident-arena`).
Incident Arena calls task 005 `07-writes-and-queue-oom-f1db8f42`. It is stored here as
`005--frappe--07-writes-and-queue-oom-f1db8f42` so that the directories sort in task order.

Each directory keeps these files from the Harbor task, unmodified:

| File | Harbor path | Used by SREGym for |
|---|---|---|
| `instruction.md` | `instruction.md` | the incident ticket in the agent prompt (minus the Harbor hand-back paragraph) |
| `task.toml` | `task.toml` | the soak window (`metadata.soak_s`) |
| `task.values.yaml` | `environment/task.values.yaml` | the load profile, pinned images, and per-task sizing |
| `ground-truth.yaml` | `environment/chart/ground-truth.yaml` | outcome bands (`thresholds`) and the answer key for the diagnosis judge |
| `solve.sh` | `solution/solve.sh` | reference only: the operational repair each problem's `recover_fault` mirrors |

These files are never rendered into a cluster.
