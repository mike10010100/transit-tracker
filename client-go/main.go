package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Version is the authoritative application version. It is overridden at build
// time via -ldflags "-X main.Version=$(cat VERSION)" so that the Go client,
// the Python server, and the Citi Bike User-Agent all share one identity.
var Version = "0.0.0"

// OTAPublicKey is the Ed25519 release public key (base64) used to authenticate
// OTA manifests and delegated server certificates. Injected via
// -ldflags "-X main.OTAPublicKey=<b64>". When empty, response verification is skipped
// and OTA self-updates are completely disabled.
var OTAPublicKey = ""

const (
	BinaryPath = "/tmp/tracker"
	PrivateDir = "/tmp/transit-tracker"
	ImagePath  = "/tmp/transit-tracker/dashboard.png"
	// The view-override and manual-lighting holds (formerly
	// ManualHoldDuration) are now the policy's `hold`: see interaction.go.
)

// ensurePrivateDir creates /tmp/transit-tracker with 0700 permissions and verifies
// that it is a real directory owned by current uid (not a pre-planted symlink).
func ensurePrivateDir() error {
	info, err := osLstat(PrivateDir)
	if err == nil {
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			_ = osRemoveAll(PrivateDir)
		} else if stat, ok := info.Sys().(*syscall.Stat_t); ok {
			if int(stat.Uid) != osGetuid() {
				_ = osRemoveAll(PrivateDir)
			}
		}
	}
	if err := osMkdirAll(PrivateDir, 0700); err != nil {
		return err
	}
	_ = osChmod(PrivateDir, 0700)
	return nil
}

// TrackerClient coordinates e-ink display updates, power states, inputs, and server communication.
type TrackerClient struct {
	client    *http.Client
	refreshCh chan struct{}
	// touchCh fires on ANY recognised touch, so an interaction session stays
	// alive while the user is poking at the screen (a frontlight tap must reset
	// the idle timer too, not just data taps).
	touchCh    chan struct{}
	logCh      chan string
	logStarted sync.Once
	panelOnce  sync.Once
	panelSize  PanelSize
	// exitOnPowerKey, when true (resident mode), treats a hardware power-key
	// press as a request to exit. In low-power dashboard mode it is false: the
	// power key is a wake source, not an exit, so a press must not kill us.
	exitOnPowerKey bool
	wg             sync.WaitGroup

	mu sync.Mutex
	// Fields protected by mu:
	clientID string
	// overlay is the interaction state machine (idle/session/hold), which
	// owns the session flag, the manual view override and the manual
	// lighting/fast-poll holds (see interaction.go).
	overlay Overlay
	// policy is the last authenticated server policy (defaultPolicy() until
	// one arrives); lastPollSec is the last server-advised poll interval.
	policy              Policy
	lastPollSec         int
	serverURL           string
	lastETag            string
	consecutiveErrors   int
	consecutiveFailures int
	lastDiscoveryTime   time.Time
	discoveryBackoff    time.Duration
	lastRenderedView    string
	// presentation is the server-advised visual/interaction state: "interactive"
	// (tappable dashboard, awake), "idle" (suspended; press power to interact)
	// or "dormant" (overnight). Logged for observability.
	presentation string
}

// NewTrackerClient constructs an initialized TrackerClient with default channels and HTTP timeouts.
func NewTrackerClient(server string, initialView string) *TrackerClient {
	policy := defaultPolicy()
	var overlay Overlay
	if initialView != "" && initialView != "auto" {
		// A view carried across a re-exec is a manual choice: resume its hold.
		overlay = Overlay{}.Transition(InteractionEvent{Kind: EvViewTap, View: initialView}, time.Now(), policy)
		overlay.FastUntil = time.Time{}
	}
	return &TrackerClient{
		clientID:  GetClientID(),
		serverURL: server,
		overlay:   overlay,
		policy:    policy,
		client: &http.Client{
			Timeout: 15 * time.Second,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		refreshCh:        make(chan struct{}, 1),
		touchCh:          make(chan struct{}, 1),
		logCh:            make(chan string, 64),
		exitOnPowerKey:   true,
		presentation:     "interactive",
		discoveryBackoff: time.Minute,
	}
}

func (tc *TrackerClient) getViewMode() string {
	// A manual view override lapses with the hold (or a phase change).
	if v := tc.overlayNow().View; v != "" {
		return v
	}
	return "auto"
}

func (tc *TrackerClient) cycleViewMode() string {
	current := tc.overlayNow().View
	if current == "" {
		tc.mu.Lock()
		current = tc.lastRenderedView
		tc.mu.Unlock()
		if current == "" {
			current = "evening"
		}
	}

	policyViews := tc.currentPolicy().Views
	if len(policyViews) > 1 {
		idx := -1
		for i, v := range policyViews {
			if v == current {
				idx = i
				break
			}
		}
		next := policyViews[(idx+1)%len(policyViews)]
		return tc.setExplicitViewMode(next)
	}

	// Clean 2-way toggle between Morning (Citi Bike) and Evening (Bus) views
	next := "morning"
	if current == "morning" {
		next = "evening"
	}
	return tc.setExplicitViewMode(next)
}

func (tc *TrackerClient) setExplicitViewMode(target string) string {
	tc.interact(InteractionEvent{Kind: EvViewTap, View: target})
	return target
}

func (tc *TrackerClient) getServerURL() string {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	return tc.serverURL
}

func (tc *TrackerClient) setServerURL(url string) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	tc.serverURL = url
}

