package main

import (
	"errors"
	"os"
	"regexp"
	"strings"
	"testing"
)

var uuidRegex = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func TestResolveClientID_KindleSerial(t *testing.T) {
	tests := []struct {
		name     string
		serial   string
		expected string
	}{
		{
			name:     "exact serial",
			serial:   "G000WM1234567890",
			expected: "G000WM1234567890",
		},
		{
			name:     "serial with surrounding whitespace",
			serial:   "  \tB006123456789012\r\n  ",
			expected: "B006123456789012",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			lipcCalled := false
			readCalled := false
			writeCalled := false

			got := ResolveClientID(
				func(prop, key string) string {
					lipcCalled = true
					if prop != "com.lab126.system" || key != "serialNumber" {
						t.Errorf("unexpected lipc call: %s %s", prop, key)
					}
					return tt.serial
				},
				func(path string) ([]byte, error) {
					readCalled = true
					return nil, os.ErrNotExist
				},
				func(path string, data []byte, perm os.FileMode) error {
					writeCalled = true
					return nil
				},
			)

			if !lipcCalled {
				t.Error("expected lipcGetter to be called")
			}
			if readCalled {
				t.Error("expected readFile NOT to be called when serial is present")
			}
			if writeCalled {
				t.Error("expected writeFile NOT to be called when serial is present")
			}
			if got != tt.expected {
				t.Errorf("got %q, want %q", got, tt.expected)
			}
		})
	}
}

func TestResolveClientID_FileRead(t *testing.T) {
	t.Run("primary file exists", func(t *testing.T) {
		writeCalled := false
		got := ResolveClientID(
			func(prop, key string) string { return "" },
			func(path string) ([]byte, error) {
				if path == ClientIDConfigFile {
					return []byte("  primary-uuid-1111\n"), nil
				}
				return nil, os.ErrNotExist
			},
			func(path string, data []byte, perm os.FileMode) error {
				writeCalled = true
				return nil
			},
		)

		if writeCalled {
			t.Error("expected writeFile NOT to be called when primary file exists")
		}
		if got != "primary-uuid-1111" {
			t.Errorf("got %q, want primary-uuid-1111", got)
		}
	})

	t.Run("fallback file exists when primary missing", func(t *testing.T) {
		writeCalled := false
		got := ResolveClientID(
			func(prop, key string) string { return "   " },
			func(path string) ([]byte, error) {
				if path == ClientIDConfigFile {
					return nil, os.ErrNotExist
				}
				if path == FallbackClientIDConfigFile {
					return []byte("fallback-uuid-2222\r\n"), nil
				}
				return nil, os.ErrNotExist
			},
			func(path string, data []byte, perm os.FileMode) error {
				writeCalled = true
				return nil
			},
		)

		if writeCalled {
			t.Error("expected writeFile NOT to be called when fallback file exists")
		}
		if got != "fallback-uuid-2222" {
			t.Errorf("got %q, want fallback-uuid-2222", got)
		}
	})

	t.Run("fallback file used when primary is empty whitespace", func(t *testing.T) {
		writeCalled := false
		got := ResolveClientID(
			func(prop, key string) string { return "" },
			func(path string) ([]byte, error) {
				if path == ClientIDConfigFile {
					return []byte("  \t\n  "), nil
				}
				if path == FallbackClientIDConfigFile {
					return []byte("fallback-uuid-3333"), nil
				}
				return nil, os.ErrNotExist
			},
			func(path string, data []byte, perm os.FileMode) error {
				writeCalled = true
				return nil
			},
		)

		if writeCalled {
			t.Error("expected writeFile NOT to be called when fallback file exists")
		}
		if got != "fallback-uuid-3333" {
			t.Errorf("got %q, want fallback-uuid-3333", got)
		}
	})

	t.Run("legacy unhidden file is read and migrated to hidden file", func(t *testing.T) {
		writeCalled := false
		var writtenPath string
		var writtenData []byte
		removeCalled := false
		var removedPath string

		origRemove := osRemove
		osRemove = func(name string) error {
			removeCalled = true
			removedPath = name
			return nil
		}
		defer func() { osRemove = origRemove }()

		got := ResolveClientID(
			func(prop, key string) string { return "" },
			func(path string) ([]byte, error) {
				if path == LegacyClientIDConfigFile {
					return []byte("legacy-uuid-4444\n"), nil
				}
				return nil, os.ErrNotExist
			},
			func(path string, data []byte, perm os.FileMode) error {
				writeCalled = true
				writtenPath = path
				writtenData = data
				return nil
			},
		)

		if got != "legacy-uuid-4444" {
			t.Errorf("got %q, want legacy-uuid-4444", got)
		}
		if !writeCalled || writtenPath != ClientIDConfigFile {
			t.Errorf("expected write to hidden file %q, got writeCalled=%v, writtenPath=%q", ClientIDConfigFile, writeCalled, writtenPath)
		}
		if string(writtenData) != "legacy-uuid-4444\n" {
			t.Errorf("writtenData = %q, want legacy-uuid-4444\n", string(writtenData))
		}
		if !removeCalled || removedPath != LegacyClientIDConfigFile {
			t.Errorf("expected remove of legacy file %q, got removeCalled=%v, removedPath=%q", LegacyClientIDConfigFile, removeCalled, removedPath)
		}
	})
}

