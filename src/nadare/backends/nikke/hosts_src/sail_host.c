/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 Nadare contributors
 *
 * sail_host.c - start NIKKE without its publisher launcher.
 *
 * The game expects its launcher to answer a short start-up exchange. This program
 * plays that part (the same role as czn_pcsdk_stub.c for STOVE): it starts the
 * game, answers the start-up message with the account session read from a JSON
 * file, and stays out of the way. It never creates or changes a sign-in, and the
 * anti-cheat is not touched.
 *
 * Build:
 *   x86_64-w64-mingw32-gcc -O2 -mwindows -o sail_host.exe sail_host.c
 * Usage (inside the game's Wine prefix):
 *   wine sail_host.exe <session.json> --launch <game.exe> <cwd> [args...]
 */

#ifndef SAIL_HOST_TEST
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <tlhelp32.h>
#endif
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#ifndef STUB_VERSION
#define STUB_VERSION "dev"
#endif

static const char *g_session_path = "session.json";
static FILE *g_log;

/* ---------- protobuf writer (append into a grow buffer) ---------- */
typedef struct { unsigned char *p; size_t len, cap; } buf_t;

static void buf_need(buf_t *b, size_t extra) {
    if (b->len + extra <= b->cap) return;
    size_t cap = b->cap ? b->cap * 2 : 256;
    while (cap < b->len + extra) cap *= 2;
    b->p = realloc(b->p, cap);
    b->cap = cap;
}
static void buf_byte(buf_t *b, unsigned char c) { buf_need(b, 1); b->p[b->len++] = c; }
static void buf_raw(buf_t *b, const void *d, size_t n) {
    buf_need(b, n); memcpy(b->p + b->len, d, n); b->len += n;
}
static void pb_varint(buf_t *b, uint64_t v) {
    do { unsigned char c = v & 0x7f; v >>= 7; if (v) c |= 0x80; buf_byte(b, c); } while (v);
}
static void pb_key(buf_t *b, int field, int wire) { pb_varint(b, ((uint64_t)field << 3) | wire); }
/* field = varint */
static void pb_uint(buf_t *b, int field, uint64_t v) { pb_key(b, field, 0); pb_varint(b, v); }
/* field = length-delimited bytes/string */
static void pb_bytes(buf_t *b, int field, const void *d, size_t n) {
    pb_key(b, field, 2); pb_varint(b, n); buf_raw(b, d, n);
}
static void pb_str(buf_t *b, int field, const char *s) { pb_bytes(b, field, s, s ? strlen(s) : 0); }
/* field = embedded message (sub already built) */
static void pb_msg(buf_t *b, int field, const buf_t *sub) { pb_bytes(b, field, sub->p, sub->len); }

/* ---------- minimal JSON reader (flat string/number fields) ---------- */
static char *read_file(const char *path, size_t *out_n) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) { fclose(f); return NULL; }
    char *buf = malloc((size_t)n + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t r = fread(buf, 1, (size_t)n, f); buf[r] = 0;
    if (out_n) *out_n = r;
    fclose(f); return buf;
}
/* Copy "key":"value" into out (unescaping \" and \\). Returns 1 if found. */
static int json_str(const char *json, const char *key, char *out, size_t outn) {
    char pat[96]; snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return 0;
    p = strchr(p + strlen(pat), ':'); if (!p) return 0;
    ++p; while (*p == ' ' || *p == '\t') ++p;
    if (*p != '"') return 0;
    ++p; size_t o = 0;
    while (*p && *p != '"' && o + 1 < outn) {
        if (*p == '\\' && p[1]) { ++p; }
        out[o++] = *p++;
    }
    out[o] = 0;
    return 1;
}
/* "key": <number>. Returns the value or `dflt`. */
static long long json_num(const char *json, const char *key, long long dflt) {
    char pat[96]; snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return dflt;
    p = strchr(p + strlen(pat), ':'); if (!p) return dflt;
    ++p; while (*p == ' ' || *p == '\t') ++p;
    return strtoll(p, NULL, 10);
}

