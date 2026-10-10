package main

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/mike10010100/transit-tracker/client-go/internal/otasig"
)

var (
	certCacheMu sync.Mutex
	certCache   = make(map[string]ed25519.PublicKey)

	lastUntrustedLogMu sync.Mutex
	lastUntrustedLog   time.Time

	ownExeSHAMu   sync.Mutex
	ownExeSHAVals string

	otaBackoffMu sync.Mutex
	otaBackoffs  = make(map[string]*otaBackoffEntry)

	otaHTTPClient = &http.Client{
		Timeout: 180 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
)

type otaBackoffEntry struct {
	nextAttempt time.Time
	delay       time.Duration
}

func isOTABackoff(sha string) bool {
	otaBackoffMu.Lock()
	defer otaBackoffMu.Unlock()
	entry, ok := otaBackoffs[sha]
	if !ok {
		return false
	}
	return time.Now().Before(entry.nextAttempt)
}

func recordOTAFailure(sha string) {
	if sha == "" {
		return
	}
	otaBackoffMu.Lock()
	defer otaBackoffMu.Unlock()
	entry, ok := otaBackoffs[sha]
	if !ok {
		delay := 5 * time.Minute
		otaBackoffs[sha] = &otaBackoffEntry{
			nextAttempt: time.Now().Add(delay),
			delay:       delay,
		}
	} else {
		newDelay := entry.delay * 4
		if newDelay > 6*time.Hour {
			newDelay = 6 * time.Hour
		}
		entry.delay = newDelay
		entry.nextAttempt = time.Now().Add(newDelay)
	}
}

func getOwnExeSHA() string {
	ownExeSHAMu.Lock()
	defer ownExeSHAMu.Unlock()
	if ownExeSHAVals != "" {
		return ownExeSHAVals
	}
	data, err := osReadFile("/proc/self/exe")
	if err != nil {
		if exePath, err := osExecutable(); err == nil {
			data, _ = osReadFile(exePath)
		}
	}
	if len(data) > 0 {
		ownExeSHAVals = otasig.SHA256Hex(data)
	}
	return ownExeSHAVals
}

func (tc *TrackerClient) logUntrustedResponse(err error) {
	lastUntrustedLogMu.Lock()
	defer lastUntrustedLogMu.Unlock()
	if time.Since(lastUntrustedLog) >= time.Minute {
		lastUntrustedLog = time.Now()
		tc.logRemote(fmt.Sprintf("WARNING: Untrusted response from %s: %v", tc.getServerURL(), err))
	}
}

func verifyResponseAuth(releasePub ed25519.PublicKey, nonce, path string, resp *http.Response, bodyBytes []byte) error {
	certHdr := resp.Header.Get(otasig.CertHeader)
	if certHdr == "" {
		return errors.New("missing X-Tracker-Cert header")
	}
	certCacheMu.Lock()
	serverPub, cached := certCache[certHdr]
	certCacheMu.Unlock()
	if !cached {
		var err error
		serverPub, err = otasig.VerifyCertHeader(releasePub, certHdr)
		if err != nil {
			return fmt.Errorf("invalid cert: %w", err)
		}
		certCacheMu.Lock()
		certCache[certHdr] = serverPub
		certCacheMu.Unlock()
	}

	authHdr := resp.Header.Get(otasig.AuthHeader)
	if authHdr == "" {
		return errors.New("missing X-Tracker-Auth header")
	}
	bodySHA := otasig.SHA256Hex(bodyBytes)
	return otasig.VerifyResponse(serverPub, nonce, path, resp.StatusCode, bodySHA, resp.Header, authHdr)
}

var (
	minPollIntervalSec = 30
	maxPollIntervalSec = 7200
)

func parsePollInterval(s string) int {
	if s == "" {
		return 0
	}
	v, err := strconv.Atoi(s)
	if err != nil || v <= 0 {
		return 0
	}
	if v < minPollIntervalSec {
		return minPollIntervalSec
	}
	if v > maxPollIntervalSec {
		return maxPollIntervalSec
	}
	return v
}

func buildReexecArgs(origArgs []string, server, view string, manualViewActive bool) []string {
	var kept []string
	skipNext := false
	for i := 1; i < len(origArgs); i++ {
		if skipNext {
			skipNext = false
			continue
		}
		arg := origArgs[i]
		if arg == "-server" || arg == "--server" {
			skipNext = true
			continue
		}
		if strings.HasPrefix(arg, "-server=") || strings.HasPrefix(arg, "--server=") {
			continue
		}
		if arg == "-view" || arg == "--view" {
			skipNext = true
			continue
		}
		if strings.HasPrefix(arg, "-view=") || strings.HasPrefix(arg, "--view=") {
			continue
		}
		kept = append(kept, arg)
	}

	result := []string{BinaryPath, "-server", server}
	if view == "auto" || manualViewActive {
		result = append(result, "-view", view)
	}
	result = append(result, kept...)
	return result
}

func updateBackup(src string) {
	data, err := osReadFile(src)
	if err != nil {
		return
	}
	tmpBackup := "/mnt/us/documents/tracker_backup.tmp"
	backupPath := "/mnt/us/documents/tracker_backup"
	if err := osWriteFile(tmpBackup, data, 0755); err != nil {
		return
	}
	_ = osRename(tmpBackup, backupPath)
}

// maybeUpdateBinary downloads and installs a new binary if the version/SHA
// advertised by the server in a verified dashboard response differs from the
// running binary. Implements the full OTA protocol per security spec §5.
func (tc *TrackerClient) maybeUpdateBinary(ctx context.Context, serverVer, serverSHA string) bool {
	if OTAPublicKey == "" {
		return false
	}
	releasePub, err := otasig.ParsePublicKey(OTAPublicKey)
	if err != nil {
		return false
	}
	if serverSHA == "" {
		return false
	}
	ownSHA := getOwnExeSHA()
	if ownSHA != "" && serverSHA == ownSHA {
		return false
	}
	if isOTABackoff(serverSHA) {
		return false
	}

	server := tc.getServerURL()

	// Step 1: GET /tracker-arm.manifest
	req, err := http.NewRequestWithContext(ctx, "GET", server+"/tracker-arm.manifest", nil)
	if err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	req.Header.Set("X-Tracker-Client-ID", tc.getClientID())
	req.Header.Set("X-Tracker-Client-Version", Version)
	if fw := GetFirmwareVersion(); fw != "" {
		req.Header.Set("X-Tracker-Firmware", fw)
	}
	resp, err := otaHTTPClient.Do(req)
	if err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		recordOTAFailure(serverSHA)
		return false
	}
	manifestBytes, err := io.ReadAll(io.LimitReader(resp.Body, otasig.MaxManifestSize+1))
	if err != nil || int64(len(manifestBytes)) > otasig.MaxManifestSize {
		recordOTAFailure(serverSHA)
		return false
	}
	manifest, err := otasig.VerifyManifest(releasePub, manifestBytes)
	if err != nil {
		tc.logRemote(fmt.Sprintf("OTA manifest verification failed: %v", err))
		recordOTAFailure(serverSHA)
		return false
	}

	// Step 2: semver check, sha check, size check
	if !otasig.IsNewer(manifest.Version, Version) {
		tc.logRemote(fmt.Sprintf("OTA manifest version %s not newer than %s", manifest.Version, Version))
		recordOTAFailure(serverSHA)
		return false
	}
	if ownSHA != "" && manifest.SHA256 == ownSHA {
		recordOTAFailure(serverSHA)
		return false
	}
	if manifest.Size <= 0 || manifest.Size > otasig.MaxBinarySize {
		recordOTAFailure(serverSHA)
		return false
	}

	// Step 3: GET /tracker-arm with dedicated client
	tc.logRemote(fmt.Sprintf("Downloading verified update v%s (size: %d)...", manifest.Version, manifest.Size))
	binReq, err := http.NewRequestWithContext(ctx, "GET", server+"/tracker-arm", nil)
	if err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	binReq.Header.Set("X-Tracker-Client-ID", tc.getClientID())
	binReq.Header.Set("X-Tracker-Client-Version", Version)
	if fw := GetFirmwareVersion(); fw != "" {
		binReq.Header.Set("X-Tracker-Firmware", fw)
	}
	binResp, err := otaHTTPClient.Do(binReq)
	if err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	defer binResp.Body.Close()
	if binResp.StatusCode != http.StatusOK {
		recordOTAFailure(serverSHA)
		return false
	}

	_ = ensurePrivateDir()
	tmpFile, err := osCreateTemp(PrivateDir, "tracker-ota-*.tmp")
	if err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	tmpPath := tmpFile.Name()
	defer func() {
		if tmpFile != nil {
			tmpFile.Close()
			_ = osRemove(tmpPath)
		}
	}()

	hasher := sha256.New()
	written, err := io.Copy(io.MultiWriter(tmpFile, hasher), io.LimitReader(binResp.Body, manifest.Size+1))
	if err != nil || written != manifest.Size {
		tc.logRemote(fmt.Sprintf("OTA download size mismatch: wrote %d, want %d (err: %v)", written, manifest.Size, err))
		recordOTAFailure(serverSHA)
		return false
	}
	actualSHA := hex.EncodeToString(hasher.Sum(nil))
	if actualSHA != manifest.SHA256 {
		tc.logRemote(fmt.Sprintf("OTA download checksum mismatch: %s != %s", actualSHA, manifest.SHA256))
		recordOTAFailure(serverSHA)
		return false
	}
	if err := tmpFile.Sync(); err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	if err := tmpFile.Close(); err != nil {
		recordOTAFailure(serverSHA)
		return false
	}
	tmpFile = nil // Prevent double close in defer

	// Step 4: chmod 0755, rename to BinaryPath
	if err := osChmod(tmpPath, 0755); err != nil {
		_ = osRemove(tmpPath)
		recordOTAFailure(serverSHA)
		return false
	}
	if err := osRename(tmpPath, BinaryPath); err != nil {
		_ = osRemove(tmpPath)
		recordOTAFailure(serverSHA)
		return false
	}

	// Step 5: Update persistent backup
	updateBackup(BinaryPath)

	// Step 6: syscall.Exec
	rebuiltArgs := buildReexecArgs(os.Args, server, tc.getViewMode(), tc.isManualViewActive())
	tc.logRemote(fmt.Sprintf("OTA update successful! Hot-reloading into v%s via syscall.Exec...", manifest.Version))
	if err := sysExec(BinaryPath, rebuiltArgs, os.Environ()); err != nil {
		tc.logRemote(fmt.Sprintf("syscall.Exec failed: %v", err))
		recordOTAFailure(serverSHA)
		return false
	}
	return true
}

