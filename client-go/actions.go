// Package main implements the Kindle Transit Tracker client.
package main

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

var (
	osExit    = os.Exit
	exitDelay = 500 * time.Millisecond
)

// DeviceAction is a named, allowlisted maintenance action the server can ask a
// client to run. Actions are fixed in code (never arbitrary shell from the
// network), so a compromised/misconfigured server can only trigger these.
type DeviceAction struct {
	Fn func(ctx context.Context) string
	// Timeout bounds the action. A suspend test blocks for its whole duration,
	// so it needs a larger budget than a quick probe.
	Timeout time.Duration
}

// defaultActionTimeout bounds actions that don't specify their own.
var defaultActionTimeout = 60 * time.Second

// deviceActions maps action names to their implementations.
var deviceActions = map[string]DeviceAction{
	"restart":          {Fn: actionRestart},
	"reboot":           {Fn: actionReboot},
	"update":           {Fn: actionUpdate},
	"clear_backup":     {Fn: actionClearBackup},
	"clear-backup":     {Fn: actionClearBackup},
	"disable-ads":      {Fn: actionDisableAds},
	"disable_ads":      {Fn: actionDisableAds},
	"stop-framework":   {Fn: actionStopFramework},
	"stop_framework":   {Fn: actionStopFramework},
	"start-framework":  {Fn: actionStartFramework},
	"start_framework":  {Fn: actionStartFramework},
	"framework-state":  {Fn: actionFrameworkState},
	"framework_state":  {Fn: actionFrameworkState},
	"sleep-test":       {Fn: actionSleepTest},
	"sleep_test":       {Fn: actionSleepTest},
	"rtc-suspend":      {Fn: actionRTCSuspend, Timeout: 5 * time.Minute},
	"rtc_suspend":      {Fn: actionRTCSuspend, Timeout: 5 * time.Minute},
	"input-wake-probe": {Fn: actionInputWakeProbe},
	"input_wake_probe": {Fn: actionInputWakeProbe},
	"touch-wake-test":  {Fn: actionTouchWakeTest, Timeout: 7 * time.Minute},
	"touch_wake_test":  {Fn: actionTouchWakeTest, Timeout: 7 * time.Minute},
	"touch-wake-probe": {Fn: actionTouchWakeProbe},
	"touch_wake_probe": {Fn: actionTouchWakeProbe},
}

func actionRestart(ctx context.Context) string {
	go func() {
		if exitDelay > 0 {
			time.Sleep(exitDelay)
		}
		osExit(0)
	}()
	return "restarting client binary..."
}

func actionReboot(ctx context.Context) string {
	return shell(ctx, "reboot 2>&1 || shutdown -r now 2>&1")
}

func actionUpdate(ctx context.Context) string {
	go func() {
		if exitDelay > 0 {
			time.Sleep(exitDelay)
		}
		osExit(0)
	}()
	return shell(ctx, "rm -f /tmp/tracker-arm /tmp/tracker 2>&1; echo update scheduled on restart")
}

func actionClearBackup(ctx context.Context) string {
	return shell(ctx, "rm -rf /mnt/us/documents/tracker_backup /tmp/tracker_backup* 2>&1; echo done")
}

// runAction executes a named action and returns a human-readable result.
func runAction(ctx context.Context, name string) (string, bool) {
	act, ok := deviceActions[name]
	if !ok {
		return fmt.Sprintf("unknown action %q", name), false
	}
	timeout := act.Timeout
	if timeout == 0 {
		timeout = defaultActionTimeout
	}
	actx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return act.Fn(actx), true
}

func shell(ctx context.Context, script string) string {
	cmd := execCommandContext(ctx, "sh", "-c", script)
	out, err := cmd.CombinedOutput()
	s := strings.TrimSpace(string(out))
	if err != nil && s == "" {
		return fmt.Sprintf("<error: %v>", err)
	}
	if err != nil {
		return fmt.Sprintf("%s\n<exit: %v>", s, err)
	}
	return s
}

