# [Problem RFC] Calico route-reflector selector drift partitions cross-node pod networking

## Problem ID

```text
calico_route_reflector_label_drift_hotel_reservation
```

## Real-world failure story

This problem is based on Reddit's [Pi-Day outage](https://www.reddit.com/r/RedditEng/comments/11xx5o0/you_broke_reddit_the_piday_outage/). During Reddit's Kubernetes 1.23 to 1.24 upgrade, a Calico route-reflector topology still depended on the legacy `node-role.kubernetes.io/master` node label. As the cluster moved to the newer `node-role.kubernetes.io/control-plane` labeling convention, the Calico `BGPPeer` selector stopped matching the intended control-plane route-reflector nodes. In a Calico BGP topology with `nodeToNodeMeshEnabled: false`, losing the route-reflector peerings can partition cross-node pod and service traffic while many application containers still appear `Running`. The visible symptoms may look like DNS, application, or admission-controller noise, but the root cause is stale CNI/BGP peer selection after a Kubernetes label migration.

References:

- [Reddit Engineering: You Broke Reddit: The Pi-Day Outage](https://www.reddit.com/r/RedditEng/comments/11xx5o0/you_broke_reddit_the_piday_outage/)
- [Kubernetes 1.24 removals and deprecations](https://kubernetes.io/blog/2022/04/07/upcoming-changes-in-kubernetes-1-24/)
- [Calico BGP route reflector configuration](https://docs.tigera.io/calico/latest/networking/configuring/bgp)

## Required cluster mode

This problem requires a disposable multi-node kind cluster with the default CNI disabled and Calico installed in a BGP-based mode where cross-node pod routes depend on Calico BGP peerings. A VXLAN-only Calico configuration is not sufficient, because route-reflector peer loss would not necessarily break pod traffic.

The minimum intended topology is:

```text
kind-control-plane   route reflector, no app workloads
kind-worker          app/probe node A
kind-worker2         app/probe node B
```

## Known adjacent SREGym work

This RFC is intentionally separate from existing network and scheduler problems:

- [`pod_cidr_exhaustion_hotel_reservation`](https://github.com/SREGym/SREGym/pull/774) exhausts Calico IPAM capacity and leaves pods unable to obtain IP addresses.
- [`node_routing_table_wipeout`](https://github.com/SREGym/SREGym/pull/818) removes per-pod kernel routes from one worker node.
- [`priority_preemption_cascade_hotel_reservation`](https://github.com/SREGym/SREGym/pull/821) covers the Grafana Kubernetes Pod Priorities outage through scheduler preemption policy.

The Reddit Pi-Day scenario has a different root cause: Calico route-reflector peer selection drifts after Kubernetes control-plane label migration, so `BGPPeer` selectors no longer match the intended route-reflector node while route-reflector mode is still enabled.

## How this simulates the failure on SREGym

The Hotel Reservation app is deployed normally on a multi-node Calico kind cluster. Fault injection pins `frontend` and `reservation` to different worker nodes and creates a small `platform-health` cross-node probe so the network failure can be checked directly.

The injector then configures Calico route-reflector mode:

- `BGPConfiguration/default` has `nodeToNodeMeshEnabled: false`.
- The control-plane node is annotated as a route reflector.
- `BGPPeer/stale-master-route-reflectors` selects peers with `has(node-role.kubernetes.io/master)`.

The setup first adds the old `node-role.kubernetes.io/master` label so the topology is healthy. The actual fault removes that label, simulating a Kubernetes label migration. The BGPPeer selector no longer matches a route-reflector node, so cross-node service/DNS traffic fails even though the app pods still look mostly normal.

The synthetic probe is mandatory, not just a convenience. App symptoms can be noisy, so the proof should include cross-node traffic that is independent of Hotel Reservation:

```text
netprobe-a pinned to worker A
netprobe-b pinned to worker B
netprobe-b exposes HTTP /health
netprobe-a curls netprobe-b through both Pod IP and Service DNS
```

The expected shape is:

- same-node traffic remains healthy.
- cross-node Pod IP traffic fails.
- cross-node Service traffic fails.
- DNS may be healthy or may show downstream noise, but DNS alone is not the diagnosis.

## Cluster requirements and blast radius

This problem must run only on a disposable SREGym validation cluster. It intentionally mutates cluster-scoped Calico routing state, disables Calico node-to-node mesh, creates a cluster-scoped `BGPPeer`, annotates and labels the control-plane node, and restarts the `calico-node` DaemonSet during injection and recovery. The injector refuses to run unless Calico BGP CRDs are present, the Calico BGP dataplane is active, and the cluster has one control-plane node plus at least two worker nodes.

Cleanup restores a pre-existing `BGPConfiguration/default` when one was captured before injection, deletes only a `BGPConfiguration/default` that the problem knows it created, restores any pre-existing legacy route-reflector label or route-reflector annotation on the control-plane node, and removes only node state that the problem created. Problem-created cluster resources and route-reflector node state are labeled or annotated so a later cleanup pass can remove them after an interrupted same-cluster run. Recovery restarts `calico-node`; cleanup then relies on Calico's normal CRD and node-label watches to converge teardown state without a second DaemonSet rollout. If the process is killed after mutating pre-existing Calico routing state, a disposable cluster should be reset before reuse because the original cluster-wide BGP settings cannot be reconstructed from a fresh process.

## Validation plan

The proof-of-concept should first prove the healthy topology:

- Calico is running in BGP mode.
- `BGPConfiguration/default.spec.nodeToNodeMeshEnabled` is `false`.
- the route-reflector node has `routeReflectorClusterID` configured.
- the `BGPPeer` selector matches the route-reflector node.
- cross-node synthetic probe traffic succeeds.

After fault injection:

- the legacy `node-role.kubernetes.io/master` label is removed.
- the `BGPPeer` selector matches zero route-reflector nodes.
- cross-node synthetic probe traffic fails.
- Hotel Reservation shows request failures or dependency failures while relevant pods may remain `Running`.

After mitigation:

- the `BGPPeer` selector matches the intended route-reflector node again, preferably through a stable custom label such as `route-reflector=true`.
- cross-node synthetic probe traffic succeeds.
- Hotel Reservation request probes succeed.
- route-reflector mode remains enabled and node-to-node mesh remains disabled.

## Accepted mitigation

The strict oracle accepts recovery only when:

- Hotel Reservation deployments are ready.
- the synthetic cross-node probe still exists and can reach its Service.
- probe pods remain pinned to different worker nodes.
- Calico `calico-node` is rolled out.
- route-reflector mode is preserved with node-to-node mesh disabled.
- at least one `BGPPeer` still exists for the route-reflector topology.
- a BGPPeer selects an existing route-reflector node through the current control-plane label or an intentionally restored route-reflector label.
- the matched route-reflector node has `routeReflectorClusterID` configured.
- stale unmatched `node-role.kubernetes.io/master` selectors are not left behind.
- app replicas were not reduced.
- app and probe pods were not forced onto a single node.

Accepted mitigations include patching the `BGPPeer` peer selector to match `node-role.kubernetes.io/control-plane`, adding a stable `route-reflector=true` label and updating the peer selector to use it, or deliberately restoring the legacy label to the intended route-reflector node.

Rejected shortcuts include deleting the probe, enabling node-to-node mesh to bypass the route-reflector topology, scaling app deployments down, scheduling all app/probe pods onto one node, hardcoding Pod IPs or ClusterIPs, or deleting Calico/BGPPeer resources without restoring cross-node routing.
