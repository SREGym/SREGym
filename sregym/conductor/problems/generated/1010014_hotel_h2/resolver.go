// Copyright 2016-present Oliver Eilhard. All rights reserved.
// Use of this source code is governed by a MIT-license.
// See http://olivere.mit-license.org/license.txt for details.

package consul

import (
	"log"
 "encoding/json"
 "sort"
	"net"
	"strconv"

	"github.com/hashicorp/consul/api"
	"google.golang.org/grpc/naming"
)

// Resolver implements the gRPC Resolver interface using a Consul backend.
//
// See the gRPC load balancing documentation for details about Balancer and
// Resolver: https://github.com/grpc/grpc/blob/master/doc/load-balancing.md.
type Resolver struct {
	c           *api.Client
	service     string
	tag         string
	passingOnly bool

	quitc    chan struct{}
	updatesc chan []*naming.Update
}

// NewResolver initializes and returns a new Resolver.
//
// It resolves addresses for gRPC connections to the given service and tag.
// If the tag is irrelevant, use an empty string.
func NewResolver(client *api.Client, service, tag string) (*Resolver, error) {
	r := &Resolver{
		c:           client,
		service:     service,
		tag:         tag,
		passingOnly: true,
		quitc:       make(chan struct{}),
		updatesc:    make(chan []*naming.Update, 1),
	}

	// Retrieve instances immediately
	instances, index, err := r.getInstances(0)
	if err != nil {
		log.Printf("grpc/lb/consul: error retrieving instances from Consul: %v", err)
	}
	updates := r.makeUpdates(nil, instances)
	if len(updates) > 0 {
		r.updatesc <- updates
	}

	// Start updater
	go r.updater(instances, index)

	return r, nil
}

// Resolve creates a watcher for target. The watcher interface is implemented
// by Resolver as well, see Next and Close.
func (r *Resolver) Resolve(target string) (naming.Watcher, error) {
	return r, nil
}

// Next blocks until an update or error happens. It may return one or more
// updates. The first call will return the full set of instances available
// as NewResolver will look those up. Subsequent calls to Next() will
// block until the resolver finds any new or removed instance.
//
// An error is returned if and only if the watcher cannot recover.
func (r *Resolver) Next() ([]*naming.Update, error) {
	return <-r.updatesc, nil
}

// Close closes the watcher.
func (r *Resolver) Close() {
	select {
	case <-r.quitc:
	default:
		close(r.quitc)
		close(r.updatesc)
	}
}

// updater is a background process started in NewResolver. It takes
// a list of previously resolved instances (in the format of host:port, e.g.
// 192.168.0.1:1234) and the last index returned from Consul.
func (r *Resolver) updater(instances []string, lastIndex uint64) {
	var err error
	var oldInstances = instances
	var newInstances []string

	// TODO Cache the updates for a while, so that we don't overwhelm Consul.
	for {
		select {
		case <-r.quitc:
			break
		default:
			newInstances, lastIndex, err = r.getInstances(lastIndex)
			if err != nil {
				log.Printf("grpc/lb/consul: error retrieving instances from Consul: %v", err)
				continue
			}
			updates := r.makeUpdates(oldInstances, newInstances)
			if len(updates) > 0 {
				r.updatesc <- updates
			}
			oldInstances = newInstances
		}
	}
}

// getInstances retrieves the new set of instances registered for the
// service from Consul.
func (r *Resolver) getInstances(lastIndex uint64) ([]string, uint64, error) {
	services, meta, err := r.c.Health().Service(r.service, r.tag, r.passingOnly, &api.QueryOptions{
		WaitIndex: lastIndex,
	})
	if err != nil {
		return nil, lastIndex, err
	}

	var instances []string
	for _, service := range services {
		s := service.Service.Address
		if len(s) == 0 {
			s = service.Node.Address
		}
		addr := net.JoinHostPort(s, strconv.Itoa(service.Service.Port))
		instances = append(instances, addr)
	}
	// Replay the endpoint journal after the authoritative catalog snapshot.
 // Journal removals preserve retirement across catalog reconciliation.
 snapshotKey := "service-discovery/materialized/"+r.service
 cached, _, e := r.c.KV().Get(snapshotKey, nil)
 if e != nil { return nil, lastIndex, e }
 if cached != nil {
  var snapshot struct { Endpoints []string `json:"endpoints"`; Revision uint64 `json:"revision"` }
  if e = json.Unmarshal(cached.Value, &snapshot); e != nil { return nil, lastIndex, e }
  return snapshot.Endpoints, meta.LastIndex, nil
 }
 pair, _, e := r.c.KV().Get("service-discovery/journal/"+r.service, nil)
 if e != nil { return nil, lastIndex, e }
 if pair != nil {
  var entries []struct { Revision string `json:"revision"`; Operation string `json:"operation"`; Endpoint string `json:"endpoint"` }
  if e = json.Unmarshal(pair.Value, &entries); e != nil { return nil, lastIndex, e }
  sort.SliceStable(entries, func(i, j int) bool { return entries[i].Revision < entries[j].Revision })
  active := make(map[string]bool)
  for _, endpoint := range instances { active[endpoint] = true }
  for _, entry := range entries {
   switch entry.Operation {
   case "upsert": active[entry.Endpoint] = true
   case "retire": delete(active, entry.Endpoint)
   }
  }
  instances = nil
  for endpoint := range active { instances = append(instances, endpoint) }
  sort.Strings(instances)
  var highWater uint64
  for _, entry := range entries { revision, _ := strconv.ParseUint(entry.Revision, 10, 64); if revision > highWater { highWater = revision } }
  snapshot, _ := json.Marshal(struct { Endpoints []string `json:"endpoints"`; Revision uint64 `json:"revision"` }{instances, highWater})
  if _, e = r.c.KV().Put(&api.KVPair{Key:snapshotKey, Value:snapshot}, nil); e != nil { return nil, lastIndex, e }
 }
 return instances, meta.LastIndex, nil
}

// makeUpdates calculates the difference between and old and a new set of
// instances and turns it into an array of naming.Updates.
func (r *Resolver) makeUpdates(oldInstances, newInstances []string) []*naming.Update {
	oldAddr := make(map[string]struct{}, len(oldInstances))
	for _, instance := range oldInstances {
		oldAddr[instance] = struct{}{}
	}
	newAddr := make(map[string]struct{}, len(newInstances))
	for _, instance := range newInstances {
		newAddr[instance] = struct{}{}
	}

	var updates []*naming.Update
	for addr := range newAddr {
		if _, ok := oldAddr[addr]; !ok {
			updates = append(updates, &naming.Update{Op: naming.Add, Addr: addr})
		}
	}
	for addr := range oldAddr {
		if _, ok := newAddr[addr]; !ok {
			updates = append(updates, &naming.Update{Op: naming.Delete, Addr: addr})
		}
	}

	return updates
}