// actionDisableAds removes Amazon Special Offers so the ad screensaver no longer
// covers the dashboard. Idempotent; reversible via the Amazon account.
func actionDisableAds(ctx context.Context) string {
	var b strings.Builder

	// Which sqlite tool exists? The Kindle image usually ships none of these.
	b.WriteString("sqlite tools: " + shell(ctx, "command -v sqlite3; command -v sqlite; command -v dbclient") + "\n")

	// Flip adunit.viewable=false via sqlite3 if present.
	db := "/var/local/appreg.db"
	if shell(ctx, "command -v sqlite3") != "" {
		sql := `sqlite3 ` + db + ` "UPDATE properties SET value='false' WHERE name='adunit.viewable';" 2>&1`
		b.WriteString("sqlite3 update: " + shell(ctx, sql) + "\n")
	}

	// Always remove the ad unit assets and the store marker.
	b.WriteString("rm adunits: " + shell(ctx, "rm -rf /var/local/adunits /mnt/us/.assets 2>&1; echo done") + "\n")

	// Verify the flag's raw bytes and the asset dirs.
	b.WriteString("flag: " + shell(ctx, "grep -a -o 'adunit.viewable[^ ]*' "+db+" 2>/dev/null | head -3; echo") + "\n")
	b.WriteString("assets: " + shell(ctx, "ls -la /var/local/adunits 2>&1 | head -2; ls -la /mnt/us/.assets 2>&1 | head -2") + "\n")
	return strings.TrimSpace(b.String())
}

// actionStopFramework stops the Amazon UI framework (lab126_gui/cvm) so nothing
// repaints over the dashboard and the device can idle-suspend. powerd and eips
// are unaffected.
func actionStopFramework(ctx context.Context) string {
	return shell(ctx, "stop lab126_gui 2>&1; stop framework 2>&1; initctl stop lab126_gui 2>&1; echo done; ps -eo pid,comm | grep -iE 'lab126|cvm|framework' 2>&1")
}

// actionStartFramework restarts the Amazon UI (for maintenance/reading).
func actionStartFramework(ctx context.Context) string {
	return shell(ctx, "start lab126_gui 2>&1; echo done")
}

// actionFrameworkState reports which framework daemons are running.
func actionFrameworkState(ctx context.Context) string {
	return shell(ctx, "ps -eo pid,user,comm 2>/dev/null | grep -iE 'lab126|powerd|blanket|framework|appmgrd|cvm' ; echo '---'; cat /sys/power/wake_lock 2>/dev/null; echo '---wakeup_count---'; cat /sys/power/wakeup_count 2>/dev/null")
}

// actionSleepTest reports the conditions relevant to a suspend attempt without
// actually suspending: wakelocks, framework presence, and the RTC alarm node.
func actionSleepTest(ctx context.Context) string {
	var b strings.Builder
	b.WriteString("wake_lock: " + shell(ctx, "cat /sys/power/wake_lock 2>/dev/null; echo") + "\n")
	b.WriteString("framework: " + shell(ctx, "ps -eo comm | grep -iE 'cvm|lab126' | tr '\\n' ' '") + "\n")
	b.WriteString("rtc: " + shell(ctx, "cat /sys/class/rtc/rtc0/wakealarm 2>/dev/null; echo") + "\n")
	b.WriteString("ads: " + shell(ctx, "ls /var/local/adunits 2>/dev/null && echo present || echo absent") + "\n")
	return strings.TrimSpace(b.String())
}

