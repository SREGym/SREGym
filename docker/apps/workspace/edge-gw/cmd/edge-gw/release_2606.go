//go:build release2606

package main

// The 2026.06 release line: per-connection session state, and no tuning
// keys (worker_threads and admission_timeout arrived later).
const releaseTuning = false

func newSessionCache() sessionCache { return newSessionShards() }
