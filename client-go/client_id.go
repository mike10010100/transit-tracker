package main

import (
	"crypto/rand"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	// ClientIDConfigFile is the persistent hidden path on Kindle storage for non-hardware serial client ID.
	ClientIDConfigFile = "/mnt/us/documents/.tracker_client_id.txt"
	// LegacyClientIDConfigFile is the legacy unhidden path from earlier releases.
	LegacyClientIDConfigFile = "/mnt/us/documents/tracker_client_id.txt"
	// SystemClientIDConfigFile is the persistent path in Kindle system folder.
	SystemClientIDConfigFile = "/mnt/us/system/tracker_client_id.txt"
	// FallbackClientIDConfigFile is the temporary path fallback for host development or testing.
	FallbackClientIDConfigFile = "/tmp/tracker_client_id.txt"
)

// uuidReader provides random bytes for UUID generation. Overridable in tests.
var uuidReader io.Reader = rand.Reader

// generateUUID creates an RFC 4122 v4 UUID using crypto/rand and fmt.Sprintf
// with zero external dependencies.
func generateUUID() string {
	var b [16]byte
	if _, err := io.ReadFull(uuidReader, b[:]); err != nil {
		return fmt.Sprintf("fallback-%016x", time.Now().UnixNano())
	}
	// Version 4: bits 4-7 of byte 6 set to 0100 (0x40)
	b[6] = (b[6] & 0x0f) | 0x40
	// Variant RFC 4122: bits 6-7 of byte 8 set to 10 (0x80)
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// ResolveClientID determines the unique client identifier using a prioritized strategy:
//  1. Checks hardware serial via lipcGetter("com.lab126.system", "serialNumber").
//     If non-empty, returns the trimmed serial.
//  2. Checks ClientIDConfigFile, LegacyClientIDConfigFile (migrating it), SystemClientIDConfigFile,
//     then FallbackClientIDConfigFile. If non-empty, returns the trimmed stored string.
//  3. Generates an RFC 4122 v4 UUID using crypto/rand and fmt.Sprintf.
//  4. Persists the UUID to ClientIDConfigFile (0644). If that fails (e.g. non-Kindle path),
//     persists to FallbackClientIDConfigFile.
//  5. Returns the UUID string.
func ResolveClientID(
	lipcGetter func(prop, key string) string,
	readFile func(string) ([]byte, error),
	writeFile func(string, []byte, os.FileMode) error,
) string {
	// 1. Check Kindle LIPC hardware serial
	if lipcGetter != nil {
		if serial := strings.TrimSpace(lipcGetter("com.lab126.system", "serialNumber")); serial != "" {
			return serial
		}
	}

	// 2. Try reading existing persistent ID files
	if readFile != nil {
		for _, p := range []string{ClientIDConfigFile, LegacyClientIDConfigFile, SystemClientIDConfigFile, FallbackClientIDConfigFile} {
			if data, err := readFile(p); err == nil {
				if id := strings.TrimSpace(string(data)); id != "" {
					if p == LegacyClientIDConfigFile && writeFile != nil {
						_ = osMkdirAll(filepath.Dir(ClientIDConfigFile), 0755)
						_ = writeFile(ClientIDConfigFile, []byte(id+"\n"), 0644)
						_ = osRemove(LegacyClientIDConfigFile)
					}
					return id
				}
			}
		}
	}

	// 3. Generate new RFC 4122 v4 UUID
	id := generateUUID()

	// 4. Persist to disk (primary first, then fallback)
	if writeFile != nil {
		_ = osMkdirAll(filepath.Dir(ClientIDConfigFile), 0755)
		if err := writeFile(ClientIDConfigFile, []byte(id+"\n"), 0644); err != nil {
			_ = osMkdirAll(filepath.Dir(FallbackClientIDConfigFile), 0755)
			_ = writeFile(FallbackClientIDConfigFile, []byte(id+"\n"), 0644)
		}
	}

	return id
}

// GetClientID obtains the client ID using production system calls.
// It is a package-level variable so tests can substitute a deterministic ID.
var GetClientID = func() string {
	return ResolveClientID(lipcGet, osReadFile, osWriteFile)
}
