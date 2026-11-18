// workspace-clients is the tenant's user population, run out of the agent's
// view. It keeps four things going:
//
//   - mail clients that log in over IMAP through their region's gateway and
//     idle on the inbox, reconnecting when dropped;
//   - chat clients that hold a websocket open to the chat service through the
//     gateway (or wherever chat is currently placed);
//   - probes: one full mail session and one chat API call per region every
//     few seconds, which are the user-visible success measurement;
//   - an external sender that submits mail to the transport, recording every
//     accepted message, and a sink that records every bounce returned to it.
//
// Population sizes change at runtime through the control API, which is how the
// load follows the day and how the fault is driven.
package main

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	mrand "math/rand/v2"
	"net"
	"net/http"
	"net/smtp"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

var (
	regions     = flag.String("regions", "a,b,c", "regions")
	mailTarget  = flag.String("mail-target", "edge-%s:1143", "IMAP endpoint per region (%s is the region)")
	chatTarget  = flag.String("chat-target", "chat-%s:8065", "chat endpoint per region (%s is the region)")
	chatHost    = flag.String("chat-host", "chat.tenant.example", "Host header for chat requests")
	usersFile   = flag.String("users", "/etc/workspace/users", "mail accounts, user@domain:password per line")
	tokensFile  = flag.String("chat-tokens", "/etc/workspace/chat-tokens", "chat access tokens, one per line")
	hold        = flag.Duration("hold", 60*time.Second, "mean connection lifetime")
	retry       = flag.Duration("retry", time.Second, "first wait before reconnecting; doubles per failure")
	retryCap    = flag.Duration("retry-cap", 30*time.Second, "longest wait before reconnecting")
	timeout     = flag.Duration("timeout", 5*time.Second, "per-step timeout")
	probeEvery  = flag.Duration("probe-every", 2*time.Second, "probe interval per region and service")
	smtpTarget  = flag.String("smtp", "mail-transport:25", "transport for external mail")
	mailRate    = flag.Float64("mail-rate", 1, "external messages per second")
	sinkListen  = flag.String("sink", ":2525", "SMTP listener that receives bounces")
	control     = flag.String("control", ":8090", "control and metrics listener")
	stateDir    = flag.String("state", "/data", "where the accepted and bounced message logs live")
	initialSpec = flag.String("population", "", "initial population: service.region=n,...")
)

// ---- accounts ------------------------------------------------------------------

type account struct{ user, pass string }

func readLines(path string) []string {
	b, err := os.ReadFile(path)
	if err != nil {
		log.Fatal(err)
	}
	var out []string
	for _, l := range strings.Split(string(b), "\n") {
		if l = strings.TrimSpace(l); l != "" && !strings.HasPrefix(l, "#") {
			out = append(out, l)
		}
	}
	return out
}

// ---- measurement ---------------------------------------------------------------

type series struct {
	ok, fail, dropped atomic.Int64
	connected         atomic.Int64
}

type window struct {
	mu     sync.Mutex
	events []struct {
		at time.Time
		ok bool
	}
}

func (w *window) add(ok bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	now := time.Now()
	w.events = append(w.events, struct {
		at time.Time
		ok bool
	}{now, ok})
	cut := 0
	for cut < len(w.events) && now.Sub(w.events[cut].at) > 30*time.Minute {
		cut++
	}
	w.events = w.events[cut:]
}

// ratio is the success ratio over the last d, and the number of samples.
func (w *window) ratio(d time.Duration) (float64, int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	var ok, n int
	now := time.Now()
	for i := len(w.events) - 1; i >= 0 && now.Sub(w.events[i].at) <= d; i-- {
		n++
		if w.events[i].ok {
			ok++
		}
	}
	if n == 0 {
		return 0, 0
	}
	return float64(ok) / float64(n), n
}

type key struct{ service, region string }

var (
	statsMu sync.Mutex
	stats   = map[key]*series{}
	probes  = map[key]*window{}
)

