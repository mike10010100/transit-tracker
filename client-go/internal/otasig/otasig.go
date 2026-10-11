// Package otasig implements the transit-tracker release-signing protocol
// (security spec v1): the signed release manifest (§1), the server identity
// certificate (§2) and per-response authentication (§3).
//
// Every signed message is a set of UTF-8 lines joined by "\n" with no
// trailing newline. All base64 is standard base64 with padding and all hex is
// lowercase. The package is stdlib-only so it can be shared by the Kindle
// client and the otasign build tool.
package otasig

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Protocol format identifiers. They are the first line of each signed message
// and the "format" field of the JSON documents.
const (
	ManifestFormat   = "transit-tracker-ota-v1"
	CertFormat       = "transit-tracker-server-v1"
	ResponseFormatV1 = "transit-tracker-resp-v1"
	ResponseFormatV2 = "transit-tracker-resp-v2"
	ResponseFormat   = ResponseFormatV2
)

// Size bounds shared by the client and the build tooling.
const (
	// MaxBinarySize is the largest release binary a manifest may describe.
	MaxBinarySize = 32 << 20
	// MaxManifestSize bounds how many bytes of a manifest a client reads.
	MaxManifestSize = 4 << 10
)

// Header names used by per-response authentication.
const (
	NonceHeader = "X-Tracker-Nonce"
	CertHeader  = "X-Tracker-Cert"
	AuthHeader  = "X-Tracker-Auth"
)

// SignedHeadersV1 lists the response header names covered by v1 signatures.
var SignedHeadersV1 = []string{
	"etag",
	"x-kindle-poll-interval",
	"x-tracker-presentation",
	"x-tracker-mode",
	"x-tracker-action",
	"x-tracker-diag",
	"x-kindle-brightness",
	"x-kindle-warmth",
	"x-tracker-version",
	"x-tracker-sha256",
	"x-resolved-view",
	"x-tracker-view",
}

// SignedHeadersV2 lists the response header names covered by v2 signatures (§3).
var SignedHeadersV2 = []string{
	"etag",
	"x-kindle-poll-interval",
	"x-tracker-presentation",
	"x-tracker-mode",
	"x-tracker-action",
	"x-tracker-diag",
	"x-kindle-brightness",
	"x-kindle-warmth",
	"x-tracker-version",
	"x-tracker-sha256",
	"x-resolved-view",
	"x-tracker-view",
	"x-tracker-policy",
}

// SignedHeaders lists, in signing order, the lowercase response header names
// covered by a response signature (§3). Treat it as read-only.
var SignedHeaders = SignedHeadersV2

var (
	versionRe = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)
	sha256Re  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	nonceRe   = regexp.MustCompile(`^[0-9a-f]{32}$`)
)

// Sentinel errors. Wrapped errors carry more detail; use errors.Is.
var (
	ErrFormat    = errors.New("otasig: wrong or missing format")
	ErrVersion   = errors.New("otasig: invalid version (want N.N.N)")
	ErrSHA256    = errors.New("otasig: invalid sha256 (want 64 lowercase hex)")
	ErrSize      = errors.New("otasig: invalid size")
	ErrSignature = errors.New("otasig: signature verification failed")
	ErrKey       = errors.New("otasig: invalid key")
	ErrNonce     = errors.New("otasig: invalid nonce")
	ErrHeader    = errors.New("otasig: header value contains CR or LF")
)

// joinLines builds a signed message: lines joined by "\n", no trailing newline.
func joinLines(lines ...string) []byte {
	return []byte(strings.Join(lines, "\n"))
}

// SHA256Hex returns the lowercase hex SHA-256 of data.
func SHA256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// ValidVersion reports whether v matches ^[0-9]+\.[0-9]+\.[0-9]+$.
func ValidVersion(v string) bool { return versionRe.MatchString(v) }

// ValidSHA256 reports whether s is 64 lowercase hex characters.
func ValidSHA256(s string) bool { return sha256Re.MatchString(s) }

// ---------------------------------------------------------------------------
// Keys
// ---------------------------------------------------------------------------

// EncodePublicKey returns the standard base64 of a raw 32-byte public key.
func EncodePublicKey(pub ed25519.PublicKey) string {
	return base64.StdEncoding.EncodeToString(pub)
}

// ParsePublicKey decodes a standard-base64 raw 32-byte Ed25519 public key.
func ParsePublicKey(b64 string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(b64))
	if err != nil {
		return nil, fmt.Errorf("%w: public key base64: %v", ErrKey, err)
	}
	if len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("%w: public key is %d bytes, want %d", ErrKey, len(raw), ed25519.PublicKeySize)
	}
	return ed25519.PublicKey(raw), nil
}

