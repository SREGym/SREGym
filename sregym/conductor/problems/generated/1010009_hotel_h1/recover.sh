#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
kubectl -n "$NAMESPACE" patch deployment profile --type=merge -p '{"spec":{"progressDeadlineSeconds":600,"replicas":1,"revisionHistoryLimit":10,"selector":{"matchLabels":{"io.kompose.service":"profile"}},"strategy":{"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"25%"},"type":"RollingUpdate"},"template":{"metadata":{"annotations":{"kompose.cmd":"kompose convert","kompose.version":"1.22.0 (955b78124)","sidecar.istio.io/statsInclusionPrefixes":"cluster.outbound,cluster_manager,listener_manager,http_mixer_filter,tcp_mixer_filter,server,cluster.xds-grp,listener,connection_manager","sidecar.istio.io/statsInclusionRegexps":"http.*"},"creationTimestamp":null,"labels":{"io.kompose.service":"profile"}},"spec":{"containers":[{"command":["profile"],"env":[{"name":"JAEGER_SAMPLE_RATIO","value":"1"}],"image":"ghcr.io/sregym/hotel-reservation:sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a@sha256:1c685e1c4c304f327952397b3a99c54dadb086607774d71f222b200ec0d2e65e","imagePullPolicy":"IfNotPresent","name":"hotel-reserv-profile","ports":[{"containerPort":8081,"protocol":"TCP"}],"resources":{"limits":{"cpu":"1"},"requests":{"cpu":"100m"}},"terminationMessagePath":"/dev/termination-log","terminationMessagePolicy":"File"}],"dnsPolicy":"ClusterFirst","restartPolicy":"Always","schedulerName":"default-scheduler","securityContext":{},"terminationGracePeriodSeconds":30}}}}'
kubectl -n "$NAMESPACE" patch deployment profile --type=strategic -p '{"spec":{"template":{"spec":{"volumes":null,"containers":[{"name":"hotel-reserv-profile","volumeMounts":null,"readinessProbe":null}]},"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":null}}}}}'
kubectl -n "$NAMESPACE" rollout status deployment/profile --timeout=240s
kubectl -n "$NAMESPACE" delete cm profile-runtime --ignore-not-found
kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'db.profile_batches.drop()'
# Retire registrations for pods removed by the rollout. The registry has no checks.
live_ips=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o json | python3 -c 'import json,sys; print(" ".join(p["status"].get("podIP", "") for p in json.load(sys.stdin)["items"] if not p["metadata"].get("deletionTimestamp")))')
services=$(kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS http://consul:8500/v1/agent/services)
retired=$(printf '%s' "$services" | python3 -c 'import json,sys; live=set(sys.argv[1].split()); print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-profile" and v["Address"] not in live))' "$live_ips")
while IFS= read -r service_id; do
 [[ -z "$service_id" ]] && continue
 kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS -X PUT "http://consul:8500/v1/agent/service/deregister/$service_id"
done <<< "$retired"
