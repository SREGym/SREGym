# OpenSRE agent client

Runs the SREGym benchmark with **OpenSRE as the diagnosis planner** and any
existing SREGym coding agent as the **mitigation executor**.

```
DIAGNOSIS   OpenSRE investigates the cluster  ──►  submit root cause  (LLM judge)
MITIGATION  executor applies OpenSRE's plan via kubectl  ──►  submit ""  (live oracle)
```

OpenSRE is **never modified**. It is invoked only as the installed `opensre`
CLI on `PATH` — this client never imports from or edits the OpenSRE source tree.
The mitigation executor exists because OpenSRE's own Kubernetes integration is
read-only; it diagnoses but cannot change the cluster.

## Prerequisites

1. **Install the OpenSRE CLI** (the package, not the local clone):
   ```bash
   curl -fsSL https://install.opensre.com | bash
   # ensure `opensre` is on PATH, then:
   opensre onboard
   ```
2. **Point OpenSRE at the SREGym cluster.** For OpenSRE to diagnose the injected
   fault it must be able to read the cluster / observability stack. Configure its
   Kubernetes (kubeconfig for the kind cluster) and Prometheus integrations via
   `opensre integrations setup`. This is OpenSRE-side runtime config, not code.
3. **Configure the executor's LLM** (e.g. `ANTHROPIC_API_KEY` for the default
   `claudecode` executor), exactly as for the other SREGym clients.

## Run

Start a problem with the conductor as usual, then:

```bash
# full pipeline: OpenSRE diagnoses, Claude Code applies the fix
python -m clients.opensre.driver --problem-id <id>

# diagnosis-only spike (skips mitigation, submits empty):
OPENSRE_DIAGNOSIS_ONLY=1 python -m clients.opensre.driver --problem-id <id>

# swap the executor:
OPENSRE_EXECUTOR=codex python -m clients.opensre.driver --problem-id <id>
```

## Config (env)

| Var | Default | Meaning |
| --- | --- | --- |
| `OPENSRE_EXECUTOR` | `claudecode` | mitigation executor: `claudecode`, `codex`, `opencode`, `geminicli`, `copilot` |
| `OPENSRE_DIAGNOSIS_ONLY` | unset | `1` skips mitigation (submit empty) |
| `AGENT_MODEL_ID` | `claude-sonnet-4-5` | model for the executor |
| `AGENT_LOGS_DIR` | `./logs/opensre` | logs directory |
| `API_HOSTNAME` / `API_PORT` | `localhost` / `8000` | conductor API |

## What each score means

- **Diagnosis** — OpenSRE's root-cause accuracy, graded by SREGym's LLM judge.
- **Mitigation** — whether OpenSRE's *plan*, executed by the chosen agent's
  "hands", actually repairs the live cluster. This isolates diagnosis quality
  from execution ability: a good plan should succeed regardless of which
  executor applies it.

> Attribution note: the mitigation score reflects **OpenSRE's plan + the
> executor**, not OpenSRE alone — OpenSRE does not act on infrastructure.