// actionRTCSuspend performs ONE proven suspend/wake cycle to validate the
// mechanism end to end:
//
//	stop lab126_gui, unload screensavers, disable wi-fi (releases "WLAN timeout"),
//	arm /sys/class/rtc/rtc0/wakealarm +90, echo mem > /sys/power/state,
//	(wake) re-enable wi-fi (the client's next poll proves the control channel).
//
// It reports what happened at each step. This is the decisive experiment.
func actionRTCSuspend(ctx context.Context) string {
	var b strings.Builder

	b.WriteString("stop framework: " + shell(ctx, "stop lab126_gui 2>&1; echo done") + "\n")
	b.WriteString("unload screensaver: " + shell(ctx, "lipc-set-prop com.lab126.blanket unload screensaver 2>&1; lipc-set-prop com.lab126.blanket unload splash 2>&1; echo done") + "\n")
	b.WriteString("frontlight off: " + shell(ctx, "lipc-set-prop -i com.lab126.powerd flIntensity 0 2>&1; echo done") + "\n")

	// Arm the RTC BEFORE suspending: clear then +90s.
	b.WriteString("arm rtc: " + shell(ctx, "echo 0 > /sys/class/rtc/rtc0/wakealarm; echo +90 > /sys/class/rtc/rtc0/wakealarm; echo rc=$?; cat /sys/class/rtc/rtc0/wakealarm") + "\n")

	// Free the Wi-Fi wakeup source, then suspend. The go binary is a separate
	// process; this whole cycle runs inside it, so it survives the freeze.
	b.WriteString("wifi off: " + shell(ctx, "lipc-set-prop com.lab126.cmd wirelessEnable 0 2>&1; echo done") + "\n")
	time.Sleep(1 * time.Second)

	before := time.Now()
	// This write blocks until the RTC wakes the device (or fails).
	b.WriteString("suspend: " + shell(ctx, "echo mem > /sys/power/state 2>&1; echo rc=$?") + "\n")
	elapsed := time.Since(before)

	b.WriteString("wifi on: " + shell(ctx, "lipc-set-prop com.lab126.cmd wirelessEnable 1 2>&1; echo done") + "\n")
	b.WriteString(fmt.Sprintf("RESULT: suspended+resumed in %s (if ~90s, the RTC wake worked)", elapsed.Round(time.Second)))
	return b.String()
}

// actionTouchWakeProbe digs into whether the touch panel can actually wake the
// SoC: the i2c device's power attributes and IRQ, /proc/interrupts, debugfs
// wakeup_sources, and relevant dmesg. Read-only; no user interaction needed.
func actionTouchWakeProbe(ctx context.Context) string {
	var b strings.Builder
	b.WriteString("input devices:\n")
	b.WriteString(shell(ctx, "grep -E 'Name|Handlers' /proc/bus/input/devices 2>/dev/null") + "\n")

	b.WriteString("touch i2c device (2-0024) power/irq:\n")
	b.WriteString(shell(ctx, "d=/sys/bus/i2c/devices/2-0024; "+
		"echo \"name=$(cat $d/name 2>/dev/null)\"; "+
		"echo \"driver=$(readlink -f $d/driver 2>/dev/null)\"; "+
		"echo \"wakeup=$(cat $d/power/wakeup 2>/dev/null)\"; "+
		"echo \"control=$(cat $d/power/control 2>/dev/null)\"; "+
		"echo \"irq=$(cat $d/irq 2>/dev/null)\"; "+
		"ls $d/power 2>/dev/null") + "\n")

	b.WriteString("all i2c devices with wakeup:\n")
	b.WriteString(shell(ctx, "for d in /sys/bus/i2c/devices/*; do "+
		"[ -e \"$d/power/wakeup\" ] && echo \"  $(basename $d) [$(cat $d/name 2>/dev/null)] wakeup=$(cat $d/power/wakeup 2>/dev/null)\"; done") + "\n")

	b.WriteString("interrupts (i2c/touch):\n")
	b.WriteString(shell(ctx, "grep -iE 'i2c|touch|2-0024|pt_mt|goodix|cyttsp|elan' /proc/interrupts 2>/dev/null") + "\n")

	b.WriteString("debugfs wakeup_sources:\n")
	b.WriteString(shell(ctx, "cat /sys/kernel/debug/wakeup_sources 2>/dev/null | head -50 || echo '<no debugfs>'") + "\n")

	b.WriteString("dmesg (touch/i2c):\n")
	b.WriteString(shell(ctx, "dmesg 2>/dev/null | grep -iE 'touch|pt_mt|2-0024|cyttsp|goodix|elan|i2c-2' | tail -25 || echo '<no dmesg>'") + "\n")
	return strings.TrimSpace(b.String())
}

