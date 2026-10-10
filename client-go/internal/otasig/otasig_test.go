package otasig

import (
	"bytes"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net/http"
	"os"
	"strings"
	"testing"
)

// vectors mirrors testdata/vectors.json (shared with the Python server tests).
type vectors struct {
	Cert struct {
		Header    string `json:"header"`
		IssuedAt  int64  `json:"issued_at"`
		JSON      string `json:"json"`
		Message   string `json:"message"`
		Signature string `json:"signature"`
	} `json:"cert"`
	Manifest struct {
		JSON      string `json:"json"`
		Message   string `json:"message"`
		SHA256    string `json:"sha256"`
		Signature string `json:"signature"`
		Size      int64  `json:"size"`
		Version   string `json:"version"`
	} `json:"manifest"`
	ReleasePublicKey string `json:"release_public_key"`
	ReleaseSeedHex   string `json:"release_seed_hex"`
	Response         struct {
		Body          string            `json:"body"`
		Headers       map[string]string `json:"headers"`
		Message       string            `json:"message"`
		Nonce         string            `json:"nonce"`
		Path          string            `json:"path"`
		Signature     string            `json:"signature"`
		SignedHeaders []string          `json:"signed_headers"`
		Status        int               `json:"status"`
	} `json:"response"`
	ServerPublicKey string `json:"server_public_key"`
	ServerSeedHex   string `json:"server_seed_hex"`
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	data, err := os.ReadFile("testdata/vectors.json")
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var v vectors
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	return v
}

// seedKey builds a key from bytes [start, start+32).
func seedKey(start byte) ed25519.PrivateKey {
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = start + byte(i)
	}
	return ed25519.NewKeyFromSeed(seed)
}

func releaseKey() ed25519.PrivateKey { return seedKey(1) }
func serverKey() ed25519.PrivateKey  { return seedKey(33) }

func TestVectors_Seeds(t *testing.T) {
	v := loadVectors(t)
	if got := hex.EncodeToString(releaseKey().Seed()); got != v.ReleaseSeedHex {
		t.Errorf("release seed = %s, want %s", got, v.ReleaseSeedHex)
	}
	if got := hex.EncodeToString(serverKey().Seed()); got != v.ServerSeedHex {
		t.Errorf("server seed = %s, want %s", got, v.ServerSeedHex)
	}
	if got := EncodePublicKey(PublicKeyOf(releaseKey())); got != v.ReleasePublicKey {
		t.Errorf("release pub = %s, want %s", got, v.ReleasePublicKey)
	}
	if got := EncodePublicKey(PublicKeyOf(serverKey())); got != v.ServerPublicKey {
		t.Errorf("server pub = %s, want %s", got, v.ServerPublicKey)
	}
}

func TestVectors_Manifest(t *testing.T) {
	v := loadVectors(t)
	sha := SHA256Hex([]byte("hello"))
	if sha != v.Manifest.SHA256 {
		t.Fatalf("sha256(hello) = %s, want %s", sha, v.Manifest.SHA256)
	}
	if got := string(ManifestMessage("1.2.3", sha, 5)); got != v.Manifest.Message {
		t.Fatalf("message =\n%q\nwant\n%q", got, v.Manifest.Message)
	}
	m, err := SignManifest(releaseKey(), "1.2.3", sha, 5)
	if err != nil {
		t.Fatal(err)
	}
	if m.Signature != v.Manifest.Signature {
		t.Errorf("signature = %s, want %s", m.Signature, v.Manifest.Signature)
	}
	js, err := m.MarshalCompact()
	if err != nil {
		t.Fatal(err)
	}
	if string(js) != v.Manifest.JSON {
		t.Errorf("json =\n%s\nwant\n%s", js, v.Manifest.JSON)
	}
	pub, _ := ParsePublicKey(v.ReleasePublicKey)
	got, err := VerifyManifest(pub, []byte(v.Manifest.JSON))
	if err != nil {
		t.Fatalf("verify vector manifest: %v", err)
	}
	if got.Version != "1.2.3" || got.Size != 5 || got.SHA256 != sha {
		t.Errorf("unexpected parsed manifest %+v", got)
	}
}

