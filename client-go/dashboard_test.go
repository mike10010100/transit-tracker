package main

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mike10010100/transit-tracker/client-go/internal/otasig"
)

// otaServer serves the binary at /tracker-arm with a signed manifest at /tracker-arm.manifest.
func otaServer(t *testing.T, binary []byte, digest string) *httptest.Server {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatalf("ed25519.GenerateKey: %v", err)
	}
	OTAPublicKey = otasig.EncodePublicKey(pub)

	var manifestBytes []byte
	if digest != "" {
		signDigest := digest
		if len(signDigest) != 64 {
			signDigest = strings.Repeat("a", 64)
		}
		manifest, err := otasig.SignManifest(priv, "9.9.9", signDigest, int64(len(binary)))
		if err != nil {
			t.Fatalf("SignManifest: %v", err)
		}
		manifestBytes, err = json.Marshal(manifest)
		if err != nil {
			t.Fatalf("json.Marshal: %v", err)
		}
	}

	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/tracker-arm.manifest":
			if len(manifestBytes) == 0 {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			w.Write(manifestBytes)
		case "/tracker-arm":
			w.Header().Set("X-Tracker-SHA256", digest)
			w.WriteHeader(http.StatusOK)
			w.Write(binary)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func TestMaybeUpdateBinary_ValidChecksumExecs(t *testing.T) {
	patchRuntime(t)
	binary := []byte("NEWBINARY-BYTES")
	sum := sha256.Sum256(binary)
	digest := hex.EncodeToString(sum[:])
	srv := otaServer(t, binary, digest)
	defer srv.Close()

	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		return os.CreateTemp(t.TempDir(), "ota-*")
	}
	osRename = func(oldpath, newpath string) error { return nil }
	osChmod = func(name string, mode os.FileMode) error { return nil }

	var execCalled bool
	sysExec = func(argv0 string, argv []string, envv []string) error {
		execCalled = true
		return nil
	}

	tc := NewTrackerClient(srv.URL, "auto")
	if !tc.maybeUpdateBinary(context.Background(), "9.9.9", digest) {
		t.Fatal("expected verified OTA update to be applied")
	}
	if !execCalled {
		t.Error("expected sysExec after verified download")
	}
}

func TestMaybeUpdateBinary_ChecksumMismatchRejects(t *testing.T) {
	patchRuntime(t)
	tamperedDigest := strings.Repeat("b", 64)
	srv := otaServer(t, []byte("TAMPERED"), tamperedDigest)
	defer srv.Close()

	var removed bool
	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		return os.CreateTemp(t.TempDir(), "ota-*")
	}
	osRemove = func(name string) error { removed = true; return nil }
	sysExec = func(string, []string, []string) error {
		t.Fatal("sysExec must NOT be called on checksum mismatch")
		return nil
	}

	tc := NewTrackerClient(srv.URL, "auto")
	if tc.maybeUpdateBinary(context.Background(), "9.9.9", tamperedDigest) {
		t.Fatal("OTA must be rejected on checksum mismatch")
	}
	if !removed {
		t.Error("expected corrupted update file to be removed")
	}
}

func TestMaybeUpdateBinary_MissingDigestFailsClosed(t *testing.T) {
	patchRuntime(t)
	srv := otaServer(t, []byte("NO DIGEST"), "")
	defer srv.Close()

	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		return os.CreateTemp(t.TempDir(), "ota-*")
	}
	osRemove = func(name string) error { return nil }
	sysExec = func(string, []string, []string) error {
		t.Fatal("sysExec must NOT run without a digest")
		return nil
	}

	tc := NewTrackerClient(srv.URL, "auto")
	if tc.maybeUpdateBinary(context.Background(), "9.9.9", "") {
		t.Fatal("OTA without digest must fail closed")
	}
}

func TestMaybeUpdateBinary_NoUpdateWhenVersionMatches(t *testing.T) {
	patchRuntime(t)
	var contacted bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		contacted = true
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	tc := NewTrackerClient(srv.URL, "auto")
	if tc.maybeUpdateBinary(context.Background(), Version, "whatever") {
		t.Fatal("no update expected when versions match")
	}
	if contacted {
		t.Error("matching version must not perform any network I/O")
	}
}