// fetchAndDrawDashboard fetches dashboard PNG, verifies response, applies lighting, and pushes to e-ink.
// Returns the target poll interval in seconds reported by the server header (or 0 if unavailable).
func (tc *TrackerClient) fetchAndDrawDashboard(ctx context.Context, exitCancel context.CancelFunc) int {
	batt := GetBatteryInfo()
	chargeVal := 0
	if batt.IsCharging {
		chargeVal = 1
	}

	viewMode := tc.getViewMode()
	server := tc.getServerURL()
	panel := tc.getPanelSize()
	url := fmt.Sprintf("%s/dashboard.png?kindle=pw5&w=%d&h=%d&batt=%d&charging=%d&view=%s&t=%d",
		server, panel.LandscapeW, panel.LandscapeH, batt.Level, chargeVal, viewMode, time.Now().Unix())
	if tc.isInteracting() {
		url += "&present=interactive"
	}

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		tc.handleNetworkError(ctx)
		return 0
	}

	// Per-response authentication nonce (§3)
	nonce, err := otasig.NewNonce()
	if err != nil {
		tc.handleNetworkError(ctx)
		return 0
	}
	req.Header.Set(otasig.NonceHeader, nonce)
	req.Header.Set("X-Tracker-Client-ID", tc.getClientID())
	req.Header.Set("X-Tracker-Client-Version", Version)
	if fw := GetFirmwareVersion(); fw != "" {
		req.Header.Set("X-Tracker-Firmware", fw)
	}

	req.Header.Set("X-Kindle-Battery", strconv.Itoa(batt.Level))
	req.Header.Set("X-Kindle-Charging", strconv.Itoa(chargeVal))
	req.Header.Set("X-Tracker-View", viewMode)
	req.Header.Set("X-Tracker-Mode", currentModeName())

	tc.mu.Lock()
	etag := tc.lastETag
	tc.mu.Unlock()
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}

	resp, err := tc.client.Do(req)
	if err != nil {
		tc.handleNetworkError(ctx)
		return 0
	}
	defer resp.Body.Close()

	// Read bounded body up to 4 MiB
	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20+1))
	if err != nil || len(bodyBytes) > 4<<20 {
		tc.handleNetworkError(ctx)
		return 0
	}

	// Verify per-response authentication if OTAPublicKey is configured
	if OTAPublicKey != "" {
		releasePub, err := otasig.ParsePublicKey(OTAPublicKey)
		if err != nil {
			tc.logRemote(fmt.Sprintf("Invalid OTAPublicKey: %v", err))
			tc.recordPollFailure(ctx)
			return 0
		}
		if err := verifyResponseAuth(releasePub, nonce, "/dashboard.png", resp, bodyBytes); err != nil {
			tc.logUntrustedResponse(err)
			tc.recordPollFailure(ctx)
			return 0
		}
	}

	// Response is authenticated and trusted
	tc.recordPollSuccess()

	// Server version/SHA from manifest
	serverVer := resp.Header.Get("X-Tracker-Version")
	serverSHA := resp.Header.Get("X-Tracker-SHA256")

	// HTTP 205 remote stop command
	if resp.StatusCode == 205 {
		tc.logRemote("Server sent HTTP 205 Stop signal. Exiting cleanly...")
		exitCancel()
		return 0
	}

	// HTTP 304: dashboard unchanged
	if resp.StatusCode == http.StatusNotModified {
		pollSec := parsePollInterval(resp.Header.Get("X-Kindle-Poll-Interval"))
		tc.processControlHeaders(ctx, resp.Header)
		if tc.maybeUpdateBinary(ctx, serverVer, serverSHA) {
			return 0
		}
		return pollSec
	}

	if resp.StatusCode != http.StatusOK {
		return 0
	}

	// Apply view
	if resView := resp.Header.Get("X-Resolved-View"); resView != "" {
		tc.mu.Lock()
		tc.lastRenderedView = resView
		tc.mu.Unlock()
	}

	tc.processControlHeaders(ctx, resp.Header)

	serverPollSec := parsePollInterval(resp.Header.Get("X-Kindle-Poll-Interval"))

	// Write dashboard image atomically via temp file in PrivateDir
	_ = ensurePrivateDir()
	tmpImg, err := osCreateTemp(PrivateDir, "dash-*.png")
	if err != nil {
		return serverPollSec
	}
	tmpImgPath := tmpImg.Name()
	if _, err := tmpImg.Write(bodyBytes); err != nil {
		tmpImg.Close()
		_ = osRemove(tmpImgPath)
		return serverPollSec
	}
	_ = tmpImg.Close()
	if err := osRename(tmpImgPath, ImagePath); err != nil {
		_ = osRemove(tmpImgPath)
		return serverPollSec
	}

	// Draw to screen
	cmd := execCommand("eips", "-f", "-g", ImagePath)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err == nil {
		// Only commit lastETag after eips succeeded (L2)
		tc.mu.Lock()
		if newTag := resp.Header.Get("ETag"); newTag != "" {
			tc.lastETag = newTag
		}
		tc.mu.Unlock()
	}

	if tc.maybeUpdateBinary(ctx, serverVer, serverSHA) {
		return 0
	}
	return serverPollSec
}