func TestVectors_Cert(t *testing.T) {
	v := loadVectors(t)
	spub := PublicKeyOf(serverKey())
	if got := string(CertMessage(EncodePublicKey(spub), 1700000000)); got != v.Cert.Message {
		t.Fatalf("cert message = %q, want %q", got, v.Cert.Message)
	}
	c, err := SignCert(releaseKey(), spub, v.Cert.IssuedAt)
	if err != nil {
		t.Fatal(err)
	}
	if c.Signature != v.Cert.Signature {
		t.Errorf("cert sig = %s, want %s", c.Signature, v.Cert.Signature)
	}
	js, _ := c.MarshalCompact()
	if string(js) != v.Cert.JSON {
		t.Errorf("cert json = %s, want %s", js, v.Cert.JSON)
	}
	if got := base64.StdEncoding.EncodeToString(js); got != v.Cert.Header {
		t.Errorf("cert header = %s, want %s", got, v.Cert.Header)
	}
	rpub := PublicKeyOf(releaseKey())
	gotPub, err := VerifyCertHeader(rpub, v.Cert.Header)
	if err != nil {
		t.Fatalf("verify cert header: %v", err)
	}
	if !gotPub.Equal(spub) {
		t.Error("cert header yielded wrong server key")
	}
}

func TestVectors_Response(t *testing.T) {
	v := loadVectors(t)
	if strings.Join(v.Response.SignedHeaders, ",") != strings.Join(SignedHeaders, ",") {
		t.Fatalf("signed header list mismatch:\n%v\n%v", v.Response.SignedHeaders, SignedHeaders)
	}
	h := MapHeaders(v.Response.Headers)
	bodySHA := SHA256Hex([]byte(v.Response.Body))
	msg, err := ResponseMessage(v.Response.Nonce, v.Response.Path, v.Response.Status, bodySHA, h)
	if err != nil {
		t.Fatal(err)
	}
	if string(msg) != v.Response.Message {
		t.Fatalf("response message =\n%q\nwant\n%q", msg, v.Response.Message)
	}
	sig, err := SignResponse(serverKey(), v.Response.Nonce, v.Response.Path, v.Response.Status, bodySHA, h)
	if err != nil {
		t.Fatal(err)
	}
	if sig != v.Response.Signature {
		t.Errorf("response sig = %s, want %s", sig, v.Response.Signature)
	}

	// The same headers presented as an http.Header must verify identically.
	hh := http.Header{}
	for k, val := range v.Response.Headers {
		hh.Set(k, val)
	}
	spub, _ := ParsePublicKey(v.ServerPublicKey)
	if err := VerifyResponse(spub, v.Response.Nonce, v.Response.Path, v.Response.Status, bodySHA, hh, v.Response.Signature); err != nil {
		t.Fatalf("verify vector response: %v", err)
	}
}

func TestVerifyResponse_Tamper(t *testing.T) {
	v := loadVectors(t)
	spub := PublicKeyOf(serverKey())
	bodySHA := SHA256Hex([]byte(v.Response.Body))
	base := func() http.Header {
		hh := http.Header{}
		for k, val := range v.Response.Headers {
			hh.Set(k, val)
		}
		return hh
	}
	sig := v.Response.Signature
	n, p, s := v.Response.Nonce, v.Response.Path, v.Response.Status

	cases := []struct {
		name string
		fn   func() error
	}{
		{"nonce", func() error {
			return VerifyResponse(spub, "ffffffffffffffffffffffffffffffff", p, s, bodySHA, base(), sig)
		}},
		{"bad nonce format", func() error { return VerifyResponse(spub, "XYZ", p, s, bodySHA, base(), sig) }},
		{"path", func() error { return VerifyResponse(spub, n, "/identity", s, bodySHA, base(), sig) }},
		{"status", func() error { return VerifyResponse(spub, n, p, 205, bodySHA, base(), sig) }},
		{"body", func() error { return VerifyResponse(spub, n, p, s, SHA256Hex([]byte("PNG!")), base(), sig) }},
		{"added mode header", func() error {
			h := base()
			h.Set("X-Tracker-Mode", "sleep")
			return VerifyResponse(spub, n, p, s, bodySHA, h, sig)
		}},
		{"changed interval", func() error {
			h := base()
			h.Set("X-Kindle-Poll-Interval", "1")
			return VerifyResponse(spub, n, p, s, bodySHA, h, sig)
		}},
		{"removed etag", func() error {
			h := base()
			h.Del("Etag")
			return VerifyResponse(spub, n, p, s, bodySHA, h, sig)
		}},
		{"crlf header", func() error {
			h := base()
			h["X-Tracker-Action"] = []string{"x\r\nInjected: 1"}
			return VerifyResponse(spub, n, p, s, bodySHA, h, sig)
		}},
		{"wrong key", func() error {
			return VerifyResponse(PublicKeyOf(releaseKey()), n, p, s, bodySHA, base(), sig)
		}},
		{"bad sig base64", func() error { return VerifyResponse(spub, n, p, s, bodySHA, base(), "!!!") }},
		{"short sig", func() error {
			return VerifyResponse(spub, n, p, s, bodySHA, base(), base64.StdEncoding.EncodeToString([]byte("short")))
		}},
		{"short key", func() error { return VerifyResponse(spub[:5], n, p, s, bodySHA, base(), sig) }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if err := c.fn(); err == nil {
				t.Fatal("tampered response verified")
			}
		})
	}
}