// ParsePrivateKeyPEM parses an Ed25519 private key from a PKCS#8 PEM block
// ("-----BEGIN PRIVATE KEY-----"), as written by MarshalPrivateKeyPEM or by
// `openssl genpkey -algorithm ed25519`.
func ParsePrivateKeyPEM(data []byte) (ed25519.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, fmt.Errorf("%w: no PEM block found", ErrKey)
	}
	if block.Type != "PRIVATE KEY" {
		return nil, fmt.Errorf("%w: PEM type %q, want \"PRIVATE KEY\"", ErrKey, block.Type)
	}
	key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrKey, err)
	}
	priv, ok := key.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("%w: PKCS#8 key is %T, want Ed25519", ErrKey, key)
	}
	return priv, nil
}

// MarshalPrivateKeyPEM encodes an Ed25519 private key as PKCS#8 PEM.
func MarshalPrivateKeyPEM(priv ed25519.PrivateKey) ([]byte, error) {
	if len(priv) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("%w: private key is %d bytes", ErrKey, len(priv))
	}
	der, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrKey, err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

// PublicKeyOf returns the public half of priv.
func PublicKeyOf(priv ed25519.PrivateKey) ed25519.PublicKey {
	return priv.Public().(ed25519.PublicKey)
}

// sign returns the base64 Ed25519 signature of msg.
func sign(priv ed25519.PrivateKey, msg []byte) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(priv, msg))
}

// verify checks a base64 Ed25519 signature of msg against pub.
func verify(pub ed25519.PublicKey, msg []byte, sigB64 string) error {
	if len(pub) != ed25519.PublicKeySize {
		return fmt.Errorf("%w: public key is %d bytes", ErrKey, len(pub))
	}
	sig, err := base64.StdEncoding.Strict().DecodeString(sigB64)
	if err != nil {
		return fmt.Errorf("%w: signature base64: %v", ErrSignature, err)
	}
	if len(sig) != ed25519.SignatureSize {
		return fmt.Errorf("%w: signature is %d bytes", ErrSignature, len(sig))
	}
	if !ed25519.Verify(pub, msg, sig) {
		return ErrSignature
	}
	return nil
}

// ---------------------------------------------------------------------------
// §1 Release manifest
// ---------------------------------------------------------------------------

// Manifest is the signed release manifest. Field order matches the compact
// JSON key order mandated by the spec.
type Manifest struct {
	Format    string `json:"format"`
	Version   string `json:"version"`
	SHA256    string `json:"sha256"`
	Size      int64  `json:"size"`
	Signature string `json:"signature"`
}

// ManifestMessage builds the bytes signed for a release manifest.
func ManifestMessage(version, sha256Hex string, size int64) []byte {
	return joinLines(
		ManifestFormat,
		"version="+version,
		"sha256="+sha256Hex,
		"size="+strconv.FormatInt(size, 10),
	)
}

// Message rebuilds the signed message from the manifest's fields.
func (m Manifest) Message() []byte { return ManifestMessage(m.Version, m.SHA256, m.Size) }

// validateFields checks every manifest field except the signature.
func (m Manifest) validateFields() error {
	if m.Format != ManifestFormat {
		return fmt.Errorf("%w: %q", ErrFormat, m.Format)
	}
	if !ValidVersion(m.Version) {
		return fmt.Errorf("%w: %q", ErrVersion, m.Version)
	}
	if !ValidSHA256(m.SHA256) {
		return fmt.Errorf("%w: %q", ErrSHA256, m.SHA256)
	}
	if m.Size <= 0 || m.Size > MaxBinarySize {
		return fmt.Errorf("%w: %d", ErrSize, m.Size)
	}
	return nil
}

// SignManifest validates the inputs and returns a signed manifest.
func SignManifest(priv ed25519.PrivateKey, version, sha256Hex string, size int64) (Manifest, error) {
	m := Manifest{Format: ManifestFormat, Version: version, SHA256: sha256Hex, Size: size}
	if err := m.validateFields(); err != nil {
		return Manifest{}, err
	}
	if len(priv) != ed25519.PrivateKeySize {
		return Manifest{}, fmt.Errorf("%w: private key is %d bytes", ErrKey, len(priv))
	}
	m.Signature = sign(priv, m.Message())
	return m, nil
}

