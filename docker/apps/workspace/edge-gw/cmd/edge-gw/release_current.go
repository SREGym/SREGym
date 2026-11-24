//go:build !release2606

package main

// Releases from 2026.07 on: session admission resumes sessions across
// reconnects through one shared table, and the tuning keys exist.
const releaseTuning = true

func newSessionCache() sessionCache { return newSessionDirectory() }