func TestResolveClientID_GenerateAndWrite(t *testing.T) {
	t.Run("persists to primary when writable", func(t *testing.T) {
		var writtenPath string
		var writtenData []byte
		var writtenPerm os.FileMode

		got := ResolveClientID(
			func(prop, key string) string { return "" },
			func(path string) ([]byte, error) { return nil, os.ErrNotExist },
			func(path string, data []byte, perm os.FileMode) error {
				writtenPath = path
				writtenData = data
				writtenPerm = perm
				return nil
			},
		)

		if !uuidRegex.MatchString(got) {
			t.Errorf("generated ID %q does not match RFC 4122 v4 UUID format", got)
		}
		if writtenPath != ClientIDConfigFile {
			t.Errorf("writtenPath = %q, want %q", writtenPath, ClientIDConfigFile)
		}
		if writtenPerm != 0644 {
			t.Errorf("writtenPerm = %v, want 0644", writtenPerm)
		}
		if string(writtenData) != got+"\n" {
			t.Errorf("writtenData = %q, want %q", string(writtenData), got+"\n")
		}
	})

	t.Run("persists to fallback when primary write fails", func(t *testing.T) {
		var writeCalls []string

		got := ResolveClientID(
			func(prop, key string) string { return "" },
			func(path string) ([]byte, error) { return nil, os.ErrNotExist },
			func(path string, data []byte, perm os.FileMode) error {
				writeCalls = append(writeCalls, path)
				if path == ClientIDConfigFile {
					return os.ErrPermission
				}
				return nil
			},
		)

		if !uuidRegex.MatchString(got) {
			t.Errorf("generated ID %q does not match RFC 4122 v4 UUID format", got)
		}
		if len(writeCalls) != 2 {
			t.Fatalf("expected 2 write attempts, got %d (%v)", len(writeCalls), writeCalls)
		}
		if writeCalls[0] != ClientIDConfigFile {
			t.Errorf("first write attempt = %q, want %q", writeCalls[0], ClientIDConfigFile)
		}
		if writeCalls[1] != FallbackClientIDConfigFile {
			t.Errorf("second write attempt = %q, want %q", writeCalls[1], FallbackClientIDConfigFile)
		}
	})

	t.Run("succeeds returning UUID even if both writes fail", func(t *testing.T) {
		got := ResolveClientID(
			func(prop, key string) string { return "" },
			func(path string) ([]byte, error) { return nil, os.ErrNotExist },
			func(path string, data []byte, perm os.FileMode) error {
				return os.ErrPermission
			},
		)

		if !uuidRegex.MatchString(got) {
			t.Errorf("generated ID %q does not match RFC 4122 v4 UUID format", got)
		}
	})

	t.Run("succeeds with nil callbacks without panic", func(t *testing.T) {
		got := ResolveClientID(nil, nil, nil)
		if !uuidRegex.MatchString(got) {
			t.Errorf("generated ID %q does not match RFC 4122 v4 UUID format", got)
		}
	})
}

func TestGenerateUUID(t *testing.T) {
	id1 := generateUUID()
	id2 := generateUUID()

	if !uuidRegex.MatchString(id1) {
		t.Errorf("id1 %q does not match RFC 4122 v4 UUID format", id1)
	}
	if !uuidRegex.MatchString(id2) {
		t.Errorf("id2 %q does not match RFC 4122 v4 UUID format", id2)
	}
	if id1 == id2 {
		t.Errorf("two consecutive UUIDs should be unique, got %q == %q", id1, id2)
	}
}

type errReader struct{}

func (e *errReader) Read(p []byte) (n int, err error) {
	return 0, errors.New("simulated read error")
}

func TestGenerateUUID_ReaderErrorFallback(t *testing.T) {
	origReader := uuidReader
	uuidReader = &errReader{}
	defer func() { uuidReader = origReader }()

	got := generateUUID()
	if !strings.HasPrefix(got, "fallback-") {
		t.Errorf("expected fallback ID prefix, got %q", got)
	}
}

func TestGetClientID_Default(t *testing.T) {
	// Call default GetClientID to verify default seam wiring without panic
	id := GetClientID()
	if strings.TrimSpace(id) == "" {
		t.Errorf("expected non-empty client ID from GetClientID, got %q", id)
	}
}
