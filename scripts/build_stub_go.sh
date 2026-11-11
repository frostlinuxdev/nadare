#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Build the Chaos Zero Nightmare PCSDK stub from its Go source (stub/pcsdk-go).
#
#   scripts/build_stub_go.sh                  # -> dist/czn_pcsdk_stub.exe
#   scripts/build_stub_go.sh /path/out.exe    # explicit output
#   scripts/build_stub_go.sh --test           # run the Go tests and vet only
#
# The build always runs in a container pinned by digest (Go 1.27.2) with module
# checksums from go.sum, so two builds are byte-identical and match the shipped
# src/nadare/assets/czn_pcsdk_stub.exe. Needs podman or docker and network the
# first time (image and golang.org/x/sys).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/stub/pcsdk-go"
image="${GO_IMAGE:-docker.io/library/golang@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e}"
version="${STUB_VERSION:-nadare}"

engine="$(command -v podman || command -v docker || true)"
[[ -n "$engine" ]] || { echo "[!] podman or docker is needed" >&2; exit 1; }

run() {
    "$engine" run --rm -v "$src":/src:ro,Z "$@"
}
env_args=(-w /src -e GOTOOLCHAIN=local -e GOFLAGS=-mod=readonly -e CGO_ENABLED=0)

if [[ "${1:-}" == "--test" ]]; then
    run "${env_args[@]}" "$image" sh -c \
        'test -z "$(gofmt -l .)" && GOOS=linux go test ./... && GOOS=windows go vet ./...'
    exit
fi

out="${1:-$root/dist/czn_pcsdk_stub.exe}"
mkdir -p "$(dirname "$out")"
outdir="$(cd "$(dirname "$out")" && pwd)"
name="$(basename "$out")"
echo "[*] Building the PCSDK stub (Go) -> $out"
"$engine" run --rm -v "$src":/src:ro,Z -v "$outdir":/out:Z "${env_args[@]}" \
    -e GOOS=windows -e GOARCH=amd64 "$image" \
    go build -trimpath -buildvcs=false \
    -ldflags "-s -w -buildid= -H windowsgui -X main.version=$version" \
    -o "/out/$name" .
sha256sum "$out"
