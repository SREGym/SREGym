// api is Larkbank's customer API: the people a customer pays (payees) and the
// standing orders that pay them. Everything is kept in the Cassandra ring,
// in the customer's partition, and read and written at QUORUM.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"larkbank/internal/larkbank"

	"github.com/gocql/gocql"
)

type server struct {
	ring *gocql.Session
}

var (
	customerID = regexp.MustCompile(`^C[0-9]{4,8}$`)
	sortCode   = regexp.MustCompile(`^[0-9]{2}-[0-9]{2}-[0-9]{2}$`)
	accountNo  = regexp.MustCompile(`^[0-9]{8}$`)
)

func newID(prefix string) string {
	raw := make([]byte, 8)
	rand.Read(raw)
	return prefix + hex.EncodeToString(raw)
}

// unavailable answers for a ring that did not: the customer's app shows an
// error, which is not the same as showing nothing.
func unavailable(w http.ResponseWriter, err error) {
	log.Printf("ring: %v", err)
	larkbank.Fail(w, 503, "the ring did not answer: %v", err)
}

func customer(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("customer")
	if !customerID.MatchString(id) {
		larkbank.Fail(w, 400, "customer: want an id like C1042")
		return "", false
	}
	return id, true
}

func (s *server) payees(id string) ([]larkbank.Payee, error) {
	out := []larkbank.Payee{}
	iter := s.ring.Query(`SELECT payee_id, name, sort_code, account_number, created_at FROM payee WHERE customer_id = ?`, id).Iter()
	var p larkbank.Payee
	for iter.Scan(&p.ID, &p.Name, &p.SortCode, &p.AccountNumber, &p.CreatedAt) {
		out = append(out, p)
	}
	return out, iter.Close()
}

func (s *server) orders(id string) ([]larkbank.StandingOrder, error) {
	out := []larkbank.StandingOrder{}
	iter := s.ring.Query(`SELECT order_id, payee_id, amount, reference, every_seconds, first_due, created_at FROM standing_order WHERE customer_id = ?`, id).Iter()
	var o larkbank.StandingOrder
	for iter.Scan(&o.ID, &o.PayeeID, &o.Amount, &o.Reference, &o.EverySeconds, &o.FirstDue, &o.CreatedAt) {
		out = append(out, o)
	}
	return out, iter.Close()
}

func (s *server) listPayees(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	payees, err := s.payees(id)
	if err != nil {
		unavailable(w, err)
		return
	}
	larkbank.WriteJSON(w, 200, map[string]any{"customer": id, "payees": payees})
}

func (s *server) addPayee(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	var in larkbank.Payee
	if err := larkbank.Decode(r, &in); err != nil || in.Name == "" || !sortCode.MatchString(in.SortCode) || !accountNo.MatchString(in.AccountNumber) {
		larkbank.Fail(w, 400, "want a name, a sort_code like 20-45-77 and an 8-digit account_number")
		return
	}
	in.ID, in.CreatedAt = newID("pe_"), time.Now().UTC().Truncate(time.Millisecond)
	if err := s.ring.Query(`INSERT INTO payee (customer_id, payee_id, name, sort_code, account_number, created_at) VALUES (?, ?, ?, ?, ?, ?)`,
		id, in.ID, in.Name, in.SortCode, in.AccountNumber, in.CreatedAt).Exec(); err != nil {
		unavailable(w, err)
		return
	}
	larkbank.WriteJSON(w, 201, in)
}

func (s *server) deletePayee(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	payee := r.PathValue("id")
	orders, err := s.orders(id)
	if err != nil {
		unavailable(w, err)
		return
	}
	for _, o := range orders {
		if o.PayeeID == payee {
			larkbank.Fail(w, 409, "standing order %s pays this payee: delete it first", o.ID)
			return
		}
	}
	if err := s.ring.Query(`DELETE FROM payee WHERE customer_id = ? AND payee_id = ?`, id, payee).Exec(); err != nil {
		unavailable(w, err)
		return
	}
	w.WriteHeader(204)
}

func (s *server) listOrders(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	orders, err := s.orders(id)
	if err != nil {
		unavailable(w, err)
		return
	}
	larkbank.WriteJSON(w, 200, map[string]any{"customer": id, "standing_orders": orders})
}