func TestResponseMessage_NilHeadersAndCRLF(t *testing.T) {
	msg, err := ResponseMessage("000102030405060708090a0b0c0d0e0f", "/identity", 200, SHA256Hex(nil), nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(string(msg), "\nx-tracker-policy=") || strings.HasSuffix(string(msg), "\n") {
		t.Errorf("unexpected message tail: %q", msg)
	}
	if _, err := ResponseMessage("n", "/a\nb", 200, "", nil); !errors.Is(err, ErrHeader) {
		t.Errorf("CRLF in path must be rejected, got %v", err)
	}
}

func TestSignResponse_Errors(t *testing.T) {
	if _, err := SignResponse(serverKey()[:3], "000102030405060708090a0b0c0d0e0f", "/", 200, "", nil); !errors.Is(err, ErrKey) {
		t.Errorf("short key: %v", err)
	}
	if _, err := SignResponse(serverKey(), "nope", "/", 200, "", nil); !errors.Is(err, ErrNonce) {
		t.Errorf("bad nonce: %v", err)
	}
	h := MapHeaders{"etag": "a\nb"}
	if _, err := SignResponse(serverKey(), "000102030405060708090a0b0c0d0e0f", "/", 200, "", h); !errors.Is(err, ErrHeader) {
		t.Errorf("crlf header: %v", err)
	}
}

func TestVerifyManifest_Tamper(t *testing.T) {
	v := loadVectors(t)
	pub := PublicKeyOf(releaseKey())
	var good map[string]any
	_ = json.Unmarshal([]byte(v.Manifest.JSON), &good)

	mutate := func(k string, val any) []byte {
		m := map[string]any{}
		for kk, vv := range good {
			m[kk] = vv
		}
		if val == nil {
			delete(m, k)
		} else {
			m[k] = val
		}
		b, _ := json.Marshal(m)
		return b
	}
	cases := map[string][]byte{
		"version bump":      mutate("version", "9.9.9"),
		"sha swap":          mutate("sha256", strings.Repeat("a", 64)),
		"size":              mutate("size", 6),
		"format":            mutate("format", "other"),
		"no signature":      mutate("signature", nil),
		"bad version":       mutate("version", "1.2"),
		"uppercase sha":     mutate("sha256", strings.ToUpper(v.Manifest.SHA256)),
		"zero size":         mutate("size", 0),
		"huge size":         mutate("size", MaxBinarySize+1),
		"not json":          []byte("{"),
		"signature garbage": mutate("signature", "@@@@"),
	}
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := VerifyManifest(pub, data); err == nil {
				t.Fatal("tampered manifest verified")
			}
		})
	}
	// Key order must not matter: reorder keys and still verify.
	reordered := `{"signature":"` + v.Manifest.Signature + `","size":5,"sha256":"` + v.Manifest.SHA256 + `","version":"1.2.3","format":"transit-tracker-ota-v1"}`
	if _, err := VerifyManifest(pub, []byte(reordered)); err != nil {
		t.Errorf("reordered manifest should verify: %v", err)
	}
	// Wrong release key.
	if _, err := VerifyManifest(PublicKeyOf(serverKey()), []byte(v.Manifest.JSON)); !errors.Is(err, ErrSignature) {
		t.Errorf("wrong key: %v", err)
	}
}

