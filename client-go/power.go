package main

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// suspendSettleDelay is how long to let powerd settle after releasing the
// screensaver, before attempting to suspend. Variable for tests.
var suspendSettleDelay = 2 * time.Second

// Sysfs wakealarm and power-state nodes. On the PW5 (BD71828 RTC) the kernel
// wakealarm is the reliable wake source; powerd's rtcWakeup is only writable
// inside its readyToSuspend window and adds a ~60s delay, so we drive the
// kernel directly.
var (
	sysfsWakeAlarmPath = "/sys/class/rtc/rtc0/wakealarm"
	powerStatePath     = "/sys/power/state"
)

// minSuspendInterval is the shortest poll interval at which we bother to
// suspend. Below this, the ~15-25s suspend/resume round-trip (RTC relock, EPDC
// refresh, Wi-Fi re-association) costs more than it saves, so we stay awake.
var minSuspendInterval = 120 * time.Second

// setWireless toggles the Wi-Fi radio. Disabling it is REQUIRED before a direct
// suspend: an associated interface holds the "WLAN timeout" wakeup source, which
// makes /sys/power/state return EBUSY.
func (tc *TrackerClient) setWireless(on bool) {
	val := "0"
	if on {
		val = "1"
	}
	lipcSet("com.lab126.cmd", "wirelessEnable", val)
}

// armSysfsWake programs the hardware RTC to fire in `in` seconds. The alarm must
// be cleared (0) before setting +N on this kernel.
func (tc *TrackerClient) armSysfsWake(in time.Duration) bool {
	secs := int(in.Seconds())
	if secs < 1 {
		secs = 1
	}
	_ = osWriteFile(sysfsWakeAlarmPath, []byte("0"), 0644)
	if err := osWriteFile(sysfsWakeAlarmPath, []byte("+"+strconv.Itoa(secs)), 0644); err != nil {
		return false
	}
	return true
}

// enterSuspend writes "mem" to /sys/power/state. It blocks until the device
// resumes (RTC alarm or user input), returning the elapsed time. On failure it
// returns the error.
func (tc *TrackerClient) enterSuspend() (time.Duration, error) {
	start := time.Now()
	err := osWriteFile(powerStatePath, []byte("mem"), 0644)
	return time.Since(start), err
}

// rtcAlarmStillArmed reports whether the RTC wake alarm is still programmed after
// a resume. A fired alarm clears itself, so "still armed" means the RTC did NOT
// wake us -- a person did (touch/power). This is the decisive wake-reason signal.
func (tc *TrackerClient) rtcAlarmStillArmed() bool {
	b, err := osReadFile(sysfsWakeAlarmPath)
	if err != nil {
		return false
	}
	v := strings.TrimSpace(string(b))
	return v != "" && v != "0"
}

// disarmRTC clears the wake alarm (used after a non-RTC wake so a stale alarm
// can't fire a spurious resume mid-interaction).
func (tc *TrackerClient) disarmRTC() {
	_ = osWriteFile(sysfsWakeAlarmPath, []byte("0"), 0644)
}

