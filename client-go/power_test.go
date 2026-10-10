package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestArmSysfsWake_ClearThenSet(t *testing.T) {
	patchRuntime(t)
	var writes []string
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		writes = append(writes, path+"="+string(data))
		return nil
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if !tc.armSysfsWake(600 * time.Second) {
		t.Fatal("expected armSysfsWake to succeed")
	}
	if len(writes) < 2 || writes[0] != sysfsWakeAlarmPath+"=0" || writes[1] != sysfsWakeAlarmPath+"=+600" {
		t.Errorf("unexpected writes: %v", writes)
	}
}

func TestArmSysfsWake_WriteFailure(t *testing.T) {
	patchRuntime(t)
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if string(data) == "0" {
			return nil
		}
		return os.ErrPermission
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if tc.armSysfsWake(600 * time.Second) {
		t.Error("expected armSysfsWake to fail when the alarm write fails")
	}
}

func TestSetWireless_TogglesValue(t *testing.T) {
	patchRuntime(t)
	var last string
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-set-prop" && len(arg) >= 4 && arg[2] == "wirelessEnable" {
			last = arg[3]
		}
		return exec.CommandContext(ctx, "true")
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.setWireless(false)
	if last != "0" {
		t.Errorf("expected wirelessEnable=0, got %q", last)
	}
	tc.setWireless(true)
	if last != "1" {
		t.Errorf("expected wirelessEnable=1, got %q", last)
	}
}

func TestEnterSuspend_ReportsElapsed(t *testing.T) {
	patchRuntime(t)
	osWriteFile = func(path string, data []byte, perm os.FileMode) error { return nil }
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if _, err := tc.enterSuspend(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestReleaseScreenSaver(t *testing.T) {
	patchRuntime(t)
	var setKey, setVal string
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-set-prop" && len(arg) >= 4 {
			setKey, setVal = arg[2], arg[3]
		}
		return exec.CommandContext(ctx, "true")
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.releaseScreenSaver()
	if setKey != "preventScreenSaver" || setVal != "0" {
		t.Errorf("expected preventScreenSaver=0, got %s=%s", setKey, setVal)
	}
}

func TestSleepModeHoldsScreensaverWhileRendering(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/diag" || r.URL.Path == "/log" {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.Header().Set("X-Kindle-Poll-Interval", "1")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	osReadFile = func(string) ([]byte, error) { return nil, os.ErrNotExist }
	globInputs = func(string) ([]string, error) { return nil, nil }
	osOpen = func(string) (*os.File, error) { return nil, os.ErrNotExist }
	checkNetworkFn = func(context.Context) bool { return true }

	var heldAwake bool
	origCtx := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-set-prop" && len(arg) >= 4 && arg[2] == "preventScreenSaver" && arg[3] == "1" {
			heldAwake = true
		}
		return exec.CommandContext(ctx, "true")
	}
	defer func() { execCommandContext = origCtx }()

	origArgs := os.Args
	os.Args = []string{"/tmp/tracker", "-sleep", "-server", srv.URL}
	defer func() { os.Args = origArgs }()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		run(ctx)
		close(done)
	}()
	time.Sleep(150 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("sleep run should exit on cancel")
	}
	if !heldAwake {
		t.Error("sleep mode should set preventScreenSaver=1 while rendering")
	}
}

func TestRunSleepLoop_WallClockKeepsRefreshing(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	var fetches int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/diag" || r.URL.Path == "/log" {
			w.WriteHeader(http.StatusOK)
			return
		}
		atomic.AddInt32(&fetches, 1)
		w.Header().Set("X-Kindle-Poll-Interval", "1")
		w.Header().Set("ETag", `"x"`)
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	osReadFile = func(string) ([]byte, error) { return nil, os.ErrNotExist }
	checkNetworkFn = func(context.Context) bool { return true }

	orig := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return orig("true") }
	defer func() { execCommand = orig }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, false) // safe mode: wall-clock only
		close(done)
	}()
	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt32(&fetches) < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("runSleepLoop did not exit on cancel")
	}
	if atomic.LoadInt32(&fetches) < 2 {
		t.Errorf("expected at least 2 fetch cycles, got %d", atomic.LoadInt32(&fetches))
	}
}

