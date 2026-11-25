// repair removes what customers set up a second time while their data could
// not be seen: the reference repair of the data, and the variants the grader
// is checked against. It works through the customer API, as staff would.
//
// A customer whose payees had vanished from the app set them up again, with
// the same details and a new id. Once the old data can be read again the
// customer has each of them twice, and two standing orders paying the same
// payee the same amount.
//
//	-keep=oldest   of each such pair, keep the one made first (the default)
//	-keep=newest   keep the one made last
//	-payees=false  leave the payees alone; only standing orders are put right
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"sort"
	"time"

	"larkbank/internal/larkbank"
)

type tool struct {
	api    string
	client *http.Client
}

func (t *tool) call(method, path string, out any) int {
	for try := 0; ; try++ {
		req, _ := http.NewRequest(method, t.api+path, bytes.NewReader(nil))
		resp, err := t.client.Do(req)
		status := 0
		if err == nil {
			raw, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			status = resp.StatusCode
			if out != nil && status == 200 {
				json.Unmarshal(raw, out)
			}
		}
		if (status != 0 && status < 500) || try == 5 {
			return status
		}
		time.Sleep(2 * time.Second)
	}
}

func main() {
	api := flag.String("api", "", "the customer API's URL")
	ring := flag.String("ring", "", "the ring, to list the customers")
	keep := flag.String("keep", "oldest", "oldest or newest")
	payees := flag.Bool("payees", true, "also remove payees set up twice")
	flag.Parse()
	if *keep != "oldest" && *keep != "newest" {
		log.Fatalf("-keep=%s", *keep)
	}
	t := &tool{api: *api, client: &http.Client{Timeout: 20 * time.Second}}

	session := larkbank.Connect(*ring)
	customers := map[string]bool{}
	for _, table := range []string{"payee", "standing_order"} {
		iter := session.Query(`SELECT DISTINCT customer_id FROM ` + table).PageSize(1000).Iter()
		var id string
		for iter.Scan(&id) {
			customers[id] = true
		}
		if err := iter.Close(); err != nil {
			log.Fatalf("listing customers: %v", err)
		}
	}
	session.Close()

	ordersGone, payeesGone, failed := 0, 0, 0
	for id := range customers {
		var v struct {
			Payees []larkbank.Payee         `json:"payees"`
			Orders []larkbank.StandingOrder `json:"standing_orders"`
		}
		if t.call("GET", "/v1/customers/"+id+"/payees", &v) != 200 || t.call("GET", "/v1/customers/"+id+"/standing-orders", &v) != 200 {
			failed++
			continue
		}
		byID := map[string]larkbank.Payee{}
		for _, p := range v.Payees {
			byID[p.ID] = p
		}
		// Standing orders that say the same thing: the same payee's account, amount and reference.
		groups := map[string][]larkbank.StandingOrder{}
		for _, o := range v.Orders {
			p, ok := byID[o.PayeeID]
			if !ok {
				continue
			}
			key := fmt.Sprintf("%s|%s|%d|%s", p.SortCode, p.AccountNumber, o.Amount, o.Reference)
			groups[key] = append(groups[key], o)
		}
		used := map[string]bool{}
		for _, group := range groups {
			sort.Slice(group, func(i, j int) bool { return group[i].CreatedAt.Before(group[j].CreatedAt) == (*keep == "oldest") })
			used[group[0].PayeeID] = true
			for _, o := range group[1:] {
				if t.call("DELETE", "/v1/customers/"+id+"/standing-orders/"+o.ID, nil) == 204 {
					ordersGone++
				} else {
					failed++
				}
			}
		}
		for _, o := range v.Orders {
			if _, ok := byID[o.PayeeID]; !ok {
				used[o.PayeeID] = true
			}
		}
		if !*payees {
			continue
		}
		// Payees with the same account: keep the one a remaining standing order pays, or else by age.
		same := map[string][]larkbank.Payee{}
		for _, p := range v.Payees {
			same[p.SortCode+"|"+p.AccountNumber] = append(same[p.SortCode+"|"+p.AccountNumber], p)
		}
		for _, group := range same {
			sort.Slice(group, func(i, j int) bool {
				if used[group[i].ID] != used[group[j].ID] {
					return used[group[i].ID]
				}
				return group[i].CreatedAt.Before(group[j].CreatedAt) == (*keep == "oldest")
			})
			for _, p := range group[1:] {
				if used[p.ID] {
					continue
				}
				if t.call("DELETE", "/v1/customers/"+id+"/payees/"+p.ID, nil) == 204 {
					payeesGone++
				} else {
					failed++
				}
			}
		}
	}
	out, _ := json.Marshal(map[string]any{"customers": len(customers), "standing_orders_removed": ordersGone, "payees_removed": payeesGone, "failed": failed})
	fmt.Println(string(out))
}
