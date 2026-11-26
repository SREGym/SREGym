// world is everything outside Larkbank that its services touch, and the
// record of what really happened:
//
//   - the payment rail: it takes the payments the scheduler sends, and a
//     payment it has taken is with the payee's bank. Larkbank reaches it over
//     its API only.
//   - Larkbank's customers: each looks at their payees and standing orders in
//     the app now and then. A customer who finds something they set up is no
//     longer there may set it up again; some add a new payee and standing
//     order, and some cancel a standing order.
//
// It keeps what each customer set up, saw and did, which is what the repair is
// judged against. That record is served on the loopback only.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	mrand "math/rand"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type payee struct {
	ID            string `json:"id,omitempty"`
	Name          string `json:"name"`
	SortCode      string `json:"sort_code"`
	AccountNumber string `json:"account_number"`
}

type order struct {
	ID        string `json:"id,omitempty"`
	PayeeID   string `json:"payee_id,omitempty"`
	Payee     int    `json:"payee"` // in the spec: which of the customer's payees
	Amount    int64  `json:"amount"`
	Reference string `json:"reference"`
}

// customer is one customer as the spec gives them.
type customer struct {
	ID     string  `json:"id"`
	Period float64 `json:"period"` // seconds between looks at the app
	Payees []payee `json:"payees"`
	Orders []order `json:"orders"`
	// Redo: what the customer does on finding something gone. "": nothing;
	// "hasty": sets it up again at once; "patient": looks again a while later first.
	Redo string `json:"redo"`
	// AddAt, CancelAt: seconds after the "scale" mark at which the customer adds
	// a new payee and standing order, or cancels one of theirs (-1: never).
	AddAt     float64 `json:"add_at"`
	Add       *order  `json:"add"`
	AddPayee  *payee  `json:"add_payee"`
	CancelAt  float64 `json:"cancel_at"`
	CancelIdx int     `json:"cancel"`
}

// item is one thing a customer has set up, as the customer knows it: by what
// it says, not by an id.
type item struct {
	Kind      string   `json:"kind"` // "payee" or "standing_order"
	Key       string   `json:"key"`
	PayeeKey  string   `json:"payee_key,omitempty"`
	Origin    string   `json:"origin"`            // "before": from before the incident; "new": added since
	MadeAt    float64  `json:"made_at,omitempty"` // when the customer added it, if since the incident began
	Cancelled float64  `json:"cancelled,omitempty"`
	Redone    float64  `json:"redone,omitempty"`   // when the customer set it up again
	MadeIDs   []string `json:"made_ids,omitempty"` // ids the customer was given for it since
	TwiceAt   float64  `json:"twice_at,omitempty"` // when the customer first saw it twice
	payee     payee
	order     order
}

type state struct {
	items     []*item
	recheckAt float64
	newPayee  *item // the payee a customer is adding, once it is made
	added     bool
	cancelled bool
}

type world struct {
	api, key string
	spec     []customer
	state    []*state
	client   *http.Client
	started  time.Time

	phase    atomic.Value // "idle", "live" or "stop"
	inFlight atomic.Int64

	mu       sync.Mutex
	marks    map[string]float64
	reads    [][]any // [customer index, at, payees status, standing orders status]
	did      []map[string]any
	payments []map[string]any
	paid     map[string]bool
}

func now() float64 { return float64(time.Now().UnixMicro()) / 1e6 }

func payeeKey(p payee) string { return p.SortCode + "|" + p.AccountNumber }
func orderKey(p payee, o order) string {
	return fmt.Sprintf("%s|%d|%s", payeeKey(p), o.Amount, o.Reference)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// ---- the payment rail ---------------------------------------------------------

func (w *world) takePayment(rw http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Bearer "+w.key {
		writeJSON(rw, 401, map[string]string{"error": "missing or wrong API key (Authorization: Bearer <key>)"})
		return
	}
	var in map[string]any
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil || in["payment_id"] == nil || in["account_number"] == nil || in["amount"] == nil {
		writeJSON(rw, 400, map[string]string{"error": "want payment_id, sort_code, account_number, amount and reference"})
		return
	}
	id := fmt.Sprint(in["payment_id"])
	w.mu.Lock()
	again := w.paid[id]
	if !again {
		w.paid[id] = true
		in["at"] = now()
		w.payments = append(w.payments, in)
	}
	w.mu.Unlock()
	writeJSON(rw, 200, map[string]any{"payment_id": id, "status": "sent", "sent_before": again})
}

func railIndex(rw http.ResponseWriter, r *http.Request) {
	writeJSON(rw, 200, map[string]any{"service": "Payment rail", "amounts": "in pence", "authorization": "Authorization: Bearer <API key>", "routes": []string{
		`POST /v1/payments {"payment_id": "...", "sort_code": "...", "account_number": "...", "amount": n, "reference": "..."}  (sent at once; a payment id sent before is not sent again; a payment sent cannot be recalled)`,
	}})
}