// actionInputWakeProbe is read-only: it enumerates every input device, its name,
// and whether it is (or can be) registered as a wakeup source. This tells us if
// a screen tap could ever resume the SoC from suspend, or only the power button
// and RTC can.
func actionInputWakeProbe(ctx context.Context) string {
	var b strings.Builder
	b.WriteString("input devices:\n")
	b.WriteString(shell(ctx, "for d in /sys/class/input/event*; do "+
		"name=$(cat $d/device/name 2>/dev/null); "+
		"echo \"  $(basename $d): $name\"; done") + "\n")

	b.WriteString("wakeup capability (device/power/wakeup):\n")
	b.WriteString(shell(ctx, "for d in /sys/class/input/event*; do "+
		"name=$(cat $d/device/name 2>/dev/null); "+
		"w=$(cat $d/device/power/wakeup 2>/dev/null || echo '<none>'); "+
		"c=$(cat $d/device/power/control 2>/dev/null || echo '<none>'); "+
		"echo \"  $(basename $d) [$name] wakeup=$w control=$c\"; done") + "\n")

	// The input node often isn't the wakeup-capable device; walk up its parents
	// looking for a power/wakeup attribute.
	b.WriteString("parent wakeup nodes (walking up from each event device):\n")
	b.WriteString(shell(ctx, "for d in /sys/class/input/event*; do "+
		"p=$(readlink -f $d/device 2>/dev/null); "+
		"echo \"  $(basename $d) -> $p\"; "+
		"while [ -n \"$p\" ] && [ \"$p\" != \"/\" ]; do "+
		"if [ -e \"$p/power/wakeup\" ]; then "+
		"echo \"      wakeup-capable: $p/power/wakeup = $(cat $p/power/wakeup 2>/dev/null)\"; fi; "+
		"p=$(dirname $p); done; done") + "\n")

	b.WriteString("all wakeup-capable devices in the tree:\n")
	b.WriteString(shell(ctx, "find /sys/devices -name wakeup -path '*/power/*' 2>/dev/null | while read w; do "+
		"v=$(cat $w 2>/dev/null); case \"$v\" in enabled|disabled) echo \"  $v  $w\";; esac; done") + "\n")

	b.WriteString("armed/locked wakeup sources:\n")
	b.WriteString(shell(ctx, "echo '  wake_lock:'; sed 's/^/    /' /sys/power/wake_lock 2>/dev/null; "+
		"echo '  wakeup_count: '$(cat /sys/power/wakeup_count 2>/dev/null)") + "\n")
	return strings.TrimSpace(b.String())
}

