// Runner-only offline storage preparation. Bolt's live buckets are retained.
// This creates actual fragmented pages through transactions, not a latency flag.
package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	bolt "go.etcd.io/bbolt"
)

func main() {
	path := flag.String("path", "", "offline Bolt database")
	mib := flag.Int("mib", 256, "value MiB to allocate before deleting alternate records")
	flag.Parse()
	if *path == "" || *mib < 16 || *mib > 4096 {
		panic("path required; mib must be 16..4096")
	}
	db, err := bolt.Open(*path, 0600, &bolt.Options{Timeout: time.Second, FreelistType: bolt.FreelistArrayType})
	must(err)
	defer db.Close()
	start := time.Now()
	name := []byte("placement-history")
	must(db.Update(func(tx *bolt.Tx) error { _, e := tx.CreateBucket(name); return e }))
	count := *mib * 256
	value := make([]byte, 4096)
	for offset := 0; offset < count; offset += 1024 {
		must(db.Update(func(tx *bolt.Tx) error {
			b := tx.Bucket(name)
			for i := offset; i < offset+1024 && i < count; i++ {
				key := make([]byte, 8)
				binary.BigEndian.PutUint64(key, uint64(i))
				if err := b.Put(key, value); err != nil {
					return err
				}
			}
			return nil
		}))
	}
	// Leave interleaved live records: coalescing contiguous free extents alone
	// cannot undo the layout. A real rewrite/rebuild changes the file layout.
	for offset := 0; offset < count; offset += 1024 {
		must(db.Update(func(tx *bolt.Tx) error {
			b := tx.Bucket(name)
			for i := offset; i < offset+1024 && i < count; i += 2 {
				key := make([]byte, 8)
				binary.BigEndian.PutUint64(key, uint64(i))
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