// ---- customers ------------------------------------------------------------------

func (w *world) call(method, url string, body, out any) int {
	var reader io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
	}
	req, _ := http.NewRequest(method, url, reader)
	req.Header.Set("Content-Type", "application/json")
	resp, err := w.client.Do(req)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if out != nil {
		json.Unmarshal(raw, out)
	}
	return resp.StatusCode
}

func (w *world) begin() bool {
	if w.phase.Load() != "live" {
		return false
	}
	w.inFlight.Add(1)
	if w.phase.Load() != "live" {
		w.inFlight.Add(-1)
		return false
	}
	return true
}

type view struct {
	Payees []payee `json:"payees"`
	Orders []order `json:"standing_orders"`
}

// look is the customer opening the app: their payees and standing orders.
func (w *world) look(id string) (int, int, view) {
	var v view
	a := w.call("GET", w.api+"/v1/customers/"+id+"/payees", nil, &v)
	b := w.call("GET", w.api+"/v1/customers/"+id+"/standing-orders", nil, &v)
	return a, b, v
}

func (w *world) note(i int, what string, more map[string]any) {
	entry := map[string]any{"customer": w.spec[i].ID, "at": now(), "did": what}
	for k, v := range more {
		entry[k] = v
	}
	w.mu.Lock()
	w.did = append(w.did, entry)
	w.mu.Unlock()
}

// seen counts, for each thing the customer has set up, how many times the app shows it.
func seen(v view) (map[string][]string, map[string][]string, map[string]payee) {
	byID := map[string]payee{}
	payees, orders := map[string][]string{}, map[string][]string{}
	for _, p := range v.Payees {
		byID[p.ID] = p
		payees[payeeKey(p)] = append(payees[payeeKey(p)], p.ID)
	}
	for _, o := range v.Orders {
		if p, ok := byID[o.PayeeID]; ok {
			orders[orderKey(p, o)] = append(orders[orderKey(p, o)], o.ID)
		}
	}
	return payees, orders, byID
}

func (w *world) addPayee(i int, it *item) (string, bool) {
	var made payee
	if w.call("POST", w.api+"/v1/customers/"+w.spec[i].ID+"/payees", it.payee, &made) != 201 || made.ID == "" {
		return "", false
	}
	w.mu.Lock()
	it.MadeIDs = append(it.MadeIDs, made.ID)
	w.mu.Unlock()
	return made.ID, true
}

func (w *world) addOrder(i int, it *item, payeeID string) bool {
	var made order
	body := map[string]any{"payee_id": payeeID, "amount": it.order.Amount, "reference": it.order.Reference}
	if w.call("POST", w.api+"/v1/customers/"+w.spec[i].ID+"/standing-orders", body, &made) != 201 || made.ID == "" {
		return false
	}
	w.mu.Lock()
	it.MadeIDs = append(it.MadeIDs, made.ID)
	w.mu.Unlock()
	return true
}

// useApp is one customer, for as long as the world runs.
func (w *world) useApp(i int, rng *mrand.Rand) {
	c, st := w.spec[i], w.state[i]
	time.Sleep(time.Duration(rng.Float64() * c.Period * float64(time.Second)))
	for {
		if !w.begin() {
			time.Sleep(200 * time.Millisecond)
			continue
		}
		a, b, v := w.look(c.ID)
		at := now()
		w.mu.Lock()
		w.reads = append(w.reads, []any{i, at, a, b})
		scale, scaled := w.marks["scale"]
		w.mu.Unlock()
		if a == 200 && b == 200 {
			payees, orders, _ := seen(v)
			missing := false
			w.mu.Lock()
			for _, it := range st.items {
				if it.Cancelled > 0 {
					continue
				}
				n := len(payees[it.Key])
				if it.Kind == "standing_order" {
					n = len(orders[it.Key])
				}
				if n >= 2 && it.TwiceAt == 0 {
					it.TwiceAt = at
				}
				if n == 0 && it.Redone == 0 {
					missing = true
				}
			}
			w.mu.Unlock()
			switch {
			case !missing:
				st.recheckAt = 0
			case c.Redo == "patient" && st.recheckAt == 0:
				// Odd. Look again in a while before doing anything about it.
				st.recheckAt = at + 20 + 70*rng.Float64()
			case c.Redo == "hasty" || (c.Redo == "patient" && at >= st.recheckAt):
				w.redo(i, st, payees, orders)
			}
			if scaled && c.AddAt >= 0 && !st.added && at >= scale+c.AddAt {
				w.addNew(i, st, at, payees, orders)
			}
			if scaled && c.CancelAt >= 0 && !st.cancelled && at >= scale+c.CancelAt {
				// A customer can cancel only what the app shows them, and this one sees it once.
				for _, it := range st.items {
					if it.Kind != "standing_order" || it.Origin != "before" || it.order.Payee != c.Orders[c.CancelIdx].Payee || it.order.Reference != c.Orders[c.CancelIdx].Reference {
						continue
					}
					if ids := orders[it.Key]; len(ids) == 1 && it.Redone == 0 {
						if w.call("DELETE", w.api+"/v1/customers/"+c.ID+"/standing-orders/"+ids[0], nil, nil) == 204 {
							w.mu.Lock()
							it.Cancelled = now()
							w.mu.Unlock()
							st.cancelled = true
							w.note(i, "cancelled a standing order", map[string]any{"standing_order": it.Key, "id": ids[0]})
						}
					}
				}
			}
		}
		w.inFlight.Add(-1)
		time.Sleep(time.Duration(c.Period * (0.7 + 0.6*rng.Float64()) * float64(time.Second)))
	}
}

