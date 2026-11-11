// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Nadare contributors
//
//go:build windows

// czn_pcsdk_stub (Go spike): STOVE PCSDK launcher-handshake server.
//
// The game's BaseSDK.dll is a named-pipe CLIENT and refuses to start unless a
// launcher answers on \\.\pipe\<GUID>\STOVE_CHAOSZERO. This program is that
// server. Same behaviour, exit codes and log lines as the C stub; see proto.go
// for the wire protocol.
//
//	czn_pcsdk_stub.exe <session.json> --launch <game.exe> <cwd> [args...]
//	czn_pcsdk_stub.exe --version
package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// version is set with -ldflags "-X main.version=...".
var version = "dev"

const (
	gameID   = "STOVE_CHAOSZERO"
	pipeGUID = "15BADE76-0E87-422B-A75B-B2049FE5F5F5"
)

var (
	logMu      sync.Mutex
	logFile    *os.File
	connected  atomic.Bool // opened the pipe
	accepted   atomic.Bool // sent a heartbeat, i.e. took our 2000/2001
	gameLaunch struct {
		on  bool
		exe string
		cwd string
		cmd string
		pid uint32
	}
	sessionPath = "session.json"
	// preloaded holds the session read at start in --launch mode; the file is gone by then.
	preloaded []byte
)

// errno returns the numeric Win32 error code, as the C stub logs it.
func errno(err error) uint32 {
	if e, ok := err.(syscall.Errno); ok {
		return uint32(e)
	}
	return 0
}

func logf(format string, a ...any) {
	line := time.Now().Format("2006-01-02 15:04:05.000") + " " + fmt.Sprintf(format, a...)
	logMu.Lock()
	defer logMu.Unlock()
	fmt.Fprintln(os.Stdout, line)
	if logFile != nil {
		fmt.Fprintln(logFile, line)
	}
}

var procDisconnectNamedPipe = windows.NewLazySystemDLL("kernel32.dll").NewProc("DisconnectNamedPipe")

func disconnect(h windows.Handle) { procDisconnectNamedPipe.Call(uintptr(h)) }

func sendPacket(h windows.Handle, id uint16, body string) bool {
	buf, err := frame(id, body)
	if err != nil {
		return false
	}
	var wrote uint32
	err = windows.WriteFile(h, buf, &wrote, nil)
	logf(">> packet %d (%d bytes)", id, len(buf))
	return err == nil && int(wrote) == len(buf)
}

func readExact(h windows.Handle, dst []byte) bool {
	total := 0
	for total < len(dst) {
		var got uint32
		if err := windows.ReadFile(h, dst[total:], &got, nil); err != nil || got == 0 {
			return false
		}
		total += int(got)
	}
	return true
}

func readSession() ([]byte, bool) {
	f, err := os.Open(sessionPath)
	if err != nil {
		logf("cannot read %s; run czn_auth.py token first", sessionPath)
		return nil, false
	}
	defer f.Close()
	b, err := readCapped(f, maxSession)
	if err == errSessionTooBig {
		logf("%s is larger than %d bytes; refusing it", sessionPath, maxSession)
		return nil, false
	}
	if err != nil {
		logf("cannot read %s; run czn_auth.py token first", sessionPath)
		return nil, false
	}
	logf("using launcher data from %s", sessionPath)
	return b, true
}

func serveSession(h windows.Handle) {
	var session []byte
	defer func() {
		if preloaded == nil {
			wipe(session) // the session carries the access token
		}
	}()
	var heartbeats uint
	for {
		var hdr [4]byte
		if !readExact(h, hdr[:]) {
			logf("client disconnected (%d heartbeats)", heartbeats)
			return
		}
		total, id := parseHeader(hdr)
		if total < 4 {
			logf("bad frame total=%d id=%d", total, id)
			return
		}
		payload := make([]byte, total-4)
		if !readExact(h, payload) {
			return
		}
		if id != 1001 {
			logf("<< packet %d (%d bytes)", id, total)
		}
		switch id {
		case 1000: // public key -> the real (encrypted) handshake
			if session == nil && preloaded != nil {
				session = preloaded // read at start; a reconnecting game needs it again
			}
			if session == nil {
				s, ok := readSession()
				if !ok {
					return
				}
				session = s
			}
			pub, ok := findJSONString(payload, "publicKey")
			if !ok {
				logf("1000: no publicKey in payload")
				return
			}
			key, err := newAESKey()
			if err != nil {
				// never fall back to a fixed key
				logf("1000: random source failed; refusing handshake")
				return
			}
			v2000, err := value2000(pub, key)
			if err != nil {
				logf("2000: build failed: %v", err)
				return
			}
			if !sendPacket(h, 2000, reply(jsonEscape(v2000))) {
				return
			}
			time.Sleep(20 * time.Millisecond) // official launcher waits ~20ms
			v2001, err := value2001(key, session)
			if err != nil {
				logf("2001: build failed: %v", err)
				return
			}
			if !sendPacket(h, 2001, reply(v2001)) {
				return
			}
		case 1001: // heartbeat every ~5 s; no reply
			if heartbeats == 0 {
				logf("heartbeats started")
				accepted.Store(true)
			}
			heartbeats++
		default:
			logf("unhandled packet %d", id)
		}
	}
}