func seriesFor(k key) *series {
	statsMu.Lock()
	defer statsMu.Unlock()
	if stats[k] == nil {
		stats[k] = &series{}
	}
	return stats[k]
}

func probeFor(k key) *window {
	statsMu.Lock()
	defer statsMu.Unlock()
	if probes[k] == nil {
		probes[k] = &window{}
	}
	return probes[k]
}

func jitter(d time.Duration) time.Duration {
	return time.Duration(float64(d) * (0.5 + mrand.Float64()))
}

// ---- protocol steps --------------------------------------------------------------

type session struct {
	c net.Conn
	r *bufio.Reader
}

func dial(addr string) (*session, error) {
	c, err := net.DialTimeout("tcp", addr, *timeout)
	if err != nil {
		return nil, err
	}
	return &session{c, bufio.NewReader(c)}, nil
}

func (s *session) line() (string, error) {
	s.c.SetReadDeadline(time.Now().Add(*timeout))
	return s.r.ReadString('\n')
}

// imapCommand sends a tagged command and reads to its tagged reply.
func (s *session) imapCommand(tag, cmd string) (string, error) {
	s.c.SetWriteDeadline(time.Now().Add(*timeout))
	if _, err := fmt.Fprintf(s.c, "%s %s\r\n", tag, cmd); err != nil {
		return "", err
	}
	var all strings.Builder
	for {
		l, err := s.line()
		if err != nil {
			return all.String(), err
		}
		all.WriteString(l)
		if strings.HasPrefix(l, tag+" ") {
			if strings.HasPrefix(l, tag+" OK") {
				return all.String(), nil
			}
			return all.String(), fmt.Errorf("%s", strings.TrimSpace(l))
		}
	}
}

func imapLogin(region string, a account) (*session, error) {
	s, err := dial(fmt.Sprintf(*mailTarget, region))
	if err != nil {
		return nil, err
	}
	fail := func(err error) (*session, error) { s.c.Close(); return nil, err }
	if l, err := s.line(); err != nil || !strings.HasPrefix(l, "* OK") {
		return fail(fmt.Errorf("greeting: %q %v", l, err))
	}
	if _, err := s.imapCommand("a1", fmt.Sprintf("LOGIN %s %s", a.user, a.pass)); err != nil {
		return fail(err)
	}
	if _, err := s.imapCommand("a2", "SELECT INBOX"); err != nil {
		return fail(err)
	}
	return s, nil
}

func chatUpgrade(region, token string) (*session, error) {
	s, err := dial(fmt.Sprintf(*chatTarget, region))
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, 16)
	rand.Read(nonce)
	s.c.SetWriteDeadline(time.Now().Add(*timeout))
	fmt.Fprintf(s.c, "GET /api/v4/websocket HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"+
		"Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer %s\r\n\r\n",
		*chatHost, base64.StdEncoding.EncodeToString(nonce), token)
	status, err := s.line()
	if err != nil || !strings.Contains(status, " 101 ") {
		s.c.Close()
		return nil, fmt.Errorf("upgrade: %q %v", strings.TrimSpace(status), err)
	}
	for {
		l, err := s.line()
		if err != nil {
			s.c.Close()
			return nil, err
		}
		if l == "\r\n" {
			break
		}
	}
	// The server's first frame is its hello event.
	s.c.SetReadDeadline(time.Now().Add(*timeout))
	if _, err := s.r.ReadByte(); err != nil {
		s.c.Close()
		return nil, fmt.Errorf("hello: %v", err)
	}
	return s, nil
}

// ---- population ------------------------------------------------------------------

type population struct {
	mu    sync.Mutex
	stops map[key][]chan struct{}
}

var pop = &population{stops: map[key][]chan struct{}{}}
var (
	accounts []account
	tokens   []string
)

