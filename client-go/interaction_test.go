package main

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestParsePolicy_Valid(t *testing.T) {
	cases := []struct {
		name   string
		header string
		check  func(t *testing.T, p Policy)
	}{
		{
			name:   "standard peak header",
			header: "v=1;phase=peak;until=1760103000;suspend=0;session=90;fast=600;hold=2700;sl=8,12",
			check: func(t *testing.T, p Policy) {
				if p.Phase != "peak" {
					t.Errorf("Phase = %q, want peak", p.Phase)
				}
				if p.Until.Unix() != 1760103000 {
					t.Errorf("Until = %v, want 1760103000", p.Until.Unix())
				}
				if p.Suspend {
					t.Errorf("Suspend = true, want false")
				}
				if p.Session != 90*time.Second {
					t.Errorf("Session = %v, want 90s", p.Session)
				}
				if p.FastHold != 600*time.Second {
					t.Errorf("FastHold = %v, want 600s", p.FastHold)
				}
				if p.Hold != 2700*time.Second {
					t.Errorf("Hold = %v, want 2700s", p.Hold)
				}
				if p.SessionBrightness != 8 || p.SessionWarmth != 12 {
					t.Errorf("Lighting = %d,%d, want 8,12", p.SessionBrightness, p.SessionWarmth)
				}
			},
		},
		{
			name:   "clamping bounds",
			header: "v=1;phase=offpeak;suspend=1;session=5;fast=-10;hold=999999;sl=100,-5",
			check: func(t *testing.T, p Policy) {
				if !p.Suspend {
					t.Errorf("Suspend = false, want true")
				}
				// session clamped to min 10
				if p.Session != 10*time.Second {
					t.Errorf("Session = %v, want 10s", p.Session)
				}
				// fast clamped to min 0
				if p.FastHold != 0 {
					t.Errorf("FastHold = %v, want 0s", p.FastHold)
				}
				// hold clamped to max 14400 (4 hours)
				if p.Hold != 14400*time.Second {
					t.Errorf("Hold = %v, want 14400s", p.Hold)
				}
				// sl clamped to 0..24
				if p.SessionBrightness != 24 || p.SessionWarmth != 0 {
					t.Errorf("Lighting = %d,%d, want 24,0", p.SessionBrightness, p.SessionWarmth)
				}
			},
		},
		{
			name:   "forward compatibility ignores unknown keys",
			header: "v=1;phase=peak;future_key=val_123;suspend=0;another=foo;session=90",
			check: func(t *testing.T, p Policy) {
				if p.Phase != "peak" || p.Suspend || p.Session != 90*time.Second {
					t.Errorf("unexpected policy fields: %+v", p)
				}
			},
		},
		{
			name:   "minimal valid header",
			header: "v=1",
			check: func(t *testing.T, p Policy) {
				def := defaultPolicy()
				if p.Session != def.Session || p.FastHold != def.FastHold || p.Hold != def.Hold {
					t.Errorf("expected defaults for omitted fields, got %+v", p)
				}
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, ok := parsePolicy(tc.header)
			if !ok {
				t.Fatalf("parsePolicy(%q) returned ok=false", tc.header)
			}
			tc.check(t, p)
		})
	}
}

func TestParsePolicy_Invalid(t *testing.T) {
	cases := []struct {
		name   string
		header string
	}{
		{"empty string", ""},
		{"missing version", "phase=peak;suspend=0"},
		{"unsupported version", "v=2;phase=peak"},
		{"invalid version format", "v=1.0;phase=peak"},
		{"duplicate key", "v=1;phase=peak;phase=offpeak"},
		{"invalid phase regex capital", "v=1;phase=Peak"},
		{"invalid phase regex symbols", "v=1;phase=peak!"},
		{"invalid phase regex leading digit", "v=1;phase=1peak"},
		{"invalid suspend non-boolean", "v=1;suspend=true"},
		{"invalid suspend out-of-range", "v=1;suspend=2"},
		{"unparsable session number", "v=1;session=abc"},
		{"unparsable fast number", "v=1;fast=none"},
		{"unparsable hold number", "v=1;hold=xyz"},
		{"unparsable lighting missing comma", "v=1;sl=8"},
		{"unparsable lighting non-number", "v=1;sl=8,abc"},
		{"negative until epoch", "v=1;until=-100"},
		{"nonsense until epoch", "v=1;until=999999999999999999"},
		{"part without equals", "v=1;phase;suspend=0"},
		{"empty key", "v=1;=peak"},
		{"exceeds max length", "v=1;phase=" + strings.Repeat("a", 300)},
		{"too many parts", "v=1;" + strings.Repeat("p=1;", 20)},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, ok := parsePolicy(tc.header)
			if ok {
				t.Errorf("parsePolicy(%q) returned ok=true, want false", tc.header)
			}
			def := defaultPolicy()
			if p != def {
				t.Errorf("expected default policy on rejection, got %+v", p)
			}
		})
	}
}

