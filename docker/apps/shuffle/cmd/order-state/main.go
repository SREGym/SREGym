package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/twmb/franz-go/pkg/kgo"
	"log"
	"net/http"
	"os"
	"os/signal"
	"shuffle/shared"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

var remote = &http.Client{Timeout: 5 * time.Second}

type admission struct {
	Receipt  string
	Eligible bool
	State    string
}

func apply(ctx context.Context, pool *sql.DB, availability *sql.Stmt, decision *admission, e shared.Event) error {
	tx, err := pool.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var id string
	err = tx.QueryRowContext(ctx, "INSERT INTO shop.applied_events VALUES ($1) ON CONFLICT DO NOTHING RETURNING id", e.ID).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return tx.Commit()
	}
	if err != nil {
		return err
	}
	if e.Kind == "create" && decision.State == "" {
		var capacity, reserved int
		statement := tx.StmtContext(ctx, availability)
		defer statement.Close()
		if err = statement.QueryRowContext(ctx, e.SKU).Scan(&capacity, &reserved); err != nil {
			return err
		}
		body, _ := json.Marshal(e)
		req, requestErr := http.NewRequestWithContext(ctx, "POST", os.Getenv("WAREHOUSE_URL")+"/eligibility", bytes.NewReader(body))
		if requestErr != nil {
			return requestErr
		}
		resp, requestErr := remote.Do(req)
		if requestErr != nil {
			return requestErr
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			return fmt.Errorf("warehouse response %d", resp.StatusCode)
		}
		if err = json.NewDecoder(resp.Body).Decode(decision); err != nil {
			return err
		}
		if decision.Receipt == "" {
			return fmt.Errorf("missing receipt")
		}
		decision.State = "rejected"
		if decision.Eligible && reserved < capacity {
			decision.State = "reserved"
		}
	}

	if e.Kind == "create" {
		_, err = tx.ExecContext(ctx, `INSERT INTO shop.orders VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (id) DO UPDATE SET sku=EXCLUDED.sku,cents=EXCLUDED.cents,receipt=EXCLUDED.receipt,
            state=CASE WHEN EXCLUDED.last_version>shop.orders.last_version THEN EXCLUDED.state ELSE shop.orders.state END,
            last_version=GREATEST(shop.orders.last_version,EXCLUDED.last_version)`,
			e.Order, e.SKU, e.Cents, decision.State, decision.Receipt, e.Version)
	} else {
		_, err = tx.ExecContext(ctx, `INSERT INTO shop.orders VALUES ($1,'',0,'cancelled','',$2)
            ON CONFLICT (id) DO UPDATE SET state='cancelled',last_version=EXCLUDED.last_version
            WHERE shop.orders.last_version<EXCLUDED.last_version`, e.Order, e.Version)
	}
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO shop.refunds SELECT id,cents FROM shop.orders
		WHERE id=$1 AND state!='reserved' AND cents>0 ON CONFLICT DO NOTHING`, e.Order)
	if err != nil {
		return err
	}
	return tx.Commit()
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	pool, err := sql.Open("pgx", os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	pool.SetMaxOpenConns(4)
	defer pool.Close()
	availability, err := pool.PrepareContext(ctx, "SELECT capacity,(SELECT count(*) FROM shop.orders WHERE sku=$1 AND state='reserved') FROM shop.skus WHERE id=$1")
	if err != nil {
		log.Fatal(err)
	}
	defer availability.Close()
	c, err := kgo.NewClient(kgo.SeedBrokers(strings.Split(shared.Env("KAFKA_ADDR", "kafka:9092"), ",")...),
		kgo.MetadataMinAge(100*time.Millisecond), kgo.MetadataMaxAge(time.Second),
		kgo.ConsumeTopics(shared.Topic), kgo.ConsumerGroup("order-state"), kgo.DisableAutoCommit(), kgo.BlockRebalanceOnPoll())
	if err != nil {
		log.Fatal(err)
	}
	defer c.CloseAllowingRebalance()
	var processed, invalid atomic.Int64
	go func() {
		for range time.Tick(time.Minute) {
			log.Printf("processed=%d invalid=%d", processed.Load(), invalid.Load())
		}
	}()
	go func() {
		http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
		log.Fatal(http.ListenAndServe(":8080", nil))
	}()
	for ctx.Err() == nil {
		fetches := c.PollRecords(ctx, 16)
		if len(fetches.Records()) == 0 {
			time.Sleep(time.Second)
			c.AllowRebalance()
			continue
		}
		var batch sync.WaitGroup
		slots := make(chan struct{}, 2)
		for _, record := range fetches.Records() {
			slots <- struct{}{}
			batch.Add(1)
			go func(record *kgo.Record) {
				defer batch.Done()
				defer func() { <-slots }()
				var signature string
				for _, header := range record.Headers {
					if header.Key == "signature" {
						signature = string(header.Value)
					}
				}
				var event shared.Event
				valid := hmac.Equal([]byte(signature), []byte(shared.Signature(record.Value))) && json.Unmarshal(record.Value, &event) == nil
				if valid && (event.Kind == "create" || event.Kind == "cancel") {
					var decision admission
					for attempt := 0; ; attempt++ {
						if ctx.Err() != nil {
							return
						}
						err := apply(ctx, pool, availability, &decision, event)
						if err == nil {
							break
						}
						var pgErr *pgconn.PgError
						delay := 100 * time.Millisecond
						if errors.As(err, &pgErr) && (pgErr.Code == "40001" || pgErr.Code == "40P01") {
							delay = time.Duration(2+attempt%25) * time.Millisecond
						}
						time.Sleep(delay)
					}
					processed.Add(1)
				} else {
					invalid.Add(1)
				}
			}(record)
		}
		batch.Wait()
		for ctx.Err() == nil && c.CommitRecords(ctx, fetches.Records()...) != nil {
			time.Sleep(time.Second)
		}
		c.AllowRebalance()
	}
}