func TestSignManifest_Validation(t *testing.T) {
	sha := SHA256Hex([]byte("x"))
	if _, err := SignManifest(releaseKey(), "v1.2.3", sha, 1); !errors.Is(err, ErrVersion) {
		t.Errorf("version: %v", err)
	}
	if _, err := SignManifest(releaseKey(), "1.2.3", "abc", 1); !errors.Is(err, ErrSHA256) {
		t.Errorf("sha: %v", err)
	}
	if _, err := SignManifest(releaseKey(), "1.2.3", sha, 0); !errors.Is(err, ErrSize) {
		t.Errorf("size: %v", err)
	}
	if _, err := SignManifest(releaseKey()[:10], "1.2.3", sha, 1); !errors.Is(err, ErrKey) {
		t.Errorf("key: %v", err)
	}
}

func TestVerifyCert_Tamper(t *testing.T) {
	v := loadVectors(t)
	rpub := PublicKeyOf(releaseKey())
	var good map[string]any
	_ = json.Unmarshal([]byte(v.Cert.JSON), &good)
	mutate := func(k string, val any) []byte {
		m := map[string]any{}
		for kk, vv := range good {
			m[kk] = vv
		}
		m[k] = val
		b, _ := json.Marshal(m)
		return b
	}
	cases := map[string][]byte{
		"issued_at":    mutate("issued_at", 1700000001),
		"format":       mutate("format", "transit-tracker-ota-v1"),
		"public key":   mutate("public_key", EncodePublicKey(PublicKeyOf(releaseKey()))),
		"bad key b64":  mutate("public_key", "***"),
		"short key":    mutate("public_key", base64.StdEncoding.EncodeToString([]byte("abc"))),
		"bad sig":      mutate("signature", "AAAA"),
		"not json":     []byte("nope"),
		"wrong issuer": nil,
	}
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			key := rpub
			if data == nil {
				data = []byte(v.Cert.JSON)
				key = PublicKeyOf(serverKey())
			}
			if _, _, err := VerifyCert(key, data); err == nil {
				t.Fatal("tampered cert verified")
			}
		})
	}
	if _, err := VerifyCertHeader(rpub, "not base64!!"); err == nil {
		t.Error("bad header base64 must fail")
	}
	if _, err := SignCert(releaseKey()[:4], PublicKeyOf(serverKey()), 1); !errors.Is(err, ErrKey) {
		t.Errorf("short release key: %v", err)
	}
	if _, err := SignCert(releaseKey(), PublicKeyOf(serverKey())[:4], 1); !errors.Is(err, ErrKey) {
		t.Errorf("short server key: %v", err)
	}
}

func TestPrivateKeyPEM_RoundTrip(t *testing.T) {
	priv := releaseKey()
	pemBytes, err := MarshalPrivateKeyPEM(priv)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.HasPrefix(pemBytes, []byte("-----BEGIN PRIVATE KEY-----")) {
		t.Errorf("unexpected PEM header: %q", pemBytes[:30])
	}
	got, err := ParsePrivateKeyPEM(pemBytes)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Equal(priv) {
		t.Error("PEM round trip changed the key")
	}
	if _, err := MarshalPrivateKeyPEM(priv[:5]); !errors.Is(err, ErrKey) {
		t.Errorf("short key marshal: %v", err)
	}
}

// opensslPEM is `openssl genpkey -algorithm ed25519` output (RFC 8410 test key).
const opensslPEM = `-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEINTuctv5E1hK1bbY8fdp+K06/nwoy/HU++CXqI9EdVhC
-----END PRIVATE KEY-----
`

func TestParsePrivateKeyPEM_OpenSSLAndErrors(t *testing.T) {
	priv, err := ParsePrivateKeyPEM([]byte(opensslPEM))
	if err != nil {
		t.Fatalf("openssl key: %v", err)
	}
	if got := EncodePublicKey(PublicKeyOf(priv)); got != "Gb9ECWmEzf6FQbrBZ9w7lshQhqowtrbLDFw4rXAxZuE=" {
		t.Errorf("RFC 8410 public key = %s", got)
	}
	if _, err := ParsePrivateKeyPEM([]byte("garbage")); !errors.Is(err, ErrKey) {
		t.Errorf("garbage: %v", err)
	}
	wrongType := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: []byte{1}})
	if _, err := ParsePrivateKeyPEM(wrongType); !errors.Is(err, ErrKey) {
		t.Errorf("wrong type: %v", err)
	}
	badDER := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: []byte{1, 2, 3}})
	if _, err := ParsePrivateKeyPEM(badDER); !errors.Is(err, ErrKey) {
		t.Errorf("bad der: %v", err)
	}
}

