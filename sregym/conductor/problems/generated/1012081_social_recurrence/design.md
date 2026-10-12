# Recurring timeline key-scope policy regression

An operator started a keyspace-isolation promotion utility inside the existing
media MongoDB container. Its retained policy assumes timeline keys have the
`timeline:` prefix, but the live application uses decimal user IDs. The utility
narrows the existing Redis default user's key patterns to `~timeline:*`, without
changing passwords, command permissions, images, Kubernetes objects, or data.
Home-timeline reads fail with `NOPERM`, including reads of empty timelines.

The initial promotion is synchronous. A detached process then checks the runtime
policy every 15 seconds. When someone repairs key coverage, it waits 210 seconds
for the supposed rollout to settle and promotes its retained policy again. Its
Redis protocol sessions are short lived, and its delay runs inside the source
container, leaving no idle remote connection or Kubernetes controller to find.
The policy stays restrictive indefinitely without a repair; there is no expiry.
The recurring mechanism differs from a datastore write freeze, cache data
inventory, endpoint export, or a credential revocation.

The operational shell utility uses the native Redis protocol over Bash TCP
sockets, so it does not require a Redis CLI installation in its original MongoDB
administrative container. Restarting any or all Redis pods does not stop it.

An expert follows the frontend errors to the home-timeline handler and checks
Redis `ACL LIST`/`ACL LOG`. The mounted configuration has no corresponding policy.
An immediate `ACL SETUSER default allkeys` repair works, then key coverage changes
again. Inspection of processes across the existing datastore containers exposes the
detached access-sync utility. Stop its process group before restoring key access.
`mitigate.sh` discovers the script from live process arguments and contents; it
uses neither a saved PID nor the original manifests. No successful application
records are removed or rewritten. `recover.sh` additionally restores the exact
original default-user ACL saved outside the cluster.

This models a common operational lifecycle mistake: an ad hoc promotion loop
survives the rollout it was meant to finish and competes with incident response.
The reference repair is readily available to an SRE with namespace access, but
short request checks or a two-minute healthy window do not establish durability.

Live validation confirmed 11/11 healthy baseline rounds, followed by 11/11
violating rounds with approximately 60% request errors and more than 6,000 new
error lines per affected application container over two minutes. Two impact
checks for the final implementation were 93 seconds apart. Restarting all three
Redis caches yielded 11/11 healthy rounds before the same writer promoted its
policy again; the ensuing SLO check was violated. The reference mitigation ran
twice successfully, and a later check 614 seconds after mitigation was healthy
in every round. Cleanup also ran twice and restored the original ACL exactly.
Reinjection after cleanup completed twice successfully with one live writer.
The final two checks were 128 seconds apart, both with 11/11 violating rounds,
58–62% request errors, and the required application log surges; all 27 pods
remained Ready. Machine-readable validation is in `validation/summary.json`.
