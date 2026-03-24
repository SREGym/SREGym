Follow these steps to diagnose, mitigate, and resolve the incident:

1. Investigate the root cause. Get all the pods and deployments in the affected namespace to understand what services are running and which are unhealthy. Use kubectl, prometheus, loki, and jaeger to gather information. Go as deep as you can into what is causing the issue.

2. Once you have identified the root cause, submit your diagnosis using the submit MCP tool. The submission should be a natural language description of the root cause of the failure. You must pass your diagnosis as the `ans` argument, e.g. submit(ans="The frontend pod is crash-looping due to a misconfigured environment variable").

3. After submitting your diagnosis, wait a moment for it to be processed before proceeding with remediation.

4. Formulate a remediation plan with actionable steps. You have the ability to fix the issue using the kubectl tool — execute your remediation plan one step at a time using the MCP.

5. After each step, verify that your changes took effect (e.g. check pod status, wait for rollouts). Continue until the application is healthy.

6. Once all pods are running and the application is healthy, submit your mitigation using the submit MCP tool. For mitigation, the grading is based on the final state of the application, not the content of your answer — just pass a short string like "done" to the submit tool, e.g. submit(ans="done").

7. After submitting your mitigation, wait a moment for it to be processed. The system may then enter a **resolution** stage. If it does, this means the immediate symptoms have been addressed but the system needs to verify the underlying root cause is fully fixed (e.g. correct configuration, stable pod readiness, proper DNS resolution). Verify the system is fully healthy and the root cause is permanently resolved — not just the symptoms. Then submit your resolution using the submit MCP tool with a short string like "done", e.g. submit(ans="done").


## How SREGym Works

SREGym deploys a Kubernetes application, injects a fault, and then evaluates an agent's ability to:

1. **Diagnose** the root cause (evaluated by an LLM judge comparing against the known answer)
2. **Mitigate** the issue by applying a fix via kubectl (evaluated by checking alerts have been resolved)
3. **Resolve** the underlying root cause (evaluated by checking the system is fully recovered — e.g. all pods Running, correct configuration, stable readiness)

The benchmark provides MCP (Model Context Protocol) tools that agents use to interact with the cluster:

| MCP Endpoint | Tools |
|---|---|
| `/kubectl/sse` | `exec_kubectl_cmd_safely`, `rollback_command`, `get_previous_rollbackable_cmd` |
| `/prometheus/sse` | `get_metrics` |
| `/jaeger/sse` | `get_services`, `get_operations`, `get_traces`, `get_dependency_graph` |
| `/loki/sse` | `get_logs`, `get_labels`, `get_label_values` |
