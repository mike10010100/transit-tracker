package otasig

import (
	"encoding/json"
	"os"
	"testing"
)

func FuzzVerifyManifest(f *testing.F) {
	relPub := PublicKeyOf(releaseKey())

	// Seed with valid manifest vector
	if data, err := os.ReadFile("testdata/vectors.json"); err == nil {
		var v vectors
		if json.Unmarshal(data, &v) == nil && v.Manifest.JSON != "" {
			f.Add([]byte(v.Manifest.JSON))
		}
	}
	f.Add([]byte(`{}`))
	f.Add([]byte(`{"format":"invalid"}`))
	f.Add([]byte(`{"format":"transit-tracker-ota-v1","version":"1.0.0","sha256":"0000000000000000000000000000000000000000000000000000000000000000","size":100,"signature":""}`))

	f.Fuzz(func(t *testing.T, data []byte) {
		m, err := VerifyManifest(relPub, data)
		if err == nil {
			if m.Format != ManifestFormat {
				t.Fatalf("VerifyManifest succeeded with invalid format: %s", m.Format)
			}
			if !ValidVersion(m.Version) {
				t.Fatalf("VerifyManifest succeeded with invalid version: %s", m.Version)
			}
			if !ValidSHA256(m.SHA256) {
				t.Fatalf("VerifyManifest succeeded with invalid sha256: %s", m.SHA256)
			}
			if m.Size <= 0 || m.Size > MaxBinarySize {
				t.Fatalf("VerifyManifest succeeded with invalid size: %d", m.Size)
			}
		}
	})
}

func FuzzVerifyCert(f *testing.F) {
	relPub := PublicKeyOf(releaseKey())

	if data, err := os.ReadFile("testdata/vectors.json"); err == nil {
		var v vectors
		if json.Unmarshal(data, &v) == nil && v.Cert.JSON != "" {
			f.Add([]byte(v.Cert.JSON))
		}
	}
	f.Add([]byte(`{}`))
	f.Add([]byte(`{"format":"transit-tracker-server-v1","public_key":"invalid","issued_at":123,"signature":""}`))

	f.Fuzz(func(t *testing.T, data []byte) {
		_, _, _ = VerifyCert(relPub, data)
	})
}

func FuzzParseSemver(f *testing.F) {
	seeds := []string{
		"0.0.0",
		"1.2.3",
		"10.20.30",
		"999.888.777",
		"v1.2.3",
		"1.2",
		"1.2.3.4",
		"-1.0.0",
		"abc",
		"",
		"18446744073709551615.18446744073709551615.18446744073709551615",
	}
	for _, s := range seeds {
		f.Add(s)
	}

	f.Fuzz(func(t *testing.T, s string) {
		v, err := ParseSemver(s)
		if err == nil {
			rendered := v.String()
			v2, err2 := ParseSemver(rendered)
			if err2 != nil {
				t.Fatalf("Failed to re-parse rendered version %q from input %q: %v", rendered, s, err2)
			}
			if v.Compare(v2) != 0 {
				t.Fatalf("Parsed version %v not equal to re-parsed %v from input %q", v, v2, s)
			}
		}
	})
}

func FuzzVerifyResponse(f *testing.F) {
	srvPub := PublicKeyOf(serverKey())

	f.Add("000102030405060708090a0b0c0d0e0f", "/dashboard.png", 200, "796120837694d3f3f29259cfeb25091698c2a0aa87873658d840b4993ee889b3", "\"abc\"", "IJclhxV9N4ZonPdHdy1yoMn5tbIcRlL6vx0qyYONslxvoRARn6JTQ5IbPnpEVi+fnIt+DIXGW0XZNN37qNXWDQ==")
	f.Add("", "", 0, "", "", "")

	f.Fuzz(func(t *testing.T, nonce, path string, status int, bodySHA256Hex, etag, sig string) {
		headers := MapHeaders{"etag": etag}
		_ = VerifyResponse(srvPub, nonce, path, status, bodySHA256Hex, headers, sig)
	})
}