func TestFetchAndDrawDashboard_Success(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}

	var receivedClientID string
	var receivedClientVer string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedClientID = r.Header.Get("X-Tracker-Client-ID")
		receivedClientVer = r.Header.Get("X-Tracker-Client-Version")
		w.Header().Set("X-Kindle-Poll-Interval", "45")
		w.Header().Set("X-Tracker-View", r.Header.Get("X-Tracker-View"))
		w.Header().Set("X-Resolved-View", "morning")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88, IsCharging: true} }
	osCreate = tempFileCreate(t)

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if got := tc.fetchAndDrawDashboard(ctx, cancel); got != 45 {
		t.Errorf("expected poll interval 45, got %d", got)
	}
	if tc.lastRenderedView != "morning" {
		t.Errorf("expected resolved view captured, got %q", tc.lastRenderedView)
	}
	if receivedClientID != "test-client-id-1234" {
		t.Errorf("expected X-Tracker-Client-ID %q, got %q", "test-client-id-1234", receivedClientID)
	}
	if receivedClientVer != Version {
		t.Errorf("expected X-Tracker-Client-Version %q, got %q", Version, receivedClientVer)
	}
}

func TestFetchAndDrawDashboard_ReportsPanelDimensions(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}

	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("X-Kindle-Poll-Interval", "45")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)
	osReadFile = func(string) ([]byte, error) { return []byte("1236,1648\n"), nil }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)

	if !strings.Contains(gotQuery, "kindle=pw5") {
		t.Errorf("expected kindle=pw5 in query, got %q", gotQuery)
	}
	if !strings.Contains(gotQuery, "w=1648") || !strings.Contains(gotQuery, "h=1236") {
		t.Errorf("expected native panel dims in query, got %q", gotQuery)
	}
}

func TestFetchAndDrawDashboard_InteractiveOverride(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}

	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Default (not interacting): no override, server decides the face.
	tc.fetchAndDrawDashboard(ctx, cancel)
	if strings.Contains(gotQuery, "present=interactive") {
		t.Errorf("non-interacting fetch must not force interactive, got %q", gotQuery)
	}

	// In a session: request the full tappable dashboard.
	tc.setInteracting(true)
	if !tc.isInteracting() {
		t.Fatal("setInteracting(true) should stick")
	}
	tc.fetchAndDrawDashboard(ctx, cancel)
	if !strings.Contains(gotQuery, "present=interactive") {
		t.Errorf("interacting fetch must request present=interactive, got %q", gotQuery)
	}
	tc.setInteracting(false)
}

func TestFetchAndDrawDashboard_304SkipsRefresh(t *testing.T) {
	patchRuntime(t)
	var sentETag string
	var eipsCalled bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sentETag = r.Header.Get("If-None-Match")
		w.Header().Set("ETag", `"abc123"`)
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusNotModified)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	orig := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd {
		if name == "eips" {
			eipsCalled = true
		}
		return orig("true")
	}
	defer func() { execCommand = orig }()

	tc := NewTrackerClient(srv.URL, "auto")
	tc.lastETag = `"abc123"`
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if got := tc.fetchAndDrawDashboard(ctx, cancel); got != 600 {
		t.Errorf("expected poll interval 600 on 304, got %d", got)
	}
	if sentETag != `"abc123"` {
		t.Errorf("expected If-None-Match to be sent, got %q", sentETag)
	}
	if eipsCalled {
		t.Error("304 must not trigger an eips refresh")
	}
}

func TestFetchAndDrawDashboard_StoresETag(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", `"newtag"`)
		w.Header().Set("X-Kindle-Poll-Interval", "45")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)

	tc.mu.Lock()
	tag := tc.lastETag
	tc.mu.Unlock()
	if tag != `"newtag"` {
		t.Errorf("expected ETag stored, got %q", tag)
	}
}

