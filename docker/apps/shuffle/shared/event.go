package shared

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"time"
)

const Topic = "order-events"
const CommandTopic = "order-commands"

type Event struct {
	ID, Order, SKU, Kind string
	Cents                int
	Version              int64
}

func Env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func ID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(value[:])
}

func Signature(body []byte) string {
	mac := hmac.New(sha256.New, []byte(os.Getenv("EVENT_KEY")))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

func Handler(kind string, prepare func(*Event) error, send func(Event, []byte) error) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		var input struct{ SKU, Order string }
		if r.Method != "POST" || json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&input) != nil {
			http.Error(w, "invalid request", 400)
			return
		}
		e := Event{ID: ID(), Order: input.Order, SKU: input.SKU, Kind: kind}
		if kind == "create" {
			e.Order = "ord-" + ID()
			e.Cents = 100
		}
		if (kind == "create" && e.SKU == "") || e.Order == "" {
			http.Error(w, "missing identifier", 400)
			return
		}
		if prepare != nil {
			if err := prepare(&e); err != nil {
				http.Error(w, "temporarily unavailable", 503)
				return
			}
		}
		var wire any = e
		if kind == "cancel" {
			wire = map[string]any{"specversion": "1.0", "id": e.ID, "source": "urn:shop:cancel",
				"type": "shop.order.cancelled", "datacontenttype": "application/json", "data": e}
		}
		body, _ := json.Marshal(wire)
		if err := send(e, body); err != nil {
			http.Error(w, "temporarily unavailable", 503)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(struct {
			ID, Order, SKU, Kind string
			Cents                int
		}{e.ID, e.Order, e.SKU, e.Kind, e.Cents})
	})
	return mux
}

func Serve(kind string, prepare func(*Event) error, send func(Event, []byte) error) {
	log.Fatal((&http.Server{Addr: ":8080", Handler: Handler(kind, prepare, send), ReadHeaderTimeout: 5 * time.Second}).ListenAndServe())
}
