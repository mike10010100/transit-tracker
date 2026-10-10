package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// This file implements the client half of the schedule state machine
// (docs/architecture.md §3.2):
//
//   - Policy: the server-advised parameters for the active schedule phase,
//     delivered in the signed X-Tracker-Policy header.
//   - Overlay: the client-owned interaction overlay (idle -> session -> hold)
//     that temporarily overrides the phase while a person is using the device.
//
// Transition is a pure function so the whole overlay can be table-tested; the
// TrackerClient wrappers apply it under tc.mu in O(1), so the input event pump
// never blocks on it.

// PolicyHeader carries the client policy in signed dashboard responses.
const PolicyHeader = "X-Tracker-Policy"

// Bounds for parsePolicy. Values outside the numeric ranges are clamped;
// anything malformed rejects the whole header (fail safe to defaults).
const (
	maxPolicyLen   = 256
	maxPolicyParts = 16
	maxPolicyEpoch = 32503680000 // 3000-01-01: anything later is nonsense
)

var policyPhaseRe = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,23}$`)

// Phase-boundary clamping (see boundaryDelay).
const (
	// boundaryMinDelay: a boundary closer than this is treated as "now".
	boundaryMinDelay = 5 * time.Second
	// boundarySkewGrace: how long after a boundary we keep retrying in case
	// the server's clock is behind ours and it still reports the old phase.
	boundarySkewGrace = 2 * time.Minute
	// boundaryRetryDelay: the retry cadence within boundarySkewGrace.
	boundaryRetryDelay = 30 * time.Second
)

// Policy is the client view of the active schedule phase plus the interaction
// overlay's tunables.
type Policy struct {
	// Phase is the schedule phase name ("" when the server sent no policy).
	Phase string
	// Until is when the phase ends (zero when unknown / no change ahead).
	Until time.Time
	// Suspend reports whether the phase allows suspend-to-RAM in sleep mode.
	Suspend bool
	// Session is how long a power-button session lasts after the last touch.
	Session time.Duration
	// FastHold is how long a data tap keeps resident-mode polling fast.
	FastHold time.Duration
	// Hold is how long manual view/frontlight choices persist after the last
	// interaction.
	Hold time.Duration
	// SessionBrightness and SessionWarmth light a dark panel for a session.
	SessionBrightness int
	SessionWarmth     int
}

// defaultPolicy reproduces the pre-state-machine constants. It applies until
// the first policy-bearing response and whenever the server sends none (an
// older server), so behaviour against such a server is unchanged.
func defaultPolicy() Policy {
	return Policy{
		Suspend:           true,
		Session:           90 * time.Second,
		FastHold:          10 * time.Minute,
		Hold:              45 * time.Minute,
		SessionBrightness: 8,
		SessionWarmth:     12,
	}
}

func clampInt(v, lo, hi int64) int64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// parseClampedSeconds parses a decimal integer and clamps it to [lo, hi].
func parseClampedSeconds(s string, lo, hi int64) (time.Duration, bool) {
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, false
	}
	return time.Duration(clampInt(n, lo, hi)) * time.Second, true
}

// parsePolicy strictly parses an X-Tracker-Policy value:
//
//	v=1;phase=<name>;until=<epoch>;suspend=0|1;session=<s>;fast=<s>;hold=<s>;sl=<b>,<w>
//
// "v" is required and must be 1. Unknown keys are ignored (forward
// compatible); numeric values are clamped to their documented ranges; a
// malformed or duplicated known key rejects the whole header. On rejection
// it returns defaultPolicy() and false.
func parsePolicy(header string) (Policy, bool) {
	p := defaultPolicy()
	if header == "" || len(header) > maxPolicyLen {
		return defaultPolicy(), false
	}
	parts := strings.Split(header, ";")
	if len(parts) > maxPolicyParts {
		return defaultPolicy(), false
	}
	seen := make(map[string]bool, len(parts))
	for _, part := range parts {
		key, val, ok := strings.Cut(part, "=")
		if !ok || key == "" {
			return defaultPolicy(), false
		}
		if seen[key] {
			return defaultPolicy(), false
		}
		seen[key] = true
		if !p.setField(key, val) {
			return defaultPolicy(), false
		}
	}
	if !seen["v"] {
		return defaultPolicy(), false
	}
	return p, true
}

// setField applies one key=value pair, returning false if a known key has a
// malformed value. Unknown keys are accepted and ignored.
func (p *Policy) setField(key, val string) bool {
	var ok bool
	switch key {
	case "v":
		ok = val == "1"
	case "phase":
		p.Phase, ok = val, policyPhaseRe.MatchString(val)
	case "until":
		n, err := strconv.ParseInt(val, 10, 64)
		ok = err == nil && n > 0 && n < maxPolicyEpoch
		if ok {
			p.Until = time.Unix(n, 0)
		}
	case "suspend":
		p.Suspend, ok = val == "1", val == "0" || val == "1"
	case "session":
		p.Session, ok = parseClampedSeconds(val, 10, 1800)
	case "fast":
		p.FastHold, ok = parseClampedSeconds(val, 0, 7200)
	case "hold":
		p.Hold, ok = parseClampedSeconds(val, 0, 14400)
	case "sl":
		b, w, found := strings.Cut(val, ",")
		bn, errB := strconv.ParseInt(b, 10, 64)
		wn, errW := strconv.ParseInt(w, 10, 64)
		ok = found && errB == nil && errW == nil
		if ok {
			p.SessionBrightness = int(clampInt(bn, 0, 24))
			p.SessionWarmth = int(clampInt(wn, 0, 24))
		}
	default:
		ok = true
	}
	return ok
}

// boundaryDelay returns how long from now until shortly after the phase ends,
// or 0 when there is no usable boundary. If the boundary has only just passed
// (our clock may be ahead of the server's, which then still reports the old
// phase), it returns boundaryRetryDelay for up to boundarySkewGrace.
func (p Policy) boundaryDelay(now time.Time) time.Duration {
	if p.Until.IsZero() {
		return 0
	}
	d := p.Until.Sub(now) + PollSettleOffset
	if d >= boundaryMinDelay {
		return d
	}
	if now.Sub(p.Until) < boundarySkewGrace {
		return boundaryRetryDelay
	}
	return 0
}

// clampToBoundary shortens d so a wait never sleeps through a phase change.
// It never lengthens d.
func (p Policy) clampToBoundary(now time.Time, d time.Duration) time.Duration {
	if b := p.boundaryDelay(now); b > 0 && b < d {
		return b
	}
	return d
}

// current reports whether the policy still describes now (no known boundary,
// or the boundary has not passed).
func (p Policy) current(now time.Time) bool {
	return p.Until.IsZero() || now.Before(p.Until)
}

// String renders the policy for logs.
func (p Policy) String() string {
	until := "-"
	if !p.Until.IsZero() {
		until = p.Until.Format("15:04")
	}
	phase := p.Phase
	if phase == "" {
		phase = "(none)"
	}
	return fmt.Sprintf("phase=%s until=%s suspend=%v session=%s fast=%s hold=%s",
		phase, until, p.Suspend, p.Session, p.FastHold, p.Hold)
}

// InteractionState is the interaction overlay's state.
type InteractionState int

const (
	// StateIdle: the phase's settings apply unchanged.
	StateIdle InteractionState = iota
	// StateSession: a power-button session. The client requests the
	// interactive dashboard, stays awake and lights the panel.
	StateSession
	// StateHold: manual view/frontlight choices persist and resident-mode
	// polling may stay fast; ends when its timers expire or the phase changes.
	StateHold
)

func (s InteractionState) String() string {
	switch s {
	case StateSession:
		return "session"
	case StateHold:
		return "hold"
	default:
		return "idle"
	}
}

// EventKind enumerates the overlay's input events.
type EventKind int

const (
	// EvPowerWake: the power button woke the device (starts a session).
	EvPowerWake EventKind = iota
	// EvSessionEnd: the session timer expired with no touch.
	EvSessionEnd
	// EvTouch: any recognised touch (keeps a session/hold alive).
	EvTouch
	// EvDataTap: a refresh tap (arms the fast-poll hold).
	EvDataTap
	// EvViewTap: a view choice (data tap + view override).
	EvViewTap
	// EvLightTap: a manual frontlight change (suppresses auto-lighting).
	EvLightTap
	// EvTick: time passed; expire timers.
	EvTick
	// EvPhaseChanged: the server reported a different schedule phase.
	EvPhaseChanged
)

// InteractionEvent is an overlay input. View is used only by EvViewTap.
type InteractionEvent struct {
	Kind EventKind
	View string
}

// Overlay is the interaction overlay's full state. The zero value is idle.
type Overlay struct {
	State InteractionState
	// HoldUntil is when manual choices lapse (refreshed by each interaction).
	HoldUntil time.Time
	// FastUntil is when the resident-mode fast-poll hold lapses.
	FastUntil time.Time
	// LightHeld: the user changed the frontlight, so auto-lighting is
	// suppressed while in hold. Cleared at session end so a session never
	// leaves the panel lit overnight.
	LightHeld bool
	// View is the manual view override ("" = auto).
	View string
}

// Transition returns the overlay after ev at now under policy p. It is pure.
// Timers are expired first (as for EvTick) for every event except
// EvPowerWake and EvSessionEnd, which are driven by the session loop itself.
func (o Overlay) Transition(ev InteractionEvent, now time.Time, p Policy) Overlay {
	switch ev.Kind {
	case EvPowerWake:
		o.State = StateSession
		o.HoldUntil = now.Add(p.Hold)
		return o
	case EvSessionEnd:
		if o.State != StateSession {
			return o.expire(now)
		}
		o.State = StateHold
		o.LightHeld = false
		return o.expire(now)
	case EvPhaseChanged:
		// Manual choices must not leak into the next phase (e.g. overnight).
		// A live session is left alone: the user is looking at it.
		if o.State == StateHold {
			return Overlay{}
		}
		return o
	}

	o = o.expire(now)
	switch ev.Kind {
	case EvTouch:
		if o.State != StateIdle {
			o.HoldUntil = now.Add(p.Hold)
		}
	case EvDataTap, EvViewTap, EvLightTap:
		if o.State == StateIdle {
			o.State = StateHold
		}
		o.HoldUntil = now.Add(p.Hold)
		switch ev.Kind {
		case EvLightTap:
			o.LightHeld = true
		case EvViewTap:
			o.View = ev.View
			if o.View == "auto" {
				o.View = ""
			}
			o.FastUntil = now.Add(p.FastHold)
		default:
			o.FastUntil = now.Add(p.FastHold)
		}
	}
	return o
}

// expire ends a hold once both of its timers have lapsed.
func (o Overlay) expire(now time.Time) Overlay {
	if o.State == StateHold && !now.Before(o.HoldUntil) && !now.Before(o.FastUntil) {
		return Overlay{}
	}
	return o
}

// LightingHeld reports whether server auto-lighting must be suppressed.
func (o Overlay) LightingHeld() bool {
	return o.State == StateSession || (o.State == StateHold && o.LightHeld)
}

// FastPoll reports whether resident-mode polling should stay fast at now.
func (o Overlay) FastPoll(now time.Time) bool {
	return o.State != StateIdle && now.Before(o.FastUntil)
}

// ---------------------------------------------------------------------------
// TrackerClient wiring (all under tc.mu)
// ---------------------------------------------------------------------------

// interact applies ev to the overlay and logs state changes.
func (tc *TrackerClient) interact(ev InteractionEvent) Overlay {
	tc.mu.Lock()
	prev := tc.overlay.State
	tc.overlay = tc.overlay.Transition(ev, time.Now(), tc.policy)
	o := tc.overlay
	tc.mu.Unlock()
	if o.State != prev {
		tc.logRemote(fmt.Sprintf("Interaction overlay: %s -> %s.", prev, o.State))
	}
	return o
}

// overlayNow expires timers and returns the current overlay.
func (tc *TrackerClient) overlayNow() Overlay {
	return tc.interact(InteractionEvent{Kind: EvTick})
}

// currentPolicy returns the last authenticated policy (or the default).
func (tc *TrackerClient) currentPolicy() Policy {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	return tc.policy
}

// inSession reports whether a power-button session is active, during which
// the dashboard is requested in its full tappable form.
func (tc *TrackerClient) inSession() bool {
	return tc.overlayNow().State == StateSession
}

// applyResponsePolicy records the policy and poll interval from an accepted
// 200/304 dashboard response. A phase change ends a hold.
func (tc *TrackerClient) applyResponsePolicy(header string, pollSec int) {
	p := defaultPolicy()
	if header != "" {
		parsed, valid := parsePolicy(header)
		if valid {
			p = parsed
		} else {
			tc.logRemote(fmt.Sprintf("Ignoring malformed %s %q.", PolicyHeader, header))
		}
	}
	tc.mu.Lock()
	old := tc.policy
	tc.policy = p
	if pollSec > 0 {
		tc.lastPollSec = pollSec
	}
	tc.mu.Unlock()

	if old.Phase != p.Phase || !old.Until.Equal(p.Until) || old.Suspend != p.Suspend {
		tc.logRemote("Schedule policy: " + p.String())
	}
	if old.Phase != "" && p.Phase != "" && old.Phase != p.Phase {
		tc.interact(InteractionEvent{Kind: EvPhaseChanged})
	}
}