func TestFetchAndDrawDashboard_AppliesLightingHeaders(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.Header().Set("X-Kindle-Brightness", "8")
		w.Header().Set("X-Kindle-Warmth", "12")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)

	var setProps []string
	orig := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd {
		return orig("true")
	}
	defer func() { execCommand = orig }()
	origCtx := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-get-prop" {
			// Report a different current value so the set branches run.
			return exec.CommandContext(ctx, "echo", "0")
		}
		if name == "lipc-set-prop" && len(arg) >= 4 {
			setProps = append(setProps, arg[2])
		}
		return exec.CommandContext(ctx, "true")
	}
	defer func() { execCommandContext = origCtx }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)

	joined := strings.Join(setProps, ",")
	if !strings.Contains(joined, "flIntensity") || !strings.Contains(joined, "schedAmberLevel") {
		t.Errorf("expected lighting props to be set, got %v", setProps)
	}
}

func TestFetchAndDrawDashboard_AppliesLightingAndActionOn304(t *testing.T) {
	patchRuntime(t)
	var ranAction bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Brightness", "15")
		w.Header().Set("X-Kindle-Warmth", "8")
		w.Header().Set("X-Tracker-Action", "framework-state")
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusNotModified)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }

	var setProps []string
	origCtx := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if name == "lipc-get-prop" {
			return exec.CommandContext(ctx, "echo", "0")
		}
		if name == "lipc-set-prop" && len(arg) >= 4 {
			setProps = append(setProps, arg[2])
		}
		if name == "sh" {
			ranAction = true
		}
		return exec.CommandContext(ctx, "true")
	}
	defer func() { execCommandContext = origCtx }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)

	joined := strings.Join(setProps, ",")
	if !strings.Contains(joined, "flIntensity") || !strings.Contains(joined, "schedAmberLevel") {
		t.Errorf("expected lighting props to be set on 304, got %v", setProps)
	}
	if !ranAction {
		t.Error("expected requested action to run on 304")
	}
}

func TestFetchAndDrawDashboard_RunsRequestedAction(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Tracker-Action", "framework-state")
		w.Header().Set("X-Kindle-Poll-Interval", "600")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)

	var ranAction bool
	origCtxCmd := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		ranAction = true
		return origCtxCmd(ctx, "echo", "state")
	}
	defer func() { execCommandContext = origCtxCmd }()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)
	if !ranAction {
		t.Error("expected the requested device action to run")
	}
}

func TestFetchAndDrawDashboard_Stop205Cancels(t *testing.T) {
	patchRuntime(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(205)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: -1} }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	if got := tc.fetchAndDrawDashboard(ctx, cancel); got != 0 {
		t.Errorf("expected 0 on 205, got %d", got)
	}
	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("expected context cancel on HTTP 205")
	}
}

func TestFetchAndDrawDashboard_IgnoresPrivateServerHeader(t *testing.T) {
	patchRuntime(t)
	origDiscover := autoDiscover
	autoDiscover = func(context.Context) (string, error) { return "", os.ErrNotExist }
	defer func() { autoDiscover = origDiscover }()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Tracker-Server", "http://10.0.0.55:8000")
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: -1} }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)
	if tc.getServerURL() != srv.URL {
		t.Errorf("expected server URL to remain %s, got %s", srv.URL, tc.getServerURL())
	}
}

func TestFetchAndDrawDashboard_IgnoresPublicServerHeader(t *testing.T) {
	patchRuntime(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Tracker-Server", "http://8.8.8.8:8000")
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: -1} }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)
	if tc.getServerURL() == "http://8.8.8.8:8000" {
		t.Error("must not adopt a public server header")
	}
}

func TestFetchAndDrawDashboard_SendsDiagnosticsWhenRequested(t *testing.T) {
	patchRuntime(t)
	png := []byte{0x89, 0x50, 0x4E, 0x47}
	diagCh := make(chan string, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/diag" {
			b, _ := io.ReadAll(r.Body)
			diagCh <- string(b)
			w.WriteHeader(http.StatusOK)
			return
		}
		w.Header().Set("X-Tracker-Diag", "1")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 88} }
	osCreate = tempFileCreate(t)

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tc.fetchAndDrawDashboard(ctx, cancel)

	select {
	case body := <-diagCh:
		if !strings.Contains(body, "=== DIAGNOSTICS") {
			t.Errorf("expected diagnostics body, got %q", body)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("expected a diagnostics upload when X-Tracker-Diag is set")
	}
}