/* Build the cmd=100 SailGameMessage from the session JSON. */
static buf_t build_init_message(const char *json) {
    char open_id[256] = "", token[4096] = "", enc[16384] = "", region[64] = "",
         refresh[4096] = "", pf[256] = "", pf_key[256] = "",
         branch[64] = "", version[64] = "", distribute[64] = "",
         zone_info[256] = "", email[256] = "", res_path[512] = "",
         http_domain[256] = "", login_sid[256] = "", launch_sid[256] = "",
         plat_ver[128] = "", md5[256] = "";

    json_str(json, "open_id", open_id, sizeof open_id);
    json_str(json, "token", token, sizeof token);
    json_str(json, "encypt_data", enc, sizeof enc);
    json_str(json, "compliance_region", region, sizeof region);
    json_str(json, "refresh_token", refresh, sizeof refresh);
    json_str(json, "pf", pf, sizeof pf);
    json_str(json, "pf_key", pf_key, sizeof pf_key);
    json_str(json, "branch_id", branch, sizeof branch);
    json_str(json, "version_id", version, sizeof version);
    json_str(json, "distribute_id", distribute, sizeof distribute);
    json_str(json, "zone_info", zone_info, sizeof zone_info);
    json_str(json, "login_email_address", email, sizeof email);
    json_str(json, "game_resource_path", res_path, sizeof res_path);
    json_str(json, "http_domain", http_domain, sizeof http_domain);
    json_str(json, "launcher_login_session_id", login_sid, sizeof login_sid);
    json_str(json, "launch_game_session_id", launch_sid, sizeof launch_sid);
    json_str(json, "platform_version", plat_ver, sizeof plat_ver);
    json_str(json, "download_md5_code", md5, sizeof md5);

    uint64_t game_id        = (uint64_t)json_num(json, "game_id", 0);
    uint64_t launcher_gid   = (uint64_t)json_num(json, "launcher_game_id", 0);
    uint64_t channel_id     = (uint64_t)json_num(json, "channel_id", 0);
    uint64_t zone_id        = (uint64_t)json_num(json, "zone_id", 0);
    uint64_t lang           = (uint64_t)json_num(json, "platform_language_code", 1033);
    uint64_t plat_type      = (uint64_t)json_num(json, "platform_type", 1);
    uint64_t sel_zone       = (uint64_t)json_num(json, "player_selected_zone_id", 0);
    uint64_t intl_test      = (uint64_t)json_num(json, "is_intl_test_env", 0);
    uint64_t adult          = (uint64_t)json_num(json, "adult_state", 0);
    uint64_t support_debug  = (uint64_t)json_num(json, "support_debug_mode", 0);

    /* SailPlayerAccountInfo (field 2 of init) */
    buf_t acc = {0};
    pb_str(&acc, 1, open_id);
    pb_str(&acc, 2, token);
    pb_uint(&acc, 3, channel_id);
    pb_uint(&acc, 4, zone_id);
    pb_str(&acc, 5, enc);
    pb_str(&acc, 6, region);
    pb_str(&acc, 7, refresh);
    pb_str(&acc, 8, pf);
    pb_str(&acc, 9, pf_key);

    /* SailGameBranchInfo (field 3) */
    buf_t br = {0};
    pb_str(&br, 1, branch);
    pb_str(&br, 2, version);

    /* SailGameInitializeInfo */
    buf_t info = {0};
    pb_uint(&info, 1, game_id);
    pb_msg(&info, 2, &acc);
    pb_msg(&info, 3, &br);
    pb_str(&info, 4, distribute);
    pb_uint(&info, 5, lang);
    pb_uint(&info, 6, launcher_gid);
    pb_uint(&info, 7, plat_type);
    if (sel_zone) pb_uint(&info, 8, sel_zone);   /* 8/9 are left out when unset */
    if (zone_info[0]) pb_str(&info, 9, zone_info);
    pb_uint(&info, 10, intl_test);
    pb_uint(&info, 11, adult);
    pb_bytes(&info, 12, "", 0);                  /* channel_info: present but empty in the capture */
    pb_str(&info, 13, http_domain);
    pb_str(&info, 14, md5);
    pb_str(&info, 15, login_sid);
    pb_str(&info, 16, email);
    pb_str(&info, 17, launch_sid);
    pb_str(&info, 18, plat_ver);
    pb_uint(&info, 19, support_debug);
    pb_str(&info, 20, res_path);

    /* SailGameMessage: cmd=100, initialize_info=info */
    buf_t msg = {0};
    pb_uint(&msg, 1, 100);
    pb_msg(&msg, 2, &info);

    free(acc.p); free(br.p); free(info.p);
    return msg;
}

#ifndef SAIL_HOST_TEST
static wchar_t g_launch_exe[1024];
static wchar_t g_launch_cwd[1024];
static wchar_t g_launch_cmd[4096];
static int   g_launch = 0;
static DWORD g_game_pid = 0;
static volatile LONG g_connected = 0;  /* game opened sail_pipe */
static volatile LONG g_inited = 0;     /* game replied cmd=101 */

