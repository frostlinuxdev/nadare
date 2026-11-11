// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Nadare contributors
//
// Portable (non-Windows-API) protocol logic of the CZN PCSDK handshake stub.
// Behaviour mirrors src/nadare/backends/czn/stub_src/czn_pcsdk_stub.c:
//
//	game -> 1000  {"value":{"publicKey":<b64 SPKI>,...}}
//	srv  -> 2000  value = b64(RSA-PKCS1(publicKey, {"aesKey":"<32 ASCII>"})), 72-col \n-wrapped
//	srv  -> 2001  value = HEXUPPER(AES-256-CBC(aesKey, iv=aesKey[:16], PKCS7(session)))
//	game -> 1001  heartbeat (no reply)
package main

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"strings"
)

// maxPacket is the largest frame total a u16 length can describe.
const maxPacket = 0xFFFF

// frame builds one wire packet: [u16 total LE][u16 id LE][body].
func frame(id uint16, body string) ([]byte, error) {
	total := len(body) + 4
	if total > maxPacket {
		return nil, errors.New("packet too large")
	}
	out := make([]byte, 0, total)
	out = append(out, byte(total), byte(total>>8), byte(id), byte(id>>8))
	return append(out, body...), nil
}

// parseHeader decodes a 4-byte frame header.
func parseHeader(h [4]byte) (total int, id uint16) {
	return int(h[0]) | int(h[1])<<8, uint16(h[2]) | uint16(h[3])<<8
}

// cstr truncates at the first NUL, as the C stub's strstr/strchr would see it.
func cstr(b []byte) []byte {
	if i := bytes.IndexByte(b, 0); i >= 0 {
		return b[:i]
	}
	return b
}

// findJSONString mirrors json_find_string(): locate "key", the next ':', skip
// blanks, require '"', and return the text up to the next '"'. It is
// deliberately minimal (no escapes), exactly like the C version.
func findJSONString(payload []byte, key string) (string, bool) {
	s := cstr(payload)
	pat := []byte(`"` + key + `"`)
	i := bytes.Index(s, pat)
	if i < 0 {
		return "", false
	}
	s = s[i+len(pat):]
	c := bytes.IndexByte(s, ':')
	if c < 0 {
		return "", false
	}
	s = s[c+1:]
	for len(s) > 0 && (s[0] == ' ' || s[0] == '\t') {
		s = s[1:]
	}
	if len(s) == 0 || s[0] != '"' {
		return "", false
	}
	s = s[1:]
	end := bytes.IndexByte(s, '"')
	if end < 0 {
		return "", false
	}
	return string(s[:end]), true
}

// jsonEscape escapes backslashes and double quotes only, like json_escape().
func jsonEscape(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' || s[i] == '"' {
			b.WriteByte('\\')
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// b64Decode is tolerant like CryptStringToBinaryA(CRYPT_STRING_BASE64): ASCII
// whitespace (including CR/LF) is ignored. Anything else must be valid base64.
func b64Decode(s string) ([]byte, error) {
	clean := strings.Map(func(r rune) rune {
		switch r {
		case ' ', '\t', '\r', '\n':
			return -1
		}
		return r
	}, s)
	return base64.StdEncoding.DecodeString(clean)
}

// wrap72 splits b64 into 72-char lines, each followed by a literal backslash+n
// (two characters), including a trailing one.
func wrap72(b64 string) string {
	var b strings.Builder
	for i := 0; i < len(b64); i += 72 {
		end := i + 72
		if end > len(b64) {
			end = len(b64)
		}
		b.WriteString(b64[i:end])
		b.WriteString(`\n`)
	}
	return b.String()
}

// newAESKey returns a 32-char lowercase-hex ASCII key from 16 random bytes. It
// never falls back to a fixed key.
func newAESKey() (string, error) {
	var r [16]byte
	if _, err := rand.Read(r[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(r[:]), nil
}

// value2000 is RSA-PKCS1v1.5({"aesKey":"<key>"}) under the game's base64 SPKI
// key, base64'd and 72-col wrapped (not yet JSON-escaped).
func value2000(pubB64, aesKey string) (string, error) {
	der, err := b64Decode(pubB64)
	if err != nil {
		return "", errors.New("pubkey b64 decode failed")
	}
	pubAny, err := x509.ParsePKIXPublicKey(der)
	if err != nil {
		return "", errors.New("SPKI decode failed")
	}
	pub, ok := pubAny.(*rsa.PublicKey)
	if !ok {
		return "", errors.New("not an RSA key")
	}
	ct, err := rsa.EncryptPKCS1v15(rand.Reader, pub, []byte(`{"aesKey":"`+aesKey+`"}`))
	if err != nil {
		return "", err
	}
	return wrap72(base64.StdEncoding.EncodeToString(ct)), nil
}

// value2001 is HEXUPPER(AES-256-CBC(aesKey, iv=aesKey[:16], PKCS7(session))).
func value2001(aesKey string, session []byte) (string, error) {
	if len(aesKey) < 32 {
		return "", errors.New("aeskey too short")
	}
	key := []byte(aesKey[:32])
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	pad := 16 - len(session)%16
	plain := make([]byte, 0, len(session)+pad)
	plain = append(plain, session...)
	plain = append(plain, bytes.Repeat([]byte{byte(pad)}, pad)...)
	defer wipe(plain) // a copy of the session, which carries the access token
	out := make([]byte, len(plain))
	cipher.NewCBCEncrypter(block, key[:16]).CryptBlocks(out, plain)
	return strings.ToUpper(hex.EncodeToString(out)), nil
}

// maxSession caps the session file: the real one is about 2 KB (#119).
const maxSession = 64 << 10

// errSessionTooBig is returned for a session file over maxSession.
var errSessionTooBig = errors.New("session file is too large")

// readCapped reads at most max bytes from r and refuses anything longer.
func readCapped(r io.Reader, max int) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, int64(max)+1))
	if err != nil {
		wipe(b)
		return nil, err
	}
	if len(b) > max {
		wipe(b)
		return nil, errSessionTooBig
	}
	return b, nil
}

// consumeSession reads the session file (capped) and then deletes it, so the tokens it
// holds stay on disk only until the stub has started (#811). The file is left in place
// when it cannot be read.
func consumeSession(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	b, err := readCapped(f, maxSession)
	f.Close()
	if err != nil {
		return nil, err
	}
	if err := os.Remove(path); err != nil {
		wipe(b)
		return nil, err
	}
	return b, nil
}

// wipe zeroes b; used on every copy of the session.
func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// reply wraps a value in the success envelope the game expects.
func reply(value string) string {
	return `{"code":0,"message":"Success","value":"` + value + `"}`
}