func TestRunPollLoop_CleansUpOnCancel(t *testing.T) {
	patchRuntime(t)
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: -1} }
	osCreate = tempFileCreate(t)
	var restartedFramework, blanked bool
	orig := execCommand
	execCommand = func(name string, arg ...string) *exec.Cmd {
		if name == "start" && len(arg) > 0 && arg[0] == "lab126_gui" {
			restartedFramework = true
		}
		if name == "eips" && len(arg) > 0 && arg[0] == "-c" {
			blanked = true
		}
		return orig("true")
	}

	// Server returns 500 so no OTA/download occurs; loop should idle.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		tc.runPollLoop(ctx, cancel, 10*time.Millisecond)
		close(done)
	}()
	time.Sleep(40 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runPollLoop should exit on cancel")
	}
	if !restartedFramework {
		t.Error("expected cleanup to restart lab126_gui on loop exit")
	}
	if blanked {
		t.Error("cleanup must not blank the panel (breaks framework-stopped devices)")
	}
}

func TestMaybeUpdateBinary_ErrorBranches(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:1", "auto")

	if tc.maybeUpdateBinary(context.Background(), "", "sha") {
		t.Error("expected false for empty version")
	}

	srv404 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv404.Close()
	tc404 := NewTrackerClient(srv404.URL, "auto")
	if tc404.maybeUpdateBinary(context.Background(), "99.0.0", "sha") {
		t.Error("expected false when download returns 404")
	}

	srv200 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("binary-data"))
	}))
	defer srv200.Close()
	tc200 := NewTrackerClient(srv200.URL, "auto")

	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		return nil, os.ErrPermission
	}
	if tc200.maybeUpdateBinary(context.Background(), "99.0.0", "sha") {
		t.Error("expected false when osOpenFile fails")
	}

	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		return os.CreateTemp(t.TempDir(), "bin-*")
	}
	if tc200.maybeUpdateBinary(context.Background(), "99.0.0", "wrong-sha") {
		t.Error("expected false on SHA mismatch")
	}

	if tc200.maybeUpdateBinary(context.Background(), "99.0.0", "") {
		t.Error("expected false when no SHA provided")
	}
}

func TestFetchAndDrawDashboard_ErrorBranches(t *testing.T) {
	patchRuntime(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Tracker-Action", "totally-unknown-action")
		w.Header().Set("X-Tracker-Mode", "sleep")
		w.Header().Set("X-Kindle-Poll-Interval", "60")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("not-an-image"))
	}))
	defer srv.Close()

	tc := NewTrackerClient(srv.URL, "auto")
	origExec := sysExec
	sysExec = func(argv0 string, argv []string, envv []string) error {
		return os.ErrPermission
	}
	defer func() { sysExec = origExec }()

	osCreate = func(name string) (*os.File, error) {
		return nil, os.ErrPermission
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	poll := tc.fetchAndDrawDashboard(ctx, cancel)
	if poll != 60 {
		t.Fatalf("expected poll interval 60, got %d", poll)
	}

	srv304 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Tracker-Version", "99.0.0")
		w.WriteHeader(http.StatusNotModified)
	}))
	defer srv304.Close()
	tc304 := NewTrackerClient(srv304.URL, "auto")
	osOpenFile = func(string, int, os.FileMode) (*os.File, error) { return nil, os.ErrPermission }
	res304 := tc304.fetchAndDrawDashboard(ctx, cancel)
	if res304 != 0 {
		t.Logf("304 response result: %d", res304)
	}
}

func TestParsePollInterval(t *testing.T) {
	origMin := minPollIntervalSec
	origMax := maxPollIntervalSec
	minPollIntervalSec = 30
	maxPollIntervalSec = 7200
	defer func() {
		minPollIntervalSec = origMin
		maxPollIntervalSec = origMax
	}()

	if parsePollInterval("") != 0 {
		t.Error("empty interval should return 0")
	}
	if parsePollInterval("invalid") != 0 {
		t.Error("invalid interval should return 0")
	}
	if parsePollInterval("-10") != 0 {
		t.Error("negative interval should return 0")
	}
	if parsePollInterval("10") != 30 {
		t.Errorf("interval < min should clamp to min, got %d", parsePollInterval("10"))
	}
	if parsePollInterval("300") != 300 {
		t.Errorf("valid interval 300 got %d", parsePollInterval("300"))
	}
	if parsePollInterval("10000") != 7200 {
		t.Errorf("interval > max should clamp to max, got %d", parsePollInterval("10000"))
	}
}