// runSleepLoop is the low-power mode for a DEDICATED e-ink dashboard. It cycles
// true suspend-to-RAM with an RTC wake, which the device validates as:
//
//	render -> (once) stop lab126_gui + unload the screensaver
//	  -> arm /sys/class/rtc/rtc0/wakealarm +interval
//	  -> disable Wi-Fi (releases the WLAN wakeup source; else EBUSY)
//	  -> echo mem > /sys/power/state   (blocks until the RTC fires)
//	  -> re-enable Wi-Fi, render again
//
// The rendered e-ink image is bistable and stays visible through the suspend.
// `allowSuspend` gates the whole path for safe testing; without it we wait on
// the wall clock. Intervals below minSuspendInterval also stay awake.
func (tc *TrackerClient) runSleepLoop(ctx context.Context, cancel context.CancelFunc, allowSuspend bool) {
	prepared := false
	for {
		select {
		case <-ctx.Done():
			tc.cleanup()
			return
		default:
		}

		// Stay awake and ensure Wi-Fi is up to fetch/render.
		lipcSet("com.lab126.powerd", "preventScreenSaver", "1")
		tc.setWireless(true)
		if !tc.waitForNetwork(ctx) {
			tc.cleanup()
			return
		}
		serverPollSec := tc.fetchAndDrawDashboard(ctx, cancel)
		// Sleep mode follows the *schedule's* cadence, deliberately ignoring the
		// resident-mode "fast poll" hold. Otherwise an off-peak data tap held the
		// device awake for 10 minutes showing the idle face -- while the button
		// handlers were still live (deceptive). Off-peak we suspend; peak (60s)
		// keeps us awake and interactive.
		interval := time.Duration(serverPollSec) * time.Second
		if serverPollSec <= 0 {
			interval = tc.getNextPollInterval(serverPollSec)
		}

		policy := tc.currentPolicy()
		if !allowSuspend || !policy.Suspend || interval < minSuspendInterval {
			tc.logRemote(fmt.Sprintf("Sleep mode: staying awake for %s (allow=%v, suspend=%v).", interval.Round(time.Second), allowSuspend, policy.Suspend))
			if !tc.sleepWallClock(ctx, interval) {
				tc.cleanup()
				return
			}
			continue
		}

		wakeInterval := policy.clampToBoundary(time.Now(), interval)
		if wakeInterval < minSuspendInterval {
			tc.logRemote(fmt.Sprintf("Sleep mode: boundary wake in %s (< minSuspendInterval); staying awake.", wakeInterval.Round(time.Second)))
			if !tc.sleepWallClock(ctx, wakeInterval) {
				tc.cleanup()
				return
			}
			continue
		}
		if wakeInterval < interval {
			tc.logRemote(fmt.Sprintf("Sleep mode: clamped sleep interval %s -> %s (phase until %s).", interval.Round(time.Second), wakeInterval.Round(time.Second), policy.Until.Format("15:04")))
		}

		if !prepared {
			tc.prepareDisplayForSleep(ctx)
			prepared = true
			tc.logRemote("Sleep mode: framework stopped, screensaver unloaded.")
		}

		// Touch cannot wake this SoC (the Parade driver never arms its IRQ for
		// wake, and the touch rails are cut in suspend), so we do not arm it.
		// The power button is the wake source; pressing it starts an interaction
		// session (detected below via a still-armed RTC alarm).
		tc.releaseScreenSaver()
		if !tc.armSysfsWake(wakeInterval) {
			tc.logRemote("Sleep mode: could not arm RTC; wall-clock wait.")
			tc.setWireless(true)
			if !tc.sleepWallClock(ctx, wakeInterval) {
				tc.cleanup()
				return
			}
			continue
		}

		tc.logRemote(fmt.Sprintf("Sleep mode: suspending for %s (rtc armed, face=%s).", wakeInterval.Round(time.Second), tc.getPresentation()))
		tc.setWireless(false)
		time.Sleep(suspendSettleDelay)
		elapsed, err := tc.enterSuspend()
		tc.setWireless(true)
		if err != nil {
			tc.logRemote(fmt.Sprintf("Sleep mode: suspend failed (%v) after %s; wall-clock wait.", err, elapsed.Round(time.Second)))
			if !tc.sleepWallClock(ctx, wakeInterval) {
				tc.cleanup()
				return
			}
			continue
		}

		// Wake reason: a fired RTC alarm clears itself, so a still-armed alarm
		// means a person woke us by pressing the power button, not the schedule.
		if tc.rtcAlarmStillArmed() {
			tc.disarmRTC()
			tc.logRemote(fmt.Sprintf("Power-button wake after %s: starting interactive session.", elapsed.Round(time.Second)))
			sessionTimeout := policy.Session
			if interactionHoldDuration != 90*time.Second || sessionTimeout == 0 {
				sessionTimeout = interactionHoldDuration
			}
			if !tc.interactionAwake(ctx, cancel, sessionTimeout) {
				tc.cleanup()
				return
			}
			continue
		}
		tc.logRemote(fmt.Sprintf("Sleep mode: woke after %s.", elapsed.Round(time.Second)))
	}
}

// waitForNetwork waits until the default route is reachable, so a fetch right
// after resume doesn't fail before Wi-Fi has re-associated. Bounded so it can't
// hang; the caller still proceeds and lets the fetch retry if it times out.
func (tc *TrackerClient) waitForNetwork(ctx context.Context) bool {
	for i := 0; i < 20; i++ {
		select {
		case <-ctx.Done():
			return false
		default:
		}
		if checkNetworkFn(ctx) {
			return true
		}
		time.Sleep(500 * time.Millisecond)
	}
	return true
}

// checkNetworkFn reports whether the network looks usable. Seam for tests.
var checkNetworkFn = func(ctx context.Context) bool {
	cmd := execCommandContext(ctx, "ip", "route")
	out, err := cmd.Output()
	if err != nil {
		return false
	}
	return strings.Contains(string(out), "default")
}

// sleepWallClock waits `d`, interruptible by ctx or forced refresh taps. Returns false if ctx ended.
func (tc *TrackerClient) sleepWallClock(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	case <-tc.refreshCh:
		return true
	}
}