// actionTouchWakeTest is the decisive experiment: register any touch input device
// as a wakeup source, then suspend with a short RTC alarm as a SAFETY NET so the
// device can never stay stuck. If the device resumes before the RTC fires, a
// touch woke it (tap-to-wake is possible). If it resumes at ~the RTC delay, only
// the RTC woke it. Wi-Fi is disabled so the "WLAN timeout" source can't muddy the
// result. Read-only w.r.t. persistent state except the wakeup toggle, which it
// restores afterward.
func actionTouchWakeTest(ctx context.Context) string {
	var b strings.Builder

	// Snapshot the two wakeup sources that matter (touch panel + RTC) so we can
	// compare event/wakeup counters across the suspend. Independent of timing.
	wakeupBefore := wakeupSourceSnapshot(ctx)
	b.WriteString("wakeup_sources before:\n" + wakeupBefore + "\n")

	// Safety net: arm the RTC first so we always wake even if nothing else does.
	// A long 300s window gives a distracted human plenty of time to tap.
	b.WriteString("arm rtc safety: " + shell(ctx, "echo 0 > /sys/class/rtc/rtc0/wakealarm; echo +300 > /sys/class/rtc/rtc0/wakealarm; echo rc=$?; cat /sys/class/rtc/rtc0/wakealarm") + "\n")

	// Snapshot every wakeup-capable ancestor of an input device, then enable
	// wakeup on all of them. Snapshotting lets us restore the exact prior state
	// (notably, we must not leave the power key unable to wake the device).
	_ = ensurePrivateDir()
	b.WriteString("snapshot wakeup: " + shell(ctx,
		": > /tmp/transit-tracker/wakeup_before; "+
			"for d in /sys/class/input/event*; do "+
			"p=$(readlink -f $d/device 2>/dev/null); "+
			"while [ -n \"$p\" ] && [ \"$p\" != \"/\" ]; do "+
			"if [ -e \"$p/power/wakeup\" ]; then "+
			"echo \"$p $(cat $p/power/wakeup 2>/dev/null)\" >> /tmp/transit-tracker/wakeup_before; fi; "+
			"p=$(dirname $p); done; done; "+
			"sort -u /tmp/transit-tracker/wakeup_before") + "\n")

	b.WriteString("enable touch wakeup: " + shell(ctx,
		"for d in /sys/class/input/event*; do "+
			"n=$(cat $d/device/name 2>/dev/null); "+
			"p=$(readlink -f $d/device 2>/dev/null); "+
			"while [ -n \"$p\" ] && [ \"$p\" != \"/\" ]; do "+
			"if [ -e \"$p/power/wakeup\" ]; then "+
			"echo enabled > $p/power/wakeup 2>/dev/null; "+
			"echo \"  $(basename $d) [$n] $p -> wakeup=$(cat $p/power/wakeup 2>/dev/null)\"; fi; "+
			"p=$(dirname $p); done; done; echo done") + "\n")

	b.WriteString("stop framework: " + shell(ctx, "stop lab126_gui 2>&1; echo done") + "\n")
	b.WriteString("unload screensaver: " + shell(ctx, "lipc-set-prop com.lab126.blanket unload screensaver 2>&1; echo done") + "\n")

	// Visible cue: blink the frontlight 3x so the person at the device knows the
	// moment to start tapping. powerd is still up (we only stop lab126_gui).
	for i := 0; i < 3; i++ {
		runQuiet(ctx, "lipc-set-prop", "-i", "com.lab126.powerd", "flIntensity", "18")
		time.Sleep(700 * time.Millisecond)
		runQuiet(ctx, "lipc-set-prop", "-i", "com.lab126.powerd", "flIntensity", "0")
		time.Sleep(700 * time.Millisecond)
	}
	b.WriteString("frontlight off: " + shell(ctx, "lipc-set-prop -i com.lab126.powerd flIntensity 0 2>&1; echo done") + "\n")

	b.WriteString("wifi off: " + shell(ctx, "lipc-set-prop com.lab126.cmd wirelessEnable 0 2>&1; echo done") + "\n")
	time.Sleep(1 * time.Second)

	before := time.Now()
	b.WriteString("suspend: " + shell(ctx, "echo mem > /sys/power/state 2>&1; echo rc=$?") + "\n")
	elapsed := time.Since(before)

	// Decisive signal #1: when the RTC alarm fires it clears itself. If it is
	// still set after resume, the RTC did NOT wake us -> something else did.
	alarmAfter := strings.TrimSpace(shell(ctx, "cat /sys/class/rtc/rtc0/wakealarm 2>/dev/null"))
	// Disarm the safety net so it can't fire a spurious wake after we're back.
	runQuiet(ctx, "sh", "-c", "echo 0 > /sys/class/rtc/rtc0/wakealarm")

	// Decisive signal #2: did the touch wakeup source's counters move? If a tap
	// generated a wake event during suspend, event_count/wakeup_count rises.
	wakeupAfter := wakeupSourceSnapshot(ctx)
	b.WriteString("wakeup_sources after:\n" + wakeupAfter + "\n")

	b.WriteString("wifi on: " + shell(ctx, "lipc-set-prop com.lab126.cmd wirelessEnable 1 2>&1; echo done") + "\n")

	// Restore the exact prior state from the snapshot.
	b.WriteString("restore wakeup: " + shell(ctx,
		"while read -r path val; do "+
			"[ -e \"$path\" ] && echo \"$val\" > \"$path\" 2>/dev/null; "+
			"echo \"  $path restored to $(cat $path 2>/dev/null)\"; "+
			"done < /tmp/transit-tracker/wakeup_before; rm -f /tmp/transit-tracker/wakeup_before; echo done") + "\n")

	touchMoved := wakeupCountersChanged(wakeupBefore, wakeupAfter, "2-0024")
	rtcMoved := wakeupCountersChanged(wakeupBefore, wakeupAfter, "bd70528-rtc")
	powerMoved := wakeupCountersChanged(wakeupBefore, wakeupAfter, "gpio-keys.7.auto") ||
		wakeupCountersChanged(wakeupBefore, wakeupAfter, "bd71827-power.4.auto")

	var verdict string
	switch {
	case powerMoved:
		verdict = "POWER BUTTON WOKE IT (power-key wake counter moved) -- deep-suspend + press-to-interact POSSIBLE"
	case touchMoved:
		verdict = "TOUCH WOKE IT (touch wake counter moved) -- tap-to-wake POSSIBLE"
	case alarmAfter != "" && alarmAfter != "0":
		verdict = "NON-RTC WAKE (RTC alarm still armed; source unclear) -- something woke the SoC"
	case elapsed < 280*time.Second:
		verdict = "EARLY WAKE (resumed well before the 300s RTC safety)"
	default:
		verdict = "RTC WOKE IT (alarm cleared at ~300s) -- neither touch nor power woke the SoC"
	}
	b.WriteString(fmt.Sprintf(
		"RESULT: resumed after %s; rtc alarm now %q; touch moved=%v, rtc moved=%v, power moved=%v. %s",
		elapsed.Round(time.Second), alarmAfter, touchMoved, rtcMoved, powerMoved, verdict))
	return b.String()
}