func TestBuildReexecArgs(t *testing.T) {
	orig := []string{"tracker", "-server", "http://old:8000", "-view", "morning", "-custom-flag", "val"}
	args := buildReexecArgs(orig, "http://new:8000", "auto", false)
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "-server http://new:8000") {
		t.Errorf("expected updated server, got %v", args)
	}
	if !strings.Contains(joined, "-view auto") {
		t.Errorf("expected auto view, got %v", args)
	}
	if !strings.Contains(joined, "-custom-flag val") {
		t.Errorf("expected custom flags preserved, got %v", args)
	}

	// Flag syntax with = and manual view active
	orig2 := []string{"tracker", "-server=http://old:8000", "--view=evening"}
	args2 := buildReexecArgs(orig2, "http://new:8000", "evening", true)
	joined2 := strings.Join(args2, " ")
	if strings.Contains(joined2, "http://old:8000") {
		t.Errorf("old server should be stripped: %v", args2)
	}
	if !strings.Contains(joined2, "-view evening") {
		t.Errorf("expected manual view preserved: %v", args2)
	}
}

func TestUpdateBackup(t *testing.T) {
	patchRuntime(t)
	// Non-existent source returns silently
	updateBackup("/nonexistent/file")

	td := t.TempDir()
	src := filepath.Join(td, "src-binary")
	_ = os.WriteFile(src, []byte("binary-content"), 0755)

	var wroteBackup bool
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if strings.Contains(path, "tracker_backup") {
			wroteBackup = true
		}
		return nil
	}
	updateBackup(src)
	if !wroteBackup {
		t.Error("expected backup file to be written")
	}

	// Write failure fails silently
	osWriteFile = func(path string, data []byte, perm os.FileMode) error {
		return os.ErrPermission
	}
	updateBackup(src)
}

func TestOTABackoffAndRecordFailure(t *testing.T) {
	patchRuntime(t)
	sha := "testsha123"

	if isOTABackoff(sha) {
		t.Fatal("expected no initial backoff")
	}

	recordOTAFailure("") // Empty sha is no-op
	if isOTABackoff("") {
		t.Fatal("empty sha should not have backoff")
	}

	recordOTAFailure(sha)
	if !isOTABackoff(sha) {
		t.Fatal("expected backoff after recording failure")
	}

	// Multiple failures increase delay up to max 6 hours
	for i := 0; i < 10; i++ {
		recordOTAFailure(sha)
	}
	otaBackoffMu.Lock()
	entry := otaBackoffs[sha]
	otaBackoffMu.Unlock()
	if entry.delay > 6*time.Hour {
		t.Errorf("backoff exceeded 6h: %v", entry.delay)
	}
}

