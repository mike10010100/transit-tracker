package main

import (
	"context"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestShell_EdgeCases(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()

	// Success case
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "hello world")
	}
	res := shell(context.Background(), "test")
	if res != "hello world" {
		t.Fatalf("expected 'hello world', got %q", res)
	}

	// Error with empty output -> line 57: <error: %v>
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("sh", "-c", "exit 1")
	}
	res = shell(context.Background(), "test")
	if !strings.HasPrefix(res, "<error:") {
		t.Fatalf("expected error prefix, got %q", res)
	}

	// Error with non-empty output -> line 60: %s\n<exit: %v>
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("sh", "-c", "echo failed; exit 2")
	}
	res = shell(context.Background(), "test")
	if !strings.Contains(res, "failed") || !strings.Contains(res, "<exit:") {
		t.Fatalf("expected exit format, got %q", res)
	}
}

func TestRunAction_AllowlistAndDefaultTimeout(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "ok")
	}

	// Unknown action -> line 41
	msg, ok := runAction(context.Background(), "nonexistent-action")
	if ok || !strings.Contains(msg, "unknown action") {
		t.Fatalf("expected unknown action failure, got %v, %q", ok, msg)
	}

	// Action with act.Timeout == 0 falls back to defaultActionTimeout -> line 45
	msg, ok = runAction(context.Background(), "disable-ads")
	if !ok || !strings.Contains(msg, "sqlite tools:") {
		t.Fatalf("expected successful runAction, got %v, %q", ok, msg)
	}
}

func TestActionDisableAds(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		script := ""
		if len(arg) >= 2 {
			script = arg[1]
		}
		if strings.Contains(script, "command -v sqlite3") {
			return exec.Command("echo", "/usr/bin/sqlite3")
		}
		if strings.Contains(script, "UPDATE properties") {
			return exec.Command("echo", "updated")
		}
		if strings.Contains(script, "rm -rf") {
			return exec.Command("echo", "done")
		}
		if strings.Contains(script, "grep -a -o") {
			return exec.Command("echo", "adunit.viewable=false")
		}
		return exec.Command("echo", "ok")
	}

	res := actionDisableAds(context.Background())
	if !strings.Contains(res, "sqlite tools:") || !strings.Contains(res, "sqlite3 update:") || !strings.Contains(res, "rm adunits:") {
		t.Fatalf("unexpected actionDisableAds output: %s", res)
	}
}

func TestActionStopAndStartFramework(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "done")
	}

	stopRes := actionStopFramework(context.Background())
	if stopRes != "done" {
		t.Fatalf("expected 'done', got %q", stopRes)
	}

	startRes := actionStartFramework(context.Background())
	if startRes != "done" {
		t.Fatalf("expected 'done', got %q", startRes)
	}

	stateRes := actionFrameworkState(context.Background())
	if stateRes != "done" {
		t.Fatalf("expected 'done', got %q", stateRes)
	}
}

func TestActionSleepTest(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "test-val")
	}

	res := actionSleepTest(context.Background())
	for _, expected := range []string{"wake_lock: test-val", "framework: test-val", "rtc: test-val", "ads: test-val"} {
		if !strings.Contains(res, expected) {
			t.Fatalf("missing %q in %s", expected, res)
		}
	}
}

func TestActionRTCSuspend(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "ok")
	}

	res := actionRTCSuspend(context.Background())
	if !strings.Contains(res, "RESULT: suspended+resumed") {
		t.Fatalf("unexpected actionRTCSuspend result: %s", res)
	}
}

func TestActionInputWakeProbe(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "probe-output")
	}

	inputRes := actionInputWakeProbe(context.Background())
	if !strings.Contains(inputRes, "input devices:") || !strings.Contains(inputRes, "wakeup capability") {
		t.Fatalf("unexpected actionInputWakeProbe result: %s", inputRes)
	}
}

func TestWakeupSourceSnapshot_Unavailable(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()

	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "2-0024 1 2 3 0")
	}
	out := wakeupSourceSnapshot(context.Background())
	if out != "2-0024 1 2 3 0" {
		t.Fatalf("expected snapshot output, got %q", out)
	}

	// Empty string triggers <unavailable> line 333
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "")
	}
	out = wakeupSourceSnapshot(context.Background())
	if out != "<unavailable>" {
		t.Fatalf("expected <unavailable>, got %q", out)
	}
}

