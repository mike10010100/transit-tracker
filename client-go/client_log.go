package main

import (
	"context"
	"net/http"
	"strings"
)

// logRemote enqueues a diagnostic log for the single background sender.
// It never blocks the caller and never spawns a goroutine per call, so a burst
// of taps cannot open a burst of radio-waking connections. If the queue is
// full the message is dropped (diagnostics are best-effort).
func (tc *TrackerClient) logRemote(msg string) {
	select {
	case tc.logCh <- msg:
	default:
	}
}

// startLogSender launches the one goroutine that serializes log delivery.
func (tc *TrackerClient) startLogSender(ctx context.Context) {
	tc.logStarted.Do(func() {
		tc.wg.Add(1)
		go func() {
			defer tc.wg.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case msg := <-tc.logCh:
					tc.postLog(ctx, msg)
				}
			}
		}()
	})
}

// postLog performs a single synchronous diagnostic POST using the provided context.
func (tc *TrackerClient) postLog(ctx context.Context, msg string) {
	tc.postText(ctx, "/log", msg)
}

// postDiagnostics gathers a device report synchronously (so the probes run on
// the caller's goroutine and don't leak past the caller's lifetime) and uploads
// it to the server's /diag endpoint asynchronously. When active is true, it also
// runs the heavier on-demand capability probe (identity, crontab, RTC devices,
// boot hook) which is safe but shells out a little.
func (tc *TrackerClient) postDiagnostics(active bool) {
	report := GatherDiagnostics().Format()
	go func() {
		if active {
			report += "\n\n" + RunActiveProbe().Format()
		}
		tc.postText(context.Background(), "/diag", report)
	}()
}

func (tc *TrackerClient) postText(ctx context.Context, path, msg string) {
	server := tc.getServerURL()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, server+path, strings.NewReader(msg))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "text/plain")
	req.Header.Set("X-Tracker-Client-ID", tc.getClientID())
	req.Header.Set("X-Tracker-Client-Version", Version)
	if fw := GetFirmwareVersion(); fw != "" {
		req.Header.Set("X-Tracker-Firmware", fw)
	}
	req.Header.Set("X-Tracker-Mode", currentModeName())
	resp, err := tc.client.Do(req)
	if err == nil {
		resp.Body.Close()
	}
}

func (tc *TrackerClient) handleNetworkError(ctx context.Context) {
	tc.recordPollFailure(ctx)
}
