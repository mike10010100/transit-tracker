package main

import (
	"fmt"
	"strconv"
	"time"
)

const (
	// PeakPollInterval is also the fast-poll cadence and the first retry
	// delay when no schedule is known.
	PeakPollInterval = 60 * time.Second
	// EcoPollInterval caps the retry backoff when no schedule is known.
	EcoPollInterval = 10 * time.Minute
	// Polls of a minute or more are aligned to the wall clock and delayed by
	// this offset so the refresh lands just *after* the on-screen clock ticks
	// over, rather than a hair before it.
	PollSettleOffset = 500 * time.Millisecond
	// The fast-poll hold armed by a *data-affecting* interaction (view switch
	// / refresh) is the policy's `fast` (default 10 min, formerly
	// FastPollHoldDuration). Screen-only actions (frontlight, exit)
	// deliberately do not arm it.
)

// dataInteraction records a user action that changes what is fetched/rendered
// (explicit refresh). It arms the fast-poll hold. View switches arm it via
// setExplicitViewMode; screen-only actions (frontlight, exit) deliberately do
// not call this.
func (tc *TrackerClient) dataInteraction() {
	tc.interact(InteractionEvent{Kind: EvDataTap})
}

// applySessionLighting lights a dark panel to the policy's session level at
// the start of a power-button session. While the overlay is in `session` the
// schedule auto-lighting is suppressed; when the session ends it resumes
// (e.g. off overnight).
func (tc *TrackerClient) applySessionLighting() {
	p := tc.currentPolicy()
	if p.SessionBrightness <= 0 {
		return
	}
	curr := lipcGet("com.lab126.powerd", "flIntensity")
	if curr == "" || curr == "0" {
		lipcSet("com.lab126.powerd", "flIntensity", strconv.Itoa(p.SessionBrightness))
		lipcSet("com.lab126.powerd", "schedAmberLevel", strconv.Itoa(p.SessionWarmth))
		tc.logRemote(fmt.Sprintf("Interaction lighting on (was %q).", curr))
	}
}

// noteTouch signals that the user touched the screen. Used to keep an awake
// interaction session (and any hold) alive while the user is interacting,
// even for a tap that only changes (say) the frontlight. Non-blocking.
func (tc *TrackerClient) noteTouch() {
	tc.interact(InteractionEvent{Kind: EvTouch})
	select {
	case tc.touchCh <- struct{}{}:
	default:
	}
}

// getNextPollInterval picks the resident-mode poll interval: fast while the
// overlay's fast-poll hold is active, else the server's advice, else a
// fallback (see fallbackPollInterval).
func (tc *TrackerClient) getNextPollInterval(serverIntervalSec int) time.Duration {
	now := time.Now()
	o := tc.overlayNow()
	if o.FastPoll(now) {
		return PeakPollInterval
	}
	if serverIntervalSec > 0 {
		return time.Duration(serverIntervalSec) * time.Second
	}
	tc.mu.Lock()
	p, lastPoll, failures := tc.policy, tc.lastPollSec, tc.consecutiveFailures
	tc.mu.Unlock()
	return fallbackPollInterval(now, p, lastPoll, failures)
}

// fallbackPollInterval is used when a poll yielded no server interval (the
// server was unreachable or the response was rejected). While the last
// policy still describes the current phase, keep its cadence; otherwise
// (no schedule known yet, or the phase has ended) retry at 60 s, doubling per
// consecutive failure up to EcoPollInterval, so a reboot recovers quickly
// without a dead server keeping a sleeping device awake.
func fallbackPollInterval(now time.Time, p Policy, lastPollSec, failures int) time.Duration {
	if lastPollSec > 0 && p.current(now) {
		return time.Duration(lastPollSec) * time.Second
	}
	d := PeakPollInterval
	for i := 1; i < failures && d < EcoPollInterval; i++ {
		d *= 2
	}
	if d > EcoPollInterval {
		d = EcoPollInterval
	}
	return d
}

// nextPollDelay is the resident-mode wait before the next poll: the interval
// aligned to the wall clock, but never past the current phase's end.
func (tc *TrackerClient) nextPollDelay(now time.Time, interval time.Duration) time.Duration {
	return tc.currentPolicy().clampToBoundary(now, alignDelay(now, interval))
}

// alignDelay returns how long to wait (from `now`) so that the next poll lands
// on the next wall-clock multiple of interval, offset by PollSettleOffset so it
// fires just after the target boundary. Intervals below a minute are returned
// unchanged (drifting polls are fine there).
func alignDelay(now time.Time, interval time.Duration) time.Duration {
	if interval < time.Minute {
		return interval
	}
	untilBoundary := interval - time.Duration(now.UnixNano()%int64(interval))
	return untilBoundary + PollSettleOffset
}
