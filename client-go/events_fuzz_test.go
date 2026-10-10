package main

import (
	"encoding/binary"
	"testing"
)

func FuzzParseInputEvents(f *testing.F) {
	// Seed with empty buffer
	f.Add([]byte{}, 0)

	// Seed with 16-byte event (32-bit ARM struct input_event: timeval 8 bytes, type 2, code 2, value 4)
	buf16 := make([]byte, 16)
	binary.LittleEndian.PutUint16(buf16[8:10], EV_KEY)
	binary.LittleEndian.PutUint16(buf16[10:12], KEY_POWER)
	binary.LittleEndian.PutUint32(buf16[12:16], 1)
	f.Add(buf16, 16)

	// Seed with 24-byte event (64-bit struct input_event: timeval 16 bytes, type 2, code 2, value 4)
	buf24 := make([]byte, 24)
	binary.LittleEndian.PutUint16(buf24[16:18], EV_ABS)
	binary.LittleEndian.PutUint16(buf24[18:20], ABS_X)
	binary.LittleEndian.PutUint32(buf24[20:24], 450)
	f.Add(buf24, 24)

	// Seed with multiple events back-to-back
	multi := append(append([]byte{}, buf16...), buf24...)
	f.Add(multi, len(multi))

	// Seed with mismatched/partial lengths
	f.Add([]byte{1, 2, 3, 4}, 10)
	f.Add([]byte{1, 2, 3, 4, 5, 6, 7, 8}, -1)
	f.Add(make([]byte, 32), 16)

	f.Fuzz(func(t *testing.T, data []byte, n int) {
		// Must never panic regardless of data or n
		events := ParseInputEvents(data, n, "/dev/input/event0")

		curX, curY := int32(0), int32(0)
		for _, ev := range events {
			_ = IsPowerKeyEvent(ev)
			_ = IsTouchEvent(ev)
			_ = IsExplicitTouchRelease(ev)
			curX, curY, _ = ExtractCoordinates(ev, curX, curY)
		}
	})
}
