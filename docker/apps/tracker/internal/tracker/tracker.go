// Package tracker holds what Tracker's programs share: the database
// connection and the JSON helpers.
package tracker

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Connect opens a pool on the database named by the environment variable,
// retrying until the database answers.
func Connect(ctx context.Context, envVar string, maxConns int32) *pgxpool.Pool {
	dsn := os.Getenv(envVar)
	if dsn == "" {
		log.Fatalf("%s is not set", envVar)
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		log.Fatalf("%s: %v", envVar, err)
	}
	cfg.MaxConns = maxConns
	for {
		pool, err := pgxpool.NewWithConfig(ctx, cfg)
		if err == nil {
			if err = pool.Ping(ctx); err == nil {
				return pool
			}
			pool.Close()
		}
		log.Printf("waiting for the database: %v", err)
		time.Sleep(2 * time.Second)
	}
}

// WriteJSON writes v as the response body.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("writing a response: %v", err)
	}
}

// Fail writes an error body.
func Fail(w http.ResponseWriter, status int, format string, args ...any) {
	WriteJSON(w, status, map[string]string{"error": fmt.Sprintf(format, args...)})
}
