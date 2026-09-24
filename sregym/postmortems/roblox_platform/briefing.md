You are on call for the Meridian gaming platform. Players report intermittent
login failures, unavailable experiences, and purchases that do not appear in
their inventory. Investigate the running system and restore its user workflows.
Preserve player balances, inventory, acknowledged purchases, and session records.
Confirm that asynchronous processing catches up and service remains stable.

You have the native Consul, Nomad, and Vault CLIs, curl, and SSH. The inventory in
/workspace/operations lists hosts, service ownership, and normal operating
procedures. SSH keys and CLI environment settings are provisioned in your home.
Application source and deployment specifications are available on operational
hosts. Logs are local to each process; the aggregate telemetry service is also
available when its dependencies are healthy. There is no task-specific repair
API. Use normal configuration, deployment, storage, and process tools.

Traffic continues while you investigate. An HTTP health endpoint establishes
process liveness only. Check the actual workflows and durable outcomes before
declaring recovery. Record your findings and incident updates in /workspace.
