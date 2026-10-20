/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 czn-linux contributors
 */
/*
 * czn_pcsdk_stub.c — STOVE PCSDK launcher-handshake server.
 *
 * The game's BaseSDK.dll is a named-pipe CLIENT and refuses to start unless a
 * launcher answers on \\.\pipe\15BADE76-0E87-422B-A75B-B2049FE5F5F5\STOVE_CHAOSZERO.
 * This program is that server, so the game runs without STOVE:
 *
 *   game -> 1000  {"value":{"publicKey":<b64 SPKI>,"processId":...}}
 *   srv  -> 2000  value = b64(RSA-PKCS1(publicKey, {"aesKey":"<32 ASCII>"}))
 *   srv  -> 2001  value = HEXUPPER(AES-256-CBC(aesKey, iv=aesKey[:16], session))
 *   game -> 1001  heartbeat (no reply)
 *
 * The session (launcher data, incl. access_token) is read from a JSON file
 * written by czn_auth.py. The anti-cheat is not touched.
 * Protocol details: docs/czn-pcsdk-ipc.md.
 *
 * Build:
 *   x86_64-w64-mingw32-gcc -O2 -mwindows -o czn_pcsdk_stub.exe czn_pcsdk_stub.c -lbcrypt -lcrypt32
 *
 * Release builds: scripts/build_stub.sh (reproducible, sets STUB_VERSION).
 *
 * Usage (inside the game's Wine prefix):
 *   wine czn_pcsdk_stub.exe <session.json> --launch <game.exe> <cwd> [args...]
 *   wine czn_pcsdk_stub.exe --version
 */

#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <wincrypt.h>
#include <tlhelp32.h>
#include <bcrypt.h>
#include <ntstatus.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

/* link: -lbcrypt -lcrypt32  (see build line in the header) */

#define MAX_FRAME (1024 * 1024)

#ifndef STUB_VERSION
#define STUB_VERSION "dev"
#endif

static const char *g_session_path = "session.json";
/* Log file from $CZN_STUB_LOG (a Windows path), truncated each run; stdout only if unset. */
static FILE *g_log;

/* Optional ``--launch`` mode: run the game as our own child so it shares this
 * single Wine session (and therefore this wineserver / this named pipe). */
static wchar_t g_launch_exe[1024];
static wchar_t g_launch_cwd[1024];
static wchar_t g_launch_cmd[4096];
static int g_launch = 0;
static int g_launched = 0;
static DWORD g_game_pid = 0;
/* What the game did with us, for the end-of-run verdict (see main). */
static volatile LONG g_connected = 0;   /* opened the pipe */
static volatile LONG g_accepted = 0;    /* sent a heartbeat, i.e. took our 2000/2001 */

static void logf_(const char *fmt, ...) {
    va_list ap;
    char buf[4096];
    SYSTEMTIME st;
    GetLocalTime(&st);
    int n = snprintf(buf, sizeof buf, "%04d-%02d-%02d %02d:%02d:%02d.%03d ",
                     st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute,
                     st.wSecond, st.wMilliseconds);
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof buf - n, fmt, ap);
    va_end(ap);
    fputs(buf, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    if (g_log) { fputs(buf, g_log); fputc('\n', g_log); fflush(g_log); }
}

/* Send one framed packet: [u16 total][u16 id][utf8 json]. */
static BOOL send_packet(HANDLE h, uint16_t id, const char *json) {
    size_t len = strlen(json);
    if (len + 4 > 0xFFFF) return FALSE;
    size_t total = len + 4;
    unsigned char *buf = malloc(total);
    if (!buf) return FALSE;
    buf[0] = (unsigned char)(total & 0xFF);
    buf[1] = (unsigned char)((total >> 8) & 0xFF);
    buf[2] = (unsigned char)(id & 0xFF);
    buf[3] = (unsigned char)((id >> 8) & 0xFF);
    memcpy(buf + 4, json, len);
    DWORD wrote = 0;
    BOOL ok = WriteFile(h, buf, (DWORD)total, &wrote, NULL) && wrote == total;
    free(buf);
    logf_(">> packet %u (%zu bytes)", id, total);
    return ok;
}

