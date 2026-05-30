# [Problem RFC] Mis-scoped PriorityClass causes cascading preemption of production pods

## Problem ID

```text
priority_preemption_cascade_hotel_reservation
```

## Real-world failure story

This problem is based on Grafana Labs' Kubernetes Pod Priorities outage in Hosted Prometheus. In that incident, a new Cortex cluster was deployed with medium-priority ingesters while the existing production ingesters had no priority. Under resource pressure, the scheduler preempted production ingesters for the new workload, and replacement production pods inherited the same unintended medium-priority relationship. The result was a cascading production outage.

Kubernetes documents this scheduler behavior directly: when a higher-priority pod cannot be scheduled because of resource pressure, the scheduler may preempt lower-priority pods to make room.

Sources:

- Grafana Labs: <https://grafana.com/blog/how-a-production-outage-was-caused-using-kubernetes-pod-priorities/>
- Kubernetes Pod Priority and Preemption: <https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/>

## How this simulates the failure on SREGym

The Hotel Reservation app is deployed normally. Fault injection then prepares the existing `reservation` pod with realistic memory requests and asserts it still has priority `0` on the selected pressure node. Only after that low-priority victim exists does the injector create two PriorityClasses:

- `platform-medium`: value `100000`, mistakenly set as the global default.
- `production-critical`: value `200000`, intended for protected production services but not applied.

The injector then creates a synthetic tenant workload, `analytics-batch/tenant-ingester`, pinned to the same node as the existing `reservation` pod. The tenant workload uses `platform-medium` and requests enough memory, computed from node allocatable/requested memory and the `reservation` request, that it can only schedule by preempting the lower-priority `reservation` pod.

To keep victim selection deterministic, the injector explicitly assigns `platform-medium` to the other Hotel Reservation deployments before creating the tenant workload. `reservation` remains the only app deployment with an existing priority-0 pod on the pressure node.

Once the `reservation` pod is preempted, its replacement pod inherits `platform-medium` from the unsafe global default. It cannot preempt the already-running tenant workload with the same priority, so the `reservation` deployment remains under-replicated even though the image, service, and application configuration are valid.

## Problem runtime behavior

The agent should observe that `reservation` is unavailable or under-replicated while other app components may still look healthy. The key evidence is in scheduler events and priority policy, not application logs.

Useful commands include:

```shell
kubectl get events -A --sort-by=.lastTimestamp
kubectl describe deployment reservation -n hotel-reservation
kubectl describe pod -n hotel-reservation -l io.kompose.service=reservation
kubectl get priorityclass
kubectl get pods -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,PRIORITY:.spec.priorityClassName,NODE:.spec.nodeName,PHASE:.status.phase
kubectl describe node <node>
```

Expected evidence includes a scheduler preemption event, a high-priority `analytics-batch/tenant-ingester` pod, an unsafe global `platform-medium` PriorityClass, and a `reservation` rollout that cannot regain capacity. A successful local run should show evidence in this shape:

```shell
kubectl get events -A --sort-by=.lastTimestamp | grep -Ei "preempt|failedscheduling|reservation|tenant-ingester"

kubectl get pod -n hotel-reservation \
  -l io.kompose.service=reservation \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName,PRIORITY:.spec.priority,CLASS:.spec.priorityClassName,NOMINATED:.status.nominatedNodeName

kubectl get pod -n analytics-batch \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName,PRIORITY:.spec.priority,CLASS:.spec.priorityClassName
```

## Correct diagnosis

A good diagnosis should identify a scheduler-level priority-policy failure:

```text
The reservation deployment is not failing because of its container, service, image, or config. It was preempted by a higher-priority tenant workload after platform-medium was made the global PriorityClass. Replacement reservation pods inherit the same medium priority instead of production-critical, so they cannot reclaim capacity from the tenant workload. The fix is to remove the unsafe global default and explicitly protect reservation with production-critical priority.
```

## Accepted mitigation

The current oracle is intentionally strict. It accepts mitigation when:

- `reservation` is healthy with all desired replicas available.
- the `reservation` Service has ready endpoints.
- all Hotel Reservation deployments still have at least one desired replica and are ready.
- `platform-medium` still exists but is no longer `globalDefault: true`.
- `production-critical` exists and has a higher value than `platform-medium`.
- the `reservation` deployment template explicitly uses `priorityClassName: production-critical`.
- the synthetic tenant workload still exists and has not simply been deleted or scaled to zero.
- the memory requests used to create the scheduler pressure were not reduced to avoid the policy fix.

This strictness is deliberate: deleting the tenant workload, deleting all PriorityClasses, or scaling `reservation` down can make a pod-health check pass while avoiding the scheduler-policy diagnosis.

## Agent behavior to evaluate

Weak agents are likely to restart pods, inspect logs, or delete the tenant workload. Strong agents should inspect scheduler events, PriorityClasses, pod priorities, and node resource pressure, then repair the policy relationship so production can safely preempt the tenant workload while preserving the tenant workload as evidence.

## Validation command

Run the standard lifecycle validation on a Linux Kubernetes environment with Docker, Helm, kubectl, and uv installed:

```shell
uv run python tests/integration/validate_problem.py \
  --problem priority_preemption_cascade_hotel_reservation \
  --summary /tmp/priority-preemption-validation.md \
  --inject-timeout 300 \
  --recover-timeout 600 \
  --poll-interval 15
```

Local Docker Desktop validation may need enough Docker VM memory and a `/run/udev` host path for OpenEBS NDM. A Linux VM or GPU pod runner must provide privileged Docker networking for kind; standard unprivileged containers that cannot run `docker network create` are not sufficient. Use at least 4 vCPU, 16 GB RAM, Docker, Helm, kubectl, and either a kind cluster created from `kind/setup_kind_cluster.sh` or another Kubernetes cluster available through the active kubeconfig.
