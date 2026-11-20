package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

var client = &http.Client{Timeout: 5 * time.Second}

type pending struct {
	order, key string
	cents      int
	revision   int64
	attempts   int
}

func send(ctx context.Context, endpoint string, item pending) (string, error) {
	body, _ := json.Marshal(map[string]any{"order_id": item.order, "cents": item.cents, "revision": item.revision})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint+"/refunds", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", item.key)
	response, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("provider status %d", response.StatusCode)
	}
	var result struct {
		ID    string `json:"id"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&result); err != nil {
		return "", err
	}
	if result.ID == "" {
		return "", fmt.Errorf("provider result has no id")
	}
	return result.ID, nil
}

func cycle(ctx context.Context, db *sql.DB, endpoint string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO shop.refund_outbox(order_id,cents,revision,key)
		SELECT r.order_id,r.cents,o.last_version,'refund:'||o.last_version::text
		FROM shop.refunds r JOIN shop.orders o ON o.id=r.order_id
		ON CONFLICT(order_id) DO NOTHING`)
	if err != nil {
		return err
	}
	rows, err := db.QueryContext(ctx, `SELECT order_id,cents,revision,key,attempts
		FROM shop.refund_outbox WHERE provider_id IS NULL AND next_attempt<=now()
		ORDER BY next_attempt LIMIT 100`)
	if err != nil {
		return err
	}
	var pendingRows []pending
	for rows.Next() {
		var p pending
		if err = rows.Scan(&p.order, &p.cents, &p.revision, &p.key, &p.attempts); err != nil {
			break
		}
		pendingRows = append(pendingRows, p)
	}
	rows.Close()
	if err != nil {
		return err
	}
	for _, p := range pendingRows {
		id, requestErr := send(ctx, endpoint, p)
		if requestErr == nil {
			_, err = db.ExecContext(ctx, `UPDATE shop.refund_outbox SET provider_id=$2 WHERE order_id=$1`, p.order, id)
		} else {
			delay := time.Duration(1<<min(p.attempts, 5)) * time.Second
			_, err = db.ExecContext(ctx, `UPDATE shop.refund_outbox SET attempts=attempts+1,
				next_attempt=now()+$2::interval WHERE order_id=$1`, p.order, fmt.Sprintf("%d seconds", int(delay.Seconds())))
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	db, err := sql.Open("pgx", os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	endpoint := os.Getenv("PAYMENT_URL")
	if endpoint == "" {
		log.Fatal("PAYMENT_URL required")
	}
	go func() {
		http.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })
		log.Fatal(http.ListenAndServe(":8080", nil))
	}()
	for ctx.Err() == nil {
		if err := cycle(ctx, db, endpoint); err != nil && ctx.Err() == nil {
			log.Printf("refund dispatch: %v", err)
		}
		time.Sleep(200 * time.Millisecond)
	}
}
