package main

import (
	"context"
	"crypto/ed25519"
	"encoding/binary"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func init() {
	origVerify := verifyServerFn
	verifyServerFn = func(ctx context.Context, u string, timeout time.Duration) bool {
		// Prevent tests from accidentally contacting a live host daemon on port 8000
		if strings.Contains(u, "127.0.0.1:8000") || strings.Contains(u, "localhost:8000") {
			return false
		}
		return origVerify(ctx, u, timeout)
	}
}

// patchRuntime restores all process/file seams after the test.
func patchRuntime(t *testing.T) {
	t.Helper()
	origOpenFile := osOpenFile
	origRemove := osRemove
	origRename := osRename
	origChmod := osChmod
	origCreate := osCreate
	origExec := sysExec
	origGlob := globInputs
	origOpen := osOpen
	origReadFile := osReadFile
	origWriteFile := osWriteFile
	origGetBattery := GetBatteryInfo
	origGetClientID := GetClientID
	origDiscover := autoDiscover
	origExecCmd := execCommand
	origExecCmdCtx := execCommandContext
	origOTAPub := OTAPublicKey
	origAllowLoopback := allowLoopbackDiscovery
	origMinPoll := minPollIntervalSec

	GetClientID = func() string { return "test-client-id-1234" }

	// Discovery is disabled by default in tests so a real LAN server cannot
	// interfere with assertions; individual tests may override it.
	autoDiscover = func(ctx context.Context) (string, error) { return "", os.ErrNotExist }
	allowLoopbackDiscovery = true
	minPollIntervalSec = 1

	execCommand = func(name string, arg ...string) *exec.Cmd {
		if name == "eips" {
			return exec.Command("true")
		}
		return origExecCmd(name, arg...)
	}

	t.Cleanup(func() {
		osOpenFile = origOpenFile
		osRemove = origRemove
		osRename = origRename
		osChmod = origChmod
		osCreate = origCreate
		sysExec = origExec
		globInputs = origGlob
		osOpen = origOpen
		osReadFile = origReadFile
		osWriteFile = origWriteFile
		GetBatteryInfo = origGetBattery
		GetClientID = origGetClientID
		autoDiscover = origDiscover
		execCommand = origExecCmd
		execCommandContext = origExecCmdCtx
		OTAPublicKey = origOTAPub
		allowLoopbackDiscovery = origAllowLoopback
		minPollIntervalSec = origMinPoll

		otaBackoffMu.Lock()
		otaBackoffs = make(map[string]*otaBackoffEntry)
		otaBackoffMu.Unlock()

		certCacheMu.Lock()
		certCache = make(map[string]ed25519.PublicKey)
		certCacheMu.Unlock()
	})
}

// tempFileCreate returns an osCreate replacement that materializes a real temp
// file so io.Copy succeeds during OTA download.
func tempFileCreate(t *testing.T) func(string) (*os.File, error) {
	t.Helper()
	return func(name string) (*os.File, error) {
		return os.CreateTemp(t.TempDir(), "tracker-*")
	}
}

func TestRawTouchToDesign(t *testing.T) {
	patchRuntime(t)
	osReadFile = func(string) ([]byte, error) { return []byte("1236,1648\n"), nil }
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")

	// Portrait panel center (618,824) maps to design center (~400,~300).
	dx, dy := tc.rawTouchToDesign(618, 824)
	if dx < 390 || dx > 410 || dy < 290 || dy > 310 {
		t.Errorf("center map = (%d,%d), want ~(400,300)", dx, dy)
	}
	// The rendered button bar sits at portrait x 1145..1215 -> design y 556..590
	// (see the render rotation). Raw px drives design dy.
	_, by := tc.rawTouchToDesign(1180, 800)
	if by < 556 || by > 590 {
		t.Errorf("button bar design y = %d, want 556..590", by)
	}
	// Raw py drives design dx:
	// Portrait py=41 -> right edge, design x ~780 (REFRESH button area).
	rx, _ := tc.rawTouchToDesign(600, 41)
	if rx < 770 || rx > 790 {
		t.Errorf("right-edge design x = %d, want ~780", rx)
	}
	// Portrait py=1607 -> left edge, design x ~20 (BUSES button area).
	bx, _ := tc.rawTouchToDesign(600, 1607)
	if bx < 15 || bx > 25 {
		t.Errorf("left-edge design x = %d, want ~20", bx)
	}
	// Citi Bike button: raw py ≈ 1032 -> design x ~298 (CITI BIKE zone 202..394).
	cbX, cbY := tc.rawTouchToDesign(1197, 1032)
	if cbX < 290 || cbX > 305 || cbY < 556 || cbY > 590 {
		t.Errorf("citi bike map = (%d,%d), want ~(298,581)", cbX, cbY)
	}
}

func TestGetPanelSize_CachesDetection(t *testing.T) {
	patchRuntime(t)
	calls := 0
	osReadFile = func(string) ([]byte, error) {
		calls++
		return []byte("1236,1648\n"), nil
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	_ = tc.getPanelSize()
	_ = tc.getPanelSize()
	if calls != 1 {
		t.Errorf("expected panel size detected once, got %d reads", calls)
	}
}

func TestCleanupInvokesCommands(t *testing.T) {
	patchRuntime(t)
	var called []string
	orig := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd {
		called = append(called, name)
		return orig("true")
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.cleanup()
	if len(called) == 0 {
		t.Fatal("expected cleanup to invoke external commands")
	}
}

func TestStartInputListenersFallsBackWhenGlobEmpty(t *testing.T) {
	patchRuntime(t)
	globInputs = func(string) ([]string, error) { return nil, nil }
	osOpen = func(string) (*os.File, error) { return nil, os.ErrNotExist }

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())
	tc.startInputListeners(ctx, cancel)
	cancel()
}

func TestCycleFrontlightReadsAndWrites(t *testing.T) {
	patchRuntime(t)
	var setProps int
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-set-prop" {
			setProps++
		}
		return exec.CommandContext(ctx, "true")
	}
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.cycleFrontlight()
	if setProps == 0 {
		t.Error("expected frontlight properties to be written")
	}
}