func createPipe(name *uint16) (windows.Handle, error) {
	return windows.CreateNamedPipe(name,
		windows.PIPE_ACCESS_DUPLEX,
		// Only local clients: the game runs in this Wine prefix (#119).
		windows.PIPE_TYPE_BYTE|windows.PIPE_READMODE_BYTE|windows.PIPE_WAIT|windows.PIPE_REJECT_REMOTE_CLIENTS,
		windows.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, nil)
}

// serverLoop serves one pipe name forever, accepting any number of clients.
// first is an endpoint created (and logged) before the game was launched.
func serverLoop(name string, first windows.Handle) {
	wname, _ := windows.UTF16PtrFromString(name)
	h := first
	for {
		if h == 0 || h == windows.InvalidHandle {
			var err error
			h, err = createPipe(wname)
			if err != nil {
				logf("CreateNamedPipeW(%s) failed: %d", name, errno(err))
				time.Sleep(time.Second)
				h = 0
				continue
			}
		}
		logf("waiting on %s", name)
		err := windows.ConnectNamedPipe(h, nil)
		if err != nil && err != windows.ERROR_PIPE_CONNECTED {
			logf("ConnectNamedPipe(%s) failed: %d", name, errno(err))
			windows.CloseHandle(h)
			h = 0
			continue
		}
		logf("*** game connected on %s", name)
		connected.Store(true)
		serveSession(h)
		disconnect(h)
		windows.CloseHandle(h)
		h = 0 // next client gets a fresh instance
	}
}

func launchGame() {
	if !gameLaunch.on {
		return
	}
	exe, err1 := windows.UTF16PtrFromString(gameLaunch.exe)
	cwd, err2 := windows.UTF16PtrFromString(gameLaunch.cwd)
	cmd, err3 := windows.UTF16FromString(gameLaunch.cmd)
	if err1 != nil || err2 != nil || err3 != nil {
		logf("CreateProcessW failed: bad argument (cmd=%s)", gameLaunch.cmd)
		return
	}
	logf("launching game: %s  (cwd=%s)", gameLaunch.exe, gameLaunch.cwd)
	var si windows.StartupInfo
	si.Cb = uint32(unsafe.Sizeof(si))
	var pi windows.ProcessInformation
	if err := windows.CreateProcess(exe, &cmd[0], nil, nil, false, 0, nil, cwd, &si, &pi); err != nil {
		logf("CreateProcessW failed: %d (cmd=%s)", errno(err), gameLaunch.cmd)
		return
	}
	logf("game process started: pid=%d", pi.ProcessId)
	gameLaunch.pid = pi.ProcessId
	windows.CloseHandle(pi.Thread)
	windows.CloseHandle(pi.Process)
}

// Wine's own desktop/system processes: they never exit, so adopting them would
// keep us waiting forever after the game is gone.
var wineSystem = map[string]bool{
	"explorer.exe": true, "tabtip.exe": true, "conhost.exe": true, "services.exe": true,
	"winedevice.exe": true, "plugplay.exe": true, "svchost.exe": true, "rpcss.exe": true,
	"xalia.exe": true, "winemenubuilder.exe": true, "wineboot.exe": true,
}

const maxTracked = 512

type procInfo struct {
	pid, ppid uint32
	exe       string
}