func (tc *TrackerClient) getClientID() string {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	if tc.clientID == "" {
		tc.clientID = GetClientID()
	}
	return tc.clientID
}

func (tc *TrackerClient) setClientID(id string) {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	tc.clientID = id
}

// setPresentation records the server-advised visual/interaction state.
func (tc *TrackerClient) setPresentation(p string) {
	if p == "" {
		return
	}
	tc.mu.Lock()
	tc.presentation = p
	tc.mu.Unlock()
}

// getPresentation returns the current presentation ("interactive" by default).
func (tc *TrackerClient) getPresentation() string {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	if tc.presentation == "" {
		return "interactive"
	}
	return tc.presentation
}

// interactionHoldDuration is how long a touch wake keeps the device awake with
// no further taps before it re-suspends. Variable for tests.
var interactionHoldDuration = 90 * time.Second

func (tc *TrackerClient) isInteracting() bool {
	return tc.inSession()
}

func (tc *TrackerClient) setInteracting(v bool) {
	if v {
		tc.interact(InteractionEvent{Kind: EvPowerWake})
	} else {
		tc.interact(InteractionEvent{Kind: EvSessionEnd})
	}
}

// interactionAwake runs a power-button session: the overlay enters `session`,
// the client stays awake for `d` (the policy's session timeout) after the last
// touch, servicing forced-refresh taps so the user can browse, then the
// overlay leaves the session (to `hold`). Returns false if ctx ended.
func (tc *TrackerClient) interactionAwake(ctx context.Context, cancel context.CancelFunc, d time.Duration) bool {
	tc.interact(InteractionEvent{Kind: EvPowerWake})
	defer tc.interact(InteractionEvent{Kind: EvSessionEnd})
	// Hold the screensaver open so powerd doesn't auto-sleep mid-browse.
	lipcSet("com.lab126.powerd", "preventScreenSaver", "1")
	// Light the panel for the session (off-peak the auto-lighting leaves it
	// dark, so a night-time interaction would be unreadable). Auto-lighting
	// is suppressed while the overlay is in `session`.
	tc.applySessionLighting()

	// Render the full tappable dashboard: the user just pressed power to engage
	// and the suspended face was the inert strip. Wi-Fi is still re-associating
	// right after the wake, so wait for the link and retry a few times, or the
	// interactive render would silently fail and the panel would stay idle.
	rendered := false
	for i := 0; i < 5 && !rendered; i++ {
		tc.waitForNetwork(ctx)
		if tc.fetchAndDrawDashboard(ctx, cancel) > 0 {
			rendered = true
			break
		}
		if !tc.sleepWallClock(ctx, 2*time.Second) {
			return false
		}
	}
	tc.logRemote(fmt.Sprintf("Interactive session: dashboard rendered=%v.", rendered))

	timer := time.NewTimer(d)
	defer timer.Stop()
	reset := func() {
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(d)
	}
	for {
		select {
		case <-ctx.Done():
			return false
		case <-timer.C:
			return true
		case <-tc.touchCh:
			// Any touch keeps the session alive (even a frontlight-only tap).
			reset()
		case <-tc.refreshCh:
			tc.fetchAndDrawDashboard(ctx, cancel)
			reset()
		}
	}
}

func (tc *TrackerClient) isManualViewActive() bool {
	return tc.overlayNow().View != ""
}

func (tc *TrackerClient) recordPollSuccess() {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	tc.consecutiveFailures = 0
	tc.consecutiveErrors = 0
	tc.discoveryBackoff = time.Minute
}

func (tc *TrackerClient) recordPollFailure(ctx context.Context) {
	tc.mu.Lock()
	tc.consecutiveFailures++
	tc.consecutiveErrors++
	count := tc.consecutiveFailures
	now := time.Now()
	backoff := tc.discoveryBackoff
	if backoff < time.Minute {
		backoff = time.Minute
	}
	shouldRediscover := count >= 3 && now.Sub(tc.lastDiscoveryTime) >= backoff
	if shouldRediscover {
		tc.lastDiscoveryTime = now
		tc.discoveryBackoff = backoff * 2
		if tc.discoveryBackoff > 60*time.Minute {
			tc.discoveryBackoff = 60 * time.Minute
		}
	}
	tc.mu.Unlock()

	if shouldRediscover {
		tc.logRemote(fmt.Sprintf("3+ consecutive poll failures (count=%d). Running LAN rediscovery...", count))
		if discovered, err := autoDiscover(ctx); err == nil && discovered != "" {
			tc.setServerURL(discovered)
			tc.recordPollSuccess()
			_ = SaveServerURL(discovered)
			tc.logRemote(fmt.Sprintf("LAN Auto-discovery adopted verified server: %s", discovered))
		}
	}
}