func TestConfigureGestureHandlers_WiresViewAndRefresh(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	gd := NewGestureDetector(DefaultGestureConfig())
	_, cancel := context.WithCancel(context.Background())
	defer cancel()

	tc.configureGestureHandlers(gd, cancel)

	if gd.OnBusesTap == nil || gd.OnBikesTap == nil ||
		gd.OnLightTap == nil || gd.OnRefreshTap == nil || gd.OnSingleTap == nil ||
		gd.OnDoubleTap == nil || gd.OnTopLeftTap == nil || gd.OnTopRightTap == nil ||
		gd.OnBottomLeftTap == nil {
		t.Fatal("expected all gesture handlers to be wired")
	}

	// Bikes button sets morning view and queues a refresh.
	gd.OnBikesTap(0, 0)
	if tc.getViewMode() != "morning" {
		t.Errorf("bikes tap should set morning view, got %s", tc.getViewMode())
	}
	select {
	case <-tc.refreshCh:
	default:
		t.Error("bikes tap should enqueue a refresh")
	}

	// Buses button sets evening view.
	gd.OnBusesTap(0, 0)
	if tc.getViewMode() != "evening" {
		t.Errorf("buses tap should set evening view, got %s", tc.getViewMode())
	}
}

func TestConfigureGestureHandlers_AllButtonsAndCorners(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	gd := NewGestureDetector(DefaultGestureConfig())
	ctx, cancel := context.WithCancel(context.Background())
	// Track frontlight invocations without a Kindle.
	origCmd := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return origCmd("true") }
	defer func() { execCommand = origCmd }()

	tc.configureGestureHandlers(gd, cancel)

	// Light button cycles the frontlight.
	gd.OnLightTap(0, 0)
	// Top-left and refresh enqueue refresh signals.
	gd.OnTopLeftTap(0, 0)
	gd.OnRefreshTap(0, 0)
	// Bottom-left cycles the view.
	tc.lastRenderedView = "evening"
	gd.OnBottomLeftTap(0, 0)
	if tc.getViewMode() != "morning" && tc.getViewMode() != "evening" {
		t.Errorf("unexpected view after bottom-left tap: %s", tc.getViewMode())
	}
	// Single tap should not panic.
	gd.OnSingleTap(0, 0)
	// Top-right and double-tap must NOT exit (that caused blank, unresponsive
	// screens); they enqueue a refresh instead.
	gd.OnTopRightTap(0, 0)
	gd.OnDoubleTap(0, 0)
	select {
	case <-ctx.Done():
		t.Fatal("top-right/double-tap must not cancel in dashboard mode")
	case <-time.After(200 * time.Millisecond):
	}
	select {
	case <-tc.refreshCh:
	default:
		t.Fatal("double-tap/top-right should enqueue a refresh")
	}
}