// addNew is the customer adding a new payee and a standing order to it. A step
// that fails is tried again at the next look, unless the app shows it was made
// after all (a request can fail after the write).
func (w *world) addNew(i int, st *state, at float64, payees, orders map[string][]string) {
	c := w.spec[i]
	if st.newPayee == nil {
		it := &item{Kind: "payee", Key: payeeKey(*c.AddPayee), Origin: "new", MadeAt: at, payee: *c.AddPayee}
		if ids := payees[it.Key]; len(ids) > 0 {
			it.MadeIDs = append(it.MadeIDs, ids[0])
		} else if _, ok := w.addPayee(i, it); !ok {
			return
		}
		w.mu.Lock()
		st.items = append(st.items, it)
		w.mu.Unlock()
		st.newPayee = it
	}
	so := &item{Kind: "standing_order", Key: orderKey(*c.AddPayee, *c.Add), PayeeKey: st.newPayee.Key, Origin: "new", MadeAt: at, payee: *c.AddPayee, order: *c.Add}
	if ids := orders[so.Key]; len(ids) > 0 {
		so.MadeIDs = append(so.MadeIDs, ids[0])
	} else if !w.addOrder(i, so, st.newPayee.MadeIDs[0]) {
		return
	}
	w.mu.Lock()
	st.items = append(st.items, so)
	w.mu.Unlock()
	st.added = true
	w.note(i, "added a payee and a standing order", map[string]any{"payee": so.PayeeKey})
}

// redo is the customer setting up again what the app no longer shows: the
// same payee, the same standing order. Each thing once.
func (w *world) redo(i int, st *state, payees, orders map[string][]string) {
	made := map[string]string{}
	for _, it := range st.items {
		if it.Kind != "payee" || it.Cancelled > 0 || it.Redone > 0 || len(payees[it.Key]) > 0 {
			continue
		}
		if id, ok := w.addPayee(i, it); ok {
			made[it.Key] = id
			w.mu.Lock()
			it.Redone = now()
			w.mu.Unlock()
			w.note(i, "set a payee up again", map[string]any{"payee": it.Key, "id": id})
		}
	}
	for _, it := range st.items {
		if it.Kind != "standing_order" || it.Cancelled > 0 || it.Redone > 0 || len(orders[it.Key]) > 0 {
			continue
		}
		payeeID := made[it.PayeeKey]
		if ids := payees[it.PayeeKey]; payeeID == "" && len(ids) > 0 {
			payeeID = ids[0]
		}
		if payeeID == "" {
			continue
		}
		if w.addOrder(i, it, payeeID) {
			w.mu.Lock()
			it.Redone = now()
			w.mu.Unlock()
			w.note(i, "set a standing order up again", map[string]any{"standing_order": it.Key})
		}
	}
}

// ---- the record, and the stage directions ----------------------------------------

