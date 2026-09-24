// Runner-only offline storage preparation. Bolt's native Raft buckets are
// retained; temporary log keys are deleted before Consul restarts. This creates
// real free pages through transactions, not a latency flag or a visible bucket.
package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"time"

	bolt "go.etcd.io/bbolt"
)

func main() {
	path := flag.String("path", "", "offline Bolt database")
	mib := flag.Int("mib", 256, "temporary Raft log MiB to allocate and then delete")
	statsOnly := flag.Bool("stats-only", false, "report page statistics without changing the database")
	flag.Parse()
	if *path == "" || *mib < 16 || *mib > 4096 {
		panic("path required; mib must be 16..4096")
	}
	db, err := bolt.Open(*path, 0600, &bolt.Options{Timeout: time.Second, FreelistType: bolt.FreelistArrayType})
	must(err)
	defer db.Close()
	start := time.Now()
	if *statsOnly {
		info, err := os.Stat(*path)
		must(err)
		must(json.NewEncoder(os.Stdout).Encode(map[string]interface{}{
			"file_bytes": info.Size(), "stats": db.Stats(),
		}))
		return
	}
	name := []byte("logs")
	must(db.View(func(tx *bolt.Tx) error {
		if tx.Bucket(name) == nil {
			return fmt.Errorf("native Raft logs bucket is missing")
		}
		return nil
	}))
	count := *mib * 256
	value := make([]byte, 4096)
	for offset := 0; offset < count; offset += 1024 {
		must(db.Update(func(tx *bolt.Tx) error {
			b := tx.Bucket(name)
			for i := offset; i < offset+1024 && i < count; i++ {
				key := make([]byte, 8)
				binary.BigEndian.PutUint64(key, math.MaxUint64-uint64(count)+uint64(i))
				if err := b.Put(key, value); err != nil {
					return err
				}
			}
			return nil
		}))
	}
	// Delete every temporary key. The database file and its persisted freelist
	// remain large, while the native Raft log contents stay exactly as they were.
	for offset := 0; offset < count; offset += 1024 {
		must(db.Update(func(tx *bolt.Tx) error {
			b := tx.Bucket(name)
			for i := offset; i < offset+1024 && i < count; i++ {
				key := make([]byte, 8)
				binary.BigEndian.PutUint64(key, math.MaxUint64-uint64(count)+uint64(i))
				if err := b.Delete(key); err != nil {
					return err
				}
			}
			return nil
		}))
	}
	info, err := os.Stat(*path)
	must(err)
	must(json.NewEncoder(os.Stdout).Encode(map[string]interface{}{
		"file_bytes": info.Size(), "seconds": time.Since(start).Seconds(), "stats": db.Stats(),
	}))
}

func must(err error) {
	if err != nil {
		panic(fmt.Sprintf("storage preparation: %v", err))
	}
}