func (tc *TrackerClient) processControlHeaders(ctx context.Context, header http.Header) {
	tc.setPresentation(header.Get("X-Tracker-Presentation"))

	// Diagnostics request
	if diag := header.Get("X-Tracker-Diag"); diag != "" {
		tc.postDiagnostics(diag == "full")
	}

	// Named device action
	if action := header.Get("X-Tracker-Action"); action != "" {
		result, known := runAction(ctx, action)
		tc.waitForNetwork(ctx)
		tc.logRemote(fmt.Sprintf("Device action %q ->\n%s", action, result))
		if !known {
			tc.logRemote(fmt.Sprintf("Unknown device action %q ignored.", action))
		}
	}

	// Mode switch request
	if want := strings.ToLower(strings.TrimSpace(header.Get("X-Tracker-Mode"))); want != "" && want != currentModeName() {
		if flags := modeFlags(want); flags != nil {
			tc.logRemote(fmt.Sprintf("Server requested run mode %q; relaunching.", want))
			rebuiltArgs := buildReexecArgs(os.Args, tc.getServerURL(), tc.getViewMode(), tc.isManualViewActive())
			rebuiltArgs = append(rebuiltArgs, flags...)
			if err := sysExec(BinaryPath, rebuiltArgs, os.Environ()); err != nil {
				tc.logRemote(fmt.Sprintf("Mode relaunch exec FAILED (%v); staying in %q.", err, currentModeName()))
			}
		}
	}

	// Commute auto-lighting control
	tc.mu.Lock()
	manualActive := time.Since(tc.manualLightTime) < ManualHoldDuration
	tc.mu.Unlock()

	if !manualActive {
		brightStr := header.Get("X-Kindle-Brightness")
		warmStr := header.Get("X-Kindle-Warmth")
		currB := lipcGet("com.lab126.powerd", "flIntensity")
		currW := lipcGet("com.lab126.powerd", "schedAmberLevel")

		if brightVal, err := strconv.Atoi(brightStr); err == nil && brightVal >= 0 && brightVal <= 24 && brightStr != currB {
			lipcSet("com.lab126.powerd", "flIntensity", brightStr)
			tc.logRemote(fmt.Sprintf("Commute auto-lighting applied: brightness %s -> %s", currB, brightStr))
		}
		if warmVal, err := strconv.Atoi(warmStr); err == nil && warmVal >= 0 && warmVal <= 24 && warmStr != currW {
			lipcSet("com.lab126.powerd", "schedAmberLevel", warmStr)
			tc.logRemote(fmt.Sprintf("Commute auto-lighting applied: warmth %s -> %s", currW, warmStr))
		}
	}
}

// runPollLoop drives the fetch/OTA cycle until ctx is cancelled.
func (tc *TrackerClient) runPollLoop(ctx context.Context, cancel context.CancelFunc, interval time.Duration) {
	timer := time.NewTimer(alignDelay(time.Now(), interval))
	defer timer.Stop()

	reschedule := func(serverPollSec int) {
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(alignDelay(time.Now(), tc.getNextPollInterval(serverPollSec)))
	}

	for {
		select {
		case <-ctx.Done():
			tc.cleanup()
			return

		case <-tc.refreshCh:
			serverPollSec := tc.fetchAndDrawDashboard(ctx, cancel)
			reschedule(serverPollSec)

		case <-timer.C:
			serverPollSec := tc.fetchAndDrawDashboard(ctx, cancel)
			reschedule(serverPollSec)
		}
	}
}