func (w *world) control() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /clock", func(rw http.ResponseWriter, r *http.Request) {
		writeJSON(rw, 200, map[string]any{"now": now(), "phase": w.phase.Load()})
	})
	mux.HandleFunc("POST /phase", func(rw http.ResponseWriter, r *http.Request) {
		name := r.URL.Query().Get("name")
		if name != "live" && name != "idle" && name != "stop" {
			writeJSON(rw, 400, map[string]string{"error": "name: live, idle or stop"})
			return
		}
		w.phase.Store(name)
		if name != "live" {
			for deadline := time.Now().Add(60 * time.Second); w.inFlight.Load() > 0 && time.Now().Before(deadline); {
				time.Sleep(50 * time.Millisecond)
			}
		}
		writeJSON(rw, 200, map[string]any{"phase": name, "now": now(), "in_flight": w.inFlight.Load()})
	})
	mux.HandleFunc("POST /mark", func(rw http.ResponseWriter, r *http.Request) {
		at := now()
		w.mu.Lock()
		w.marks[r.URL.Query().Get("name")] = at
		w.mu.Unlock()
		writeJSON(rw, 200, map[string]any{"now": at})
	})
	// What every customer's app shows right now.
	mux.HandleFunc("GET /final", func(rw http.ResponseWriter, r *http.Request) {
		out := make([]map[string]any, len(w.spec))
		var wg sync.WaitGroup
		slots := make(chan struct{}, 16)
		for i, c := range w.spec {
			wg.Add(1)
			go func() {
				defer wg.Done()
				slots <- struct{}{}
				defer func() { <-slots }()
				for try := 0; try < 3; try++ {
					a, b, v := w.look(c.ID)
					out[i] = map[string]any{"customer": c.ID, "payees_status": a, "standing_orders_status": b, "payees": v.Payees, "standing_orders": v.Orders}
					if a == 200 && b == 200 {
						return
					}
					time.Sleep(time.Second)
				}
			}()
		}
		wg.Wait()
		writeJSON(rw, 200, out)
	})
	mux.HandleFunc("GET /truth", func(rw http.ResponseWriter, r *http.Request) {
		w.mu.Lock()
		defer w.mu.Unlock()
		customers := make([]map[string]any, len(w.spec))
		for i, c := range w.spec {
			customers[i] = map[string]any{"id": c.ID, "redo": c.Redo, "items": w.state[i].items}
		}
		writeJSON(rw, 200, map[string]any{"now": now(), "marks": w.marks, "customers": customers, "reads": w.reads, "did": w.did, "payments": w.payments})
	})
	mux.HandleFunc("GET /summary", func(rw http.ResponseWriter, r *http.Request) {
		w.mu.Lock()
		defer w.mu.Unlock()
		counts := map[string]int{}
		for _, d := range w.did {
			counts[fmt.Sprint(d["did"])]++
		}
		codes := map[string]int{}
		for _, read := range w.reads {
			codes[fmt.Sprintf("%v/%v", read[2], read[3])]++
		}
		twice := 0
		for _, st := range w.state {
			for _, it := range st.items {
				if it.TwiceAt > 0 {
					twice++
				}
			}
		}
		writeJSON(rw, 200, map[string]any{"did": counts, "reads": codes, "payments": len(w.payments), "seen_twice": twice, "marks": w.marks})
	})
	return mux
}

func main() {
	spec := flag.String("spec", "/config/world.json", "the customers, what each has set up and what each does")
	api := flag.String("api", "", "the customer API's URL")
	listen := flag.String("listen", ":8080", "where the payment rail's API listens")
	status := flag.String("status", "127.0.0.1:8091", "where the record is served")
	seed := flag.Int64("seed", 7, "seed for when customers do things")
	flag.Parse()

	raw, err := os.ReadFile(*spec)
	if err != nil {
		log.Fatal(err)
	}
	w := &world{api: strings.TrimRight(*api, "/"), key: os.Getenv("RAIL_KEY"), marks: map[string]float64{}, paid: map[string]bool{}, started: time.Now(),
		client: &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{MaxIdleConnsPerHost: 64, MaxConnsPerHost: 64}}}
	if err := json.Unmarshal(raw, &w.spec); err != nil {
		log.Fatal(err)
	}
	sort.SliceStable(w.spec, func(i, j int) bool { return w.spec[i].ID < w.spec[j].ID })
	w.phase.Store("idle")
	for i, c := range w.spec {
		st := &state{}
		for _, p := range c.Payees {
			st.items = append(st.items, &item{Kind: "payee", Key: payeeKey(p), Origin: "before", payee: p})
		}
		for _, o := range c.Orders {
			p := c.Payees[o.Payee]
			st.items = append(st.items, &item{Kind: "standing_order", Key: orderKey(p, o), PayeeKey: payeeKey(p), Origin: "before", payee: p, order: o})
		}
		w.state = append(w.state, st)
		go w.useApp(i, mrand.New(mrand.NewSource(*seed*1000+int64(i))))
	}

	rail := http.NewServeMux()
	rail.HandleFunc("GET /healthz", func(rw http.ResponseWriter, r *http.Request) { rw.Write([]byte("ok\n")) })
	rail.HandleFunc("GET /{$}", railIndex)
	rail.HandleFunc("POST /v1/payments", w.takePayment)
	go func() { log.Fatal(http.ListenAndServe(*status, w.control())) }()
	log.Printf("%d customers; the payment rail on %s", len(w.spec), *listen)
	log.Fatal(http.ListenAndServe(*listen, rail))
}
