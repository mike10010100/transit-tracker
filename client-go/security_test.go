package main

import (
	"errors"
	"net"
	"strings"
	"testing"
)

func TestAdoptableServerURL(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want bool
	}{
		{"Loopback IPv4 rejected", "http://127.0.0.1:8000", false},
		{"Unspecified IPv4 rejected", "http://0.0.0.0:8000", false},
		{"Private 192.168 accepted", "http://192.168.1.100:8000", true},
		{"Private 10.x accepted", "http://10.0.0.5:8000", true},
		{"Private 172.16 accepted", "http://172.16.4.5:8000", true},
		{"Public IP rejected", "http://8.8.8.8:8000", false},
		{"Non-http scheme rejected", "ftp://192.168.1.1", false},
		{"Empty rejected", "", false},
		{"Garbage rejected", "not a url", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := AdoptableServerURL(tt.raw); got != tt.want {
				t.Errorf("AdoptableServerURL(%q) = %v, want %v", tt.raw, got, tt.want)
			}
		})
	}
}

func TestAdoptableServerURL_HostnameResolution(t *testing.T) {
	origLookup := lookupIPWithTimeout
	defer func() { lookupIPWithTimeout = origLookup }()

	// Localhost resolves to loopback -> rejected
	lookupIPWithTimeout = func(host string) ([]net.IP, error) {
		return []net.IP{net.ParseIP("127.0.0.1")}, nil
	}
	if AdoptableServerURL("http://localhost:8000") {
		t.Error("expected localhost resolving to 127.0.0.1 to be rejected")
	}

	// Hostname resolving to all private -> accepted
	lookupIPWithTimeout = func(host string) ([]net.IP, error) {
		return []net.IP{net.ParseIP("192.168.1.50"), net.ParseIP("10.0.0.50")}, nil
	}
	if !AdoptableServerURL("http://transit.lan:8000") {
		t.Error("expected transit.lan resolving to all private to be accepted")
	}

	// Hostname resolving to mixed private + public -> rejected
	lookupIPWithTimeout = func(host string) ([]net.IP, error) {
		return []net.IP{net.ParseIP("192.168.1.50"), net.ParseIP("8.8.8.8")}, nil
	}
	if AdoptableServerURL("http://mixed.lan:8000") {
		t.Error("expected mixed resolving to private and public to be rejected")
	}

	// Lookup error -> rejected
	lookupIPWithTimeout = func(host string) ([]net.IP, error) {
		return nil, errors.New("lookup failed")
	}
	if AdoptableServerURL("http://bad.lan:8000") {
		t.Error("expected DNS error to be rejected")
	}
}

func TestNewTrackerClient_DefaultsEmptyViewToAuto(t *testing.T) {
	tc := NewTrackerClient("http://192.168.1.100:8000", "")
	if got := tc.getViewMode(); got != "auto" {
		t.Errorf("expected empty initial view to default to auto, got %q", got)
	}
	if tc.client == nil || tc.refreshCh == nil {
		t.Fatal("expected client and refresh channel to be initialized")
	}
}

func TestVerifySHA256(t *testing.T) {
	const digest = "61d247c404d23b5020960924f86d395b531aa3bfccee2b78cc2f39fab133cc79"

	tests := []struct {
		name     string
		actual   string
		expected string
		want     bool
	}{
		{"Exact match", digest, digest, true},
		{"Case-insensitive match", digest, strings.ToUpper(digest), true},
		{"Mismatch", digest, "deadbeef", false},
		{"Empty expected fails closed", digest, "", false},
		{"Empty actual fails", "", digest, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := VerifySHA256(tt.actual, tt.expected); got != tt.want {
				t.Errorf("VerifySHA256(%q, %q) = %v, want %v", tt.actual, tt.expected, got, tt.want)
			}
		})
	}
}

func TestIsPrivateIP(t *testing.T) {
	if IsPrivateIP(nil) {
		t.Error("nil must not be private")
	}
	if !IsPrivateIP(net.ParseIP("127.0.0.1")) {
		t.Error("loopback should be private")
	}
	if !IsPrivateIP(net.ParseIP("10.0.0.1")) {
		t.Error("10.0.0.1 should be private")
	}
	if !IsPrivateIP(net.ParseIP("192.168.1.1")) {
		t.Error("192.168.1.1 should be private")
	}
	if !IsPrivateIP(net.ParseIP("172.16.1.1")) {
		t.Error("172.16.1.1 should be private")
	}
	if !IsPrivateIP(net.ParseIP("169.254.1.1")) {
		t.Error("link local unicast should be private")
	}
	if IsPrivateIP(net.ParseIP("8.8.8.8")) {
		t.Error("8.8.8.8 must not be private")
	}
}

func TestIsAdoptableIP_EdgeCases(t *testing.T) {
	if IsAdoptableIP(nil) {
		t.Error("nil must not be adoptable")
	}
	if IsAdoptableIP(net.ParseIP("0.0.0.0")) {
		t.Error("0.0.0.0 must not be adoptable")
	}
	if IsAdoptableIP(net.ParseIP("224.0.0.1")) {
		t.Error("multicast must not be adoptable")
	}

	origAllow := allowLoopbackDiscovery
	allowLoopbackDiscovery = false
	if IsAdoptableIP(net.ParseIP("127.0.0.1")) {
		t.Error("loopback must not be adoptable when allowLoopbackDiscovery=false")
	}
	allowLoopbackDiscovery = true
	if !IsAdoptableIP(net.ParseIP("127.0.0.1")) {
		t.Error("loopback should be adoptable when allowLoopbackDiscovery=true")
	}
	allowLoopbackDiscovery = origAllow
}

func TestAdoptableServerURL_MoreEdgeCases(t *testing.T) {
	if AdoptableServerURL("http://") {
		t.Error("empty host should be rejected")
	}
	if AdoptableServerURL("http://[:::1") {
		t.Error("malformed URL should be rejected")
	}
	if !AdoptableServerURL("https://192.168.1.10:8000") {
		t.Error("https to private LAN should be accepted")
	}
}