func TestOverlay_Transitions(t *testing.T) {
	now := time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
	p := defaultPolicy()
	p.Session = 90 * time.Second
	p.FastHold = 600 * time.Second
	p.Hold = 2700 * time.Second

	t.Run("Idle to Session via PowerWake", func(t *testing.T) {
		o := Overlay{}
		o = o.Transition(InteractionEvent{Kind: EvPowerWake}, now, p)
		if o.State != StateSession {
			t.Errorf("State = %v, want StateSession", o.State)
		}
		if o.HoldUntil != now.Add(p.Hold) {
			t.Errorf("HoldUntil = %v, want %v", o.HoldUntil, now.Add(p.Hold))
		}
	})

	t.Run("Session touch extends hold", func(t *testing.T) {
		o := Overlay{State: StateSession, HoldUntil: now.Add(10 * time.Second)}
		later := now.Add(5 * time.Second)
		o = o.Transition(InteractionEvent{Kind: EvTouch}, later, p)
		if o.State != StateSession {
			t.Errorf("State = %v, want StateSession", o.State)
		}
		if o.HoldUntil != later.Add(p.Hold) {
			t.Errorf("HoldUntil = %v, want %v", o.HoldUntil, later.Add(p.Hold))
		}
	})

	t.Run("SessionEnd moves to Hold and clears LightHeld", func(t *testing.T) {
		o := Overlay{
			State:     StateSession,
			HoldUntil: now.Add(2000 * time.Second),
			FastUntil: now.Add(500 * time.Second),
			LightHeld: true,
		}
		o = o.Transition(InteractionEvent{Kind: EvSessionEnd}, now, p)
		if o.State != StateHold {
			t.Errorf("State = %v, want StateHold", o.State)
		}
		if o.LightHeld {
			t.Errorf("LightHeld = true, want false after SessionEnd")
		}
	})

	t.Run("ViewTap sets view, arms fast poll and hold", func(t *testing.T) {
		o := Overlay{}
		o = o.Transition(InteractionEvent{Kind: EvViewTap, View: "morning"}, now, p)
		if o.State != StateHold {
			t.Errorf("State = %v, want StateHold", o.State)
		}
		if o.View != "morning" {
			t.Errorf("View = %q, want morning", o.View)
		}
		if !o.FastPoll(now) {
			t.Errorf("FastPoll(now) = false, want true")
		}
	})

	t.Run("ViewTap auto clears manual view", func(t *testing.T) {
		o := Overlay{State: StateHold, View: "morning"}
		o = o.Transition(InteractionEvent{Kind: EvViewTap, View: "auto"}, now, p)
		if o.View != "" {
			t.Errorf("View = %q, want empty for auto", o.View)
		}
	})

	t.Run("LightTap sets LightHeld and LightingHeld is true", func(t *testing.T) {
		o := Overlay{State: StateHold}
		o = o.Transition(InteractionEvent{Kind: EvLightTap}, now, p)
		if !o.LightHeld || !o.LightingHeld() {
			t.Errorf("expected LightHeld and LightingHeld() to be true")
		}
	})

	t.Run("PhaseChanged clears Hold", func(t *testing.T) {
		o := Overlay{
			State:     StateHold,
			View:      "evening",
			HoldUntil: now.Add(1000 * time.Second),
			LightHeld: true,
		}
		o = o.Transition(InteractionEvent{Kind: EvPhaseChanged}, now, p)
		if o.State != StateIdle || o.View != "" || o.LightHeld {
			t.Errorf("expected clean idle state on phase change, got %+v", o)
		}
	})

	t.Run("PhaseChanged does not abort active Session", func(t *testing.T) {
		o := Overlay{
			State:     StateSession,
			HoldUntil: now.Add(1000 * time.Second),
		}
		o = o.Transition(InteractionEvent{Kind: EvPhaseChanged}, now, p)
		if o.State != StateSession {
			t.Errorf("Session should persist across phase change, got %v", o.State)
		}
	})

	t.Run("Timer expiry returns to Idle", func(t *testing.T) {
		o := Overlay{
			State:     StateHold,
			HoldUntil: now.Add(10 * time.Second),
			FastUntil: now.Add(5 * time.Second),
			View:      "morning",
		}
		// Before expiry
		o1 := o.Transition(InteractionEvent{Kind: EvTick}, now.Add(8*time.Second), p)
		if o1.State != StateHold {
			t.Errorf("State = %v, want StateHold before expiry", o1.State)
		}
		// Fast poll expires first
		if o1.FastPoll(now.Add(8 * time.Second)) {
			t.Errorf("FastPoll should have expired at 5s")
		}
		// Full hold expires
		o2 := o.Transition(InteractionEvent{Kind: EvTick}, now.Add(11*time.Second), p)
		if o2.State != StateIdle || o2.View != "" {
			t.Errorf("expected reset to Idle after HoldUntil, got %+v", o2)
		}
	})

	t.Run("SessionEnd when not in Session expires timers safely", func(t *testing.T) {
		o := Overlay{State: StateHold, HoldUntil: now.Add(10 * time.Second)}
		o = o.Transition(InteractionEvent{Kind: EvSessionEnd}, now, p)
		if o.State != StateHold {
			t.Errorf("State = %v, want StateHold", o.State)
		}
	})
}

