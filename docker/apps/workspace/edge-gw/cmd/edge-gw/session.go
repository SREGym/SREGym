package main

// Connection session admission (the release decides which implementation).
//
// Every accepted connection is admitted before it is proxied: the gateway
// derives a session key, looks for a resumable session from the same client,
// and records the new one. Two implementations exist, chosen by the
// `session_cache` setting at startup:
//
//   per-connection  each connection owns its session state; lookups go to a
//                   sharded map with short critical sections.
//   shared          one table of every live session, so a reconnecting client
//                   can resume. Admission reconciles the table (expiry and
//                   resume matching) while holding a single lock. The lock
//                   spins before yielding, which keeps admission latency low
//                   when the table is small.
//
// The shared table's cost per admission grows with the number of live
// sessions, and its spinning waiters consume the CPU quota the lock holder
// needs; both get worse with more cores. Nothing here is simulated: the
// failure is ordinary contention.

import (
	"crypto/sha256"
	"encoding/binary"
	"runtime"
	"sync"
	"sync/atomic"
	"time"
)

type session struct {
	key     [32]byte
	created int64
	expires int64
}

type sessionCache interface {
	admit(client string) (*session, error)
	release(s *session)
	size() int
}

func deriveKey(client string, n uint64) [32]byte {
	var buf [64]byte
	copy(buf[:], client)
	binary.LittleEndian.PutUint64(buf[56:], n)
	return sha256.Sum256(buf[:])
}

var admissionSeq atomic.Uint64

// ---- per-connection ----------------------------------------------------------

const shards = 256

type sessionShards struct {
	shard [shards]struct {
		sync.Mutex
		m map[[32]byte]*session
	}
	n atomic.Int64
}

func newSessionShards() *sessionShards {
	c := &sessionShards{}
	for i := range c.shard {
		c.shard[i].m = map[[32]byte]*session{}
	}
	return c
}

func (c *sessionShards) admit(client string) (*session, error) {
	now := time.Now().UnixNano()
	s := &session{key: deriveKey(client, admissionSeq.Add(1)), created: now, expires: now + int64(sessionTTL)}
	sh := &c.shard[s.key[0]]
	sh.Lock()
	sh.m[s.key] = s
	sh.Unlock()
	c.n.Add(1)
	return s, nil
}

func (c *sessionShards) release(s *session) {
	sh := &c.shard[s.key[0]]
	sh.Lock()
	delete(sh.m, s.key)
	sh.Unlock()
	c.n.Add(-1)
}

func (c *sessionShards) size() int { return int(c.n.Load()) }

// ---- shared ------------------------------------------------------------------

type fastLock struct{ v atomic.Int32 }

func (l *fastLock) lock() {
	for i := 1; !l.v.CompareAndSwap(0, 1); i++ {
		if i%lockPatience == 0 {
			runtime.Gosched()
		}
	}
}

func (l *fastLock) unlock() { l.v.Store(0) }

type sessionDirectory struct {
	mu      fastLock
	entries []*session
	n       atomic.Int64
}

func newSessionDirectory() *sessionDirectory { return &sessionDirectory{} }

// reconcile walks the whole table: expired entries are dropped and every live
// entry is checked as a resume candidate for the incoming client.
func (c *sessionDirectory) reconcile(key [32]byte, now int64) bool {
	resumable := false
	live := c.entries[:0]
	for _, e := range c.entries {
		if e.expires < now {
			continue
		}
		if resumes(e.key, key) {
			resumable = true
		}
		live = append(live, e)
	}
	for i := len(live); i < len(c.entries); i++ {
		c.entries[i] = nil
	}
	c.entries = live
	return resumable
}

// resumes verifies the incoming key against a stored ticket, a few hash
// rounds like a ticket MAC check.
func resumes(stored, incoming [32]byte) bool {
	var probe [64]byte
	copy(probe[:32], stored[:])
	copy(probe[32:], incoming[:])
	h := sha256.Sum256(probe[:])
	for i := 1; i < ticketRounds; i++ {
		copy(probe[:32], h[:])
		h = sha256.Sum256(probe[:])
	}
	return h[0] == 0 && h[1] == 0
}

func (c *sessionDirectory) admit(client string) (*session, error) {
	key := deriveKey(client, admissionSeq.Add(1))
	c.mu.lock()
	now := time.Now().UnixNano()
	c.reconcile(key, now)
	s := &session{key: key, created: now, expires: now + int64(sessionTTL)}
	c.entries = append(c.entries, s)
	c.n.Store(int64(len(c.entries)))
	c.mu.unlock()
	return s, nil
}

// release marks the session expired; the next reconcile drops it.
func (c *sessionDirectory) release(s *session) {
	c.mu.lock()
	s.expires = 0
	c.mu.unlock()
}

func (c *sessionDirectory) size() int { return int(c.n.Load()) }