func (p *population) set(k key, n int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for len(p.stops[k]) < n {
		ch := make(chan struct{})
		idx := len(p.stops[k])
		p.stops[k] = append(p.stops[k], ch)
		go runClient(k, idx, ch)
	}
	for len(p.stops[k]) > n {
		last := len(p.stops[k]) - 1
		close(p.stops[k][last])
		p.stops[k] = p.stops[k][:last]
	}
}

func (p *population) sizes() map[string]int {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := map[string]int{}
	for k, v := range p.stops {
		out[k.service+"."+k.region] = len(v)
	}
	return out
}

// backoff is the wait before reconnect attempt n+1 after n consecutive
// failures: exponential with jitter, like a mail or chat client.
func backoff(failures int) time.Duration {
	d := *retry << min(failures, 10)
	if d > *retryCap || d <= 0 {
		d = *retryCap
	}
	return jitter(d)
}

func runClient(k key, idx int, stop <-chan struct{}) {
	st := seriesFor(k)
	failures := 0
	time.Sleep(time.Duration(mrand.Int64N(int64(*hold))))
	for {
		select {
		case <-stop:
			return
		default:
		}
		var s *session
		var err error
		if k.service == "mail" {
			s, err = imapLogin(k.region, accounts[idx%len(accounts)])
			if err == nil {
				s.c.SetWriteDeadline(time.Now().Add(*timeout))
				_, err = fmt.Fprintf(s.c, "a3 IDLE\r\n")
				if err == nil {
					var l string
					if l, err = s.line(); err == nil && !strings.HasPrefix(l, "+") {
						err = fmt.Errorf("idle: %q", l)
					}
				}
				if err != nil {
					s.c.Close()
				}
			}
		} else {
			s, err = chatUpgrade(k.region, tokens[idx%len(tokens)])
		}
		if err != nil {
			st.fail.Add(1)
			time.Sleep(backoff(failures))
			failures++
			continue
		}
		failures = 0
		st.ok.Add(1)
		st.connected.Add(1)
		// Hold until the lifetime ends, the server drops us, or we are stopped.
		deadline := time.Now().Add(jitter(*hold))
		dropped := false
		for time.Now().Before(deadline) {
			s.c.SetReadDeadline(time.Now().Add(time.Second))
			if _, err := s.r.Discard(s.r.Buffered()); err != nil {
				break
			}
			buf := make([]byte, 512)
			if _, err := s.r.Read(buf); err != nil {
				if ne, ok := err.(net.Error); ok && ne.Timeout() {
					select {
					case <-stop:
						deadline = time.Now()
					default:
					}
					continue
				}
				dropped = true
				break
			}
		}
		if k.service == "mail" && !dropped {
			s.c.SetWriteDeadline(time.Now().Add(time.Second))
			fmt.Fprintf(s.c, "DONE\r\na4 LOGOUT\r\n")
		}
		s.c.Close()
		st.connected.Add(-1)
		if dropped {
			st.dropped.Add(1)
			time.Sleep(backoff(0))
		}
	}
}

// ---- probes -------------------------------------------------------------------------

func probeMail(region string) error {
	s, err := imapLogin(region, accounts[mrand.IntN(len(accounts))])
	if err != nil {
		return err
	}
	defer s.c.Close()
	if _, err := s.imapCommand("p1", "UID FETCH 1:* (FLAGS)"); err != nil {
		return err
	}
	_, err = s.imapCommand("p2", "LOGOUT")
	if err == io.EOF {
		err = nil
	}
	return err
}

func probeChat(region string) error {
	tr := &http.Transport{DisableKeepAlives: true, DialContext: (&net.Dialer{Timeout: *timeout}).DialContext}
	cl := &http.Client{Transport: tr, Timeout: 2 * *timeout}
	req, _ := http.NewRequest("GET", "http://"+fmt.Sprintf(*chatTarget, region)+"/api/v4/users/me", nil)
	req.Host = *chatHost
	req.Header.Set("Authorization", "Bearer "+tokens[mrand.IntN(len(tokens))])
	resp, err := cl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != 200 {
		return fmt.Errorf("status %d", resp.StatusCode)
	}
	return nil
}