// wakeupSourceSnapshot returns the debugfs wakeup_sources lines for the sources
// that matter to us: the touch panel (2-0024), the RTC (bd70528-rtc), and the
// power button (gpio-keys / bd71827-power).
func wakeupSourceSnapshot(ctx context.Context) string {
	out := strings.TrimSpace(shell(ctx, "grep -E '^(2-0024|bd70528-rtc|gpio-keys|bd71827-power)' /sys/kernel/debug/wakeup_sources 2>/dev/null"))
	if out == "" {
		return "<unavailable>"
	}
	return out
}

// wakeupCountersChanged reports whether the named wakeup source's event_count or
// wakeup_count column increased between two snapshot blobs. The columns are:
// name active_count event_count wakeup_count expire_count ...
func wakeupCountersChanged(before, after, name string) bool {
	parse := func(blob, name string) (int, int) {
		for _, line := range strings.Split(blob, "\n") {
			f := strings.Fields(strings.TrimSpace(line))
			if len(f) >= 4 && f[0] == name {
				e, _ := strconv.Atoi(f[2])
				w, _ := strconv.Atoi(f[3])
				return e, w
			}
		}
		return -1, -1
	}
	be, bw := parse(before, name)
	ae, aw := parse(after, name)
	if ae < 0 || be < 0 {
		return false
	}
	return ae > be || aw > bw
}
