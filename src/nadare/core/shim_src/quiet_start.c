/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Copyright (C) 2026 Nadare contributors */
/* Run a program and keep its windows hidden until it exits; returns its exit code.
 * usage: quiet_start.exe <program.exe> [args...]
 *
 * NIKKE's miniloader with /Silent still opens its downloader window (its own log says it should
 * not). This starts it with a hidden initial window and then hides every top-level window the
 * process creates, from inside Wine, so it works the same on X11 and Wayland. */
#include <windows.h>

static DWORD g_pid;

static BOOL CALLBACK hide_window(HWND hwnd, LPARAM unused)
{
    DWORD pid = 0;

    (void)unused;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid == g_pid && IsWindowVisible(hwnd))
        ShowWindow(hwnd, SW_HIDE);
    return TRUE;
}

/* Everything after our own program name, exactly as it was quoted for us. */
static const WCHAR *rest_of_command_line(void)
{
    const WCHAR *p = GetCommandLineW();

    if (*p == L'"') {
        for (p++; *p && *p != L'"'; p++)
            ;
        if (*p)
            p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t')
            p++;
    }
    while (*p == L' ' || *p == L'\t')
        p++;
    return p;
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    const WCHAR *rest = rest_of_command_line();
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    WCHAR *line;
    DWORD code = 1, len;

    (void)inst; (void)prev; (void)cmd; (void)show;
    if (!*rest)
        return 2;
    for (len = 0; rest[len]; len++)
        ;
    line = (WCHAR *)HeapAlloc(GetProcessHeap(), 0, (len + 1) * sizeof(WCHAR));
    if (!line)
        return 3;
    for (len = 0; rest[len]; len++)
        line[len] = rest[len];
    line[len] = 0;

    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    if (!CreateProcessW(NULL, line, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi))
        return 4;
    g_pid = pi.dwProcessId;
    while (WaitForSingleObject(pi.hProcess, 5) == WAIT_TIMEOUT)
        EnumWindows(hide_window, 0);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)code;
}