func runProbes(region string) {
	for range time.Tick(*probeEvery) {
		for _, svc := range []string{"mail", "chat"} {
			go func(svc string) {
				var err error
				if svc == "mail" {
					err = probeMail(region)
				} else {
					err = probeChat(region)
				}
				probeFor(key{svc, region}).add(err == nil)
			}(svc)
		}
	}
}

// ---- external mail and bounces -------------------------------------------------------

var (
	logMu      sync.Mutex
	acceptedN  atomic.Int64
	bouncedN   atomic.Int64
	submitFail atomic.Int64
	msgIDRe    = regexp.MustCompile(`<m-[0-9a-f]+-[0-9]+@partners\.example>`)
)

func appendLog(name, line string) {
	logMu.Lock()
	defer logMu.Unlock()
	f, err := os.OpenFile(*stateDir+"/"+name, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Printf("state %s: %v", name, err)
		return
	}
	defer f.Close()
	fmt.Fprintln(f, line)
}

func runSender() {
	if *mailRate <= 0 {
		return
	}
	run := make([]byte, 4)
	rand.Read(run)
	prefix := fmt.Sprintf("%x", run)
	interval := time.Duration(float64(time.Second) / *mailRate)
	for n := 1; ; n++ {
		time.Sleep(jitter(interval))
		a := accounts[mrand.IntN(len(accounts))]
		id := fmt.Sprintf("<m-%s-%d@partners.example>", prefix, n)
		from := fmt.Sprintf("notify%d@partners.example", n%50)
		body := fmt.Sprintf("From: %s\r\nTo: %s\r\nSubject: Update %d\r\nMessage-ID: %s\r\nDate: %s\r\n\r\nMessage %d.\r\n",
			from, a.user, n, id, time.Now().UTC().Format(time.RFC1123Z), n)
		if err := smtp.SendMail(*smtpTarget, nil, from, []string{a.user}, []byte(body)); err != nil {
			submitFail.Add(1)
			continue
		}
		acceptedN.Add(1)
		appendLog("accepted", fmt.Sprintf("%d %s %s", time.Now().Unix(), id, a.user))
	}
}

// runSink accepts any SMTP transaction and records the message IDs of ours
// that a bounce mentions.
func runSink() {
	ln, err := net.Listen("tcp", *sinkListen)
	if err != nil {
		log.Fatal(err)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			continue
		}
		go func(c net.Conn) {
			defer c.Close()
			c.SetDeadline(time.Now().Add(2 * time.Minute))
			r := bufio.NewReader(c)
			fmt.Fprintf(c, "220 partners.example ESMTP\r\n")
			for {
				l, err := r.ReadString('\n')
				if err != nil {
					return
				}
				cmd := strings.ToUpper(strings.TrimSpace(l))
				switch {
				case strings.HasPrefix(cmd, "EHLO"), strings.HasPrefix(cmd, "HELO"):
					fmt.Fprintf(c, "250 partners.example\r\n")
				case cmd == "DATA":
					fmt.Fprintf(c, "354 go ahead\r\n")
					var msg strings.Builder
					for {
						l, err := r.ReadString('\n')
						if err != nil {
							return
						}
						if l == ".\r\n" || l == ".\n" {
							break
						}
						msg.WriteString(l)
					}
					seen := map[string]bool{}
					for _, id := range msgIDRe.FindAllString(msg.String(), -1) {
						if !seen[id] {
							seen[id] = true
							bouncedN.Add(1)
							appendLog("bounced", fmt.Sprintf("%d %s", time.Now().Unix(), id))
						}
					}
					fmt.Fprintf(c, "250 ok\r\n")
				case cmd == "QUIT":
					fmt.Fprintf(c, "221 bye\r\n")
					return
				default:
					fmt.Fprintf(c, "250 ok\r\n")
				}
			}
		}(c)
	}
}