// Wait blocks until all background goroutines tracked by tc have finished.
func (tc *TrackerClient) Wait() {
	tc.wg.Wait()
}

func setupStderr() {
	_ = ensurePrivateDir()
	logPath := filepath.Join(PrivateDir, "client.log")
	flags := os.O_CREATE | os.O_WRONLY | os.O_APPEND
	if info, err := osStat(logPath); err == nil && info.Size() > 256*1024 {
		flags = os.O_CREATE | os.O_WRONLY | os.O_TRUNC
	}
	if f, err := osOpenFile(logPath, flags, 0600); err == nil {
		_ = syscallDup2(int(f.Fd()), int(os.Stderr.Fd()))
	} else if nullFile, err := osOpenFile(os.DevNull, os.O_WRONLY, 0); err == nil {
		_ = syscallDup2(int(nullFile.Fd()), int(os.Stderr.Fd()))
	}
}

func main() {
	setupStderr()
	run(context.Background())
}

// run dispatches on the requested run mode. The default (resident) mode blocks
// in the poll loop; oneshot renders once and exits; sleep renders, arms an RTC
// wake, and cycles through device suspend.
func run(parent context.Context) {
	_ = ensurePrivateDir()

	mode := ResolveRunMode(os.Args)
	currentRunMode = mode

	serverURL := GetServerURL()
	initialView := ResolveViewMode(os.Args)
	ctx, cancel := signal.NotifyContext(parent, os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	tc := NewTrackerClient(serverURL, initialView)
	defer func() {
		cancel()
		tc.Wait()
	}()

	tc.wg.Add(1)
	go func() {
		defer tc.wg.Done()
		select {
		case <-parent.Done():
			return
		case <-ctx.Done():
			if parent.Err() == nil {
				tc.logRemote("OS signal received. Exiting...")
			}
		}
	}()

	tc.startLogSender(ctx)
	_ = osWriteFile("/tmp/tracker_server.txt", []byte(serverURL), 0644)

	// Launcher self-update
	launcherPath := resolveLauncherPath(os.Args)
	if err := selfUpdateLauncher(launcherPath); err != nil {
		tc.logRemote(fmt.Sprintf("Launcher self-update warning: %v", err))
	}

	if OTAPublicKey == "" {
		tc.logRemote("WARNING: OTAPublicKey is empty! Response verification is disabled and OTA updates are DISABLED.")
	} else {
		tc.logRemote("OTA release verification is ACTIVE.")
	}

	tc.logRemote(fmt.Sprintf("Transit Tracker v%s starting up (mode: %s, server: %s, view: %s)...", Version, currentModeName(), serverURL, initialView))
	// Log the raw framebuffer geometry so panel/orientation issues are visible.
	if modes, err := osReadFile("/sys/class/graphics/fb0/modes"); err == nil {
		tc.logRemote("fb0/modes: " + strings.TrimSpace(string(modes)))
	}
	if vsize, err := osReadFile("/sys/class/graphics/fb0/virtual_size"); err == nil {
		tc.logRemote("fb0/virtual_size: " + strings.TrimSpace(string(vsize)))
	}
	p := tc.getPanelSize()
	tc.logRemote(fmt.Sprintf("Detected panel (landscape): %dx%d", p.LandscapeW, p.LandscapeH))
	if devData, err := osReadFile("/proc/bus/input/devices"); err == nil {
		tc.logRemote(fmt.Sprintf("Input devices:\n%s", string(devData)))
	}

	// Upload a device/jailbreak capability report on every startup (passive
	// only; the active probe runs on request) so the server can track the fleet.
	tc.postDiagnostics(false)

	if mode == ModeOneshot {
		// Render exactly once and exit, leaving the image on screen. Deliberately
		// do NOT set preventScreenSaver or run cleanup (which clears the screen).
		tc.logRemote("Oneshot mode: rendering once and exiting.")
		tc.fetchAndDrawDashboard(ctx, cancel)
		return
	}

	if mode == ModeSleep {
		// Low-power mode: do NOT hold the screensaver open (suspending is the
		// point) and do NOT start the power listener -- it exits on
		// goingToScreenSaver, which is exactly the suspend we want to allow.
		// The power key is a wake source here, not an exit.
		tc.exitOnPowerKey = false
		tc.startInputListeners(ctx, cancel)
		tc.runSleepLoop(ctx, cancel, wantsSuspend(os.Args))
		return
	}

	lipcSet("com.lab126.powerd", "preventScreenSaver", "1")

	tc.startInputListeners(ctx, cancel)
	tc.wg.Add(1)
	go func() {
		defer tc.wg.Done()
		tc.startPowerListener(ctx, cancel)
	}()

	initialPollSec := tc.fetchAndDrawDashboard(ctx, cancel)
	tc.runPollLoop(ctx, cancel, tc.getNextPollInterval(initialPollSec))
}
