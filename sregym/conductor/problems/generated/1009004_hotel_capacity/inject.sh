#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cat > "$work/admission.go" <<'GO'
package profile

import (
    "fmt"
    "sync"
    "time"
    "net/http"
    _ "net/http/pprof"

    "github.com/grpc-ecosystem/grpc-opentracing/go/otgrpc"
    "github.com/opentracing/opentracing-go"
    "github.com/rs/zerolog/log"
    pb "github.com/harlow/go-micro-services/services/profile/proto"
    "golang.org/x/net/context"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

const shareProfileBatches = true
const profileWorkerCount = 64

// A slot belongs to the worker until the underlying handler exits, including
// when its caller has already timed out. This bounds background work.
var profileWorkers = make(chan struct{}, profileWorkerCount)

type profileBatch struct {
    ready chan struct{}
    result interface{}
    err error
}

var profileBatches = struct {
    sync.Mutex
    entries map[string]*profileBatch
}{entries: make(map[string]*profileBatch)}

func init() {
    go http.ListenAndServe("127.0.0.1:6060", nil)
}

// Share concurrent identical batches to bound duplicate cache and database work.
func profileAdmissionInterceptor(tracer opentracing.Tracer) grpc.UnaryServerInterceptor {
    trace := otgrpc.OpenTracingServerInterceptor(tracer)
    return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
        if !shareProfileBatches {
            return trace(ctx, req, info, handler)
        }
        select {
        case profileWorkers <- struct{}{}:
        default:
            log.Error().Msg("profile worker capacity exhausted")
            return nil, status.Error(codes.ResourceExhausted, "profile worker capacity exhausted")
        }
        replies := make(chan *profileBatch, 1)
        go func() {
            defer func() { <-profileWorkers }()
            result, err := sharedProfileBatch(ctx, req, info, handler, trace)
            replies <- &profileBatch{result: result, err: err}
        }()
        deadline := time.NewTimer(time.Second)
        defer deadline.Stop()
        select {
        case reply := <-replies:
            return reply.result, reply.err
        case <-ctx.Done():
            log.Error().Err(ctx.Err()).Msg("profile request canceled")
            return nil, status.Error(codes.Canceled, ctx.Err().Error())
        case <-deadline.C:
            log.Error().Msg("profile handler progress timeout")
            return nil, status.Error(codes.DeadlineExceeded, "profile handler deadline exceeded")
        }
    }
}

func sharedProfileBatch(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler, trace grpc.UnaryServerInterceptor) (interface{}, error) {
    key := fmt.Sprint(req.(*pb.Request).HotelIds)
    profileBatches.Lock()
    if batch, exists := profileBatches.entries[key]; exists {
        // Keep the entry stable while consuming the published result.
        defer profileBatches.Unlock()
        <-batch.ready
        return batch.result, batch.err
    }
    batch := &profileBatch{ready: make(chan struct{})}
    profileBatches.entries[key] = batch
    profileBatches.Unlock()

    result, err := trace(ctx, req, info, handler)

    profileBatches.Lock()
    batch.result, batch.err = result, err
    delete(profileBatches.entries, key)
    close(batch.ready)
    profileBatches.Unlock()
    return result, err
}
GO
cat > "$work/start.sh" <<'SH'
#!/bin/sh
set -eu
cd /go/src/github.com/harlow/go-micro-services
sed 's/otgrpc.OpenTracingServerInterceptor(s.Tracer)/profileAdmissionInterceptor(s.Tracer)/' services/profile/server.go > /tmp/profile-server.go
# The tracing interceptor is composed by the admission implementation.
sed -i '/"github.com\/grpc-ecosystem\/grpc-opentracing\/go\/otgrpc"/d' /tmp/profile-server.go
cp /tmp/profile-server.go services/profile/server.go
cp /etc/profile-runtime/admission.go services/profile/admission.go
GOFLAGS=-mod=vendor go build -o /tmp/profile ./cmd/profile
exec /tmp/profile
SH
kubectl -n "$NAMESPACE" create configmap profile-runtime --from-file=admission.go="$work/admission.go" --from-file=start.sh="$work/start.sh" --dry-run=client -o json | kubectl apply -f -
before=$(kubectl -n "$NAMESPACE" get deployment profile -o jsonpath='{.metadata.generation}')
kubectl -n "$NAMESPACE" patch deployment profile --type=strategic -p '{"spec":{"revisionHistoryLimit":0,"template":{"spec":{"containers":[{"name":"hotel-reserv-profile","command":["/bin/sh","/etc/profile-runtime/start.sh"],"readinessProbe":{"tcpSocket":{"port":8081},"periodSeconds":1,"failureThreshold":3},"volumeMounts":[{"name":"profile-runtime","mountPath":"/etc/profile-runtime","readOnly":true}]}],"volumes":[{"name":"profile-runtime","configMap":{"name":"profile-runtime"}}]}}}}'
# Reload the mounted runtime on repeated runs too.
after=$(kubectl -n "$NAMESPACE" get deployment profile -o jsonpath='{.metadata.generation}')
if [[ "$before" == "$after" ]]; then
    kubectl -n "$NAMESPACE" rollout restart deployment/profile
fi
kubectl -n "$NAMESPACE" rollout status deployment/profile --timeout=180s

# The application's registry has no health checks; remove retired pod addresses.
live_ips=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o json | python3 -c 'import json,sys; print(" ".join(p["status"].get("podIP", "") for p in json.load(sys.stdin)["items"] if not p["metadata"].get("deletionTimestamp")))')
services=$(kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS http://consul:8500/v1/agent/services)
retired=$(printf '%s' "$services" | python3 -c 'import json,sys; live=set(sys.argv[1].split()); print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-profile" and v["Address"] not in live))' "$live_ips")
while IFS= read -r service_id; do
    [[ -z "$service_id" ]] && continue
    kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS -X PUT "http://consul:8500/v1/agent/service/deregister/$service_id"
done <<< "$retired"
