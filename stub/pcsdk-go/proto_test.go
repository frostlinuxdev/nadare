// SPDX-License-Identifier: GPL-3.0-or-later
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
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFrameRoundTrip(t *testing.T) {
	b, err := frame(2000, `{"x":1}`)
	if err != nil {
		t.Fatal(err)
	}
	var h [4]byte
	copy(h[:], b[:4])
	total, id := parseHeader(h)
	if total != len(b) || id != 2000 || string(b[4:]) != `{"x":1}` {
		t.Fatalf("got total=%d id=%d body=%q", total, id, b[4:])
	}
}

func TestFrameTooLarge(t *testing.T) {
	if _, err := frame(1, strings.Repeat("x", 0xFFFF)); err == nil {
		t.Fatal("expected an error for a 64 KiB body")
	}
	if _, err := frame(1, strings.Repeat("x", 0xFFFF-4)); err != nil {
		t.Fatalf("largest legal body refused: %v", err)
	}
}

func TestFindJSONString(t *testing.T) {
	cases := []struct {
		in, want string
		ok       bool
	}{
		{`{"value":{"publicKey":"AbC=","processId":5}}`, "AbC=", true},
		{"{\"publicKey\" \t: \t\"x\"}", "x", true},
		{`{"other":"y"}`, "", false},
		{`{"publicKey":12}`, "", false},
		{`{"publicKey":"unterminated`, "", false},
		{"{\"publicKey\":\"a\x00b\"}", "", false}, // NUL ends the C string before the closing quote
		{`{"publicKey"`, "", false},
	}
	for _, c := range cases {
		got, ok := findJSONString([]byte(c.in), "publicKey")
		if got != c.want || ok != c.ok {
			t.Errorf("%q: got (%q,%v) want (%q,%v)", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestJSONEscape(t *testing.T) {
	if got := jsonEscape(`a\nb"c`); got != `a\\nb\"c` {
		t.Fatalf("got %q", got)
	}
}

func TestWrap72(t *testing.T) {
	in := strings.Repeat("A", 150)
	got := wrap72(in)
	want := strings.Repeat("A", 72) + `\n` + strings.Repeat("A", 72) + `\n` + strings.Repeat("A", 6) + `\n`
	if got != want {
		t.Fatalf("got %q", got)
	}
	if wrap72("") != "" {
		t.Fatal("empty input must stay empty")
	}
}

func TestB64DecodeIsWhitespaceTolerant(t *testing.T) {
	raw := []byte("hello world, this is a test of base64")
	enc := base64.StdEncoding.EncodeToString(raw)
	spaced := enc[:10] + "\r\n" + enc[10:20] + " \t" + enc[20:]
	got, err := b64Decode(spaced)
	if err != nil || string(got) != string(raw) {
		t.Fatalf("got %q err %v", got, err)
	}
	if _, err := b64Decode("!!!not base64!!!"); err == nil {
		t.Fatal("expected an error")
	}
}

// The game's side: decrypt 2000 with the private key, then 2001 with the key it
// carried, and compare with the session. Same algorithm scripts/stub_check/verify.py uses.
func TestHandshakeRoundTrip(t *testing.T) {
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKIXPublicKey(&priv.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	session := `{"access_token":"tok-` + strings.Repeat("x", 40) + `","n":[0,1,2,3]}`

	key, err := newAESKey()
	if err != nil || len(key) != 32 {
		t.Fatalf("key %q err %v", key, err)
	}
	v2000, err := value2000(base64.StdEncoding.EncodeToString(der), key)
	if err != nil {
		t.Fatal(err)
	}
	// as it travels: JSON-escaped inside the envelope, then parsed by the game
	var env struct{ Value string }
	if err := json.Unmarshal([]byte(reply(jsonEscape(v2000))), &env); err != nil {
		t.Fatal(err)
	}
	ct, err := base64.StdEncoding.DecodeString(strings.ReplaceAll(env.Value, `\n`, ""))
	if err != nil {
		t.Fatal(err)
	}
	pt, err := rsa.DecryptPKCS1v15(nil, priv, ct)
	if err != nil {
		t.Fatal(err)
	}
	var got struct{ AesKey string }
	if err := json.Unmarshal(pt, &got); err != nil || got.AesKey != key {
		t.Fatalf("aesKey mismatch: %q vs %q (%v)", got.AesKey, key, err)
	}

	v2001, err := value2001(key, []byte(session))
	if err != nil {
		t.Fatal(err)
	}
	if v2001 != strings.ToUpper(v2001) {
		t.Fatal("2001 must be upper-case hex")
	}
	raw, err := hex.DecodeString(v2001)
	if err != nil || len(raw)%16 != 0 {
		t.Fatalf("bad hex: %v len=%d", err, len(raw))
	}
	block, _ := aes.NewCipher([]byte(key))
	out := make([]byte, len(raw))
	cipher.NewCBCDecrypter(block, []byte(key[:16])).CryptBlocks(out, raw)
	pad := int(out[len(out)-1])
	if pad < 1 || pad > 16 || string(out[:len(out)-pad]) != session {
		t.Fatalf("session did not round-trip: %q", out)
	}
}

func TestValue2000RejectsJunkKeys(t *testing.T) {
	for _, k := range []string{"", "not base64!!", base64.StdEncoding.EncodeToString([]byte("not DER"))} {
		if _, err := value2000(k, strings.Repeat("a", 32)); err == nil {
			t.Errorf("accepted junk public key %q", k)
		}
	}
}

func TestValue2001RejectsShortKey(t *testing.T) {
	if _, err := value2001("short", []byte("{}")); err == nil {
		t.Fatal("expected an error")
	}
}

func TestReadCappedAcceptsUpToTheCapAndRefusesMore(t *testing.T) {
	ok := bytes.Repeat([]byte("a"), maxSession)
	got, err := readCapped(bytes.NewReader(ok), maxSession)
	if err != nil || len(got) != maxSession {
		t.Fatalf("at the cap: len=%d err=%v", len(got), err)
	}
	if _, err := readCapped(bytes.NewReader(append(ok, 'b')), maxSession); err != errSessionTooBig {
		t.Fatalf("over the cap: err=%v", err)
	}
}

func TestValue2001LeavesTheCallersSessionIntact(t *testing.T) {
	session := []byte(`{"access_token":"x"}`)
	before := string(session)
	if _, err := value2001(strings.Repeat("k", 32), session); err != nil {
		t.Fatal(err)
	}
	if string(session) != before {
		t.Fatalf("session changed: %q", session)
	}
}

func TestConsumeSessionReadsThenDeletesTheFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(path, []byte(`{"access_token":"x"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := consumeSession(path)
	if err != nil || string(got) != `{"access_token":"x"}` {
		t.Fatalf("got %q err=%v", got, err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("file still there: %v", err)
	}
}

func TestConsumeSessionLeavesAnOversizedFileAlone(t *testing.T) {
	path := filepath.Join(t.TempDir(), "session.json")
	if err := os.WriteFile(path, bytes.Repeat([]byte("a"), maxSession+1), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := consumeSession(path); err != errSessionTooBig {
		t.Fatalf("err=%v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("file was removed: %v", err)
	}
}

func TestConsumeSessionWithoutAFile(t *testing.T) {
	if _, err := consumeSession(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("expected an error")
	}
}