func TestTrackerClient_ApplyResponsePolicy(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")

	// 1. Empty header leaves default
	tc.applyResponsePolicy("", 60)
	if tc.currentPolicy().Phase != "" {
		t.Errorf("expected empty phase on empty header, got %q", tc.currentPolicy().Phase)
	}

	// 2. Malformed header logs warning and leaves default
	tc.applyResponsePolicy("malformed-header", 60)
	if tc.currentPolicy().Phase != "" {
		t.Errorf("expected default policy on malformed header, got %q", tc.currentPolicy().Phase)
	}

	// 3. Valid policy applied
	tc.applyResponsePolicy("v=1;phase=peak;suspend=0", 60)
	if tc.currentPolicy().Phase != "peak" {
		t.Errorf("expected peak phase, got %q", tc.currentPolicy().Phase)
	}

	// 4. Arm hold, then phase change clears hold
	tc.interact(InteractionEvent{Kind: EvViewTap, View: "morning"})
	if tc.getViewMode() != "morning" {
		t.Errorf("view mode = %q, want morning", tc.getViewMode())
	}
	tc.applyResponsePolicy("v=1;phase=offpeak;suspend=1", 600)
	if tc.getViewMode() != "auto" {
		t.Errorf("view mode after phase change = %q, want auto", tc.getViewMode())
	}
}

func TestPolicy_BoundaryDelayAndClamp(t *testing.T) {
	base := time.Date(2026, 10, 10, 10, 0, 0, 0, time.UTC)

	t.Run("zero Until", func(t *testing.T) {
		p := Policy{}
		if d := p.boundaryDelay(base); d != 0 {
			t.Errorf("boundaryDelay = %v, want 0", d)
		}
		if c := p.clampToBoundary(base, 600*time.Second); c != 600*time.Second {
			t.Errorf("clampToBoundary = %v, want 600s", c)
		}
	})

	t.Run("future Until", func(t *testing.T) {
		p := Policy{Until: base.Add(180 * time.Second)}
		// boundaryDelay includes PollSettleOffset (500ms)
		expected := 180*time.Second + PollSettleOffset
		if d := p.boundaryDelay(base); d != expected {
			t.Errorf("boundaryDelay = %v, want %v", d, expected)
		}
		// clamps interval that exceeds boundary
		if c := p.clampToBoundary(base, 600*time.Second); c != expected {
			t.Errorf("clampToBoundary(600s) = %v, want %v", c, expected)
		}
		// does not lengthen smaller interval
		if c := p.clampToBoundary(base, 60*time.Second); c != 60*time.Second {
			t.Errorf("clampToBoundary(60s) = %v, want 60s", c)
		}
	})

	t.Run("skew grace period", func(t *testing.T) {
		// Boundary was 30 seconds ago (< 2min skew grace)
		p := Policy{Until: base.Add(-30 * time.Second)}
		if d := p.boundaryDelay(base); d != boundaryRetryDelay {
			t.Errorf("boundaryDelay within skew grace = %v, want %v", d, boundaryRetryDelay)
		}

		// Boundary was 5 minutes ago (> 2min skew grace)
		pOld := Policy{Until: base.Add(-300 * time.Second)}
		if d := pOld.boundaryDelay(base); d != 0 {
			t.Errorf("boundaryDelay past skew grace = %v, want 0", d)
		}
	})
}