static BOOL read_exact(HANDLE h, void *dst, DWORD n) {
    DWORD got = 0, total = 0;
    while (total < n) {
        if (!ReadFile(h, (char *)dst + total, n - total, &got, NULL)) return FALSE;
        if (got == 0) return FALSE;
        total += got;
    }
    return TRUE;
}

/* Read whole file into a malloc'd NUL-terminated string. */
static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n < 0) { fclose(f); return NULL; }
    char *buf = malloc((size_t)n + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t r = fread(buf, 1, (size_t)n, f);
    buf[r] = 0;
    fclose(f);
    return buf;
}

static char *launcher_data(void) {
    char *s = read_file(g_session_path);
    if (s) logf_("using launcher data from %s", g_session_path);
    else   logf_("cannot read %s; run czn_auth.py token first", g_session_path);
    return s;
}

/* ---- crypto helpers (Win32 CNG + crypt32) for the real handshake ----
 *
 * Handshake crypto, matching the STOVE launcher:
 *   2000.value = base64( RSA_PKCS1v1.5(game_pubkey, {"aesKey":"<32 ASCII>"}) ),
 *                wrapped to 72-char lines joined by a literal backslash-n.
 *   2001.value = HEXUPPER( AES-256-CBC(key=<32 ASCII bytes>, iv=key[:16],
 *                                       PKCS7(required_info_json)) )
 * The aesKey is a 32-char ASCII string used verbatim as the 32-byte AES key. */

/* base64 (no CRLF). Returns malloc'd NUL-terminated string, or NULL. */
static char *b64_encode(const unsigned char *data, DWORD len) {
    DWORD n = 0;
    if (!CryptBinaryToStringA(data, len, CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF,
                              NULL, &n)) return NULL;
    char *out = malloc(n + 1);
    if (!out) return NULL;
    if (!CryptBinaryToStringA(data, len, CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF,
                              out, &n)) { free(out); return NULL; }
    out[n] = 0;
    return out;
}

/* base64 decode. Returns malloc'd buffer, sets *outlen, or NULL. */
static unsigned char *b64_decode(const char *s, DWORD *outlen) {
    DWORD n = 0;
    if (!CryptStringToBinaryA(s, 0, CRYPT_STRING_BASE64, NULL, &n, NULL, NULL))
        return NULL;
    unsigned char *out = malloc(n ? n : 1);
    if (!out) return NULL;
    if (!CryptStringToBinaryA(s, 0, CRYPT_STRING_BASE64, out, &n, NULL, NULL)) {
        free(out); return NULL;
    }
    *outlen = n;
    return out;
}

/* uppercase hex of a buffer. malloc'd NUL-terminated, or NULL. */
static char *hex_upper(const unsigned char *data, DWORD len) {
    char *out = malloc((size_t)len * 2 + 1);
    if (!out) return NULL;
    static const char t[] = "0123456789ABCDEF";
    for (DWORD i = 0; i < len; ++i) {
        out[i * 2]     = t[data[i] >> 4];
        out[i * 2 + 1] = t[data[i] & 0xF];
    }
    out[len * 2] = 0;
    return out;
}

/* RSA PKCS#1 v1.5 encrypt `data` with an X.509 SubjectPublicKeyInfo DER key.
 * Returns malloc'd ciphertext, sets *outlen, or NULL. */
