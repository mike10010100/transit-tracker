package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestClientLog_PostLogAndDiagnosticsHeader(t *testing.T) {
	patchRuntime(t)

	var mu sync.Mutex
	type receivedReq struct {
		path        string
		clientID    string
		clientVer   string
		firmware    string
		mode        string
		contentType string
		body        string
	}
	received := make([]receivedReq, 0)
	reqCh := make(chan struct{}, 10)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bodyBytes, _ := io.ReadAll(r.Body)
		mu.Lock()
		received = append(received, receivedReq{
			path:        r.URL.Path,
			clientID:    r.Header.Get("X-Tracker-Client-ID"),
			clientVer:   r.Header.Get("X-Tracker-Client-Version"),
			firmware:    r.Header.Get("X-Tracker-Firmware"),
			mode:        r.Header.Get("X-Tracker-Mode"),
			contentType: r.Header.Get("Content-Type"),
			body:        string(bodyBytes),
		})
		mu.Unlock()
		reqCh <- struct{}{}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	tc := NewTrackerClient(srv.URL, "auto")
	tc.setClientID("device-test-log-client-id")

	// 1. Test postLog
	tc.postLog(context.Background(), "test diagnostic message")
	select {
	case <-reqCh:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for postLog request")
	}

	mu.Lock()
	if len(received) != 1 {
		t.Fatalf("expected 1 request, got %d", len(received))
	}
	r0 := received[0]
	mu.Unlock()

	if r0.path != "/log" {
		t.Errorf("path = %q, want /log", r0.path)
	}
	if r0.clientID != "device-test-log-client-id" {
		t.Errorf("clientID = %q, want device-test-log-client-id", r0.clientID)
	}
	if r0.clientVer != Version {
		t.Errorf("clientVer = %q, want %q", r0.clientVer, Version)
	}
	if r0.mode == "" {
		t.Errorf("expected non-empty X-Tracker-Mode header")
	}
	if r0.contentType != "text/plain" {
		t.Errorf("contentType = %q, want text/plain", r0.contentType)
	}
	if r0.body != "test diagnostic message" {
		t.Errorf("body = %q, want 'test diagnostic message'", r0.body)
	}

	// 2. Test postDiagnostics (passive: active=false)
	tc.postDiagnostics(false)
	select {
	case <-reqCh:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for postDiagnostics(false) request")
	}

	mu.Lock()
	if len(received) != 2 {
		t.Fatalf("expected 2 requests, got %d", len(received))
	}
	r1 := received[1]
	mu.Unlock()

	if r1.path != "/diag" {
		t.Errorf("path = %q, want /diag", r1.path)
	}
	if r1.clientID != "device-test-log-client-id" {
		t.Errorf("clientID = %q, want device-test-log-client-id", r1.clientID)
	}
	if r1.contentType != "text/plain" {
		t.Errorf("contentType = %q, want text/plain", r1.contentType)
	}
	if !strings.Contains(r1.body, "=== DIAGNOSTICS") {
		t.Errorf("expected diagnostics header in body, got %q", r1.body)
	}

	// 3. Test postDiagnostics (active=true)
	tc.postDiagnostics(true)
	select {
	case <-reqCh:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for postDiagnostics(true) request")
	}

	mu.Lock()
	if len(received) != 3 {
		t.Fatalf("expected 3 requests, got %d", len(received))
	}
	r2 := received[2]
	mu.Unlock()

	if r2.path != "/diag" {
		t.Errorf("path = %q, want /diag", r2.path)
	}
	if r2.clientID != "device-test-log-client-id" {
		t.Errorf("clientID = %q, want device-test-log-client-id", r2.clientID)
	}
	if !strings.Contains(r2.body, "=== ACTIVE PROBE") {
		t.Errorf("expected active probe in body for active=true, got %q", r2.body)
	}
}

func TestClientLog_LogRemoteAndSender(t *testing.T) {
	patchRuntime(t)

	msgCh := make(chan string, 10)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		msgCh <- string(body)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	tc := NewTrackerClient(srv.URL, "auto")
	tc.startLogSender(ctx)

	tc.logRemote("hello log sender")

	select {
	case msg := <-msgCh:
		if msg != "hello log sender" {
			t.Errorf("got %q, want 'hello log sender'", msg)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for message through log sender")
	}

	cancel()
	tc.Wait()
}

func TestClientLog_QueueFullDrop(t *testing.T) {
	patchRuntime(t)

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	// Fill the log channel completely
	for i := 0; i < cap(tc.logCh); i++ {
		tc.logRemote("fill")
	}
	// This call must not block when full
	tc.logRemote("overflow message")
	if len(tc.logCh) != cap(tc.logCh) {
		t.Errorf("channel len = %d, want cap %d", len(tc.logCh), cap(tc.logCh))
	}
}

func TestClientLog_PostTextErrors(t *testing.T) {
	patchRuntime(t)

	// Test invalid URL (NewRequestWithContext error)
	tcBad := NewTrackerClient("http://[invalid-url", "auto")
	tcBad.postText(context.Background(), "/bad", "msg") // should not panic

	// Test connection error
	tcUnreachable := NewTrackerClient("http://127.0.0.1:1", "auto")
	tcUnreachable.postText(context.Background(), "/unreachable", "msg") // should not panic

	// Test handleNetworkError
	tcPoll := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tcPoll.handleNetworkError(context.Background())
	if tcPoll.consecutiveFailures != 1 {
		t.Errorf("expected consecutiveFailures=1, got %d", tcPoll.consecutiveFailures)
	}
}
