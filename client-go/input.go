package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"time"
)

// configureGestureHandlers wires every gesture callback onto the detector.
// Extracted from startInputListeners so the callback behavior can be tested
// without opening real input devices.
func (tc *TrackerClient) configureGestureHandlers(gd *GestureDetector, cancel context.CancelFunc) {
	refresh := func() {
		tc.noteTouch()
		select {
		case tc.refreshCh <- struct{}{}:
		default:
		}
	}

	gd.OnLog = func(msg string) { tc.logRemote(msg) }

	gd.OnSingleTap = func(x, y int32) {
		// Taps in the main content area are deliberately inert: the frontlight
		// has its own LIGHT button, and cycling it on every tap was confusing
		// ("only brightness changed"). Still acknowledge the touch so an awake
		// interaction session doesn't expire while the user is poking around.
		tc.noteTouch()
	}
	gd.OnDoubleTap = func(x, y int32) {
		// A double tap is trivially easy to trigger accidentally; do NOT exit.
		// It forces a refresh, the least-surprising safe action.
		tc.dataInteraction()
		tc.logRemote(fmt.Sprintf("Double tap at (%d, %d)! Refreshing...", x, y))
		refresh()
	}
	gd.OnTopRightTap = func(x, y int32) {
		// Like the double tap, this corner must not exit accidentally.
		tc.dataInteraction()
		tc.logRemote(fmt.Sprintf("Top-Right corner tapped at (%d, %d)! Refreshing...", x, y))
		refresh()
	}
	gd.OnTopLeftTap = func(x, y int32) {
		tc.dataInteraction()
		tc.logRemote(fmt.Sprintf("Top-Left corner tapped at (%d, %d)! Refreshing...", x, y))
		refresh()
	}
	gd.OnBottomLeftTap = func(x, y int32) {
		// The view choice itself arms the hold and fast poll (EvViewTap).
		newMode := tc.cycleViewMode()
		tc.logRemote(fmt.Sprintf("Bottom-Left corner tapped at (%d, %d)! View mode cycled to: %s. Refreshing...", x, y, newMode))
		refresh()
	}
	gd.OnBusesTap = func(x, y int32) {
		// The view choice itself arms the hold and fast poll (EvViewTap).
		newMode := tc.setExplicitViewMode("evening")
		tc.logRemote(fmt.Sprintf("BUSES button tapped at (%d, %d)! View set to: %s. Refreshing...", x, y, newMode))
		refresh()
	}
	gd.OnBikesTap = func(x, y int32) {
		// The view choice itself arms the hold and fast poll (EvViewTap).
		newMode := tc.setExplicitViewMode("morning")
		tc.logRemote(fmt.Sprintf("CITI BIKE button tapped at (%d, %d)! View set to: %s. Refreshing...", x, y, newMode))
		refresh()
	}
	gd.OnLightTap = func(x, y int32) {
		// Screen-only action: no fast-poll hold (see OnSingleTap). The lipc calls
		// inside are individually bounded by lipcCallTimeout, so a slow powerd
		// can stall the dispatcher at most briefly.
		tc.noteTouch()
		tc.logRemote(fmt.Sprintf("LIGHT button tapped at (%d, %d)! Cycling frontlight...", x, y))
		tc.cycleFrontlight()
	}
	gd.OnRefreshTap = func(x, y int32) {
		tc.dataInteraction()
		tc.logRemote(fmt.Sprintf("REFRESH button tapped at (%d, %d)! Refreshing...", x, y))
		refresh()
	}
}

// runEventLoop dispatches multiplexed input events to the gesture detector
// until the context is cancelled.
func (tc *TrackerClient) runEventLoop(ctx context.Context, cancel context.CancelFunc, eventCh <-chan RawEventMsg) {
	cfg := DefaultGestureConfig()
	cfg.Transform = tc.rawTouchToDesign
	gd := NewGestureDetector(cfg)
	defer gd.Stop()
	tc.configureGestureHandlers(gd, cancel)

	for {
		select {
		case <-ctx.Done():
			return
		case ev := <-eventCh:
			if IsPowerKeyEvent(ev) {
				if tc.exitOnPowerKey {
					tc.logRemote(fmt.Sprintf("Power button pressed on %s! Exiting...", ev.Device))
					cancel()
					return
				}
				// Low-power dashboard mode: the power key is a wake source, not
				// an exit. Ignore it so pressing power to wake doesn't kill us.
				tc.logRemote("Power key ignored (dedicated dashboard mode).")
				continue
			}
			gd.ProcessEvent(ev)
		}
	}
}

// startInputListeners opens ALL /dev/input/event* devices and multiplexes events into eventCh
func (tc *TrackerClient) startInputListeners(ctx context.Context, cancel context.CancelFunc) {
	// Prime the panel size once here, on the caller's goroutine, so the event
	// goroutine's coordinate transform reads a cached value and never touches
	// the filesystem (also avoids a data race with test seam restoration).
	_ = tc.getPanelSize()

	matches, err := globInputs("/dev/input/event*")
	if err != nil || len(matches) == 0 {
		matches = []string{"/dev/input/event0", "/dev/input/event1", "/dev/input/event2"}
	}

	tc.logRemote(fmt.Sprintf("Found input devices: %v", matches))

	eventCh := make(chan RawEventMsg, 128)

	for _, devPath := range matches {
		f, err := osOpen(devPath)
		if err != nil {
			continue
		}
		tc.logRemote(fmt.Sprintf("Opened input device listener on %s", devPath))

		tc.wg.Add(1)
		go func(path string, file *os.File) {
			defer tc.wg.Done()
			defer file.Close()

			closeDone := make(chan struct{})
			defer close(closeDone)
			go func() {
				select {
				case <-ctx.Done():
					_ = file.Close()
				case <-closeDone:
				}
			}()

			buf := make([]byte, 512)
			consecutiveErrs := 0

			for {
				select {
				case <-ctx.Done():
					return
				default:
				}

				n, err := file.Read(buf)
				if err != nil {
					select {
					case <-ctx.Done():
						return
					default:
					}
					consecutiveErrs++
					backoffDelay := 100 * time.Millisecond
					if consecutiveErrs > 10 {
						backoffDelay = 500 * time.Millisecond
					}
					select {
					case <-ctx.Done():
						return
					case <-time.After(backoffDelay):
					}
					continue
				}
				consecutiveErrs = 0

				events := ParseInputEvents(buf, n, path)
				for _, ev := range events {
					select {
					case eventCh <- ev:
					default:
					}
				}
			}
		}(devPath, f)
	}

	// Dispatcher goroutine: processes all events from all devices
	tc.wg.Add(1)
	go func() {
		defer tc.wg.Done()
		tc.runEventLoop(ctx, cancel, eventCh)
	}()
}

// startPowerListener watches for power button sleep events via lipc
func (tc *TrackerClient) startPowerListener(ctx context.Context, exitCancel context.CancelFunc) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		cmd := execCommandContext(ctx, "lipc-wait-event", "com.lab126.powerd", "goingToScreenSaver")
		cmd.Stdout = io.Discard
		cmd.Stderr = io.Discard
		if err := cmd.Run(); err == nil {
			tc.logRemote("powerd goingToScreenSaver event received! Exiting...")
			exitCancel()
			return
		}
		if ctx.Err() != nil {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(100 * time.Millisecond):
		}
	}
}