func TestVerifyResponseAuth_Matrix(t *testing.T) {
	patchRuntime(t)
	releasePub, releasePriv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	serverPub, serverPriv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}

	cert, err := otasig.SignCert(releasePriv, serverPub, time.Now().Unix())
	if err != nil {
		t.Fatal(err)
	}
	certJSON, _ := json.Marshal(cert)
	certHdr := base64.StdEncoding.EncodeToString(certJSON)

	nonce, _ := otasig.NewNonce()
	path := "/dashboard.png"
	body := []byte("image-data")
	bodySHA := otasig.SHA256Hex(body)

	resp := &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
	}

	// 1. Missing cert header
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err == nil {
		t.Error("expected error for missing cert header")
	}

	// 2. Invalid cert header
	resp.Header.Set(otasig.CertHeader, "invalid-base64")
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err == nil {
		t.Error("expected error for invalid cert header")
	}

	// 3. Valid cert header, but missing auth header
	resp.Header.Set(otasig.CertHeader, certHdr)
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err == nil {
		t.Error("expected error for missing auth header")
	}

	// 4. Invalid auth header signature
	resp.Header.Set(otasig.AuthHeader, base64.StdEncoding.EncodeToString([]byte("invalid-sig")))
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err == nil {
		t.Error("expected error for invalid auth signature")
	}

	// 5. Valid auth header (first call primes cert cache)
	sig, err := otasig.SignResponse(serverPriv, nonce, path, http.StatusOK, bodySHA, resp.Header)
	if err != nil {
		t.Fatal(err)
	}
	resp.Header.Set(otasig.AuthHeader, sig)
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err != nil {
		t.Fatalf("expected valid auth verification to succeed, got %v", err)
	}

	// 6. Second call hits certCache
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err != nil {
		t.Fatalf("expected cached cert verification to succeed, got %v", err)
	}

	// 7. Tampered policy header fails verification
	resp.Header.Set("X-Tracker-Policy", "v=1;phase=overnight;suspend=1")
	if err := verifyResponseAuth(releasePub, nonce, path, resp, body); err == nil {
		t.Error("expected tampered policy header to fail auth verification")
	}
}

func TestLogUntrustedResponse_RateLimited(t *testing.T) {
	patchRuntime(t)
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")

	lastUntrustedLogMu.Lock()
	lastUntrustedLog = time.Time{}
	lastUntrustedLogMu.Unlock()

	tc.logUntrustedResponse(errors.New("error 1"))

	// Second immediate call should be throttled
	tc.logUntrustedResponse(errors.New("error 2"))

	// Reset clock to 2 minutes ago to verify it logs again
	lastUntrustedLogMu.Lock()
	lastUntrustedLog = time.Now().Add(-2 * time.Minute)
	lastUntrustedLogMu.Unlock()

	tc.logUntrustedResponse(errors.New("error 3"))
}

func TestFetchAndDrawDashboard_VerifiedResponseSuccessAndFail(t *testing.T) {
	patchRuntime(t)
	releasePub, releasePriv, _ := ed25519.GenerateKey(nil)
	serverPub, serverPriv, _ := ed25519.GenerateKey(nil)
	OTAPublicKey = otasig.EncodePublicKey(releasePub)

	cert, _ := otasig.SignCert(releasePriv, serverPub, time.Now().Unix())
	certJSON, _ := json.Marshal(cert)
	certHdr := base64.StdEncoding.EncodeToString(certJSON)

	png := []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	bodySHA := otasig.SHA256Hex(png)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "60")
		w.Header().Set(otasig.CertHeader, certHdr)

		nonce := r.Header.Get(otasig.NonceHeader)
		sig, _ := otasig.SignResponse(serverPriv, nonce, "/dashboard.png", http.StatusOK, bodySHA, w.Header())
		w.Header().Set(otasig.AuthHeader, sig)
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer srv.Close()

	osCreate = tempFileCreate(t)
	GetBatteryInfo = func() BatteryInfo { return BatteryInfo{Level: 90} }

	tc := NewTrackerClient(srv.URL, "auto")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	interval := tc.fetchAndDrawDashboard(ctx, cancel)
	if interval != 60 {
		t.Errorf("expected verified poll interval 60, got %d", interval)
	}

	// Now test untrusted response: bad signature => rejected, interval returns 0
	badSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Kindle-Poll-Interval", "60")
		w.Header().Set(otasig.CertHeader, certHdr)
		w.Header().Set(otasig.AuthHeader, "bad-signature")
		w.WriteHeader(http.StatusOK)
		w.Write(png)
	}))
	defer badSrv.Close()

	tcBad := NewTrackerClient(badSrv.URL, "auto")
	badInterval := tcBad.fetchAndDrawDashboard(ctx, cancel)
	if badInterval != 0 {
		t.Errorf("untrusted response must return 0, got %d", badInterval)
	}
}

