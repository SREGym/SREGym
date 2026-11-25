// edge-gw: a TCP front door for the tenant services.
//
// Each listener admits a connection (see session.go), dials its backend and
// splices the two. A probe listener answers after admission, so health checks
// see the same path user connections take. The admin listener serves metrics
// and a liveness endpoint that skips admission.
package main

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const (
	sessionTTL       = 30 * time.Minute
	lockPatience     = 64
	ticketRounds     = 8
	connBufferBytes  = 32 << 10
	dialTimeout      = 3 * time.Second
	defaultAdmission = 5 * time.Second
)

var version = "dev" // set at build time

type route struct {
	name, listen, backend string
}

type config struct {
	unit, region     string
	workerThreads    int // 0 means one per host CPU
	admissionTimeout time.Duration
	probeListen      string
	adminListen      string
	routes           []route
}

// parseConfig reads `key = value` lines; `route = name listen backend` may repeat.
func parseConfig(path string) (*config, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	c := &config{admissionTimeout: defaultAdmission,
		probeListen: ":8081", adminListen: ":9100"}
	sc := bufio.NewScanner(f)
	for line := 1; sc.Scan(); line++ {
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		k, v, ok := strings.Cut(text, "=")
		if !ok {
			return nil, fmt.Errorf("%s:%d: expected key = value", path, line)
		}
		k, v = strings.TrimSpace(k), os.ExpandEnv(strings.TrimSpace(v))
		switch k {
		case "unit":
			c.unit = v
		case "region":
			c.region = v
		case "admission_timeout":
			if !releaseTuning {
				return nil, fmt.Errorf("%s:%d: unknown setting %q", path, line, k)
			}
			if c.admissionTimeout, err = time.ParseDuration(v); err != nil {
				return nil, fmt.Errorf("%s:%d: %w", path, line, err)
			}
		case "worker_threads":
			if !releaseTuning {
				return nil, fmt.Errorf("%s:%d: unknown setting %q", path, line, k)
			}
			if v == "auto" {
				c.workerThreads = 0
			} else if c.workerThreads, err = strconv.Atoi(v); err != nil || c.workerThreads < 1 {
				return nil, fmt.Errorf("%s:%d: worker_threads must be auto or a positive integer", path, line)
			}
		case "probe_listen":
			c.probeListen = v
		case "admin_listen":
			c.adminListen = v
		case "route":
			f := strings.Fields(v)
			if len(f) != 3 {
				return nil, fmt.Errorf("%s:%d: route = <name> <listen> <backend>", path, line)
			}
			c.routes = append(c.routes, route{f[0], f[1], f[2]})
		default:
			return nil, fmt.Errorf("%s:%d: unknown setting %q", path, line, k)
		}
	}
	return c, sc.Err()
}

type counters struct {
	accepted, admitted, admitTimeouts, backendErrors, active, pending atomic.Int64
	admitNanos                                                        atomic.Int64
}

type gateway struct {
	cfg     *config
	cache   sessionCache
	byRoute map[string]*counters
	probe   counters
	buckets [len(latencyBuckets) + 1]atomic.Int64
}

var latencyBuckets = [...]time.Duration{
	time.Millisecond, 5 * time.Millisecond, 25 * time.Millisecond, 100 * time.Millisecond,
	500 * time.Millisecond, time.Second, 2500 * time.Millisecond, 5 * time.Second,
}

func (g *gateway) observe(d time.Duration) {
	i := 0
	for i < len(latencyBuckets) && d > latencyBuckets[i] {
		i++
	}
	g.buckets[i].Add(1)
}

var errAdmission = errors.New("admission timed out")

// admit runs admission with a deadline. A connection whose admission outlives
// the deadline is refused, but the admission itself still completes and is
// then released: the work is not cancellable once queued.
func (g *gateway) admit(ctr *counters, client string) (*session, error) {
	ctr.pending.Add(1)
	start := time.Now()
	done := make(chan *session, 1)
	go func() { s, _ := g.cache.admit(client); done <- s }()
	select {
	case s := <-done:
		ctr.pending.Add(-1)
		d := time.Since(start)
		g.observe(d)
		ctr.admitNanos.Add(int64(d))
		ctr.admitted.Add(1)
		return s, nil
	case <-time.After(g.cfg.admissionTimeout):
		ctr.pending.Add(-1)
		ctr.admitTimeouts.Add(1)
		g.observe(g.cfg.admissionTimeout + time.Millisecond)
		go func() { g.cache.release(<-done) }()
		return nil, errAdmission
	}
}

func clientOf(c net.Conn) string {
	host, _, _ := net.SplitHostPort(c.RemoteAddr().String())
	return host
}

func (g *gateway) serveRoute(r route, ln net.Listener) {
	ctr := g.byRoute[r.name]
	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("route %s: accept: %v", r.name, err)
			time.Sleep(100 * time.Millisecond)
			continue
		}
		ctr.accepted.Add(1)
		go g.handle(ctr, r, conn)
	}
}