// MarshalCompact renders the manifest as compact JSON in spec key order.
func (m Manifest) MarshalCompact() ([]byte, error) { return json.Marshal(m) }

// VerifyManifest parses manifest JSON, validates every field and verifies
// the signature against the release public key. It never depends on JSON key
// order: the message is rebuilt from the parsed fields.
func VerifyManifest(releasePub ed25519.PublicKey, data []byte) (Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return Manifest{}, fmt.Errorf("otasig: manifest json: %w", err)
	}
	if err := m.validateFields(); err != nil {
		return Manifest{}, err
	}
	if err := verify(releasePub, m.Message(), m.Signature); err != nil {
		return Manifest{}, err
	}
	return m, nil
}

// ---------------------------------------------------------------------------
// §2 Server identity certificate
// ---------------------------------------------------------------------------

// Cert is a release-key-signed delegation to a per-build server key.
type Cert struct {
	Format    string `json:"format"`
	PublicKey string `json:"public_key"`
	IssuedAt  int64  `json:"issued_at"`
	Signature string `json:"signature"`
}

// CertMessage builds the bytes signed for a server identity certificate.
func CertMessage(publicKeyB64 string, issuedAt int64) []byte {
	return joinLines(
		CertFormat,
		"public_key="+publicKeyB64,
		"issued_at="+strconv.FormatInt(issuedAt, 10),
	)
}

// Message rebuilds the signed message from the certificate's fields.
func (c Cert) Message() []byte { return CertMessage(c.PublicKey, c.IssuedAt) }

// SignCert issues a certificate for serverPub signed by the release key.
func SignCert(releasePriv ed25519.PrivateKey, serverPub ed25519.PublicKey, issuedAt int64) (Cert, error) {
	if len(releasePriv) != ed25519.PrivateKeySize {
		return Cert{}, fmt.Errorf("%w: release key is %d bytes", ErrKey, len(releasePriv))
	}
	if len(serverPub) != ed25519.PublicKeySize {
		return Cert{}, fmt.Errorf("%w: server key is %d bytes", ErrKey, len(serverPub))
	}
	c := Cert{Format: CertFormat, PublicKey: EncodePublicKey(serverPub), IssuedAt: issuedAt}
	c.Signature = sign(releasePriv, c.Message())
	return c, nil
}

// MarshalCompact renders the certificate as compact JSON in spec key order.
func (c Cert) MarshalCompact() ([]byte, error) { return json.Marshal(c) }

// VerifyCert parses certificate JSON, checks its format, verifies it against
// the release public key and returns the delegated server public key.
func VerifyCert(releasePub ed25519.PublicKey, data []byte) (ed25519.PublicKey, Cert, error) {
	var c Cert
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, Cert{}, fmt.Errorf("otasig: cert json: %w", err)
	}
	if c.Format != CertFormat {
		return nil, Cert{}, fmt.Errorf("%w: %q", ErrFormat, c.Format)
	}
	serverPub, err := ParsePublicKey(c.PublicKey)
	if err != nil {
		return nil, Cert{}, err
	}
	if err := verify(releasePub, c.Message(), c.Signature); err != nil {
		return nil, Cert{}, err
	}
	return serverPub, c, nil
}

// VerifyCertHeader decodes an X-Tracker-Cert header value (base64 of the
// certificate JSON) and verifies it like VerifyCert.
func VerifyCertHeader(releasePub ed25519.PublicKey, header string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(header))
	if err != nil {
		return nil, fmt.Errorf("otasig: cert header base64: %w", err)
	}
	pub, _, err := VerifyCert(releasePub, raw)
	return pub, err
}

// ---------------------------------------------------------------------------
// §3 Per-response authentication
// ---------------------------------------------------------------------------

// HeaderGetter is satisfied by http.Header (Get canonicalises the name).
type HeaderGetter interface {
	Get(name string) string
}

// MapHeaders is a HeaderGetter over a map keyed by lowercase header name.
type MapHeaders map[string]string

// Get returns the value for name (case-insensitive), or "".
func (m MapHeaders) Get(name string) string { return m[strings.ToLower(name)] }

// NewNonce returns 16 bytes from crypto/rand as 32 lowercase hex characters.
func NewNonce() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

// ValidNonce reports whether s matches ^[0-9a-f]{32}$.
func ValidNonce(s string) bool { return nonceRe.MatchString(s) }

