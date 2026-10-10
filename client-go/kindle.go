package main

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"
)

// lipcCallTimeout bounds how long a caller waits on a lipc property call. The
// gesture handlers run on the input dispatcher goroutine, so a slow or
// unresponsive daemon (notably powerd right after a power-button wake) must
// never be able to block input forever. If a call exceeds the timeout the
// child process is terminated.
var lipcCallTimeout = 2 * time.Second

// lipcSet executes a lipc-set-prop command with a timeout bound, discarding output.
// The child process is terminated if the timeout expires.
func lipcSet(prop, key, val string) {
	ctx, cancel := context.WithTimeout(context.Background(), lipcCallTimeout)
	defer cancel()
	cmd := execCommandContext(ctx, "lipc-set-prop", "-i", prop, key, val)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	_ = cmd.Run()
}

// lipcGet reads a property using lipc-get-prop, bounded by lipcCallTimeout.
// The child process is terminated if the timeout expires.
func lipcGet(prop, key string) string {
	ctx, cancel := context.WithTimeout(context.Background(), lipcCallTimeout)
	defer cancel()
	cmd := execCommandContext(ctx, "lipc-get-prop", prop, key)
	cmd.Stderr = io.Discard
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// cleanup performs full cleanup, resets screensaver, clears screen, and restores Kindle UI
func (tc *TrackerClient) cleanup() {
	tc.logRemote("Cleaning up and exiting to Kindle Home...")

	lipcSet("com.lab126.powerd", "preventScreenSaver", "0")

	// Low-power mode stops the Amazon UI framework (lab126_gui) to allow
	// suspend. If we exited without restarting it, the panel would be stuck on
	// our image with no UI behind it -- a blank, unresponsive screen. So always
	// best-effort restart the framework before restoring Home.
	startCmd := execCommand("start", "lab126_gui")
	startCmd.Stdout = io.Discard
	startCmd.Stderr = io.Discard
	_ = startCmd.Run()

	// Deliberately do NOT `eips -c` here: clearing the panel while the framework
	// is still starting would leave it blank. "start Home" repaints it anyway.
	lipcSet("com.lab126.appmgrd", "start", "app://com.lab126.booklet.home")
}

// releaseScreenSaver lets the device sleep again (used by oneshot/sleep modes
// so we don't hold the SoC awake between renders).
func (tc *TrackerClient) releaseScreenSaver() {
	lipcSet("com.lab126.powerd", "preventScreenSaver", "0")
}

// prepareDisplayForSleep stops the Amazon UI framework and unloads the blanket
// screensaver so nothing repaints over our dashboard and the device can suspend.
// Idempotent. Only appropriate for a dedicated dashboard.
func (tc *TrackerClient) prepareDisplayForSleep(ctx context.Context) {
	runQuiet(ctx, "stop", "lab126_gui")
	runQuiet(ctx, "lipc-set-prop", "com.lab126.blanket", "unload", "screensaver")
	runQuiet(ctx, "lipc-set-prop", "com.lab126.blanket", "unload", "splash")
}

// runQuiet runs a command, discarding output.
func runQuiet(ctx context.Context, name string, args ...string) {
	cmd := execCommandContext(ctx, name, args...)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	_ = cmd.Run()
}

// cycleFrontlight advances brightness: Off (0) -> Cozy (8) -> Bright (18) -> Off (0)
func (tc *TrackerClient) cycleFrontlight() {
	tc.mu.Lock()
	tc.manualLightTime = time.Now()
	tc.mu.Unlock()

	currStr := lipcGet("com.lab126.powerd", "flIntensity")
	curr, _ := strconv.Atoi(currStr)

	nextIntensity, nextWarmth := NextFrontlightState(curr)

	lipcSet("com.lab126.powerd", "flIntensity", strconv.Itoa(nextIntensity))
	lipcSet("com.lab126.powerd", "schedAmberLevel", strconv.Itoa(nextWarmth))
	tc.logRemote(fmt.Sprintf("Frontlight cycled: %s -> intensity %d (warmth %d)", currStr, nextIntensity, nextWarmth))
}

// getPanelSize returns the device framebuffer dimensions, detecting them once.
func (tc *TrackerClient) getPanelSize() PanelSize {
	tc.panelOnce.Do(func() {
		tc.panelSize = DetectPanelSize()
	})
	return tc.panelSize
}

// rawTouchToDesign maps a raw touch coordinate (in the panel's portrait
// framebuffer space) into the renderer's design space (800px wide). The server
// draws the landscape design at the panel's native landscape resolution and
// then rotates it 90 degrees counter-clockwise into the portrait framebuffer,
// so we undo that rotation here:
//
//	dx = (Wl - 1 - py) * DesignWidth / Wl
//	dy = px * DesignWidth / Wl
//
// where (px,py) is the raw portrait coordinate and Wl is the landscape width
// (equal to the portrait height). The bottom button bar lands at portrait
// px ~= 1145..1215 (design y 556..590), and columns order left-to-right
// from py ~= 1448 (BUSES) to py ~= 130 (REFRESH).
func (tc *TrackerClient) rawTouchToDesign(px, py int32) (int32, int32) {
	p := tc.getPanelSize()
	wl := float64(p.LandscapeW)
	if wl <= 0 {
		return px, py
	}
	scale := wl / float64(DesignWidth)
	dx := (wl - 1 - float64(py)) / scale
	dy := float64(px) / scale
	return int32(dx + 0.5), int32(dy + 0.5)
}

var (
	firmwareOnce   sync.Once
	cachedFirmware string
)

// ReadFirmwareVersion inspects candidate firmware files (/etc/prettyversion.txt, /etc/version)
// and extracts a clean firmware identifier, such as "Kindle 5.18.6" or "5.16.2".
// Extraneous parenthesized build hashes like (~~otaVersion~~) are stripped.
func ReadFirmwareVersion(readFile func(string) ([]byte, error)) string {
	if readFile == nil {
		return ""
	}
	for _, p := range []string{"/etc/prettyversion.txt", "/etc/version"} {
		data, err := readFile(p)
		if err != nil {
			continue
		}
		lines := strings.Split(string(data), "\n")
		for _, raw := range lines {
			line := strings.TrimSpace(raw)
			if line == "" {
				continue
			}
			if idx := strings.Index(line, "("); idx > 0 {
				line = strings.TrimSpace(line[:idx])
			}
			if line != "" {
				if len(line) > 32 {
					line = line[:32]
				}
				return line
			}
		}
	}
	return ""
}

// GetFirmwareVersion returns the detected Kindle firmware version, cached across calls.
func GetFirmwareVersion() string {
	firmwareOnce.Do(func() {
		cachedFirmware = ReadFirmwareVersion(osReadFile)
	})
	return cachedFirmware
}

// resetFirmwareCache resets cached firmware for test isolation.
func resetFirmwareCache() {
	firmwareOnce = sync.Once{}
	cachedFirmware = ""
}
