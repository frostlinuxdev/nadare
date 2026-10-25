/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Copyright (C) 2026 Nadare contributors */
/* Start a game with a suspended launcher as its parent process.
 * usage: suspend_start.exe --launcher <Games.exe> <game.exe> [game args...] */
#include <windows.h>
#include <wchar.h>

static int append(WCHAR *dst, size_t cap, size_t *n, WCHAR c)
{
    if (*n + 1 >= cap) return 0;
    dst[(*n)++] = c;
    return 1;
}

static void quote(WCHAR *dst, size_t cap, const WCHAR *arg)
{
    size_t n = wcslen(dst), bs, i, total;

    if (!cap) return;
    if (n >= cap) n = cap - 1;
    if (n && !append(dst, cap, &n, L' ')) goto done;
    if (!append(dst, cap, &n, L'"')) goto done;
    while (*arg) {
        for (bs = 0; *arg == L'\\'; arg++) bs++;
        if (*arg == L'"') {
            total = 2 * bs + 1;
            for (i = 0; i < total; i++)
                if (!append(dst, cap, &n, L'\\')) goto done;
            if (!append(dst, cap, &n, L'"')) goto done;
            arg++;
        } else {
            total = *arg ? bs : 2 * bs;
            for (i = 0; i < total; i++)
                if (!append(dst, cap, &n, L'\\')) goto done;
            if (*arg && !append(dst, cap, &n, *arg)) goto done;
            if (*arg) arg++;
        }
    }
    append(dst, cap, &n, L'"');
done:
    dst[n] = 0;
}

int wmain(int argc, WCHAR **argv)
{
    WCHAR cmd[32768] = L"", lcmd[4096] = L"", dir[MAX_PATH];
    STARTUPINFOEXW si = { .StartupInfo.cb = sizeof(si) };
    STARTUPINFOW lsi = { .cb = sizeof(lsi) };
    PROCESS_INFORMATION lpi = {0}, pi = {0};
    SIZE_T size = 0;
    DWORD code = 1;
    WCHAR *slash;

    if (argc < 4 || wcscmp(argv[1], L"--launcher")) {
        MessageBoxW(NULL, L"usage: suspend_start.exe --launcher <Games.exe> <game.exe> [args...]", L"suspend_start", MB_OK);
        return 2;
    }
    quote(lcmd, 4096, argv[2]);
    for (int i = 3; i < argc; i++) quote(cmd, 32768, argv[i]);
    wcsncpy(dir, argv[3], MAX_PATH - 1); dir[MAX_PATH - 1] = 0;
    if ((slash = wcsrchr(dir, L'\\')) || (slash = wcsrchr(dir, L'/'))) *slash = 0;

    if (!CreateProcessW(argv[2], lcmd, NULL, NULL, FALSE, CREATE_SUSPENDED, NULL, NULL, &lsi, &lpi))
        return 3;
    InitializeProcThreadAttributeList(NULL, 1, 0, &size);
    si.lpAttributeList = HeapAlloc(GetProcessHeap(), 0, size);
    if (!InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &size) ||
        !UpdateProcThreadAttribute(si.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PARENT_PROCESS,
                                   &lpi.hProcess, sizeof(lpi.hProcess), NULL, NULL)) {
        TerminateProcess(lpi.hProcess, 0);
        return 4;
    }
    if (CreateProcessW(argv[3], cmd, NULL, NULL, FALSE, EXTENDED_STARTUPINFO_PRESENT, NULL, dir,
                       &si.StartupInfo, &pi)) {
        WaitForSingleObject(pi.hProcess, INFINITE);
        GetExitCodeProcess(pi.hProcess, &code);
        CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    }
    TerminateProcess(lpi.hProcess, 0);
    return code;
}