static unsigned char *rsa_pkcs1_encrypt(const unsigned char *der, DWORD derlen,
                                        const unsigned char *data, DWORD datalen,
                                        DWORD *outlen) {
    CERT_PUBLIC_KEY_INFO *info = NULL;
    DWORD cb = 0;
    BCRYPT_KEY_HANDLE hKey = NULL;
    unsigned char *out = NULL;

    if (!CryptDecodeObjectEx(X509_ASN_ENCODING, X509_PUBLIC_KEY_INFO,
                             der, derlen, CRYPT_DECODE_ALLOC_FLAG, NULL,
                             &info, &cb)) {
        logf_("rsa: CryptDecodeObjectEx(SPKI) failed: 0x%lx", GetLastError());
        return NULL;
    }
    if (!CryptImportPublicKeyInfoEx2(X509_ASN_ENCODING, info, 0, NULL, &hKey)) {
        logf_("rsa: CryptImportPublicKeyInfoEx2 failed: 0x%lx", GetLastError());
        LocalFree(info);
        return NULL;
    }
    DWORD need = 0;
    NTSTATUS st = BCryptEncrypt(hKey, (PUCHAR)data, datalen, NULL, NULL, 0,
                                NULL, 0, &need, BCRYPT_PAD_PKCS1);
    if (st == 0 && need) {
        out = malloc(need);
        if (out) {
            st = BCryptEncrypt(hKey, (PUCHAR)data, datalen, NULL, NULL, 0,
                               out, need, &need, BCRYPT_PAD_PKCS1);
            if (st == 0) *outlen = need;
            else { logf_("rsa: BCryptEncrypt failed: 0x%lx", (long)st);
                   free(out); out = NULL; }
        }
    } else {
        logf_("rsa: BCryptEncrypt sizing failed: 0x%lx", (long)st);
    }
    BCryptDestroyKey(hKey);
    LocalFree(info);
    return out;
}

/* AES-256-CBC encrypt pre-PKCS7-padded `data` (len multiple of 16).
 * Returns malloc'd ciphertext, sets *outlen, or NULL. */
static unsigned char *aes256cbc_encrypt(const unsigned char key[32],
                                        const unsigned char iv[16],
                                        const unsigned char *data, DWORD datalen,
                                        DWORD *outlen) {
    BCRYPT_ALG_HANDLE hAlg = NULL;
    BCRYPT_KEY_HANDLE hKey = NULL;
    unsigned char *out = NULL;
    unsigned char ivbuf[16];
    memcpy(ivbuf, iv, 16);

    if (BCryptOpenAlgorithmProvider(&hAlg, BCRYPT_AES_ALGORITHM, NULL, 0) != 0)
        return NULL;
    if (BCryptSetProperty(hAlg, BCRYPT_CHAINING_MODE,
                          (PUCHAR)BCRYPT_CHAIN_MODE_CBC,
                          sizeof(BCRYPT_CHAIN_MODE_CBC), 0) != 0)
        goto done;
    if (BCryptGenerateSymmetricKey(hAlg, &hKey, NULL, 0,
                                   (PUCHAR)key, 32, 0) != 0)
        goto done;
    DWORD need = 0;
    if (BCryptEncrypt(hKey, (PUCHAR)data, datalen, NULL, ivbuf, 16,
                      NULL, 0, &need, 0) != 0)
        goto done;
    out = malloc(need);
    if (!out) goto done;
    if (BCryptEncrypt(hKey, (PUCHAR)data, datalen, NULL, ivbuf, 16,
                      out, need, &need, 0) != 0) { free(out); out = NULL; }
    else *outlen = need;
done:
    if (hKey) BCryptDestroyKey(hKey);
    if (hAlg) BCryptCloseAlgorithmProvider(hAlg, 0);
    return out;
}

/* Extract a JSON string value: find "key":"<value>" and copy <value>.
 * Minimal (value must not contain an escaped quote). malloc'd, or NULL. */
static char *json_find_string(const char *json, const char *key) {
    char pat[64];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return NULL;
    p = strchr(p + strlen(pat), ':');
    if (!p) return NULL;
    ++p;
    while (*p == ' ' || *p == '\t') ++p;
    if (*p != '"') return NULL;
    ++p;
    const char *end = strchr(p, '"');
    if (!end) return NULL;
    size_t n = (size_t)(end - p);
    char *out = malloc(n + 1);
    if (!out) return NULL;
    memcpy(out, p, n);
    out[n] = 0;
    return out;
}

/* Build packet 2000's value: RSA-encrypt {"aesKey":"<aeskey>"} with the game's
 * base64-DER public key, base64 the ciphertext, wrap to 72-char lines joined by
 * a literal backslash-n (and a trailing one). Returns malloc'd string, or NULL. */