func (s *server) addOrder(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	var in larkbank.StandingOrder
	if err := larkbank.Decode(r, &in); err != nil || in.PayeeID == "" || in.Amount <= 0 || in.Reference == "" {
		larkbank.Fail(w, 400, "want a payee_id, an amount in pence and a reference (and, to change the default, every_seconds)")
		return
	}
	var found string
	err := s.ring.Query(`SELECT payee_id FROM payee WHERE customer_id = ? AND payee_id = ?`, id, in.PayeeID).Scan(&found)
	if errors.Is(err, gocql.ErrNotFound) {
		larkbank.Fail(w, 404, "no such payee")
		return
	}
	if err != nil {
		unavailable(w, err)
		return
	}
	if in.EverySeconds <= 0 {
		in.EverySeconds, _ = strconv.Atoi(larkbank.Env("EVERY_SECONDS", "150"))
	}
	// The first payment is due now, and then every period after.
	in.ID, in.CreatedAt = newID("so_"), time.Now().UTC().Truncate(time.Millisecond)
	in.FirstDue = in.CreatedAt
	if err := s.ring.Query(`INSERT INTO standing_order (customer_id, order_id, payee_id, amount, reference, every_seconds, first_due, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		id, in.ID, in.PayeeID, in.Amount, in.Reference, in.EverySeconds, in.FirstDue, in.CreatedAt).Exec(); err != nil {
		unavailable(w, err)
		return
	}
	larkbank.WriteJSON(w, 201, in)
}

func (s *server) deleteOrder(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	if err := s.ring.Query(`DELETE FROM standing_order WHERE customer_id = ? AND order_id = ?`, id, r.PathValue("id")).Exec(); err != nil {
		unavailable(w, err)
		return
	}
	w.WriteHeader(204)
}

func (s *server) listPayments(w http.ResponseWriter, r *http.Request) {
	id, ok := customer(w, r)
	if !ok {
		return
	}
	out := []larkbank.Payment{}
	iter := s.ring.Query(`SELECT order_id, due, payment_id, payee_id, sort_code, account_number, amount, reference, sent_at FROM payment WHERE customer_id = ?`, id).Iter()
	var p larkbank.Payment
	for iter.Scan(&p.OrderID, &p.Due, &p.PaymentID, &p.PayeeID, &p.SortCode, &p.AccountNumber, &p.Amount, &p.Reference, &p.SentAt) {
		out = append(out, p)
	}
	if err := iter.Close(); err != nil {
		unavailable(w, err)
		return
	}
	larkbank.WriteJSON(w, 200, map[string]any{"customer": id, "payments": out})
}

func index(w http.ResponseWriter, r *http.Request) {
	larkbank.WriteJSON(w, 200, map[string]any{"service": "Larkbank customer API", "amounts": "in pence", "routes": []string{
		"GET    /v1/customers/{customer}/payees",
		`POST   /v1/customers/{customer}/payees {"name": "...", "sort_code": "20-45-77", "account_number": "12345678"}`,
		"DELETE /v1/customers/{customer}/payees/{id}  (409 while a standing order pays the payee)",
		"GET    /v1/customers/{customer}/standing-orders",
		`POST   /v1/customers/{customer}/standing-orders {"payee_id": "...", "amount": n, "reference": "..."}  (first payment now, then every period)`,
		"DELETE /v1/customers/{customer}/standing-orders/{id}",
		"GET    /v1/customers/{customer}/payments  (what the customer's standing orders have paid)",
	}})
}

func main() {
	s := &server{ring: larkbank.Connect(larkbank.Env("RING", "ring:9042"))}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	mux.HandleFunc("GET /{$}", index)
	mux.HandleFunc("GET /v1/customers/{customer}/payees", s.listPayees)
	mux.HandleFunc("POST /v1/customers/{customer}/payees", s.addPayee)
	mux.HandleFunc("DELETE /v1/customers/{customer}/payees/{id}", s.deletePayee)
	mux.HandleFunc("GET /v1/customers/{customer}/standing-orders", s.listOrders)
	mux.HandleFunc("POST /v1/customers/{customer}/standing-orders", s.addOrder)
	mux.HandleFunc("DELETE /v1/customers/{customer}/standing-orders/{id}", s.deleteOrder)
	mux.HandleFunc("GET /v1/customers/{customer}/payments", s.listPayments)
	addr := larkbank.Env("LISTEN", ":8080")
	log.Printf("api listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
