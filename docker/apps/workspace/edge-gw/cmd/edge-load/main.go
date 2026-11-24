// edge-load drives connection load through edge-gw.
//
// Each client connects, waits for the backend's greeting, holds the connection
// for a while like a mail client idling on its mailbox, then disconnects and
// reconnects. A failed attempt is retried after `-retry` (with jitter), which
// is how waiting clients pile onto a gateway that is recovering. `-serve`
// runs a greeting backend instead, for local tests.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

func serve(addr string) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatal(err)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			time.Sleep(100 * time.Millisecond)
			continue
		}
		go func() {
			defer c.Close()
			c.Write([]byte("* OK ready\r\n"))
			io.Copy(io.Discard, c)
		}()
	}
}

type stats struct {
	ok, fail, connected, dropped atomic.Int64
	mu                           sync.Mutex
	lat                          []time.Duration
}

func (s *stats) record(d time.Duration) {
	s.mu.Lock()
	s.lat = append(s.lat, d)
	s.mu.Unlock()
}

func jitter(d time.Duration) time.Duration {
	return time.Duration(float64(d) * (0.5 + rand.Float64()))
}

// client keeps one connection open, like a mail client idling on its mailbox.
// A connection the gateway drops is noticed at once and redialled after the
// retry wait, which is how a restarted unit meets every client at once.
func client(target string, hold, retry, timeout time.Duration, st *stats, stop <-chan struct{}) {
	time.Sleep(time.Duration(rand.Int64N(int64(hold))))
	for {
		select {
		case <-stop:
			return
		default:
		}
		start := time.Now()
		c, err := net.DialTimeout("tcp", target, timeout)
		var r *bufio.Reader
		if err == nil {
			c.SetReadDeadline(time.Now().Add(timeout))
			r = bufio.NewReader(c)
			if _, err = r.ReadString('\n'); err != nil {
				c.Close()
			}
		}
		if err != nil {
			st.fail.Add(1)
			time.Sleep(jitter(retry))
			continue
		}
		st.ok.Add(1)
		st.record(time.Since(start))
		st.connected.Add(1)
		c.SetReadDeadline(time.Now().Add(jitter(hold)))
		_, err = r.ReadByte()
		c.Close()
		st.connected.Add(-1)
		if ne, ok := err.(net.Error); !ok || !ne.Timeout() {
			st.dropped.Add(1)
			time.Sleep(jitter(retry))
		}
	}
}

// parseSchedule reads "seconds:clients,..." into steps sorted by time.
func parseSchedule(spec string) ([][2]int, error) {
	var steps [][2]int
	for _, part := range strings.Split(spec, ",") {
		if part = strings.TrimSpace(part); part == "" {
			continue
		}
		var at, n int
		if _, err := fmt.Sscanf(part, "%d:%d", &at, &n); err != nil {
			return nil, fmt.Errorf("schedule step %q: want seconds:clients", part)
		}
		steps = append(steps, [2]int{at, n})
	}
	sort.Slice(steps, func(i, j int) bool { return steps[i][0] < steps[j][0] })
	return steps, nil
}

func main() {
	srv := flag.String("serve", "", "run a greeting backend on this address instead")
	target := flag.String("target", "127.0.0.1:1143", "gateway route to connect to")
	clients := flag.Int("clients", 100, "concurrent clients")
	hold := flag.Duration("hold", 20*time.Second, "mean time a client keeps its connection")
	retry := flag.Duration("retry", time.Second, "mean wait before retrying a failed connection")
	timeout := flag.Duration("timeout", 10*time.Second, "connect and greeting timeout")
	rampEvery := flag.Duration("ramp-every", 0, "add -ramp-step clients at this interval")
	rampStep := flag.Int("ramp-step", 0, "clients added per ramp interval")
	duration := flag.Duration("duration", 0, "stop after this long (0 runs forever)")
	schedule := flag.String("schedule", "", "set the client count over time: seconds:clients,...")
	flag.Parse()
	if *srv != "" {
		serve(*srv)
		return
	}
	steps, err := parseSchedule(*schedule)
	if err != nil {
		log.Fatal(err)
	}
	st := &stats{}
	var stops []chan struct{}
	resize := func(k int) {
		for len(stops) < k {
			ch := make(chan struct{})
			stops = append(stops, ch)
			go client(*target, *hold, *retry, *timeout, st, ch)
		}
		for len(stops) > k {
			close(stops[len(stops)-1])
			stops = stops[:len(stops)-1]
		}
	}
	resize(*clients)
	begin := time.Now()
	lastRamp := begin
	tick := time.NewTicker(time.Second)
	for now := range tick.C {
		elapsed := int(now.Sub(begin).Seconds())
		for len(steps) > 0 && steps[0][0] <= elapsed {
			resize(steps[0][1])
			steps = steps[1:]
		}
		if *rampEvery > 0 && now.Sub(lastRamp) >= *rampEvery {
			resize(len(stops) + *rampStep)
			lastRamp = now
		}
		st.mu.Lock()
		lat := st.lat
		st.lat = nil
		st.mu.Unlock()
		sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
		p := func(q float64) time.Duration {
			if len(lat) == 0 {
				return 0
			}
			return lat[int(q*float64(len(lat)-1))].Round(time.Millisecond)
		}
		fmt.Printf("t=%4ds clients=%d connected=%d ok=%d fail=%d dropped=%d p50=%v p99=%v\n",
			elapsed, len(stops), st.connected.Load(), st.ok.Swap(0), st.fail.Swap(0), st.dropped.Swap(0), p(0.5), p(0.99))
		if *duration > 0 && now.Sub(begin) >= *duration {
			return
		}
	}
}