static char *build_2000_value(const char *game_pubkey_b64, const char *aeskey) {
    DWORD derlen = 0;
    unsigned char *der = b64_decode(game_pubkey_b64, &derlen);
    if (!der) { logf_("2000: pubkey b64 decode failed"); return NULL; }

    char aes_json[64];
    int aj = snprintf(aes_json, sizeof aes_json, "{\"aesKey\":\"%s\"}", aeskey);

    DWORD clen = 0;
    unsigned char *cipher = rsa_pkcs1_encrypt(der, derlen,
                                              (unsigned char *)aes_json,
                                              (DWORD)aj, &clen);
    free(der);
    if (!cipher) return NULL;

    char *b64 = b64_encode(cipher, clen);
    free(cipher);
    if (!b64) return NULL;

    /* wrap: 72 chars, then a literal backslash-n (two chars), repeat; trailing. */
    size_t blen = strlen(b64);
    size_t lines = blen / 72 + 1;
    char *wrapped = malloc(blen + (lines + 1) * 2 + 1);
    if (!wrapped) { free(b64); return NULL; }
    size_t o = 0;
    for (size_t i = 0; i < blen; i += 72) {
        size_t chunk = blen - i < 72 ? blen - i : 72;
        memcpy(wrapped + o, b64 + i, chunk); o += chunk;
        wrapped[o++] = '\\'; wrapped[o++] = 'n';
    }
    wrapped[o] = 0;
    free(b64);
    return wrapped; /* contains literal backslash+n; JSON-escape when embedding */
}

/* Build packet 2001's value: AES-256-CBC(aeskey) of PKCS7(required_info), upper
 * hex. `aeskey` is the 32-char ASCII string (= the 32 key bytes, iv=first 16). */
static char *build_2001_value(const char *aeskey, const char *required_info) {
    size_t klen = strlen(aeskey);
    if (klen < 32) { logf_("2001: aeskey too short"); return NULL; }
    size_t plen = strlen(required_info);
    size_t pad = 16 - (plen % 16);
    size_t total = plen + pad;
    unsigned char *buf = malloc(total);
    if (!buf) return NULL;
    memcpy(buf, required_info, plen);
    memset(buf + plen, (int)pad, pad); /* PKCS7 */

    DWORD clen = 0;
    unsigned char *cipher = aes256cbc_encrypt((const unsigned char *)aeskey,
                                              (const unsigned char *)aeskey,
                                              buf, (DWORD)total, &clen);
    free(buf);
    if (!cipher) return NULL;
    char *hex = hex_upper(cipher, clen);
    free(cipher);
    return hex;
}

/* JSON-escape backslashes and quotes of `s` into a malloc'd string. */
static char *json_escape(const char *s) {
    size_t n = strlen(s), extra = 0;
    for (size_t i = 0; i < n; ++i) if (s[i] == '\\' || s[i] == '"') ++extra;
    char *out = malloc(n + extra + 1);
    if (!out) return NULL;
    size_t o = 0;
    for (size_t i = 0; i < n; ++i) {
        if (s[i] == '\\' || s[i] == '"') out[o++] = '\\';
        out[o++] = s[i];
    }
    out[o] = 0;
    return out;
}