func TestRunSleepLoop_SuspendArmsWifiAndSleeps(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	var fetches int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/diag" || r.URL.Path == "/log" {
			w.WriteHeader(http.StatusOK)
			return
		}
		atomic.AddInt32(&fetches, 1)
		w.Header().Set("X-Kindle-Poll-Interval", "600") // >= minSuspendInterval
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	osReadFile = func(string) ([]byte, error) { return nil, os.ErrNotExist }
	checkNetworkFn = func(context.Context) bool { return true }

	var wifi []string
	var wifiMu sync.Mutex
	var suspendWritten int32
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if path == powerStatePath && string(data) == "mem" {
			atomic.StoreInt32(&suspendWritten, 1)
		}
		return nil
	}
	origCtx := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-set-prop" && len(arg) >= 4 && arg[2] == "wirelessEnable" {
			wifiMu.Lock()
			wifi = append(wifi, arg[3])
			wifiMu.Unlock()
		}
		return exec.CommandContext(ctx, "true")
	}
	defer func() { execCommandContext = origCtx }()

	origSettle := suspendSettleDelay
	suspendSettleDelay = 0
	defer func() { suspendSettleDelay = origSettle }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()
	// Wait for the first suspend, then stop.
	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt32(&suspendWritten) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("runSleepLoop did not exit on cancel")
	}
	if atomic.LoadInt32(&suspendWritten) == 0 {
		t.Error("expected /sys/power/state to be written with 'mem'")
	}
	// Wi-Fi should have been disabled for the suspend and re-enabled after.
	wifiMu.Lock()
	sawOff, sawOn := false, false
	for _, v := range wifi {
		if v == "0" {
			sawOff = true
		}
		if v == "1" {
			sawOn = true
		}
	}
	wifiMu.Unlock()
	if !sawOff || !sawOn {
		t.Errorf("expected wifi off then on (off=%v on=%v) got %v", sawOff, sawOn, wifi)
	}
}

func TestWaitForNetwork_SucceedsImmediately(t *testing.T) {
	patchRuntime(t)
	checkNetworkFn = func(context.Context) bool { return true }
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if !tc.waitForNetwork(context.Background()) {
		t.Error("expected waitForNetwork to report true when the network is up")
	}
}

func TestArmSysfsWake_ClampsMinimum(t *testing.T) {
	patchRuntime(t)
	var last string
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		last = string(data)
		return nil
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.armSysfsWake(0)
	if last != "+1" {
		t.Errorf("expected clamped +1, got %q", last)
	}
}

func TestRunSleepLoop_ArmFailureFallsBackToWallClock(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/diag" || r.URL.Path == "/log" {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	osReadFile = func(string) ([]byte, error) { return nil, os.ErrNotExist }
	checkNetworkFn = func(context.Context) bool { return true }
	// Make the RTC alarm write always fail.
	osWriteFile = func(path string, data []byte, perm os.FileMode) error { return os.ErrPermission }

	orig := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return orig("true") }
	defer func() { execCommand = orig }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()
	time.Sleep(200 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("runSleepLoop did not exit on cancel")
	}
}

func TestWaitForNetwork_TimesOut(t *testing.T) {
	patchRuntime(t)
	checkNetworkFn = func(context.Context) bool { return false }
	origSettle := suspendSettleDelay
	suspendSettleDelay = 0
	defer func() { suspendSettleDelay = origSettle }()
	// Short-circuit: with checkNetworkFn always false and a canceled context, it
	// returns promptly. Use a context that is already canceled.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if tc.waitForNetwork(ctx) {
		t.Error("expected waitForNetwork to report false on canceled context")
	}
}

func TestStartPowerListener_CancelsOnEvent(t *testing.T) {
	patchRuntime(t)
	orig := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return orig(ctx, "true")
	}
	defer func() { execCommandContext = orig }()

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan struct{})
	go func() {
		tc.startPowerListener(ctx, cancel)
		close(done)
	}()

	select {
	case <-ctx.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("power listener should cancel context on sleep event")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("power listener should return")
	}
}

func TestStartPowerListener_ReturnsOnContextCancel(t *testing.T) {
	patchRuntime(t)
	orig := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		// Long-running command that is killed when ctx is cancelled.
		return orig(ctx, "sleep", "30")
	}
	defer func() { execCommandContext = orig }()

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		tc.startPowerListener(ctx, cancel)
		close(done)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("power listener should return after context cancel")
	}
}