func TestWakeupCountersChanged_EdgeCases(t *testing.T) {
	before := "2-0024 10 5 2 0\nbd70528-rtc 1 1 1 0"
	afterEventMoved := "2-0024 11 6 2 0\nbd70528-rtc 1 1 1 0"

	// Unknown source line 355
	if wakeupCountersChanged(before, afterEventMoved, "unknown-source") {
		t.Fatal("expected false for unknown source")
	}
	// Malformed lines with < 4 columns line 345
	if wakeupCountersChanged("bad line format", afterEventMoved, "2-0024") {
		t.Fatal("expected false for bad line format")
	}
	if wakeupCountersChanged(before, "bad line format", "2-0024") {
		t.Fatal("expected false for bad line format in after")
	}
}

func TestActionTouchWakeTest_Verdicts(t *testing.T) {
	orig := execCommandContext
	defer func() { execCommandContext = orig }()

	// Test Power button wake verdict: powerMoved = true -> line 310-311
	callCount := 0
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		script := ""
		if len(arg) >= 2 {
			script = arg[1]
		}
		if strings.Contains(script, "grep -E '^(2-0024|bd70528-rtc|gpio-keys|bd71827-power)'") {
			callCount++
			if callCount == 1 {
				return exec.Command("echo", "gpio-keys.7.auto 1 0 0 0")
			}
			return exec.Command("echo", "gpio-keys.7.auto 1 5 1 0")
		}
		if strings.Contains(script, "cat /sys/class/rtc/rtc0/wakealarm") {
			return exec.Command("echo", "0")
		}
		return exec.Command("echo", "ok")
	}

	res := actionTouchWakeTest(context.Background())
	if !strings.Contains(res, "POWER BUTTON WOKE IT") {
		t.Fatalf("expected POWER BUTTON WOKE IT verdict, got:\n%s", res)
	}

	// Test Non-RTC wake verdict: alarmAfter != "" && alarmAfter != "0" -> lines 314-315
	callCount = 0
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		script := ""
		if len(arg) >= 2 {
			script = arg[1]
		}
		if strings.Contains(script, "grep -E '^(2-0024|bd70528-rtc|gpio-keys|bd71827-power)'") {
			// No counter changes
			return exec.Command("echo", "gpio-keys.7.auto 1 0 0 0\n2-0024 1 0 0 0\nbd70528-rtc 1 0 0 0")
		}
		if strings.Contains(script, "cat /sys/class/rtc/rtc0/wakealarm") {
			// Alarm still armed!
			return exec.Command("echo", "1700000000")
		}
		return exec.Command("echo", "ok")
	}

	res = actionTouchWakeTest(context.Background())
	if !strings.Contains(res, "NON-RTC WAKE") {
		t.Fatalf("expected NON-RTC WAKE verdict, got:\n%s", res)
	}
}

func TestActionRestartRebootUpdateClearBackup(t *testing.T) {
	origExit := osExit
	origCmd := execCommandContext
	origDelay := exitDelay
	exitDelay = 0
	defer func() {
		exitDelay = origDelay
		osExit = origExit
		execCommandContext = origCmd
	}()

	exitCh := make(chan int, 2)
	osExit = func(code int) {
		exitCh <- code
	}
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.Command("echo", "mocked")
	}

	res, ok := runAction(context.Background(), "restart")
	if !ok || !strings.Contains(res, "restarting client binary") {
		t.Fatalf("unexpected restart result: %s", res)
	}
	select {
	case code := <-exitCh:
		if code != 0 {
			t.Errorf("expected exit code 0, got %d", code)
		}
	case <-time.After(time.Second):
		t.Error("timed out waiting for restart exit")
	}

	res, ok = runAction(context.Background(), "reboot")
	if !ok || res != "mocked" {
		t.Fatalf("unexpected reboot result: %s", res)
	}

	res, ok = runAction(context.Background(), "update")
	if !ok || res != "mocked" {
		t.Fatalf("unexpected update result: %s", res)
	}
	select {
	case code := <-exitCh:
		if code != 0 {
			t.Errorf("expected exit code 0, got %d", code)
		}
	case <-time.After(time.Second):
		t.Error("timed out waiting for update exit")
	}

	res, ok = runAction(context.Background(), "clear_backup")
	if !ok || res != "mocked" {
		t.Fatalf("unexpected clear_backup result: %s", res)
	}

	res, ok = runAction(context.Background(), "clear-backup")
	if !ok || res != "mocked" {
		t.Fatalf("unexpected clear-backup result: %s", res)
	}
}
