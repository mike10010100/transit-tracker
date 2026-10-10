package main

import (
	"testing"
	"time"
)

func TestGetNextPollInterval(t *testing.T) {
	tc := NewTrackerClient("http://localhost:8000", "auto")

	t.Run("Honors server interval when no manual interaction active", func(t *testing.T) {
		interval := tc.getNextPollInterval(600)
		if interval != 10*time.Minute {
			t.Errorf("Expected 10m from server interval 600, got %v", interval)
		}

		intervalRush := tc.getNextPollInterval(60)
		if intervalRush != 60*time.Second {
			t.Errorf("Expected 60s from server interval 60, got %v", intervalRush)
		}
	})

	t.Run("Manual interaction boosts to 60s even if server requests 10m", func(t *testing.T) {
		tc.dataInteraction()
		interval := tc.getNextPollInterval(600)
		if interval != 60*time.Second {
			t.Errorf("Expected 60s boost after manual interaction, got %v", interval)
		}
	})

	t.Run("Manual boost expires after fast poll hold", func(t *testing.T) {
		tc.mu.Lock()
		tc.overlay.FastUntil = time.Now().Add(-11 * time.Minute)
		tc.overlay.HoldUntil = time.Now().Add(-11 * time.Minute)
		tc.mu.Unlock()

		interval := tc.getNextPollInterval(600)
		if interval != 10*time.Minute {
			t.Errorf("Expected 10m after boost expiration, got %v", interval)
		}
	})

	t.Run("Manual boost still active within fast poll hold", func(t *testing.T) {
		tc.mu.Lock()
		tc.overlay.FastUntil = time.Now().Add(5 * time.Minute)
		tc.overlay.HoldUntil = time.Now().Add(5 * time.Minute)
		tc.overlay.State = StateHold
		tc.mu.Unlock()

		if interval := tc.getNextPollInterval(600); interval != 60*time.Second {
			t.Errorf("Expected 60s within boost window, got %v", interval)
		}
	})

	t.Run("Fallback interval when server sends 0 uses local policy", func(t *testing.T) {
		tc.mu.Lock()
		tc.overlay = Overlay{}
		tc.policy = defaultPolicy()
		tc.lastPollSec = 0
		tc.consecutiveFailures = 0
		tc.mu.Unlock()

		interval := tc.getNextPollInterval(0)
		if interval != 60*time.Second {
			t.Errorf("Expected 60s fallback on first poll, got %v", interval)
		}
	})
}

func TestAlignDelay(t *testing.T) {
	base := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

	t.Run("Sub-minute intervals are returned unchanged", func(t *testing.T) {
		if got := alignDelay(base, 45*time.Second); got != 45*time.Second {
			t.Errorf("expected 45s unchanged, got %v", got)
		}
	})

	t.Run("One-minute interval waits to the next minute plus offset", func(t *testing.T) {
		// At :00.000 exactly, next boundary is +60s, plus the settle offset.
		now := base.Add(3 * time.Second) // 12:00:03
		got := alignDelay(now, time.Minute)
		want := 57*time.Second + PollSettleOffset
		if got != want {
			t.Errorf("expected %v, got %v", want, got)
		}
	})

	t.Run("Ten-minute interval aligns to the wall clock", func(t *testing.T) {
		// 12:03:00 -> next ten-minute boundary is 12:10:00 = 7m, plus offset.
		now := base.Add(3 * time.Minute)
		got := alignDelay(now, 10*time.Minute)
		want := 7*time.Minute + PollSettleOffset
		if got != want {
			t.Errorf("expected %v, got %v", want, got)
		}
	})

	t.Run("Result is always positive and within one period", func(t *testing.T) {
		interval := 10 * time.Minute
		for s := 0; s < 3600; s += 17 {
			now := base.Add(time.Duration(s) * time.Second)
			got := alignDelay(now, interval)
			if got <= 0 || got > interval+PollSettleOffset {
				t.Fatalf("offset %ds: got %v out of range", s, got)
			}
		}
	})
}