func TestParsePrivateKeyPEM_NonEd25519(t *testing.T) {
	// A PKCS#8 ECDSA P-256 key (generated once with openssl) must be refused.
	const ecPEM = `-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgs2NNx9Ih7mfHYEKw
MlHhFZFt6iQX/dzv8xxX5siqhb6hRANCAAR8iXySqDr0v3gHQfoVNmAKyrjtLP5S
F2wXUx6k0o3cQ0DPM4m5x9kTCJGdypjQQYj3qr179rJMAIfwBlqIMvn9
-----END PRIVATE KEY-----
`
	block, _ := pem.Decode([]byte(ecPEM))
	if _, err := x509.ParsePKCS8PrivateKey(block.Bytes); err != nil {
		t.Fatalf("fixture must be a valid PKCS#8 key: %v", err)
	}
	if _, err := ParsePrivateKeyPEM([]byte(ecPEM)); !errors.Is(err, ErrKey) {
		t.Errorf("ECDSA key must be rejected, got %v", err)
	}
}

func TestParsePublicKey(t *testing.T) {
	v := loadVectors(t)
	if _, err := ParsePublicKey(v.ReleasePublicKey); err != nil {
		t.Errorf("valid key: %v", err)
	}
	if _, err := ParsePublicKey(" " + v.ReleasePublicKey + "\n"); err != nil {
		t.Errorf("surrounding whitespace should be tolerated: %v", err)
	}
	for _, bad := range []string{"", "!!!", base64.StdEncoding.EncodeToString(make([]byte, 31))} {
		if _, err := ParsePublicKey(bad); !errors.Is(err, ErrKey) {
			t.Errorf("ParsePublicKey(%q) = %v, want ErrKey", bad, err)
		}
	}
}

func TestNonce(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 20; i++ {
		n, err := NewNonce()
		if err != nil {
			t.Fatal(err)
		}
		if !ValidNonce(n) {
			t.Fatalf("invalid nonce %q", n)
		}
		if seen[n] {
			t.Fatal("nonce repeated")
		}
		seen[n] = true
	}
	for _, bad := range []string{"", "000102030405060708090A0B0C0D0E0F", "00010203", strings.Repeat("0", 33), strings.Repeat("g", 32)} {
		if ValidNonce(bad) {
			t.Errorf("ValidNonce(%q) = true", bad)
		}
	}
}

func TestSemver(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.10", "1.2.9", 1},
		{"1.10.0", "1.9.99", 1},
		{"2.0.0", "10.0.0", -1},
		{"0.0.1", "0.0.0", 1},
	}
	for _, c := range cases {
		a, err := ParseSemver(c.a)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := ParseSemver(c.b)
		if got := a.Compare(b); got != c.want {
			t.Errorf("Compare(%s,%s) = %d, want %d", c.a, c.b, got, c.want)
		}
		if a.String() != c.a {
			t.Errorf("String() = %s, want %s", a.String(), c.a)
		}
	}
	for _, bad := range []string{"", "1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc1", " 1.2.3", "1.2.x", "99999999999999999999.0.0"} {
		if _, err := ParseSemver(bad); !errors.Is(err, ErrVersion) {
			t.Errorf("ParseSemver(%q) = %v, want ErrVersion", bad, err)
		}
	}
	if !IsNewer("1.36.0", "1.35.5") || IsNewer("1.35.5", "1.35.5") || IsNewer("1.0.0", "1.35.5") {
		t.Error("IsNewer ordering wrong")
	}
	if IsNewer("bogus", "1.0.0") || IsNewer("2.0.0", "dev") {
		t.Error("IsNewer must fail closed on unparsable input")
	}
}

func TestMapHeadersCaseInsensitive(t *testing.T) {
	h := MapHeaders{"etag": "x"}
	if h.Get("ETag") != "x" || h.Get("missing") != "" {
		t.Error("MapHeaders lookup broken")
	}
}

func TestMarshalPrivateKeyPEM_Errors(t *testing.T) {
	if _, err := MarshalPrivateKeyPEM([]byte("short")); !errors.Is(err, ErrKey) {
		t.Errorf("MarshalPrivateKeyPEM(short) = %v, want ErrKey", err)
	}
}
