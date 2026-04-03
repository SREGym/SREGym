## Time Constraint

You have a **strict 30-minute time limit** for the entire workflow. If you do not submit within this window, you receive zero credit. Budget your time:

- **Diagnosis: aim to submit within 8–10 minutes.** An imperfect diagnosis submitted on time is worth far more than a perfect one that never arrives.
- **Remediation + mitigation submission: use the remaining time.**

Do NOT spend all your time investigating. Once you have a reasonable hypothesis, submit it and move on to fixing.

## Workflow

### Step 1: Investigate (budget: 8–10 minutes max)

Start with the highest-signal sources first:

1. **Pod & deployment status** — `kubectl get pods -n <namespace>`, `kubectl get deployments -n <namespace>`. Look for CrashLoopBackOff, ImagePullBackOff, Pending, or unhealthy pods.
2. **Describe unhealthy resources** — `kubectl describe pod <pod> -n <namespace>` for events, conditions, and scheduling failures.
3. **Recent logs** — Use Loki (`get_logs`) for crash logs, error messages, and stack traces from unhealthy pods.
4. **Metrics** — Use Prometheus (`get_metrics`) for resource pressure, error rates, or latency spikes if the issue is performance-related rather than a hard crash.
5. **Traces** — Use Jaeger only if the issue involves cross-service request failures and logs/metrics are inconclusive.

Stop investigating as soon as you can identify: (a) which component is broken, and (b) why it is broken. You do not need to be 100% certain — a strong hypothesis is sufficient.

### Step 2: Submit diagnosis

Call the `submit` MCP tool with a natural language description of the root cause. Be specific about:
- The **affected component** (e.g., deployment name, pod, configmap, service)
- The **fault mechanism** (e.g., wrong image tag, missing env var, misconfigured port, resource limit)
- The **concrete details** (e.g., the specific wrong value, the specific missing variable name)

Example:
```
submit(ans="The frontend deployment is crash-looping because the CART_ADDR environment variable was removed from the container spec, causing the frontend to fail on cart-related requests.")
```

After submitting, wait briefly for the submission to be processed before proceeding.

### Step 3: Remediate

Fix the root cause using `kubectl` (via the kubectl MCP tools). Aim to correct the underlying misconfiguration, not just restart pods. For example:

- Wrong image → patch the deployment with the correct image
- Missing env var → add it back with `kubectl set env`
- Misconfigured configmap → edit or replace the configmap and restart affected pods
- Scheduling issue → fix node selectors, affinities, or resource requests

After each change, verify with `kubectl get pods -n <namespace>` that pods are recovering.

### Step 4: Submit mitigation

Once all pods are running and healthy, call the `submit` MCP tool again:
```
submit(ans="done")
```

This triggers evaluation of both mitigation (are alerts resolved?) and resolution (is the root cause fixed?). The content of this submission does not matter — only the cluster state at the time of submission is evaluated.

## Available MCP Tools

| MCP Endpoint | Tools | Use For |
|---|---|---|
| `/kubectl/sse` | `exec_kubectl_cmd_safely` | Run any kubectl command (get, describe, patch, apply, delete, set, scale, etc.) |
| `/prometheus/sse` | `get_metrics` | Query metrics (CPU, memory, error rates, latency) |
| `/jaeger/sse` | `get_services`, `get_operations`, `get_traces`, `get_dependency_graph` | Distributed tracing for cross-service issues |
| `/loki/sse` | `get_logs`, `get_labels`, `get_label_values` | Application and system logs |
| `/submit_mcp/sse` | `submit` | Submit diagnosis and mitigation results |

### kubectl tool usage

The primary kubectl tool is `exec_kubectl_cmd_safely`. Pass any valid kubectl command as the `cmd` argument:
```
exec_kubectl_cmd_safely(cmd="kubectl get pods -n astronomy-shop")
exec_kubectl_cmd_safely(cmd="kubectl patch deployment frontend -n astronomy-shop --type=json -p='[{\"op\": \"remove\", \"path\": \"/spec/template/spec/affinity\"}]'")
```

The endpoint also exposes `rollback_command` and `get_previous_rollbackable_cmd` — these are optional utilities to undo previous kubectl changes. You do not need to use them for normal remediation.

## Key Rules

- **Submit early.** A partial diagnosis submitted in time beats a perfect one that times out. You get zero credit if you don't submit.
- **Two submissions required.** First `submit()` = diagnosis. Second `submit()` = mitigation. You must submit diagnosis before you can submit mitigation.
- **Fix root causes, not symptoms.** Restarting a pod without fixing the underlying misconfiguration will pass mitigation but fail resolution.
- **Be specific in diagnosis.** Name the exact component, the exact fault, and the exact wrong value when possible. Vague descriptions like "a service is down" score poorly.
- **You have full kubectl permissions.** The kubectl MCP tool has cluster-admin-level access to all namespaces. You can get, create, patch, delete, and update any resource (deployments, configmaps, services, pods, secrets, etc.) in any namespace. Do not assume you lack permissions — if a command fails, check the error message and fix the command syntax rather than giving up. Common mutation commands that work: `kubectl patch`, `kubectl set env`, `kubectl set image`, `kubectl apply`, `kubectl delete`, `kubectl scale`, `kubectl rollout undo`, `kubectl label`, `kubectl create`.