func TestScreenOnlyActionsDoNotArmFastPoll(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	gd := NewGestureDetector(DefaultGestureConfig())
	_, cancel := context.WithCancel(context.Background())
	defer cancel()

	origCmd := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return origCmd("true") }
	defer func() { execCommand = origCmd }()

	tc.configureGestureHandlers(gd, cancel)

	// Frontlight actions are screen-only: they must NOT pin fast polling.
	gd.OnSingleTap(0, 0)
	gd.OnLightTap(0, 0)
	if armed := tc.overlayNow().FastPoll(time.Now()); armed {
		t.Error("screen-only actions must not arm the fast-poll hold")
	}

	// Data-affecting actions DO arm it.
	gd.OnRefreshTap(0, 0)
	if armed := tc.overlayNow().FastPoll(time.Now()); !armed {
		t.Error("refresh must arm the fast-poll hold")
	}
}

func TestLogRemoteQueueDoesNotBlockAndDropsWhenFull(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	// Never started the sender, so nothing drains the queue.
	for i := 0; i < cap(tc.logCh)+50; i++ {
		tc.logRemote("msg") // must not block even when full
	}
	if len(tc.logCh) != cap(tc.logCh) {
		t.Errorf("expected queue to fill to capacity %d, got %d", cap(tc.logCh), len(tc.logCh))
	}
}

func TestRunOneshotRendersAndExitsWithoutCleanup(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/diag" {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.Header().Set("X-Kindle-Poll-Interval", "60")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	globInputs = func(string) ([]string, error) { return nil, nil }
	osOpen = func(string) (*os.File, error) { return nil, os.ErrNotExist }
	origDiscover := autoDiscover
	autoDiscover = func(context.Context) (string, error) { return srv.URL, nil }
	defer func() { autoDiscover = origDiscover }()

	// Capture commands to ensure oneshot does NOT clear the screen via 'eips -c'
	// and does NOT set preventScreenSaver=1.
	var cleanedScreen, heldAwake bool
	origCmd := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd {
		if name == "eips" && len(arg) > 0 && arg[0] == "-c" {
			cleanedScreen = true
		}
		return origCmd("true")
	}
	defer func() { execCommand = origCmd }()
	origCtx := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-set-prop" && len(arg) >= 4 && arg[2] == "preventScreenSaver" && arg[3] == "1" {
			heldAwake = true
		}
		return exec.CommandContext(ctx, "true")
	}
	defer func() { execCommandContext = origCtx }()

	origArgs := os.Args
	// Pass an explicit -server so GetServerURL doesn't consult a stale
	// /tmp/tracker_server.txt left by another test.
	os.Args = []string{"/tmp/tracker", "-oneshot", "-server", srv.URL}
	defer func() { os.Args = origArgs }()

	done := make(chan struct{})
	go func() {
		run(context.Background())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("oneshot run should return promptly")
	}
	if cleanedScreen {
		t.Error("oneshot must not clear the screen")
	}
	if heldAwake {
		t.Error("oneshot must not set preventScreenSaver=1")
	}
}

func TestPostDiagnosticsUploadsReport(t *testing.T) {
	patchRuntime(t)
	got := make(chan [2]string, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got <- [2]string{r.URL.Path, string(b)}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	tc := NewTrackerClient(srv.URL, "auto")
	tc.postDiagnostics(false)

	select {
	case pair := <-got:
		if pair[0] != "/diag" {
			t.Errorf("expected POST /diag, got %q", pair[0])
		}
		if !strings.Contains(pair[1], "=== DIAGNOSTICS") {
			t.Errorf("expected diagnostics report in body, got %q", pair[1])
		}
	case <-time.After(2 * time.Second):
		t.Fatal("expected a diagnostics POST")
	}
}

func TestLogSenderDrainsQueue(t *testing.T) {
	patchRuntime(t)
	var received int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/log" {
			atomic.AddInt32(&received, 1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.startLogSender(ctx)

	for i := 0; i < 3; i++ {
		tc.logRemote("hello")
	}
	deadline := time.Now().Add(2 * time.Second)
	for atomic.LoadInt32(&received) < 3 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := atomic.LoadInt32(&received); got != 3 {
		t.Errorf("expected 3 logs delivered, got %d", got)
	}
}

func TestRunEventLoop_FeedsTouchAndExitsOnCancel(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())

	eventCh := make(chan RawEventMsg, 8)
	done := make(chan struct{})
	go func() {
		tc.runEventLoop(ctx, cancel, eventCh)
		close(done)
	}()

	eventCh <- RawEventMsg{EvType: EV_ABS, EvCode: ABS_MT_POSITION_X, EvValue: 500}
	eventCh <- RawEventMsg{EvType: EV_ABS, EvCode: ABS_MT_POSITION_Y, EvValue: 500}
	eventCh <- RawEventMsg{EvType: EV_KEY, EvCode: BTN_TOUCH, EvValue: 0}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runEventLoop should exit on cancel")
	}
}