// ---- control API -------------------------------------------------------------------

func parsePopulation(spec string) (map[key]int, error) {
	out := map[key]int{}
	for _, part := range strings.Split(spec, ",") {
		if part = strings.TrimSpace(part); part == "" {
			continue
		}
		name, n, ok := strings.Cut(part, "=")
		svc, region, ok2 := strings.Cut(name, ".")
		count, err := strconv.Atoi(n)
		if !ok || !ok2 || err != nil || count < 0 || (svc != "mail" && svc != "chat") {
			return nil, fmt.Errorf("population %q: want service.region=n", part)
		}
		out[key{svc, region}] = count
	}
	return out, nil
}

func serveControl() {
	http.HandleFunc("/population", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			want, err := parsePopulation(r.URL.Query().Get("set"))
			if err != nil {
				http.Error(w, err.Error(), 400)
				return
			}
			for k, n := range want {
				pop.set(k, n)
			}
		}
		json.NewEncoder(w).Encode(pop.sizes())
	})
	// /status: everything the grader and the fault driver read.
	http.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		windowFor := 60 * time.Second
		if v := r.URL.Query().Get("window"); v != "" {
			if d, err := time.ParseDuration(v); err == nil {
				windowFor = d
			}
		}
		out := map[string]any{"population": pop.sizes(), "window_seconds": windowFor.Seconds(),
			"mail_accepted": acceptedN.Load(), "mail_bounced": bouncedN.Load(), "mail_submit_failures": submitFail.Load()}
		clients := map[string]any{}
		probe := map[string]any{}
		statsMu.Lock()
		keys := make([]key, 0, len(stats)+len(probes))
		for k := range stats {
			keys = append(keys, k)
		}
		for k := range probes {
			if stats[k] == nil {
				keys = append(keys, k)
			}
		}
		statsMu.Unlock()
		sort.Slice(keys, func(i, j int) bool { return keys[i].service+keys[i].region < keys[j].service+keys[j].region })
		for _, k := range keys {
			name := k.service + "." + k.region
			s := seriesFor(k)
			clients[name] = map[string]int64{"connected": s.connected.Load(), "ok_total": s.ok.Load(),
				"fail_total": s.fail.Load(), "dropped_total": s.dropped.Load()}
			ratio, n := probeFor(k).ratio(windowFor)
			probe[name] = map[string]any{"success_ratio": ratio, "samples": n}
		}
		out["clients"], out["probes"] = clients, probe
		json.NewEncoder(w).Encode(out)
	})
	serveLog := func(name string) http.HandlerFunc {
		return func(w http.ResponseWriter, _ *http.Request) {
			logMu.Lock()
			b, err := os.ReadFile(*stateDir + "/" + name)
			logMu.Unlock()
			if err != nil && !os.IsNotExist(err) {
				http.Error(w, err.Error(), 500)
				return
			}
			w.Write(b)
		}
	}
	http.HandleFunc("/mail/accepted", serveLog("accepted"))
	http.HandleFunc("/mail/bounced", serveLog("bounced"))
	log.Fatal(http.ListenAndServe(*control, nil))
}

func main() {
	flag.Parse()
	for _, l := range readLines(*usersFile) {
		u, p, ok := strings.Cut(l, ":")
		if ok {
			accounts = append(accounts, account{u, p})
		}
	}
	tokens = readLines(*tokensFile)
	if len(accounts) == 0 || len(tokens) == 0 {
		log.Fatal("need at least one mail account and one chat token")
	}
	initial, err := parsePopulation(*initialSpec)
	if err != nil {
		log.Fatal(err)
	}
	for k, n := range initial {
		pop.set(k, n)
	}
	for _, r := range strings.Split(*regions, ",") {
		go runProbes(r)
	}
	go runSender()
	go runSink()
	serveControl()
}