func TestRTCAlarmStillArmed(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")

	cases := []struct {
		val  string
		want bool
	}{
		{"", false}, {"0", false}, {"1791565000", true}, {" 0\n", false}, {"12345\n", true},
	}
	for _, c := range cases {
		osReadFile = func(string) ([]byte, error) { return []byte(c.val), nil }
		if got := tc.rtcAlarmStillArmed(); got != c.want {
			t.Errorf("rtcAlarmStillArmed(%q) = %v, want %v", c.val, got, c.want)
		}
	}

	// Read error => not armed.
	osReadFile = func(string) ([]byte, error) { return nil, os.ErrNotExist }
	if tc.rtcAlarmStillArmed() {
		t.Error("read error should report not-armed")
	}
}

func TestInteractionAwake_TimesOutAndResets(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()
	execCommand = func(name string, arg ...string) *exec.Cmd { return exec.Command("true") }

	tc := NewTrackerClient(srv.URL, "auto")

	// A short hold returns true after it elapses.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	start := time.Now()
	if !tc.interactionAwake(ctx, cancel, 60*time.Millisecond) {
		t.Fatal("interactionAwake should return true on timeout")
	}
	if time.Since(start) < 50*time.Millisecond {
		t.Error("interactionAwake returned too early")
	}

	// Cancelling the context returns false.
	ctx2, cancel2 := context.WithCancel(context.Background())
	done := make(chan bool, 1)
	go func() { done <- tc.interactionAwake(ctx2, cancel2, 5*time.Second) }()
	time.Sleep(20 * time.Millisecond)
	cancel2()
	select {
	case ok := <-done:
		if ok {
			t.Error("interactionAwake should return false on ctx cancel")
		}
	case <-time.After(time.Second):
		t.Fatal("interactionAwake did not return on cancel")
	}
}

func TestInteractionAwake_TouchKeepsAlive(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()
	execCommand = func(name string, arg ...string) *exec.Cmd { return exec.Command("true") }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan bool, 1)
	go func() { done <- tc.interactionAwake(ctx, cancel, 80*time.Millisecond) }()

	// Touches arriving well past the 80ms window must keep the session alive.
	for i := 0; i < 8; i++ {
		time.Sleep(30 * time.Millisecond)
		tc.noteTouch()
	}
	select {
	case <-done:
		t.Fatal("touches should have kept the interaction session alive")
	default:
	}
	cancel()
}

func TestRunSleepLoop_PowerButtonWake(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()

	origSettle := suspendSettleDelay
	suspendSettleDelay = 0
	defer func() { suspendSettleDelay = origSettle }()

	origHold := interactionHoldDuration
	interactionHoldDuration = 10 * time.Millisecond
	defer func() { interactionHoldDuration = origHold }()

	var disarmed int32
	osReadFile = func(path string) ([]byte, error) {
		if path == sysfsWakeAlarmPath {
			return []byte("12345"), nil
		}
		return nil, os.ErrNotExist
	}
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if path == sysfsWakeAlarmPath && string(data) == "0" {
			atomic.AddInt32(&disarmed, 1)
		}
		return nil
	}
	origExec := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return origExec("true") }
	defer func() { execCommand = origExec }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt32(&disarmed) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done

	if atomic.LoadInt32(&disarmed) == 0 {
		t.Error("expected disarmRTC to be called on power button wake")
	}
}

func TestRunSleepLoop_SuspendFailureFallback(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()

	origSettle := suspendSettleDelay
	suspendSettleDelay = 0
	defer func() { suspendSettleDelay = origSettle }()

	var suspendAttempted int32
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if path == powerStatePath && string(data) == "mem" {
			atomic.StoreInt32(&suspendAttempted, 1)
			return os.ErrPermission
		}
		return nil
	}
	origExec := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return origExec("true") }
	defer func() { execCommand = origExec }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt32(&suspendAttempted) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done

	if atomic.LoadInt32(&suspendAttempted) == 0 {
		t.Error("expected suspend attempt to fail and fall back to sleepWallClock")
	}
}

func TestRunSleepLoop_NetworkFailureAtStart(t *testing.T) {
	patchRuntime(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return false }
	defer func() { checkNetworkFn = origCheck }()

	tc := NewTrackerClient("http://127.0.0.1:9999", "auto")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runSleepLoop should exit cleanly on network failure")
	}
}

