// Package larkbank holds what Larkbank's programs share: the connection to
// the Cassandra ring, the records kept in it, and JSON helpers.
package larkbank

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/gocql/gocql"
)

// Payee is somebody a customer pays.
type Payee struct {
	ID            string    `json:"id"`
	Name          string    `json:"name"`
	SortCode      string    `json:"sort_code"`
	AccountNumber string    `json:"account_number"`
	CreatedAt     time.Time `json:"created_at"`
}

// StandingOrder pays a payee the same amount every EverySeconds, first at
// FirstDue.
type StandingOrder struct {
	ID           string    `json:"id"`
	PayeeID      string    `json:"payee_id"`
	Amount       int64     `json:"amount"`
	Reference    string    `json:"reference"`
	EverySeconds int       `json:"every_seconds"`
	FirstDue     time.Time `json:"first_due"`
	CreatedAt    time.Time `json:"created_at"`
}

// Payment is one payment a standing order made.
type Payment struct {
	OrderID       string    `json:"standing_order_id"`
	Due           time.Time `json:"due"`
	PaymentID     string    `json:"payment_id"`
	PayeeID       string    `json:"payee_id"`
	SortCode      string    `json:"sort_code"`
	AccountNumber string    `json:"account_number"`
	Amount        int64     `json:"amount"`
	Reference     string    `json:"reference"`
	SentAt        time.Time `json:"sent_at"`
}

// Env returns an environment variable, or a default.
func Env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// Connect opens a session on the ring, retrying until it answers. Every read
// and write is made at QUORUM: two of a row's three replicas must answer.
func Connect(hosts string) *gocql.Session {
	cluster := gocql.NewCluster(strings.Split(hosts, ",")...)
	cluster.Keyspace = "bank"
	cluster.Consistency = gocql.Quorum
	cluster.Timeout = 4 * time.Second
	cluster.ConnectTimeout = 4 * time.Second
	cluster.NumConns = 2
	cluster.ReconnectInterval = 2 * time.Second
	cluster.RetryPolicy = &gocql.SimpleRetryPolicy{NumRetries: 2}
	cluster.PoolConfig.HostSelectionPolicy = gocql.TokenAwareHostPolicy(gocql.RoundRobinHostPolicy())
	for {
		session, err := cluster.CreateSession()
		if err == nil {
			return session
		}
		log.Printf("waiting for the ring: %v", err)
		time.Sleep(3 * time.Second)
	}
}

// WriteJSON writes v as the response body.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// Fail writes an error body.
func Fail(w http.ResponseWriter, status int, format string, args ...any) {
	WriteJSON(w, status, map[string]string{"error": fmt.Sprintf(format, args...)})
}

// Decode reads a JSON request body into v.
func Decode(r *http.Request, v any) error {
	defer r.Body.Close()
	return json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(v)
}