func TestStartInputListeners_RealOpenPath(t *testing.T) {
	patchRuntime(t)
	// Create a real file and have glob return it, then write an event and cancel.
	dir := t.TempDir()
	devPath := dir + "/event0"
	if err := os.WriteFile(devPath, buildEventBytes(EV_ABS, ABS_MT_POSITION_X, 400), 0644); err != nil {
		t.Fatal(err)
	}
	globInputs = func(string) ([]string, error) { return []string{devPath}, nil }

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())
	tc.startInputListeners(ctx, cancel)
	time.Sleep(30 * time.Millisecond)
	cancel()
}

func buildEventBytes(evType, evCode uint16, evValue int32) []byte {
	// 16-byte little-endian input_event.
	b := make([]byte, 16)
	binary.LittleEndian.PutUint16(b[8:10], evType)
	binary.LittleEndian.PutUint16(b[10:12], evCode)
	binary.LittleEndian.PutUint32(b[12:16], uint32(evValue))
	return b
}

func TestRunEventLoop_PowerKeyCancels(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())

	eventCh := make(chan RawEventMsg, 1)
	done := make(chan struct{})
	go func() {
		tc.runEventLoop(ctx, cancel, eventCh)
		close(done)
	}()

	eventCh <- RawEventMsg{Device: "/dev/input/event0", EvType: EV_KEY, EvCode: KEY_POWER, EvValue: 1}

	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("power key should cancel the context")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runEventLoop should return after power key")
	}
}

func TestHandleNetworkErrorIncrementsCounter(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:1", "auto")

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	tc.handleNetworkError(ctx)

	tc.mu.Lock()
	count := tc.consecutiveErrors
	tc.mu.Unlock()
	if count < 1 {
		t.Errorf("expected consecutiveErrors >= 1, got %d", count)
	}
}

func TestHandleNetworkError_RediscoveryResetsCounter(t *testing.T) {
	patchRuntime(t)
	origDiscover := autoDiscover
	autoDiscover = func(context.Context) (string, error) { return "http://10.1.2.3:8000", nil }
	defer func() { autoDiscover = origDiscover }()

	tc := NewTrackerClient("http://127.0.0.1:1", "auto")
	tc.handleNetworkError(context.Background())
	tc.handleNetworkError(context.Background())
	tc.handleNetworkError(context.Background())

	if tc.getServerURL() != "http://10.1.2.3:8000" {
		t.Errorf("expected rediscovered server URL, got %s", tc.getServerURL())
	}
	tc.mu.Lock()
	count := tc.consecutiveErrors
	tc.mu.Unlock()
	if count != 0 {
		t.Errorf("expected error counter reset after rediscovery, got %d", count)
	}
}

func TestGetServerURL_UsesDiscoverySeam(t *testing.T) {
	origDiscover := autoDiscover
	autoDiscover = func(context.Context) (string, error) { return "http://10.9.9.9:8000", nil }
	defer func() { autoDiscover = origDiscover }()
	// With no CLI/env/file server, GetServerURL should consult discovery.
	got := GetServerURL()
	if got == "" {
		t.Fatal("expected a non-empty server URL")
	}
}

func TestStartInputListeners_OpensProvidedDevices(t *testing.T) {
	patchRuntime(t)
	// Simulate two device files; both fail to open, exercising the continue path.
	globInputs = func(string) ([]string, error) {
		return []string{"/dev/input/event0", "/dev/input/event1"}, nil
	}
	osOpen = func(string) (*os.File, error) { return nil, os.ErrNotExist }

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	ctx, cancel := context.WithCancel(context.Background())
	tc.startInputListeners(ctx, cancel)
	cancel()
}

