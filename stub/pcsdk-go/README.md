# The CZN PCSDK stub (Go)

This is the stub Nadare ships and runs for Chaos Zero Nightmare (issues #113,
#119). It stands in for STOVE's PCSDK launcher: it serves the named-pipe
handshake to the game, starts the game on request, follows its process tree and
reports `RESULT ok|no-connect|handshake-rejected`.

Everything that is not a Win32 process/pipe call is in Go's standard library:
RSA-PKCS1v1.5, AES-CBC, base64, hex, random, SPKI parsing. Only the named pipe,
`CreateProcess` and the Toolhelp snapshot go through
`golang.org/x/sys/windows`.

## Layout
- `proto.go` portable protocol logic (framing, minimal JSON field finder,
  72-col wrap, RSA/AES builders). `proto_test.go` covers it on Linux.
- `main_windows.go` pipe server, `--launch`, process-tree tracking, exit codes
  and log lines.
- `main_other.go` so `go vet`/`go test` work off Windows.

## Build

    scripts/build_stub_go.sh                  # -> dist/czn_pcsdk_stub.exe
    scripts/build_stub_go.sh /path/out.exe    # explicit output

The build runs in a container pinned by digest (Go 1.27.2) with module
checksums from `go.sum`, so two builds are byte-identical and match the shipped
`src/nadare/assets/czn_pcsdk_stub.exe`. Needs podman or docker, and network the
first time (image and `golang.org/x/sys`).

## Test

    scripts/build_stub_go.sh --test           # gofmt, Go tests, windows vet
    scripts/stub_check/run.sh                 # end-to-end check under Wine

`scripts/stub_check/run.sh` round-trips the handshake (including a burst of
malformed frames) without the game; it defaults to the shipped stub. The Go
test suite covers the protocol on Linux; the Wine harness covers the pipe
server.

## #119 hardening
- The session file is capped at 64 KB (the real one is about 2 KB); a larger
  file is refused. Frames from the game are limited by their 16-bit length.
- Session copies are wiped after use rather than left in memory.
- The pipe is created with `PIPE_REJECT_REMOTE_CLIENTS`, so only local
  processes can talk to it.
- In `--launch` mode (how Nadare runs it) the stub reads `session.json` once at start, keeps it in memory and deletes the file before the game starts, so the game tokens are not on disk while it runs (#811). Serve-only mode (the Wine harness) still reads the file when a client connects.

## The C stub

The C build stays available for one release as a fallback, in case a game
update breaks the Go one. It is committed as
`src/nadare/assets/czn_pcsdk_stub_c.exe`; select it per profile with
`pcsdk_stub = "c"`, or for a run with `NADARE_CZN_STUB=c`. Its source is
`src/nadare/backends/czn/stub_src/czn_pcsdk_stub.c`, built by
`scripts/build_stub.sh`.