func TestMaybeUpdateBinary_AllBranches(t *testing.T) {
	patchRuntime(t)
	releasePub, releasePriv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	OTAPublicKey = otasig.EncodePublicKey(releasePub)

	binary := []byte("VALID-BINARY-DATA")
	sum := sha256.Sum256(binary)
	validSHA := hex.EncodeToString(sum[:])

	// 1. Invalid OTAPublicKey base64
	OTAPublicKey = "not-valid-base64"
	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	if tc.maybeUpdateBinary(context.Background(), "9.9.9", validSHA) {
		t.Error("expected false for invalid OTAPublicKey")
	}
	OTAPublicKey = otasig.EncodePublicKey(releasePub)

	// 2. serverSHA == ownSHA
	ownExeSHAMu.Lock()
	origSHA := ownExeSHAVals
	ownExeSHAVals = validSHA
	ownExeSHAMu.Unlock()
	if tc.maybeUpdateBinary(context.Background(), "9.9.9", validSHA) {
		t.Error("expected false when serverSHA == ownSHA")
	}
	ownExeSHAMu.Lock()
	ownExeSHAVals = origSHA
	ownExeSHAMu.Unlock()

	// 3. isOTABackoff(serverSHA)
	recordOTAFailure("backoff-sha")
	if tc.maybeUpdateBinary(context.Background(), "9.9.9", "backoff-sha") {
		t.Error("expected false when under backoff")
	}

	// 4. Manifest HTTP 500 error
	srv500 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv500.Close()
	tc500 := NewTrackerClient(srv500.URL, "auto")
	if tc500.maybeUpdateBinary(context.Background(), "9.9.9", "some-sha-500") {
		t.Error("expected false on manifest HTTP 500")
	}

	// 5. Manifest invalid verification
	srvBadMan := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"format":"transit-tracker-ota-v1","signature":"badsig"}`))
	}))
	defer srvBadMan.Close()
	tcBadMan := NewTrackerClient(srvBadMan.URL, "auto")
	if tcBadMan.maybeUpdateBinary(context.Background(), "9.9.9", "some-sha-badman") {
		t.Error("expected false on invalid manifest verification")
	}

	// 6. Manifest version not newer
	mOlder, _ := otasig.SignManifest(releasePriv, "1.0.0", validSHA, int64(len(binary)))
	mOlderJSON, _ := json.Marshal(mOlder)
	srvOlder := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write(mOlderJSON)
	}))
	defer srvOlder.Close()
	tcOlder := NewTrackerClient(srvOlder.URL, "auto")
	if tcOlder.maybeUpdateBinary(context.Background(), "1.0.0", "sha-older") {
		t.Error("expected false on manifest version not newer")
	}

	// 7. Binary download HTTP 500
	mValid, _ := otasig.SignManifest(releasePriv, "9.9.9", validSHA, int64(len(binary)))
	mValidJSON, _ := json.Marshal(mValid)
	srvBinErr := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/tracker-arm.manifest" {
			w.WriteHeader(http.StatusOK)
			w.Write(mValidJSON)
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srvBinErr.Close()
	tcBinErr := NewTrackerClient(srvBinErr.URL, "auto")
	if tcBinErr.maybeUpdateBinary(context.Background(), "9.9.9", validSHA) {
		t.Error("expected false when binary download fails")
	}

	// 8. Size mismatch during binary download
	srvSizeMismatch := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/tracker-arm.manifest" {
			w.WriteHeader(http.StatusOK)
			w.Write(mValidJSON)
			return
		}
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("short"))
	}))
	defer srvSizeMismatch.Close()
	tcSizeMismatch := NewTrackerClient(srvSizeMismatch.URL, "auto")
	if tcSizeMismatch.maybeUpdateBinary(context.Background(), "9.9.9", "size-mismatch-sha") {
		t.Error("expected false on size mismatch")
	}

	// 9. Chmod failure
	srvChmodErr := otaServer(t, binary, validSHA)
	defer srvChmodErr.Close()
	osChmod = func(name string, mode os.FileMode) error {
		return os.ErrPermission
	}
	tcChmodErr := NewTrackerClient(srvChmodErr.URL, "auto")
	if tcChmodErr.maybeUpdateBinary(context.Background(), "9.9.9", validSHA) {
		t.Error("expected false when chmod fails")
	}

	// 10. Rename failure
	osChmod = func(name string, mode os.FileMode) error { return nil }
	osRename = func(oldpath, newpath string) error {
		return os.ErrPermission
	}
	if tcChmodErr.maybeUpdateBinary(context.Background(), "9.9.9", validSHA) {
		t.Error("expected false when rename fails")
	}

	// 11. SysExec failure
	osRename = func(oldpath, newpath string) error { return nil }
	sysExec = func(argv0 string, argv []string, envv []string) error {
		return errors.New("exec error")
	}
	if tcChmodErr.maybeUpdateBinary(context.Background(), "9.9.9", validSHA) {
		t.Error("expected false when sysExec fails")
	}
}

func TestGetOwnExeSHA_Branches(t *testing.T) {
	patchRuntime(t)
	ownExeSHAMu.Lock()
	origSHA := ownExeSHAVals
	ownExeSHAVals = ""
	ownExeSHAMu.Unlock()
	defer func() {
		ownExeSHAMu.Lock()
		ownExeSHAVals = origSHA
		ownExeSHAMu.Unlock()
	}()

	// 1. /proc/self/exe read succeeds
	osReadFile = func(p string) ([]byte, error) {
		if p == "/proc/self/exe" {
			return []byte("self-exe-bytes"), nil
		}
		return nil, os.ErrNotExist
	}
	sha1 := getOwnExeSHA()
	if sha1 == "" {
		t.Error("expected non-empty sha from /proc/self/exe")
	}

	// 2. /proc/self/exe fails, falls back to osExecutable
	ownExeSHAMu.Lock()
	ownExeSHAVals = ""
	ownExeSHAMu.Unlock()
	origExe := osExecutable
	osExecutable = func() (string, error) { return "/bin/fallback", nil }
	defer func() { osExecutable = origExe }()
	osReadFile = func(p string) ([]byte, error) {
		if p == "/bin/fallback" {
			return []byte("fallback-bytes"), nil
		}
		return nil, os.ErrNotExist
	}
	sha2 := getOwnExeSHA()
	if sha2 == "" || sha2 == sha1 {
		t.Error("expected valid distinct sha from osExecutable fallback")
	}
}

func TestMaybeUpdateBinary_SendsClientIDHeader(t *testing.T) {
	patchRuntime(t)
	binary := []byte("HEADER-CHECK-BINARY")
	sum := sha256.Sum256(binary)
	digest := hex.EncodeToString(sum[:])

	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	OTAPublicKey = otasig.EncodePublicKey(pub)
	manifest, err := otasig.SignManifest(priv, "9.9.9", digest, int64(len(binary)))
	if err != nil {
		t.Fatalf("SignManifest: %v", err)
	}
	manifestBytes, _ := json.Marshal(manifest)

	var manifestClientID, binaryClientID string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/tracker-arm.manifest":
			manifestClientID = r.Header.Get("X-Tracker-Client-ID")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			w.Write(manifestBytes)
		case "/tracker-arm":
			binaryClientID = r.Header.Get("X-Tracker-Client-ID")
			w.Header().Set("X-Tracker-SHA256", digest)
			w.WriteHeader(http.StatusOK)
			w.Write(binary)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	osOpenFile = func(name string, flag int, perm os.FileMode) (*os.File, error) {
		return os.CreateTemp(t.TempDir(), "ota-*")
	}
	osRename = func(oldpath, newpath string) error { return nil }
	osChmod = func(name string, mode os.FileMode) error { return nil }
	sysExec = func(argv0 string, argv []string, envv []string) error { return nil }

	tc := NewTrackerClient(srv.URL, "auto")
	tc.setClientID("ota-custom-client-id-42")

	if !tc.maybeUpdateBinary(context.Background(), "9.9.9", digest) {
		t.Fatal("expected OTA update to succeed")
	}

	if manifestClientID != "ota-custom-client-id-42" {
		t.Errorf("manifest X-Tracker-Client-ID = %q, want ota-custom-client-id-42", manifestClientID)
	}
	if binaryClientID != "ota-custom-client-id-42" {
		t.Errorf("binary X-Tracker-Client-ID = %q, want ota-custom-client-id-42", binaryClientID)
	}
}