static void serve_session(HANDLE h, char **psession) {
    unsigned char header[4];
    char *session = NULL;
    unsigned heartbeats = 0;

    for (;;) {
        if (!read_exact(h, header, 4)) {
            logf_("client disconnected (%u heartbeats)", heartbeats);
            return;
        }
        uint16_t total = (uint16_t)(header[0] | (header[1] << 8));
        uint16_t id = (uint16_t)(header[2] | (header[3] << 8));
        if (total < 4) {
            logf_("bad frame total=%u id=%u", total, id);
            return;
        }
        DWORD plen = total - 4;
        char *payload = malloc(plen + 1);
        if (!payload) return;
        if (!read_exact(h, payload, plen)) { free(payload); return; }
        payload[plen] = 0;
        if (id != 1001) logf_("<< packet %u (%u bytes)", id, total);

        if (id == 1000) { /* public key -> answer the real (encrypted) handshake */
            if (!session) { session = launcher_data(); *psession = session; }
            if (!session) { free(payload); return; }

            char *pubkey = json_find_string(payload, "publicKey");
            if (!pubkey) { logf_("1000: no publicKey in payload"); free(payload); return; }

            /* 32-char ASCII aesKey, used verbatim as the 32-byte AES-256 key
             * (iv = its first 16 bytes), exactly as STOVE/Lite do. */
            static const char hexd[] = "0123456789abcdef";
            unsigned char rnd[16];
            char aeskey[33];
            if (BCryptGenRandom(NULL, rnd, sizeof rnd,
                                BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
                /* never fall back to a fixed key: it would make the session
                 * (which carries the access token) decryptable by anyone */
                logf_("1000: BCryptGenRandom failed; refusing handshake");
                free(pubkey); free(payload); return;
            }
            for (int i = 0; i < 16; ++i) {
                aeskey[i * 2]     = hexd[rnd[i] >> 4];
                aeskey[i * 2 + 1] = hexd[rnd[i] & 0xF];
            }
            aeskey[32] = 0;

            /* 2000: base64(RSA_PKCS1(pubkey,{"aesKey":...})), 72-col \n-wrapped. */
            char *v2000 = build_2000_value(pubkey, aeskey);
            free(pubkey);
            if (!v2000) { logf_("2000: build failed"); free(payload); return; }
            char *e2000 = json_escape(v2000); /* escape the literal backslash-n */
            free(v2000);
            if (!e2000) { free(payload); return; }
            size_t need = strlen(e2000) + 64;
            char *frame = malloc(need);
            if (!frame) { free(e2000); free(payload); return; }
            snprintf(frame, need,
                     "{\"code\":0,\"message\":\"Success\",\"value\":\"%s\"}", e2000);
            free(e2000);
            BOOL ok = send_packet(h, 2000, frame);
            free(frame);
            if (!ok) { free(payload); return; }

            Sleep(20); /* official launcher waits ~20ms between 2000 and 2001 */

            /* 2001: HEXUPPER(AES-256-CBC(aesKey, PKCS7(required_info))). */
            char *v2001 = build_2001_value(aeskey, session);
            if (!v2001) { logf_("2001: build failed"); free(payload); return; }
            need = strlen(v2001) + 64;
            frame = malloc(need);
            if (!frame) { free(v2001); free(payload); return; }
            snprintf(frame, need,
                     "{\"code\":0,\"message\":\"Success\",\"value\":\"%s\"}", v2001);
            free(v2001);
            ok = send_packet(h, 2001, frame);
            free(frame);
            if (!ok) { free(payload); return; }
        } else if (id == 1001) {
            /* heartbeat every ~5 s; no reply (see header). Log only the first. */
            if (heartbeats++ == 0) {
                logf_("heartbeats started");
                InterlockedExchange(&g_accepted, 1);
            }
        } else {
            logf_("unhandled packet %u", id);
        }
        free(payload);
    }
}

/* Serve one client. The launcher data (it carries the access token) is loaded
 * lazily by serve_session(); whatever path it leaves by, wipe and free it here
 * so it neither leaks per connection nor lingers in the heap. */
static void serve(HANDLE h) {
    char *session = NULL;
    serve_session(h, &session);
    if (session) {
        SecureZeroMemory(session, strlen(session));
        free(session);
    }
}

static HANDLE create_pipe(LPCWSTR name) {
    return CreateNamedPipeW(
        name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES,
        65536, 65536, 0, NULL);
}

/* A pipe endpoint handed to a server thread. ``first`` is a handle already
 * created (and logged) by main() *before* the game was launched, so the pipe
 * provably exists by the time the game tries to connect — the game is the
 * named-pipe CLIENT and fails fast (IPC Connect Failed) if its endpoint does
 * not yet exist. Subsequent client instances are created inside the loop. */
typedef struct {
    LPCWSTR name;
    HANDLE  first;
} pipe_server;

/* Serve one pipe name forever, accepting any number of clients. */
static DWORD WINAPI server_thread(LPVOID p) {
    pipe_server *ps = (pipe_server *)p;
    LPCWSTR name = ps->name;
    HANDLE h = ps->first;
    for (;;) {
        if (h == INVALID_HANDLE_VALUE || h == NULL) {
            h = create_pipe(name);
            if (h == INVALID_HANDLE_VALUE) {
                logf_("CreateNamedPipeW(%ls) failed: %lu", name, GetLastError());
                Sleep(1000);
                continue;
            }
        }
        logf_("waiting on %ls", name);
        BOOL ok = ConnectNamedPipe(h, NULL) ||
                  GetLastError() == ERROR_PIPE_CONNECTED;
        if (!ok) {
            logf_("ConnectNamedPipe(%ls) failed: %lu", name, GetLastError());
            CloseHandle(h);
            h = NULL;
            continue;
        }
        logf_("*** game connected on %ls", name);
        InterlockedExchange(&g_connected, 1);
        serve(h);
        DisconnectNamedPipe(h);
        CloseHandle(h);
        h = NULL; /* next client gets a fresh instance */
    }
    return 0;
}

static void ansi_to_wide(const char *s, wchar_t *out, size_t outn) {
    if (!s || !s[0]) { if (outn) out[0] = 0; return; }
    if (!MultiByteToWideChar(CP_UTF8, 0, s, -1, out, (int)outn))
        MultiByteToWideChar(CP_ACP, 0, s, -1, out, (int)outn);
}

/* Spawn the game loader as a child of this process. */
static void launch_game(void) {
    if (!g_launch || g_launched) return;
    g_launched = 1;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    logf_("launching game: %ls  (cwd=%ls)", g_launch_exe, g_launch_cwd);
    BOOL ok = CreateProcessW(g_launch_exe, g_launch_cmd, NULL, NULL, FALSE,
                             0, NULL, g_launch_cwd, &si, &pi);
    if (!ok) {
        logf_("CreateProcessW failed: %lu (cmd=%ls)", GetLastError(), g_launch_cmd);
        return;
    }
    logf_("game process started: pid=%lu", (unsigned long)pi.dwProcessId);
    g_game_pid = pi.dwProcessId;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
}

/* Wait until the game has exited, then return. "The game" is the launched
 * process plus everything it spawns (in practice ssr-stove-shield.exe and its
 * crashpad_handler.exe). We are the pipe server, so we must outlive all of
 * them, and a launcher needs us to exit when they are gone. Only the parent
 * chain is followed: matching by image path also catches Wine's own system
 * processes. */
#define MAX_TRACKED 512

/* Wine's own desktop/system processes. Some Proton versions start them as
 * children of the first program (Proton 9 parents explorer.exe and
 * tabtip.exe to the game); they never exit, so adopting them would keep us
 * waiting forever after the game is gone. */
static int is_wine_system_process(const wchar_t *exe) {
    static const wchar_t *names[] = {
        L"explorer.exe", L"tabtip.exe", L"conhost.exe", L"services.exe",
        L"winedevice.exe", L"plugplay.exe", L"svchost.exe", L"rpcss.exe",
        L"xalia.exe", L"winemenubuilder.exe", L"wineboot.exe", NULL,
    };
    for (int i = 0; names[i]; ++i)
        if (_wcsicmp(exe, names[i]) == 0) return 1;
    return 0;
}
static void wait_for_game_exit(void) {
    static DWORD tracked[MAX_TRACKED];
    int nt = 0, gone = 0;
    if (!g_game_pid) return;
    tracked[nt++] = g_game_pid;
    for (;;) {
        Sleep(1000);
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == INVALID_HANDLE_VALUE) continue;
        PROCESSENTRY32W pe;
        int alive = 0, grew = 1;
        while (grew) { /* adopt descendants until the set stops growing */
            grew = 0;
            pe.dwSize = sizeof pe;
            for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe)) {
                if (pe.th32ProcessID == GetCurrentProcessId()) continue;
                int known = 0, parent = 0;
                for (int i = 0; i < nt; ++i) {
                    if (tracked[i] == pe.th32ProcessID) known = 1;
                    if (tracked[i] == pe.th32ParentProcessID) parent = 1;
                }
                if (!known && parent && nt < MAX_TRACKED &&
                    !is_wine_system_process(pe.szExeFile)) {
                    tracked[nt++] = pe.th32ProcessID;
                    logf_("tracking pid=%lu %ls (parent %lu)",
                          (unsigned long)pe.th32ProcessID, pe.szExeFile,
                          (unsigned long)pe.th32ParentProcessID);
                    grew = 1;
                }
            }
        }
        pe.dwSize = sizeof pe;
        for (BOOL ok = Process32FirstW(snap, &pe); ok && !alive; ok = Process32NextW(snap, &pe))
            for (int i = 0; i < nt; ++i)
                if (tracked[i] == pe.th32ProcessID) { alive = 1; break; }
        CloseHandle(snap);
        gone = alive ? 0 : gone + 1;
        if (gone >= 3) { /* 3 s grace for hand-offs */
            logf_("game exited; stopping");
            return;
        }
    }
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--version") == 0) {
        printf("czn_pcsdk_stub %s\n", STUB_VERSION);
        return 0;
    }
    const char *log_path = getenv("CZN_STUB_LOG");
    if (log_path && *log_path) g_log = fopen(log_path, "wb");
    if (argc > 1) g_session_path = argv[1];
    if (argc > 4 && strcmp(argv[2], "--launch") == 0) {
        g_launch = 1;
        ansi_to_wide(argv[3], g_launch_exe, 1024);
        ansi_to_wide(argv[4], g_launch_cwd, 1024);
        wchar_t *p = g_launch_cmd;
        size_t left = 4096;
        int n = swprintf(p, left, L"\"%ls\"", g_launch_exe);
        if (n > 0) { p += n; left -= (size_t)n; }
        for (int i = 5; i < argc && left > 2; ++i) {
            wchar_t a[1024];
            ansi_to_wide(argv[i], a, 1024);
            n = swprintf(p, left, L" %ls", a);
            if (n <= 0) break;
            p += n;
            left -= (size_t)n;
        }
    }
    logf_("czn_pcsdk_stub %s starting", STUB_VERSION);
    if (g_launch) logf_("launch command: %ls", g_launch_cmd);

    /* The endpoint is \\.\pipe\<namespace>\<name> (NamedPipeImpl::virtual_0,
     * 0x1801b1cf0) with namespace = PIPE_GUID and name = the game id; confirmed
     * by a +file trace of the client's CreateFileW and matching Lite. */