func TestRun_StartsAndStops(t *testing.T) {
	patchRuntime(t)
	// Avoid touching the real network/filesystem beyond what seams cover.
	origCreate := osCreate
	osCreate = tempFileCreate(t)
	defer func() { osCreate = origCreate }()

	origCmd := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd { return origCmd("true") }
	defer func() { execCommand = origCmd }()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: -1} }
	globInputs = func(string) ([]string, error) { return nil, nil }
	osOpen = func(string) (*os.File, error) { return nil, os.ErrNotExist }
	origDiscover := autoDiscover
	autoDiscover = func(context.Context) (string, error) { return srv.URL, nil }
	defer func() { autoDiscover = origDiscover }()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		run(ctx)
		close(done)
	}()
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("run should return after context cancellation")
	}
}

func TestPresentationRoundTrip(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if tc.getPresentation() != "interactive" {
		t.Errorf("default presentation = %q, want interactive", tc.getPresentation())
	}
	tc.setPresentation("dormant")
	if tc.getPresentation() != "dormant" {
		t.Errorf("presentation = %q, want dormant", tc.getPresentation())
	}
	// Empty is ignored.
	tc.setPresentation("")
	if tc.getPresentation() != "dormant" {
		t.Errorf("empty presentation should be ignored, got %q", tc.getPresentation())
	}
}

func TestRunEventLoop_PowerKeyIgnoredInDedicatedDashboardMode(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.exitOnPowerKey = false

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	eventCh := make(chan RawEventMsg, 10)

	done := make(chan struct{})
	go func() {
		tc.runEventLoop(ctx, cancel, eventCh)
		close(done)
	}()

	eventCh <- RawEventMsg{Device: "/dev/input/event0", EvType: EV_KEY, EvCode: KEY_POWER, EvValue: 1}

	select {
	case <-done:
		t.Fatal("event loop should not have exited on KEY_POWER when exitOnPowerKey is false")
	case <-time.After(50 * time.Millisecond):
	}

	cancel()
	<-done
}

func TestMiscMissingBranches(t *testing.T) {
	patchRuntime(t)

	origCmdCtx := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		return exec.CommandContext(ctx, "sleep", "2")
	}
	origTimeout := lipcCallTimeout
	lipcCallTimeout = 10 * time.Millisecond
	if got := lipcGet("prop", "name"); got != "" {
		t.Errorf("expected empty string on lipc timeout, got %q", got)
	}
	execCommandContext = origCmdCtx
	lipcCallTimeout = origTimeout

	doneCh := make(chan string, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
		select {
		case doneCh <- string(body):
		default:
		}
	}))
	defer srv.Close()
	tc := NewTrackerClient(srv.URL, "auto")
	tc.postDiagnostics(true)
	var postedBody string
	select {
	case postedBody = <-doneCh:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for diagnostics post")
	}
	if !strings.Contains(postedBody, "ACTIVE PROBE") {
		t.Errorf("expected active probe in diagnostics, got %s", postedBody)
	}

	tcInvalid := NewTrackerClient("http://[::1]:namedport", "auto")
	tcInvalid.postText(context.Background(), "/log", "test")

	tcDesign := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tcDesign.panelOnce.Do(func() {})
	tcDesign.panelSize = PanelSize{LandscapeW: 0}
	x, y := tcDesign.rawTouchToDesign(50, 100)
	if x != 50 || y != 100 {
		t.Errorf("expected 50, 100 on wl <= 0, got %d, %d", x, y)
	}

	tcCycle := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tcCycle.lastRenderedView = ""
	tcCycle.cycleViewMode()
	if tcCycle.getViewMode() != "morning" {
		t.Errorf("expected morning view after cycle from auto with empty last, got %s", tcCycle.getViewMode())
	}

	tcPres := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tcPres.presentation = ""
	if p := tcPres.getPresentation(); p != "interactive" {
		t.Errorf("expected interactive default presentation, got %s", p)
	}
}

