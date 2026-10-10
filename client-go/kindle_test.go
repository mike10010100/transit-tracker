package main

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestLipcSet_Table(t *testing.T) {
	patchRuntime(t)

	tests := []struct {
		name     string
		prop     string
		key      string
		val      string
		wantArgs []string
	}{
		{
			name:     "powerd_screensaver_off",
			prop:     "com.lab126.powerd",
			key:      "preventScreenSaver",
			val:      "0",
			wantArgs: []string{"-i", "com.lab126.powerd", "preventScreenSaver", "0"},
		},
		{
			name:     "frontlight_intensity",
			prop:     "com.lab126.powerd",
			key:      "flIntensity",
			val:      "18",
			wantArgs: []string{"-i", "com.lab126.powerd", "flIntensity", "18"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var recordedCmd string
			var recordedArgs []string

			execCommandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
				recordedCmd = name
				recordedArgs = args
				return exec.CommandContext(ctx, "true")
			}

			lipcSet(tt.prop, tt.key, tt.val)

			if recordedCmd != "lipc-set-prop" {
				t.Errorf("got cmd %q, want lipc-set-prop", recordedCmd)
			}
			if len(recordedArgs) != len(tt.wantArgs) {
				t.Fatalf("got args %v, want %v", recordedArgs, tt.wantArgs)
			}
			for i := range tt.wantArgs {
				if recordedArgs[i] != tt.wantArgs[i] {
					t.Errorf("arg[%d] = %q, want %q", i, recordedArgs[i], tt.wantArgs[i])
				}
			}
		})
	}
}

func TestLipcSet_TimeoutTerminates(t *testing.T) {
	patchRuntime(t)

	origTimeout := lipcCallTimeout
	lipcCallTimeout = 20 * time.Millisecond
	defer func() { lipcCallTimeout = origTimeout }()

	execCommandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		return exec.CommandContext(ctx, "sleep", "2")
	}

	start := time.Now()
	lipcSet("com.lab126.powerd", "preventScreenSaver", "0")
	elapsed := time.Since(start)

	if elapsed > 1*time.Second {
		t.Errorf("lipcSet took %v, expected timeout around 20ms", elapsed)
	}
}

func TestLipcGet_Table(t *testing.T) {
	patchRuntime(t)

	tests := []struct {
		name       string
		mockOutput string
		mockErr    bool
		want       string
	}{
		{
			name:       "trimmed_value",
			mockOutput: "  18 \n",
			want:       "18",
		},
		{
			name:       "empty_value",
			mockOutput: "",
			want:       "",
		},
		{
			name:    "command_failure",
			mockErr: true,
			want:    "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			execCommandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
				if tt.mockErr {
					return exec.CommandContext(ctx, "false")
				}
				return exec.CommandContext(ctx, "echo", tt.mockOutput)
			}

			got := lipcGet("com.lab126.powerd", "flIntensity")
			if got != tt.want {
				t.Errorf("lipcGet() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestLipcGet_TimeoutTerminates(t *testing.T) {
	patchRuntime(t)

	origTimeout := lipcCallTimeout
	lipcCallTimeout = 20 * time.Millisecond
	defer func() { lipcCallTimeout = origTimeout }()

	execCommandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		return exec.CommandContext(ctx, "sleep", "2")
	}

	start := time.Now()
	got := lipcGet("com.lab126.powerd", "flIntensity")
	elapsed := time.Since(start)

	if got != "" {
		t.Errorf("expected empty string on timeout, got %q", got)
	}
	if elapsed > 1*time.Second {
		t.Errorf("lipcGet took %v, expected timeout around 20ms", elapsed)
	}
}

func TestPrepareDisplayForSleep(t *testing.T) {
	patchRuntime(t)

	var calls []string
	execCommandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		calls = append(calls, name+" "+strings.Join(args, " "))
		return exec.CommandContext(ctx, "true")
	}

	tc := NewTrackerClient("http://127.0.0.1:8000", "auto")
	tc.prepareDisplayForSleep(context.Background())

	if len(calls) != 3 {
		t.Fatalf("expected 3 calls, got %d: %v", len(calls), calls)
	}
	if calls[0] != "stop lab126_gui" {
		t.Errorf("call 0 = %q, want 'stop lab126_gui'", calls[0])
	}
	if calls[1] != "lipc-set-prop com.lab126.blanket unload screensaver" {
		t.Errorf("call 1 = %q", calls[1])
	}
	if calls[2] != "lipc-set-prop com.lab126.blanket unload splash" {
		t.Errorf("call 2 = %q", calls[2])
	}
}

func TestReadFirmwareVersion(t *testing.T) {
	tests := []struct {
		name     string
		files    map[string]string
		readFile func(string) ([]byte, error)
		want     string
	}{
		{
			name: "prettyversion_with_ota_hash",
			files: map[string]string{
				"/etc/prettyversion.txt": "Kindle 5.18.6 (~~otaVersion~~)\n",
			},
			want: "Kindle 5.18.6",
		},
		{
			name: "prettyversion_plain",
			files: map[string]string{
				"/etc/prettyversion.txt": "Kindle 5.14.2\n",
			},
			want: "Kindle 5.14.2",
		},
		{
			name: "fallback_to_etc_version",
			files: map[string]string{
				"/etc/version": "5.16.2.1\n",
			},
			want: "5.16.2.1",
		},
		{
			name: "empty_lines_and_whitespace",
			files: map[string]string{
				"/etc/prettyversion.txt": "\n\n   Kindle 5.17.0 (build-1234)   \n",
			},
			want: "Kindle 5.17.0",
		},
		{
			name: "truncates_overly_long_string",
			files: map[string]string{
				"/etc/version": strings.Repeat("A", 50),
			},
			want: strings.Repeat("A", 32),
		},
		{
			name:  "missing_files",
			files: map[string]string{},
			want:  "",
		},
		{
			name:     "nil_read_file",
			readFile: nil,
			want:     "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rf := tt.readFile
			if rf == nil && tt.files != nil {
				rf = func(path string) ([]byte, error) {
					if content, ok := tt.files[path]; ok {
						return []byte(content), nil
					}
					return nil, os.ErrNotExist
				}
			}
			got := ReadFirmwareVersion(rf)
			if got != tt.want {
				t.Errorf("ReadFirmwareVersion() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestGetFirmwareVersion_Caching(t *testing.T) {
	patchRuntime(t)
	resetFirmwareCache()
	defer resetFirmwareCache()

	readCount := 0
	osReadFile = func(path string) ([]byte, error) {
		readCount++
		if path == "/etc/prettyversion.txt" {
			return []byte("Kindle 5.18.6 (~~otaVersion~~)\n"), nil
		}
		return nil, os.ErrNotExist
	}

	v1 := GetFirmwareVersion()
	v2 := GetFirmwareVersion()

	if v1 != "Kindle 5.18.6" {
		t.Errorf("v1 = %q, want 'Kindle 5.18.6'", v1)
	}
	if v2 != "Kindle 5.18.6" {
		t.Errorf("v2 = %q, want 'Kindle 5.18.6'", v2)
	}
	if readCount != 1 {
		t.Errorf("expected exactly 1 file read due to caching, got %d", readCount)
	}
}