func TestFallbackPollInterval(t *testing.T) {
	now := time.Date(2026, 10, 10, 10, 0, 0, 0, time.UTC)
	p := Policy{Until: now.Add(30 * time.Minute)}

	// Active policy with known last poll seconds
	if got := fallbackPollInterval(now, p, 300, 1); got != 300*time.Second {
		t.Errorf("fallbackPollInterval = %v, want 300s", got)
	}

	// Expired policy with failure backoff
	pExpired := Policy{Until: now.Add(-10 * time.Minute)}
	cases := []struct {
		failures int
		want     time.Duration
	}{
		{0, PeakPollInterval},
		{1, PeakPollInterval},
		{2, 120 * time.Second},
		{3, 240 * time.Second},
		{4, 480 * time.Second},
		{5, EcoPollInterval}, // 600s
		{10, EcoPollInterval},
	}
	for _, tc := range cases {
		t.Run(fmt.Sprintf("%d failures", tc.failures), func(t *testing.T) {
			if got := fallbackPollInterval(now, pExpired, 300, tc.failures); got != tc.want {
				t.Errorf("failures %d: got %v, want %v", tc.failures, got, tc.want)
			}
		})
	}
}

func TestPolicy_String(t *testing.T) {
	p := Policy{
		Phase:    "peak",
		Until:    time.Date(2026, 10, 10, 9, 30, 0, 0, time.UTC),
		Suspend:  false,
		Session:  90 * time.Second,
		FastHold: 600 * time.Second,
		Hold:     2700 * time.Second,
	}
	str := p.String()
	if !strings.Contains(str, "phase=peak") || !strings.Contains(str, "until=09:30") || !strings.Contains(str, "suspend=false") {
		t.Errorf("Policy.String() = %q, missing expected substrings", str)
	}

	pEmpty := Policy{}
	if !strings.Contains(pEmpty.String(), "phase=(none)") || !strings.Contains(pEmpty.String(), "until=-") {
		t.Errorf("empty Policy.String() = %q, want defaults", pEmpty.String())
	}
}

func FuzzParsePolicy(f *testing.F) {
	// Seed with valid and edge-case policies
	f.Add("v=1;phase=peak;until=1760103000;suspend=0;session=90;fast=600;hold=2700;sl=8,12")
	f.Add("v=1;phase=offpeak;suspend=1;session=5;fast=-10;hold=999999;sl=100,-5")
	f.Add("v=1")
	f.Add("")
	f.Add("garbage;not=valid")
	f.Add("v=2;phase=peak")

	f.Fuzz(func(t *testing.T, s string) {
		p, ok := parsePolicy(s)
		if ok {
			// Invariant: parsed policy values must always be clamped to documented valid ranges
			if p.Session < 10*time.Second || p.Session > 1800*time.Second {
				t.Fatalf("Session out of range: %v", p.Session)
			}
			if p.FastHold < 0 || p.FastHold > 7200*time.Second {
				t.Fatalf("FastHold out of range: %v", p.FastHold)
			}
			if p.Hold < 0 || p.Hold > 14400*time.Second {
				t.Fatalf("Hold out of range: %v", p.Hold)
			}
			if p.SessionBrightness < 0 || p.SessionBrightness > 24 {
				t.Fatalf("SessionBrightness out of range: %v", p.SessionBrightness)
			}
			if p.SessionWarmth < 0 || p.SessionWarmth > 24 {
				t.Fatalf("SessionWarmth out of range: %v", p.SessionWarmth)
			}
		}
	})
}