func (g *gateway) handle(ctr *counters, r route, conn net.Conn) {
	defer conn.Close()
	buf := make([]byte, connBufferBytes)
	s, err := g.admit(ctr, clientOf(conn))
	if err != nil {
		return
	}
	defer g.cache.release(s)
	ctr.active.Add(1)
	defer ctr.active.Add(-1)
	back, err := net.DialTimeout("tcp", r.backend, dialTimeout)
	if err != nil {
		ctr.backendErrors.Add(1)
		return
	}
	defer back.Close()
	go func() { io.Copy(back, conn); back.(*net.TCPConn).CloseWrite() }()
	io.CopyBuffer(conn, back, buf)
}

// serveProbe answers "ok" once a probe connection has been admitted.
func (g *gateway) serveProbe(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			time.Sleep(100 * time.Millisecond)
			continue
		}
		g.probe.accepted.Add(1)
		go func() {
			defer conn.Close()
			s, err := g.admit(&g.probe, clientOf(conn))
			if err != nil {
				return
			}
			g.cache.release(s)
			conn.Write([]byte("ok\n"))
		}()
	}
}

func (g *gateway) metrics(w http.ResponseWriter, _ *http.Request) {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	fmt.Fprintf(w, "# TYPE edge_gw_build_info gauge\nedge_gw_build_info{version=%q,unit=%q,region=%q,goversion=%q} 1\n",
		version, g.cfg.unit, g.cfg.region, runtime.Version())
	fmt.Fprintf(w, "# TYPE edge_gw_gomaxprocs gauge\nedge_gw_gomaxprocs %d\n", runtime.GOMAXPROCS(0))
	fmt.Fprintf(w, "# TYPE edge_gw_goroutines gauge\nedge_gw_goroutines %d\n", runtime.NumGoroutine())
	fmt.Fprintf(w, "# TYPE edge_gw_heap_bytes gauge\nedge_gw_heap_bytes %d\n", ms.HeapAlloc)
	fmt.Fprintf(w, "# TYPE edge_gw_sessions gauge\nedge_gw_sessions %d\n", g.cache.size())
	emit := func(name, typ string, get func(*counters) int64) {
		fmt.Fprintf(w, "# TYPE edge_gw_%s %s\n", name, typ)
		for rn, c := range g.byRoute {
			fmt.Fprintf(w, "edge_gw_%s{route=%q} %d\n", name, rn, get(c))
		}
		fmt.Fprintf(w, "edge_gw_%s{route=\"probe\"} %d\n", name, get(&g.probe))
	}
	emit("connections_accepted_total", "counter", func(c *counters) int64 { return c.accepted.Load() })
	emit("admissions_total", "counter", func(c *counters) int64 { return c.admitted.Load() })
	emit("admission_timeouts_total", "counter", func(c *counters) int64 { return c.admitTimeouts.Load() })
	emit("backend_errors_total", "counter", func(c *counters) int64 { return c.backendErrors.Load() })
	emit("connections_active", "gauge", func(c *counters) int64 { return c.active.Load() })
	emit("admissions_pending", "gauge", func(c *counters) int64 { return c.pending.Load() })
	fmt.Fprintf(w, "# TYPE edge_gw_admission_seconds histogram\n")
	var cum, sum int64
	for i, b := range latencyBuckets {
		cum += g.buckets[i].Load()
		fmt.Fprintf(w, "edge_gw_admission_seconds_bucket{le=\"%g\"} %d\n", b.Seconds(), cum)
	}
	cum += g.buckets[len(latencyBuckets)].Load()
	for _, c := range g.byRoute {
		sum += c.admitNanos.Load()
	}
	sum += g.probe.admitNanos.Load()
	fmt.Fprintf(w, "edge_gw_admission_seconds_bucket{le=\"+Inf\"} %d\nedge_gw_admission_seconds_sum %g\nedge_gw_admission_seconds_count %d\n",
		cum, float64(sum)/1e9, cum)
}

func main() {
	path := flag.String("config", "/etc/edge-gw/edge-gw.conf", "configuration file")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	cfg, err := parseConfig(*path)
	if err != nil {
		log.Fatal(err)
	}
	// Crash and quit output carry no goroutine stacks.
	debug.SetTraceback("none")
	if releaseTuning {
		// Like most proxies, size worker parallelism to the host, not the
		// container's CPU quota.
		workers := cfg.workerThreads
		if workers == 0 {
			workers = runtime.NumCPU()
		}
		runtime.GOMAXPROCS(workers)
	}
	g := &gateway{cfg: cfg, byRoute: map[string]*counters{}, cache: newSessionCache()}
	log.Printf("edge-gw %s unit=%s region=%s workers=%d", version, cfg.unit, cfg.region, runtime.GOMAXPROCS(0))

	for _, r := range cfg.routes {
		ln, err := net.Listen("tcp", r.listen)
		if err != nil {
			log.Fatalf("route %s: %v", r.name, err)
		}
		g.byRoute[r.name] = &counters{}
		log.Printf("route %s %s -> %s", r.name, r.listen, r.backend)
		go g.serveRoute(r, ln)
	}
	pl, err := net.Listen("tcp", cfg.probeListen)
	if err != nil {
		log.Fatal(err)
	}
	go g.serveProbe(pl)

	http.HandleFunc("/metrics", g.metrics)
	http.HandleFunc("/livez", func(w http.ResponseWriter, _ *http.Request) { io.WriteString(w, "ok\n") })
	srv := &http.Server{Addr: cfg.adminListen, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