static void logf_(const char *fmt, ...) {
    va_list ap;
    char buf[4096];
    SYSTEMTIME st;
    GetLocalTime(&st);
    int n = snprintf(buf, sizeof buf, "%02d:%02d:%02d.%03d ",
                     st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof buf - n, fmt, ap);
    va_end(ap);
    fputs(buf, stdout); fputc('\n', stdout); fflush(stdout);
    if (g_log) { fputs(buf, g_log); fputc('\n', g_log); fflush(g_log); }
}

/* ---------- framed pipe I/O ---------- */
static BOOL write_frame(HANDLE h, const unsigned char *proto, uint32_t len) {
    /* One frame: u32le length, u32le 0, then the message. */
    unsigned char *out = malloc(8 + len);
    if (!out) return FALSE;
    out[0] = len & 0xff;
    out[1] = (len >> 8) & 0xff;
    out[2] = (len >> 16) & 0xff;
    out[3] = (len >> 24) & 0xff;
    out[4] = out[5] = out[6] = out[7] = 0;
    memcpy(out + 8, proto, len);
    DWORD wrote = 0;
    BOOL ok = WriteFile(h, out, 8 + len, &wrote, NULL) && wrote == 8 + len;
    free(out);
    if (!ok) return FALSE;
    logf_(">> frame cmd=100 (%u proto bytes, %u wire bytes)", len, 8 + len);
    return TRUE;
}
static BOOL read_exact(HANDLE h, void *dst, DWORD n) {
    DWORD got, total = 0;
    while (total < n) {
        if (!ReadFile(h, (char*)dst + total, n - total, &got, NULL)) return FALSE;
        if (!got) return FALSE;
        total += got;
    }
    return TRUE;
}
/* read [u32 len][u32 x][proto]; return cmd (field 1 varint) or -1. */
static int read_frame_cmd(HANDLE h) {
    unsigned char hdr[8];
    if (!read_exact(h, hdr, 8)) return -1;
    uint32_t len = hdr[0] | (hdr[1]<<8) | (hdr[2]<<16) | ((uint32_t)hdr[3]<<24);
    if (len == 0 || len > (16u<<20)) { logf_("<< bad frame len=%u", len); return -1; }
    unsigned char *p = malloc(len);
    if (!p) return -1;
    if (!read_exact(h, p, len)) { free(p); return -1; }
    int cmd = -1;
    if (len >= 2 && p[0] == 0x08) cmd = p[1];   /* field 1, varint, 1-byte value */
    logf_("<< frame (%u proto bytes) cmd=%d", len, cmd);
    free(p);
    return cmd;
}

/* The one pipe that carries the init handshake. */
static void serve_sail_pipe(HANDLE h) {
    size_t jn = 0;
    char *json = read_file(g_session_path, &jn);
    if (!json) { logf_("cannot read session %s", g_session_path); return; }
    buf_t msg = build_init_message(json);
    free(json);
    { const char *dp = getenv("SAIL_HOST_DUMP"); if (dp && *dp) { FILE *df = fopen(dp, "wb"); if (df) { fwrite(msg.p, 1, msg.len, df); fclose(df); logf_("dumped init to %s (%zu bytes)", dp, msg.len); } } }
    if (!write_frame(h, msg.p, (uint32_t)msg.len)) { logf_("send cmd=100 failed: %lu", GetLastError()); free(msg.p); return; }
    free(msg.p);
    int cmd = read_frame_cmd(h);
    if (cmd == 101) { logf_("*** game accepted init (cmd=101)"); InterlockedExchange(&g_inited, 1); }
    else            { logf_("game did not reply cmd=101 (got %d)", cmd); }
    /* keep the pipe open; drain anything further the game sends */
    for (;;) { if (read_frame_cmd(h) < 0) break; }
}

static HANDLE create_pipe(LPCWSTR name) {
    return CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES, 1u<<20, 1u<<20, 0, NULL);
}

typedef struct { wchar_t name[128]; HANDLE first; int is_sail; } pipe_server;