func snapshot() ([]procInfo, bool) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, false
	}
	defer windows.CloseHandle(snap)
	var pe windows.ProcessEntry32
	pe.Size = uint32(unsafe.Sizeof(pe))
	var out []procInfo
	for err := windows.Process32First(snap, &pe); err == nil; err = windows.Process32Next(snap, &pe) {
		out = append(out, procInfo{pe.ProcessID, pe.ParentProcessID, windows.UTF16ToString(pe.ExeFile[:])})
	}
	return out, true
}

// waitForGameExit follows the launched process and everything it spawns (only
// the parent chain, never by image path) until all are gone for 3 s.
func waitForGameExit() {
	tracked := map[uint32]bool{gameLaunch.pid: true}
	self := windows.GetCurrentProcessId()
	gone := 0
	for {
		time.Sleep(time.Second)
		procs, ok := snapshot()
		if !ok {
			continue
		}
		for grew := true; grew; { // adopt descendants until the set stops growing
			grew = false
			for _, p := range procs {
				if p.pid == self || tracked[p.pid] || !tracked[p.ppid] ||
					len(tracked) >= maxTracked || wineSystem[strings.ToLower(p.exe)] {
					continue
				}
				tracked[p.pid] = true
				logf("tracking pid=%d %s (parent %d)", p.pid, p.exe, p.ppid)
				grew = true
			}
		}
		alive := false
		for _, p := range procs {
			if tracked[p.pid] {
				alive = true
				break
			}
		}
		if alive {
			gone = 0
		} else {
			gone++
		}
		if gone >= 3 { // 3 s grace for hand-offs
			logf("game exited; stopping")
			return
		}
	}
}

// exitWiped clears the in-memory session before the process ends.
func exitWiped(code int) {
	wipe(preloaded)
	os.Exit(code)
}

func main() {
	args := os.Args
	if len(args) > 1 && args[1] == "--version" {
		fmt.Printf("czn_pcsdk_stub %s\n", version)
		return
	}
	if p := os.Getenv("CZN_STUB_LOG"); p != "" {
		if f, err := os.Create(p); err == nil { // truncated each run
			logFile = f
		}
	}
	if len(args) > 1 {
		sessionPath = args[1]
	}
	if len(args) > 4 && args[2] == "--launch" {
		gameLaunch.on = true
		gameLaunch.exe = args[3]
		gameLaunch.cwd = args[4]
		var cmd strings.Builder
		cmd.WriteString(`"` + args[3] + `"`)
		for _, a := range args[5:] {
			cmd.WriteString(" " + a)
		}
		gameLaunch.cmd = cmd.String()
	}
	logf("czn_pcsdk_stub %s starting", version)
	if gameLaunch.on {
		logf("launch command: %s", gameLaunch.cmd)
		// Nadare's launch: take the session into memory and delete the file before the
		// game starts, so the tokens are not on disk while it runs (#811).
		if s, err := consumeSession(sessionPath); err == nil {
			preloaded = s
			logf("using launcher data from %s (read and removed)", sessionPath)
		} else {
			logf("cannot read %s at start (%v); will try again when the game connects", sessionPath, err)
		}
	}

	names := []string{`\\.\pipe\` + pipeGUID + `\` + gameID}

	// Create every endpoint BEFORE launching the game: the game is the pipe
	// CLIENT and fails fast if its endpoint does not exist yet.
	type server struct {
		name  string
		first windows.Handle
	}
	var servers []server
	for _, n := range names {
		w, _ := windows.UTF16PtrFromString(n)
		h, err := createPipe(w)
		if err != nil {
			logf("CreateNamedPipeW(%s) failed: %d", n, errno(err))
			continue
		}
		logf("listening on %s", n)
		servers = append(servers, server{n, h})
	}

	launchGame()

	if len(servers) == 0 {
		logf("no pipes could be created; nothing to serve")
		exitWiped(1)
	}
	for _, s := range servers {
		go serverLoop(s.name, s.first)
	}
	if !gameLaunch.on {
		select {} // serve-only mode
	}
	if gameLaunch.pid == 0 {
		exitWiped(1) // launch failed (logged)
	}
	waitForGameExit()

	if !connected.Load() {
		logf("RESULT no-connect: the game exited without opening the launcher pipe " +
			"(a game update may have changed the pipe name, or it failed to start)")
		exitWiped(3)
	}
	if !accepted.Load() {
		logf("RESULT handshake-rejected: the game connected but never accepted the " +
			"session (a game update may have changed the handshake crypto, or the " +
			"token was refused)")
		exitWiped(4)
	}
	logf("RESULT ok")
	wipe(preloaded)
}