func TestGetServerURL_InvokesAutoDiscover(t *testing.T) {
	patchRuntime(t)
	called := false
	autoDiscover = func(ctx context.Context) (string, error) {
		called = true
		return "http://10.0.0.123:8000", nil
	}
	origArgs := os.Args
	defer func() { os.Args = origArgs }()
	os.Args = []string{"tracker"}

	if _, err := os.Stat(FallbackConfigFile); err == nil {
		origContent, _ := os.ReadFile(FallbackConfigFile)
		_ = os.Remove(FallbackConfigFile)
		defer func() { _ = os.WriteFile(FallbackConfigFile, origContent, 0644) }()
	}

	url := GetServerURL()
	if !called || url != "http://10.0.0.123:8000" {
		t.Fatalf("expected autoDiscover to be called, got called=%v, url=%q", called, url)
	}
}

func TestTrackerClient_Wait(t *testing.T) {
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	completed := false
	tc.wg.Add(1)
	go func() {
		defer tc.wg.Done()
		time.Sleep(10 * time.Millisecond)
		completed = true
	}()
	tc.Wait()
	if !completed {
		t.Fatal("expected tc.Wait to block until background goroutine completes")
	}
}

type sizedFileInfo struct {
	fakeFileInfo
	size int64
}

func (s sizedFileInfo) Size() int64 { return s.size }

type symlinkFileInfo struct {
	fakeFileInfo
}

func (s symlinkFileInfo) Mode() os.FileMode { return os.ModeSymlink }
func (s symlinkFileInfo) IsDir() bool       { return false }

func TestSetupStderr_Branches(t *testing.T) {
	patchRuntime(t)
	var dupCalls int
	origDup := syscallDup2
	syscallDup2 = func(int, int) error {
		dupCalls++
		return nil
	}
	defer func() { syscallDup2 = origDup }()

	// 1. Regular creation
	setupStderr()
	if dupCalls == 0 {
		t.Error("expected syscallDup2 to be called")
	}

	// 2. Large file triggers truncation
	origStat := osStat
	osStat = func(name string) (os.FileInfo, error) {
		return sizedFileInfo{size: 300 * 1024}, nil
	}
	setupStderr()
	osStat = origStat

	// 3. Open error triggers fallback to /dev/null
	origOpen := osOpenFile
	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		if strings.Contains(name, "client.log") {
			return nil, os.ErrPermission
		}
		return origOpen(name, flag, perm)
	}
	setupStderr()
	osOpenFile = origOpen
}

func TestEnsurePrivateDir_Branches(t *testing.T) {
	patchRuntime(t)
	if err := ensurePrivateDir(); err != nil {
		t.Fatalf("ensurePrivateDir failed: %v", err)
	}

	origLstat := osLstat
	origRemoveAll := osRemoveAll
	var removed bool
	osLstat = func(name string) (os.FileInfo, error) {
		return symlinkFileInfo{}, nil
	}
	osRemoveAll = func(path string) error {
		removed = true
		return nil
	}
	_ = ensurePrivateDir()
	if !removed {
		t.Error("expected removeAll when PrivateDir is symlink")
	}
	osLstat = origLstat
	osRemoveAll = origRemoveAll

	origMkdir := osMkdirAll
	osMkdirAll = func(path string, perm os.FileMode) error {
		return os.ErrPermission
	}
	if err := ensurePrivateDir(); err == nil {
		t.Error("expected error when osMkdirAll fails")
	}
	osMkdirAll = origMkdir
}

func TestNewTrackerClient_Views(t *testing.T) {
	tc1 := NewTrackerClient("http://127.0.0.1:8000", "morning")
	if tc1.getViewMode() != "morning" {
		t.Errorf("expected morning view, got %s", tc1.getViewMode())
	}
	tc2 := NewTrackerClient("http://127.0.0.1:8000", "evening")
	if tc2.getViewMode() != "evening" {
		t.Errorf("expected evening view, got %s", tc2.getViewMode())
	}
}

func TestTrackerClient_ClientID(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if got := tc.getClientID(); got != "test-client-id-1234" {
		t.Errorf("expected client ID test-client-id-1234, got %q", got)
	}

	tc.setClientID("custom-client-id-5678")
	if got := tc.getClientID(); got != "custom-client-id-5678" {
		t.Errorf("expected custom client ID, got %q", got)
	}

	// Test lazy resolution when clientID is empty
	tc.setClientID("")
	GetClientID = func() string { return "lazy-client-id-9999" }
	if got := tc.getClientID(); got != "lazy-client-id-9999" {
		t.Errorf("expected lazy client ID, got %q", got)
	}
}
