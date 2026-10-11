# Discovery retirement replay across a revision format migration

A discovery rollout introduced journal-backed endpoint retirement: clients and an API facade consume a materialized snapshot stored in Consul KV. The journal contains an older endpoint upsert at revision 9, its retirement at revision 10, and the current endpoint at revision 11. A migration serialized revisions as strings and sorted them as text. Replay runs 10, 11, 9. It resurrects the retired endpoint while recording the numerically correct high-water mark 11. The retired endpoint now accepts another gRPC capability, as happens when gateway routes or service addresses are reused.

The authoritative catalog and native passing-health responses remain correct. The snapshot contains both legitimate and retired endpoints. Search distributes requests over both, emitting rate Unimplemented warnings and generic frontend failures. The facade projects that snapshot into health responses; the client library also loads it independently. Every application process remains healthy.

The previous fixer bypassed the facade and restarted search. Here the client still reads the persisted snapshot through the native API after bypass. A client rollback still receives the corrupt view from the facade. A restart retains the snapshot. A comparator fix alone retains it because committed materialization already claims revision 11. Deleting only the snapshot allows defective replay to reconstruct it. The repair must address replay semantics and persisted state.

An expert compares search destinations with native and projected passing-health responses, then follows the deployed resolver KV reads. The ConfigMap and Consul journal provide all evidence. Numerical replay retires the old endpoint. The reference mitigation repairs the live comparator, rebuilds the materialized view from the live journal, and refreshes connections. It uses no saved baseline or remembered addresses. Disabling both snapshot readers is also a valid emergency alternative.

The native Consul process is unmodified. The facade, its Python source, resolver source, and compile init container are normal deployed application artifacts. Compilation uses the existing application image, compiler, source, and vendored dependencies. The discovery rollout moves Consul from dev mode to a single-server configuration with a dedicated PVC, so the journal and committed view survive container and pod restarts. Injection preserves legitimate registrations across the native Consul rollout and synchronously constructs the journal and materialization without timing races. Recovery restores original pod templates, Service ports, and prior journal/snapshot contents and removes the added ConfigMap and disposable registry PVC. NAMESPACE and ARTIFACT_DIR are honored. Recovery state is unnecessary for mitigation.

## Validation

Injection, mitigation, and recovery each ran twice successfully. The facade preserves native HTTP status codes, including absent KV entries; uncached services resolve normally.

The settled pre-mitigation window had four violating rounds, approximately 29–30% errors, and over 3,300 search error lines. Two violation checks were separated by more than 60 seconds. The reference repair restored a full four-round window with zero errors, approximately 97–99 requests/second, and p99 latency below 100 ms. Recovery also restored a full four-round window with zero errors. Search, Consul, and frontend pod templates, Consul Service ports, and the Consul rollout strategy matched the saved baseline exactly; the added ConfigMap and PVC were absent.

Final replay ran injection twice from that recovered baseline. Its first settled check reported four violating rounds, approximately 29–31% errors, and 3,360 search error lines. Every application pod was Running and Ready. The second final result is recorded below.

Initial diagnostic verification: bypassing the facade to native port 8500 and restarting search left settled rounds at approximately 29–31% errors. Rolling search back to its original pod template while retaining the facade returned 49 HTTP 500 responses in 100 diagnostic hotel searches. Restarting the persistent Consul deployment retained both journal and bad materialization. These checks confirm that each single-reader repair from the previous incident is insufficient.

Final second check, 110 seconds later: all 4 rounds violated, with error ratios 29.3%–30.4% and 3,354 search error lines in the comparison window. The incident is left active for submission.
