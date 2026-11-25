// scheduler pays standing orders. Every few seconds it reads every standing
// order and, for each one whose latest due time has no payment yet, sends the
// payment over the payment rail and records it. A payment the rail has taken
// cannot be taken back.
package main

import (
	"bytes"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"time"

	"larkbank/internal/larkbank"

	"github.com/gocql/gocql"
)

type scheduler struct {
	ring   *gocql.Session
	rail   string
	key    string
	client *http.Client
	sent   int
}

type order struct {
	Customer string
	larkbank.StandingOrder
}

func (s *scheduler) orders() ([]order, error) {
	var out []order
	iter := s.ring.Query(`SELECT customer_id, order_id, payee_id, amount, reference, every_seconds, first_due FROM standing_order`).PageSize(500).Iter()
	var o order
	for iter.Scan(&o.Customer, &o.ID, &o.PayeeID, &o.Amount, &o.Reference, &o.EverySeconds, &o.FirstDue) {
		out = append(out, o)
	}
	return out, iter.Close()
}

// pay sends the payment for an order's latest due time, unless it was sent.
func (s *scheduler) pay(o order, now time.Time) error {
	if o.EverySeconds <= 0 || now.Before(o.FirstDue) {
		return nil
	}
	every := time.Duration(o.EverySeconds) * time.Second
	due := o.FirstDue.Add(now.Sub(o.FirstDue) / every * every)
	var paid string
	err := s.ring.Query(`SELECT payment_id FROM payment WHERE customer_id = ? AND order_id = ? AND due = ?`, o.Customer, o.ID, due).Scan(&paid)
	if err == nil {
		return nil
	}
	if !errors.Is(err, gocql.ErrNotFound) {
		return err
	}
	var payee larkbank.Payee
	err = s.ring.Query(`SELECT name, sort_code, account_number FROM payee WHERE customer_id = ? AND payee_id = ?`, o.Customer, o.PayeeID).
		Scan(&payee.Name, &payee.SortCode, &payee.AccountNumber)
	if errors.Is(err, gocql.ErrNotFound) {
		log.Printf("standing order %s of %s: its payee %s is not there; not paid", o.ID, o.Customer, o.PayeeID)
		return nil
	}
	if err != nil {
		return err
	}
	sum := sha1.Sum([]byte(o.ID + "|" + strconv.FormatInt(due.UnixMilli(), 10)))
	id := "pm_" + hex.EncodeToString(sum[:8])
	body, _ := json.Marshal(map[string]any{"payment_id": id, "customer": o.Customer, "standing_order_id": o.ID, "due": due,
		"name": payee.Name, "sort_code": payee.SortCode, "account_number": payee.AccountNumber, "amount": o.Amount, "reference": o.Reference})
	req, _ := http.NewRequest("POST", s.rail+"/v1/payments", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+s.key)
	resp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return errors.New("the payment rail answered " + resp.Status)
	}
	s.sent++
	return s.ring.Query(`INSERT INTO payment (customer_id, order_id, due, payment_id, payee_id, sort_code, account_number, amount, reference, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		o.Customer, o.ID, due, id, o.PayeeID, payee.SortCode, payee.AccountNumber, o.Amount, o.Reference, time.Now().UTC()).Exec()
}

func main() {
	s := &scheduler{ring: larkbank.Connect(larkbank.Env("RING", "ring:9042")), rail: larkbank.Env("RAIL_URL", "http://payment-rail:8080"),
		key: larkbank.Env("RAIL_KEY", ""), client: &http.Client{Timeout: 10 * time.Second}}
	log.Print("scheduler started")
	last := time.Now()
	for {
		orders, err := s.orders()
		if err != nil {
			log.Printf("cannot read the standing orders: %v", err)
		}
		failed := 0
		for _, o := range orders {
			if err := s.pay(o, time.Now().UTC()); err != nil {
				failed++
				if failed <= 3 {
					log.Printf("standing order %s of %s: %v; will try again", o.ID, o.Customer, err)
				}
			}
		}
		if time.Since(last) > 30*time.Second {
			log.Printf("%d standing orders read, %d payments sent since the start, %d could not be paid this round", len(orders), s.sent, failed)
			last = time.Now()
		}
		time.Sleep(5 * time.Second)
	}
}