#ifndef GAME_ID
#define GAME_ID L"STOVE_CHAOSZERO"
#endif
#ifndef PIPE_GUID
#define PIPE_GUID L"15BADE76-0E87-422B-A75B-B2049FE5F5F5"
#endif
    static const wchar_t *names[] = {
        L"\\\\.\\pipe\\" PIPE_GUID L"\\" GAME_ID,
        NULL,
    };

    /* Create every pipe instance BEFORE launching the game, so the client's
     * endpoint already exists when it connects. Launching first is a race the
     * game loses in ~10 ms (observed as an immediate IPC Connect Failed with no
     * "Wait for IPC... State : 5"). */
    static pipe_server servers[8];
    int n_servers = 0;
    for (int i = 0; names[i] && n_servers < 8; ++i) {
        HANDLE h = create_pipe(names[i]);
        if (h == INVALID_HANDLE_VALUE) {
            logf_("CreateNamedPipeW(%ls) failed: %lu", names[i], GetLastError());
            continue;
        }
        logf_("listening on %ls", names[i]);
        servers[n_servers].name = names[i];
        servers[n_servers].first = h;
        ++n_servers;
    }

    /* Now the pipes exist: start the game. */
    if (!g_launched) launch_game();

    if (n_servers == 0) {
        logf_("no pipes could be created; nothing to serve");
        return 1;
    }
    for (int i = 0; i < n_servers; ++i) {
        HANDLE t = CreateThread(NULL, 0, server_thread, &servers[i], 0, NULL);
        if (t) CloseHandle(t);
        else { logf_("CreateThread failed: %lu", GetLastError()); return 1; }
    }
    if (!g_launch) { Sleep(INFINITE); return 0; } /* serve-only mode */
    if (!g_game_pid) return 1;                     /* launch failed (logged) */
    wait_for_game_exit();

    /* One RESULT line for the launch script. The two failures are what a game
     * update changing the PCSDK handshake would look like. */
    if (!g_connected) {
        logf_("RESULT no-connect: the game exited without opening the launcher pipe "
              "(a game update may have changed the pipe name, or it failed to start)");
        return 3;
    }
    if (!g_accepted) {
        logf_("RESULT handshake-rejected: the game connected but never accepted the "
              "session (a game update may have changed the handshake crypto, or the "
              "token was refused)");
        return 4;
    }
    logf_("RESULT ok");
    return 0;
}