static DWORD WINAPI server_thread(LPVOID p) {
    pipe_server *ps = (pipe_server*)p;
    HANDLE h = ps->first;
    for (;;) {
        if (!h || h == INVALID_HANDLE_VALUE) {
            h = create_pipe(ps->name);
            if (h == INVALID_HANDLE_VALUE) { Sleep(500); continue; }
        }
        BOOL ok = ConnectNamedPipe(h, NULL) || GetLastError() == ERROR_PIPE_CONNECTED;
        if (!ok) { CloseHandle(h); h = NULL; continue; }
        logf_("*** game connected on %ls", ps->name);
        if (ps->is_sail) { InterlockedExchange(&g_connected, 1); serve_sail_pipe(h); }
        else { /* other channels: accept, drain, don't block init */
            unsigned char tmp[4096]; DWORD got;
            while (ReadFile(h, tmp, sizeof tmp, &got, NULL) && got) {}
        }
        DisconnectNamedPipe(h); CloseHandle(h); h = NULL;
    }
    return 0;
}

static void ansi_to_wide(const char *s, wchar_t *out, size_t outn) {
    if (!s || !s[0]) { if (outn) out[0] = 0; return; }
    if (!MultiByteToWideChar(CP_UTF8, 0, s, -1, out, (int)outn))
        MultiByteToWideChar(CP_ACP, 0, s, -1, out, (int)outn);
}

static int is_wine_system_process(const wchar_t *exe) {
    static const wchar_t *names[] = { L"explorer.exe", L"tabtip.exe", L"conhost.exe",
        L"services.exe", L"winedevice.exe", L"plugplay.exe", L"svchost.exe",
        L"rpcss.exe", L"xalia.exe", L"winemenubuilder.exe", L"wineboot.exe",
        L"UnityCrashHandler64.exe", NULL };
    for (int i = 0; names[i]; ++i) if (_wcsicmp(exe, names[i]) == 0) return 1;
    return 0;
}
#define MAX_TRACKED 512
static void wait_for_game_exit(void) {
    static DWORD tracked[MAX_TRACKED]; int nt = 0, gone = 0;
    if (!g_game_pid) return;
    tracked[nt++] = g_game_pid;
    for (;;) {
        Sleep(1000);
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == INVALID_HANDLE_VALUE) continue;
        PROCESSENTRY32W pe; int alive = 0, grew = 1;
        while (grew) {
            grew = 0; pe.dwSize = sizeof pe;
            for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe)) {
                if (pe.th32ProcessID == GetCurrentProcessId()) continue;
                int known = 0, parent = 0;
                for (int i = 0; i < nt; ++i) {
                    if (tracked[i] == pe.th32ProcessID) known = 1;
                    if (tracked[i] == pe.th32ParentProcessID) parent = 1;
                }
                if (!known && parent && nt < MAX_TRACKED && !is_wine_system_process(pe.szExeFile)) {
                    tracked[nt++] = pe.th32ProcessID; grew = 1;
                }
            }
        }
        pe.dwSize = sizeof pe;
        for (BOOL ok = Process32FirstW(snap, &pe); ok && !alive; ok = Process32NextW(snap, &pe))
            for (int i = 0; i < nt; ++i) if (tracked[i] == pe.th32ProcessID) { alive = 1; break; }
        CloseHandle(snap);
        gone = alive ? 0 : gone + 1;
        if (gone >= 3) { logf_("game exited; stopping"); return; }
    }
}

/* Create the four Sail pipe servers for one game PID and start their threads. */
static int start_servers(DWORD pid) {
    static const wchar_t *suffixes[] = {
        L"sail_pipe_%lu", L"sail_named_pipe_%lu",
        L"sail_named_pipe_host_%lu", L"sail_named_pipe_guest_%lu", NULL };
    static pipe_server servers[16]; static int used = 0; int ns = 0;
    for (int i = 0; suffixes[i] && used < 16; ++i) {
        pipe_server *s = &servers[used];
        swprintf(s->name, 128, L"\\\\.\\pipe\\");
        wchar_t tail[96]; swprintf(tail, 96, suffixes[i], (unsigned long)pid);
        wcscat(s->name, tail);
        s->is_sail = (i == 0);
        s->first = create_pipe(s->name);
        if (s->first == INVALID_HANDLE_VALUE) { logf_("CreateNamedPipeW(%ls) failed: %lu", s->name, GetLastError()); continue; }
        logf_("listening on %ls%s", s->name, s->is_sail ? "  (sail)" : "");
        HANDLE t = CreateThread(NULL, 0, server_thread, s, 0, NULL);
        if (t) CloseHandle(t);
        ++used; ++ns;
    }
    return ns;
}

/* nikke.exe starts a second nikke.exe and exits; the second one is the game that connects to
 * sail_pipe_<its own PID> (measured 2026-11-10). Wait for that child and serve its pipes too. */