// ResponseMessageFor builds the bytes signed for an authenticated response for the given format.
// bodySHA256Hex is the hex SHA-256 of the exact body bytes (sha256("") when
// the body is empty). Header values containing CR or LF are rejected.
func ResponseMessageFor(format string, nonce, path string, status int, bodySHA256Hex string, headers HeaderGetter) ([]byte, error) {
	signedHdrs := SignedHeadersV2
	if format == ResponseFormatV1 {
		signedHdrs = SignedHeadersV1
	}
	lines := make([]string, 0, 5+len(signedHdrs))
	lines = append(lines,
		format,
		"nonce="+nonce,
		"path="+path,
		"status="+strconv.Itoa(status),
		"body-sha256="+bodySHA256Hex,
	)
	for _, name := range signedHdrs {
		v := ""
		if headers != nil {
			v = headers.Get(name)
		}
		if strings.ContainsAny(v, "\r\n") {
			return nil, fmt.Errorf("%w: %s", ErrHeader, name)
		}
		lines = append(lines, name+"="+v)
	}
	for _, s := range []string{nonce, path} {
		if strings.ContainsAny(s, "\r\n") {
			return nil, ErrHeader
		}
	}
	return joinLines(lines...), nil
}

// ResponseMessage builds the bytes signed for an authenticated response using the default v2 format.
func ResponseMessage(nonce, path string, status int, bodySHA256Hex string, headers HeaderGetter) ([]byte, error) {
	return ResponseMessageFor(ResponseFormatV2, nonce, path, status, bodySHA256Hex, headers)
}

// SignResponse returns the base64 X-Tracker-Auth value for a response.
func SignResponse(serverPriv ed25519.PrivateKey, nonce, path string, status int, bodySHA256Hex string, headers HeaderGetter) (string, error) {
	if len(serverPriv) != ed25519.PrivateKeySize {
		return "", fmt.Errorf("%w: server key is %d bytes", ErrKey, len(serverPriv))
	}
	if !ValidNonce(nonce) {
		return "", ErrNonce
	}
	msg, err := ResponseMessage(nonce, path, status, bodySHA256Hex, headers)
	if err != nil {
		return "", err
	}
	return sign(serverPriv, msg), nil
}

// VerifyResponse verifies an X-Tracker-Auth signature over a response.
// It verifies against the modern v2 format first, and gracefully falls back to v1
// for backward compatibility with older servers.
func VerifyResponse(serverPub ed25519.PublicKey, nonce, path string, status int, bodySHA256Hex string, headers HeaderGetter, sigB64 string) error {
	if !ValidNonce(nonce) {
		return ErrNonce
	}
	// Try v2 format first
	msgV2, err := ResponseMessageFor(ResponseFormatV2, nonce, path, status, bodySHA256Hex, headers)
	if err != nil {
		return err
	}
	if err := verify(serverPub, msgV2, sigB64); err == nil {
		return nil
	}
	// Fall back to v1 format for backward compatibility
	msgV1, err := ResponseMessageFor(ResponseFormatV1, nonce, path, status, bodySHA256Hex, headers)
	if err != nil {
		return err
	}
	return verify(serverPub, msgV1, sigB64)
}

// ---------------------------------------------------------------------------
// Strict semantic versions
// ---------------------------------------------------------------------------

// Semver is a strict three-component numeric version.
type Semver [3]uint64

// ParseSemver parses "MAJOR.MINOR.PATCH" (digits only, no prefix/suffix).
func ParseSemver(s string) (Semver, error) {
	if !ValidVersion(s) {
		return Semver{}, fmt.Errorf("%w: %q", ErrVersion, s)
	}
	var v Semver
	for i, part := range strings.Split(s, ".") {
		n, err := strconv.ParseUint(part, 10, 64)
		if err != nil {
			return Semver{}, fmt.Errorf("%w: %q: %v", ErrVersion, s, err)
		}
		v[i] = n
	}
	return v, nil
}

// Compare returns -1, 0 or +1 as v is less than, equal to or greater than o.
func (v Semver) Compare(o Semver) int {
	for i := range v {
		switch {
		case v[i] < o[i]:
			return -1
		case v[i] > o[i]:
			return 1
		}
	}
	return 0
}

// String renders the version as MAJOR.MINOR.PATCH.
func (v Semver) String() string {
	return fmt.Sprintf("%d.%d.%d", v[0], v[1], v[2])
}

// IsNewer reports whether candidate is strictly newer than current. Either
// string failing to parse yields false (fail closed).
func IsNewer(candidate, current string) bool {
	c, err := ParseSemver(candidate)
	if err != nil {
		return false
	}
	r, err := ParseSemver(current)
	if err != nil {
		return false
	}
	return c.Compare(r) > 0
}