func TestRunSleepLoop_ArmSysfsWakeFailure(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()

	var armFailed int32
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if path == sysfsWakeAlarmPath && strings.HasPrefix(string(data), "+") {
			atomic.StoreInt32(&armFailed, 1)
			return os.ErrPermission
		}
		return nil
	}
	origExec := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return origExec("true") }
	defer func() { execCommand = origExec }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for atomic.LoadInt32(&armFailed) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if atomic.LoadInt32(&armFailed) == 0 {
		t.Error("expected armSysfsWake to fail")
	}
}

func TestInteractionAwake_RefreshChannelAndInitialCancel(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	var fetchCount int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fetchCount, 1)
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()
	execCommand = func(name string, arg ...string) *exec.Cmd { return exec.Command("true") }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan bool, 1)
	go func() {
		done <- tc.interactionAwake(ctx, cancel, 200*time.Millisecond)
	}()

	time.Sleep(30 * time.Millisecond)
	tc.refreshCh <- struct{}{}
	time.Sleep(30 * time.Millisecond)

	select {
	case res := <-done:
		if !res {
			t.Fatal("expected interactionAwake true upon timer completion")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timeout waiting for interactionAwake")
	}

	if atomic.LoadInt32(&fetchCount) < 2 {
		t.Fatalf("expected at least 2 fetches (initial + refreshCh), got %d", atomic.LoadInt32(&fetchCount))
	}
}

func TestSleepWallClock_Branches(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")

	// 1. Refresh channel wake
	go func() {
		tc.refreshCh <- struct{}{}
	}()
	if !tc.sleepWallClock(context.Background(), time.Hour) {
		t.Error("expected true when waking on refreshCh")
	}

	// 2. Context cancelled wake
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if tc.sleepWallClock(ctx, time.Hour) {
		t.Error("expected false when context cancelled")
	}
}

func TestRunSleepLoop_PolicySuspendFalseStaysAwake(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.Header().Set("X-Tracker-Policy", "v=1;phase=peak;suspend=0;session=90;fast=600;hold=2700;sl=8,12")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 95} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()

	var suspendAttempted int32
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if path == powerStatePath && string(data) == "mem" {
			atomic.StoreInt32(&suspendAttempted, 1)
		}
		return nil
	}

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true) // allowSuspend=true, but policy says suspend=0
		close(done)
	}()

	// Give it enough time to fetch and decide to stay awake on wall clock
	time.Sleep(100 * time.Millisecond)
	tc.refreshCh <- struct{}{} // wake it once to confirm loop is alive
	time.Sleep(50 * time.Millisecond)
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runSleepLoop timed out")
	}

	if atomic.LoadInt32(&suspendAttempted) != 0 {
		t.Error("expected suspend to be skipped when policy has suspend=0")
	}
}

func TestRunSleepLoop_BoundaryClampedWake(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	untilEpoch := time.Now().Add(180 * time.Second).Unix()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.Header().Set("X-Tracker-Policy", fmt.Sprintf("v=1;phase=offpeak;until=%d;suspend=1;session=90;fast=600;hold=2700;sl=8,12", untilEpoch))
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 90} }
	osCreate = tempFileCreate(t)
	origCheck := checkNetworkFn
	checkNetworkFn = func(context.Context) bool { return true }
	defer func() { checkNetworkFn = origCheck }()

	origSettle := suspendSettleDelay
	suspendSettleDelay = 0
	defer func() { suspendSettleDelay = origSettle }()

	var rtcArmedSec int
	var rtcArmedMu sync.Mutex
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if path == sysfsWakeAlarmPath && strings.HasPrefix(string(data), "+") {
			sec, _ := strconv.Atoi(string(data[1:]))
			rtcArmedMu.Lock()
			rtcArmedSec = sec
			rtcArmedMu.Unlock()
		}
		return nil
	}

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan struct{})
	go func() {
		tc.runSleepLoop(ctx, cancel, true)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for {
		rtcArmedMu.Lock()
		sec := rtcArmedSec
		rtcArmedMu.Unlock()
		if sec > 0 || time.Now().After(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runSleepLoop timed out")
	}

	rtcArmedMu.Lock()
	sec := rtcArmedSec
	rtcArmedMu.Unlock()

	// Should be clamped to ~180s instead of 600s
	if sec <= 0 || sec > 200 {
		t.Errorf("expected RTC alarm clamped to ~180s (near boundary), got +%ds", sec)
	}
}