static DWORD wait_for_child(DWORD parent, DWORD timeout_ms) {
    DWORD start = GetTickCount();
    while (GetTickCount() - start < timeout_ms) {
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap != INVALID_HANDLE_VALUE) {
            PROCESSENTRY32W pe; pe.dwSize = sizeof pe;
            for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe)) {
                if (pe.th32ParentProcessID == parent && _wcsicmp(pe.szExeFile, L"nikke.exe") == 0) {
                    CloseHandle(snap); return pe.th32ProcessID;
                }
            }
            CloseHandle(snap);
        }
        Sleep(20);
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--version") == 0) { printf("sail_host %s\n", STUB_VERSION); return 0; }
    const char *log_path = getenv("SAIL_HOST_LOG");
    if (log_path && *log_path) g_log = fopen(log_path, "wb");
    if (argc > 1) g_session_path = argv[1];
    if (argc > 4 && strcmp(argv[2], "--launch") == 0) {
        g_launch = 1;
        ansi_to_wide(argv[3], g_launch_exe, 1024);
        ansi_to_wide(argv[4], g_launch_cwd, 1024);
        wchar_t *p = g_launch_cmd; size_t left = 4096;
        int n = swprintf(p, left, L"\"%ls\"", g_launch_exe);
        if (n > 0) { p += n; left -= (size_t)n; }
        for (int i = 5; i < argc && left > 2; ++i) {
            wchar_t a[1024]; ansi_to_wide(argv[i], a, 1024);
            n = swprintf(p, left, L" %ls", a); if (n <= 0) break; p += n; left -= (size_t)n;
        }
    }
    logf_("sail_host %s starting", STUB_VERSION);
    if (!g_launch) { logf_("need --launch <game.exe> <cwd>"); return 2; }

    /* The launcher publishes its folder in a named mapping before it starts the game
     * (measured 2026-11-10): u32 game id, u32 0, then the folder as a UTF-16 path. The game's
     * sail_api64.dll reads it to find sail_api_impl64.dll; without it the pipe is never tried. */
    {
        wchar_t dir[MAX_PATH] = L"C:\\NIKKE\\Launcher";
        const char *d = getenv("SAIL_LAUNCHER_DIR");
        if (d && *d) ansi_to_wide(d, dir, MAX_PATH);
        HANDLE shm = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, 4096,
                                        L"Sail.SharedMemory.29080");
        unsigned char *view = shm ? MapViewOfFile(shm, FILE_MAP_WRITE, 0, 0, 4096) : NULL;
        if (view) {
            uint32_t head[2] = {29080, 0};
            memcpy(view, head, sizeof head);
            memcpy(view + 8, dir, (wcslen(dir) + 1) * sizeof(wchar_t));
            logf_("published Sail.SharedMemory.29080 -> %ls", dir);
        } else {
            logf_("could not create Sail.SharedMemory.29080: %lu", GetLastError());
        }
        /* Kept open (and mapped) for the life of this process. */
    }

    /* Create the game SUSPENDED so we learn its PID and can name the pipes
     * before it runs and connects. */
    STARTUPINFOW si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si; ZeroMemory(&pi, sizeof pi);
    logf_("creating game (suspended): %ls  cwd=%ls", g_launch_exe, g_launch_cwd);
    if (!CreateProcessW(g_launch_exe, g_launch_cmd, NULL, NULL, FALSE,
                        CREATE_SUSPENDED, NULL, g_launch_cwd, &si, &pi)) {
        logf_("CreateProcessW failed: %lu", GetLastError());
        return 1;
    }
    g_game_pid = pi.dwProcessId;
    logf_("game pid=%lu", (unsigned long)g_game_pid);

    if (start_servers(g_game_pid) == 0) { logf_("no pipes; aborting"); TerminateProcess(pi.hProcess, 1); return 1; }

    logf_("resuming game");
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    {
        DWORD child = wait_for_child(g_game_pid, 30000);
        if (child) { logf_("game restarted itself as pid=%lu", (unsigned long)child); start_servers(child); g_game_pid = child; }
        else logf_("no second nikke.exe within 30 s");
    }

    wait_for_game_exit();
    if (!g_connected) { logf_("RESULT no-connect: game never opened sail_pipe"); return 3; }
    if (!g_inited)    { logf_("RESULT no-init: game connected but never returned cmd=101"); return 4; }
    logf_("RESULT ok: game accepted the Sail init handshake");
    return 0;
}
#endif /* SAIL_HOST_TEST */
