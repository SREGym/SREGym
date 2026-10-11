# Thrift transport pool identity collision

A transport tuning change adds an explicit OpenResty cosocket pool name in the
frontend's existing nginx startup Lua. The pool name uses the RPC port only.
Every Thrift backend listens on 9090. OpenResty treats an explicit pool name as
the full pool identity: a subsequent connect to another host can return an idle
socket already connected to the first host. The generated client then sends a
valid method to the wrong service. Cold connections work; reused connections
intermittently return `Invalid method name` and HTTP 500.

This is a realistic connection-pooling refactor bug rather than a Service or DNS
mutation. There are no extra resources or helper workloads. The installed
transport implementation, frontend API code, images, Services and endpoints
remain unchanged. The code is loaded from the existing nginx-thrift ConfigMap
at worker startup, so ordinary restarts and Deployment rollback retain the bug.
The fault has no expiration or controller that can heal it.

An expert correlates unknown-method errors across unrelated API methods,
checks that the generated clients and backend interfaces agree, and compares
cold versus reused RPC connections. Inspecting nginx startup Lua reveals the
transport override and its port-only pool key. Scope pool identity to host and
port, and restart frontend workers. `mitigate.sh` applies this correction using
only the current ConfigMap. `recover.sh` removes the override entirely and
removes the restart annotation to restore the original pod template and exact
original nginx configuration. No datastore contents are changed.

Validation evidence is recorded in validation-*.json. Healthy baseline had zero
request errors. Injection produced approximately 30–35% errors and thousands of
new nginx error lines in a two-minute window. Repeated injection/restart retained
the violation. Recovery validation and final replay are recorded separately.

Final results: mitigation and cleanup each passed all 11 rounds in a full
two-minute window. All three scripts were run twice successfully. Final replay
remained violated after a frontend Deployment rollback, and two checks at least
60 seconds apart confirmed the incident. See validation-replay-*.json.
