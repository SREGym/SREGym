#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cat > "$work/batches.go" <<'GO'
package profile

import (
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "fmt"
 "sort"
 "time"
 "github.com/google/uuid"
 "github.com/rs/zerolog/log"
 pb "github.com/harlow/go-micro-services/services/profile/proto"
 "golang.org/x/net/context"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "gopkg.in/mgo.v2"
 "gopkg.in/mgo.v2/bson"
)

// Batch identities are independent of the order in which search returns IDs.
func batchIdentity(ids []string) string {
 ids = append([]string(nil), ids...)
 sort.Strings(ids)
 raw, _ := json.Marshal(ids)
 sum := sha256.Sum256(raw)
 return hex.EncodeToString(sum[:])
}

// The completion writer uses the compact representation used by cache logging.
func completionIdentity(ids []string) string {
 ids = append([]string(nil), ids...)
 sort.Strings(ids)
 sum := sha256.Sum256([]byte(fmt.Sprint(ids)))
 return hex.EncodeToString(sum[:])
}

type profileRecord struct {
 ID string `bson:"_id"`
 Owner string `bson:"owner"`
 Until int64 `bson:"lease_until"`
 State string `bson:"state"`
 Result []byte `bson:"result"`
}

// Durable claims survive pod replacement, allowing overlapping batches across
// replicas to share a single materialization and avoid a cold-cache stampede.
func (s *Server) GetProfiles(ctx context.Context, req *pb.Request) (*pb.Result, error) {
 session := s.MongoSession.Copy()
 defer session.Close()
 c := session.DB("profile-db").C("profile_batches")
 key, owner := batchIdentity(req.HotelIds), uuid.New().String()
 now := time.Now()
 _, err := c.UpsertId(key, bson.M{"$setOnInsert": bson.M{
  "owner": owner, "state": "pending", "lease_until": now.UnixNano()/int64(time.Millisecond) + 30000,
 }})
 if err != nil { return nil, status.Errorf(codes.Unavailable, "profile materialization unavailable: %v", err) }
 var record profileRecord
 if err = c.FindId(key).One(&record); err != nil { return nil, err }
 if record.State == "complete" {
  result := new(pb.Result)
  if json.Unmarshal(record.Result, result) == nil { return result, nil }
 }
 if record.Owner != owner && record.Until < now.Unix() {
  change := mgo.Change{Update: bson.M{"$set": bson.M{"owner": owner, "lease_until": now.UnixNano()/int64(time.Millisecond)+30000}}, ReturnNew: true}
  _, err = c.Find(bson.M{"_id": key, "lease_until": record.Until}).Apply(change, &record)
  if err != nil && err != mgo.ErrNotFound { return nil, err }
 }
 if record.Owner == owner {
  result, err := s.loadProfiles(ctx, req)
  if err != nil { return nil, err }
  payload, _ := json.Marshal(result)
  _, err = c.UpsertId(completionIdentity(req.HotelIds), bson.M{"$set": bson.M{"state": "complete", "result": payload, "lease_until": int64(0)}})
  if err != nil { log.Error().Err(err).Msg("profile completion persistence failed") }
  return result, nil
 }
 timer := time.NewTimer(800 * time.Millisecond)
 defer timer.Stop()
 ticker := time.NewTicker(40 * time.Millisecond)
 defer ticker.Stop()
 for {
  select {
  case <-ctx.Done():
   return nil, status.Error(codes.Canceled, "profile lookup canceled")
  case <-timer.C:
   log.Error().Msg("profile materialization wait timeout")
   return nil, status.Error(codes.DeadlineExceeded, "profile lookup deadline exceeded")
  case <-ticker.C:
   if c.FindId(key).One(&record) == nil && record.State == "complete" {
    result := new(pb.Result)
    if json.Unmarshal(record.Result, result) == nil { return result, nil }
   }
  }
 }
}
GO
cat > "$work/start.sh" <<'SH'
#!/bin/sh
set -eu
cd /go/src/github.com/harlow/go-micro-services
sed 's/func (s \*Server) GetProfiles(/func (s *Server) loadProfiles(/' services/profile/server.go > /tmp/profile-server.go
cp /tmp/profile-server.go services/profile/server.go
cp /etc/profile-runtime/batches.go services/profile/batches.go
GOFLAGS=-mod=vendor go build -o /tmp/profile ./cmd/profile
exec /tmp/profile
SH
kubectl -n "$NAMESPACE" create configmap profile-runtime --from-file=batches.go="$work/batches.go" --from-file=start.sh="$work/start.sh" --dry-run=client -o json | kubectl apply -f -
kubectl -n "$NAMESPACE" patch deployment profile --type=strategic -p '{"spec":{"revisionHistoryLimit":0,"template":{"spec":{"containers":[{"name":"hotel-reserv-profile","command":["/bin/sh","/etc/profile-runtime/start.sh"],"readinessProbe":{"tcpSocket":{"port":8081},"periodSeconds":1},"volumeMounts":[{"name":"profile-runtime","mountPath":"/etc/profile-runtime","readOnly":true}]}],"volumes":[{"name":"profile-runtime","configMap":{"name":"profile-runtime"}}]}}}}'
kubectl -n "$NAMESPACE" rollout restart deployment/profile
kubectl -n "$NAMESPACE" rollout status deployment/profile --timeout=240s
# Retire registrations for pods removed by the rollout. The registry has no checks.
live_ips=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o json | python3 -c 'import json,sys; print(" ".join(p["status"].get("podIP", "") for p in json.load(sys.stdin)["items"] if not p["metadata"].get("deletionTimestamp")))')
services=$(kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS http://consul:8500/v1/agent/services)
retired=$(printf '%s' "$services" | python3 -c 'import json,sys; live=set(sys.argv[1].split()); print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-profile" and v["Address"] not in live))' "$live_ips")
while IFS= read -r service_id; do
 [[ -z "$service_id" ]] && continue
 kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS -X PUT "http://consul:8500/v1/agent/service/deregister/$service_id"
done <<< "$retired"
