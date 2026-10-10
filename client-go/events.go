package main

import (
	"encoding/binary"
	"runtime"
)

// Linux input subsystem constants
const (
	EV_SYN    = 0x00
	EV_KEY    = 0x01
	EV_REL    = 0x02
	EV_ABS    = 0x03
	BTN_TOUCH = 0x14a // 330
	BTN_LEFT  = 0x110 // 272

	ABS_X              = 0x00
	ABS_Y              = 0x01
	ABS_PRESSURE       = 0x18
	ABS_MT_TOUCH_MAJOR = 0x30
	ABS_MT_POSITION_X  = 0x35
	ABS_MT_POSITION_Y  = 0x36
	ABS_MT_TRACKING_ID = 0x39

	KEY_POWER  = 116
	KEY_SLEEP  = 142
	KEY_WAKEUP = 143
)

// RawEventMsg encapsulates a parsed evdev input event
type RawEventMsg struct {
	Device  string
	EvType  uint16
	EvCode  uint16
	EvValue int32
}

// DetectEventStep determines whether the kernel is emitting 16-byte (32-bit ARM)
// or 24-byte (64-bit / y2038) input_event structures.
func DetectEventStep(buf []byte, n int) int {
	if runtime.GOARCH == "arm" || runtime.GOARCH == "386" || runtime.GOARCH == "mips" {
		return 16
	}
	if n > len(buf) {
		n = len(buf)
	}
	if n >= 24 && n%24 == 0 && len(buf) >= 18 && buf[16] <= 5 && buf[17] == 0 {
		return 24
	}
	if n >= 16 && n%16 == 0 && len(buf) >= 10 && buf[8] <= 5 && buf[9] == 0 {
		return 16
	}
	return 16
}

// ParseInputEvents parses raw byte buffer into a slice of RawEventMsg
func ParseInputEvents(buf []byte, n int, device string) []RawEventMsg {
	if n > len(buf) {
		n = len(buf)
	}
	if n < 16 {
		return nil
	}

	step := DetectEventStep(buf, n)
	var events []RawEventMsg

	for i := 0; i+step <= n; i += step {
		var evType, evCode uint16
		var evValue int32

		if step == 16 {
			evType = binary.LittleEndian.Uint16(buf[i+8 : i+10])
			evCode = binary.LittleEndian.Uint16(buf[i+10 : i+12])
			evValue = int32(binary.LittleEndian.Uint32(buf[i+12 : i+16]))
		} else {
			evType = binary.LittleEndian.Uint16(buf[i+16 : i+18])
			evCode = binary.LittleEndian.Uint16(buf[i+18 : i+20])
			evValue = int32(binary.LittleEndian.Uint32(buf[i+20 : i+24]))
		}

		events = append(events, RawEventMsg{
			Device:  device,
			EvType:  evType,
			EvCode:  evCode,
			EvValue: evValue,
		})
	}

	return events
}

// IsPowerKeyEvent returns true if the event corresponds to a power or sleep button press
func IsPowerKeyEvent(ev RawEventMsg) bool {
	if ev.EvType == EV_KEY && (ev.EvCode == KEY_POWER || ev.EvCode == KEY_SLEEP || ev.EvCode == KEY_WAKEUP) {
		return ev.EvValue == 1 // Key down
	}
	return false
}

// IsTouchEvent returns true if the event represents touch digitizer activity
func IsTouchEvent(ev RawEventMsg) bool {
	if ev.EvType == EV_ABS {
		return true
	}
	if ev.EvType == EV_KEY && (ev.EvCode == BTN_TOUCH || ev.EvCode == BTN_LEFT) {
		return true
	}
	return false
}

// IsExplicitTouchRelease returns true if an explicit finger lift event is detected
func IsExplicitTouchRelease(ev RawEventMsg) bool {
	if ev.EvType == EV_KEY && (ev.EvCode == BTN_TOUCH || ev.EvCode == BTN_LEFT) && ev.EvValue == 0 {
		return true
	}
	if ev.EvType == EV_ABS && ev.EvCode == ABS_MT_TRACKING_ID && ev.EvValue == -1 {
		return true
	}
	if ev.EvType == EV_ABS && (ev.EvCode == ABS_MT_TOUCH_MAJOR || ev.EvCode == ABS_PRESSURE) && ev.EvValue == 0 {
		return true
	}
	return false
}

// ExtractCoordinates updates coordinate state from an EV_ABS event
func ExtractCoordinates(ev RawEventMsg, curX, curY int32) (newX, newY int32, updated bool) {
	newX, newY = curX, curY
	if ev.EvType != EV_ABS {
		return newX, newY, false
	}

	if ev.EvCode == ABS_X || ev.EvCode == ABS_MT_POSITION_X {
		newX = ev.EvValue
		return newX, newY, true
	}
	if ev.EvCode == ABS_Y || ev.EvCode == ABS_MT_POSITION_Y {
		newY = ev.EvValue
		return newX, newY, true
	}

	return newX, newY, false
}
