package main

import (
	"testing"
	"time"
)

func TestCycleViewMode(t *testing.T) {
	t.Run("Default auto with evening rendered cycles to morning", func(t *testing.T) {
		tc := NewTrackerClient("http://localhost:8000", "auto")
		tc.lastRenderedView = "evening"

		mode1 := tc.cycleViewMode()
		if mode1 != "morning" {
			t.Errorf("cycleViewMode() = %q, want %q", mode1, "morning")
		}

		mode2 := tc.cycleViewMode()
		if mode2 != "evening" {
			t.Errorf("cycleViewMode() = %q, want %q", mode2, "evening")
		}

		mode3 := tc.cycleViewMode()
		if mode3 != "morning" {
			t.Errorf("cycleViewMode() = %q, want %q", mode3, "morning")
		}
	})

	t.Run("Default auto with morning rendered cycles to evening", func(t *testing.T) {
		tc := NewTrackerClient("http://localhost:8000", "auto")
		tc.lastRenderedView = "morning"

		mode1 := tc.cycleViewMode()
		if mode1 != "evening" {
			t.Errorf("cycleViewMode() = %q, want %q", mode1, "evening")
		}

		mode2 := tc.cycleViewMode()
		if mode2 != "morning" {
			t.Errorf("cycleViewMode() = %q, want %q", mode2, "morning")
		}

		mode3 := tc.cycleViewMode()
		if mode3 != "evening" {
			t.Errorf("cycleViewMode() = %q, want %q", mode3, "evening")
		}
	})

	t.Run("Manual hold expiration reverts to auto", func(t *testing.T) {
		tc := NewTrackerClient("http://localhost:8000", "auto")
		tc.lastRenderedView = "evening"

		tc.cycleViewMode() // sets to morning and manualViewTime = now
		if tc.getViewMode() != "morning" {
			t.Errorf("getViewMode() = %q, want %q", tc.getViewMode(), "morning")
		}

		// Simulate hold expiration passing
		tc.mu.Lock()
		tc.overlay.HoldUntil = time.Now().Add(-1 * time.Minute)
		tc.overlay.FastUntil = time.Now().Add(-1 * time.Minute)
		tc.mu.Unlock()
		if tc.getViewMode() != "auto" {
			t.Errorf("getViewMode() after hold = %q, want %q", tc.getViewMode(), "auto")
		}
	})

	t.Run("Set explicit view mode sets target and manualViewTime", func(t *testing.T) {
		tc := NewTrackerClient("http://localhost:8000", "auto")
		mode := tc.setExplicitViewMode("evening")
		if mode != "evening" || tc.getViewMode() != "evening" {
			t.Errorf("setExplicitViewMode(evening) = %q, want %q", mode, "evening")
		}

		mode2 := tc.setExplicitViewMode("morning")
		if mode2 != "morning" || tc.getViewMode() != "morning" {
			t.Errorf("setExplicitViewMode(morning) = %q, want %q", mode2, "morning")
		}
	})
}
