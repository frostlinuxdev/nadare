#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# End-to-end check of the PCSDK stub under Wine, without the game:
#   1. three sequential clients complete the 1000 -> 2000/2001 handshake and the
#      decrypted session equals the session file (RSA-PKCS1 + AES-256-CBC);
#   2. a burst of malformed frames (bad totals, truncated bodies, junk keys,
#      65 KB payloads, unknown ids, embedded NULs) must not kill the stub;
#   3. a valid client still succeeds afterwards.
#
#   scripts/stub_check/run.sh [path/to/czn_pcsdk_stub.exe]
#
# Needs: wine, x86_64-w64-mingw32-gcc, python3 with `cryptography`.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
stub="$(realpath "${1:-$root/src/nadare/assets/czn_pcsdk_stub.exe}")"
work="$(mktemp -d)"
trap 'wineserver -k 2>/dev/null || true; rm -rf "$work"' EXIT
# A fresh prefix makes Wine offer to download wine-mono/wine-gecko in a dialog,
# and an empty DISPLAY alone still lets it use Wayland. Keep the run headless and
# prompt-free: no display variables, and the two .NET/HTML stubs disabled (the
# stub and the test clients need neither).
unset WAYLAND_DISPLAY
export WINEPREFIX="$work/pfx" WINEDEBUG=-all DISPLAY= WINEDLLOVERRIDES="mscoree,mshtml="
CC="${CC:-x86_64-w64-mingw32-gcc}"
"$CC" -O2 -o "$work/client.exe" "$here/client.c"
"$CC" -O2 -o "$work/fuzz.exe" "$here/fuzz.c"
python3 - "$work" <<'PY'
import base64, json, sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
w = sys.argv[1]
k = rsa.generate_private_key(65537, 2048)
open(w + "/priv.pem", "wb").write(k.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
der = k.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
open(w + "/pub.b64", "w").write(base64.b64encode(der).decode())
json.dump({"access_token": "tok-" + "x" * 40, "refresh": "r", "n": list(range(50))}, open(w + "/session.json", "w"))
PY
z() { echo "Z:${1//\//\\}"; }
CZN_STUB_LOG="$(z "$work/stub.log")" wine "$stub" "$(z "$work/session.json")" >"$work/stub.out" 2>&1 &
sleep 6
timeout 120 wine "$work/client.exe" "$(z "$work/pub.b64")" "$(z "$work/got.txt")" 3
python3 "$here/verify.py" "$work/priv.pem" "$work/session.json" "$work/got.txt"
timeout 180 wine "$work/fuzz.exe" | tail -1
timeout 60 wine "$work/client.exe" "$(z "$work/pub.b64")" "$(z "$work/got2.txt")" 1
python3 "$here/verify.py" "$work/priv.pem" "$work/session.json" "$work/got2.txt"
echo "PASS: handshake round-trips and the stub survived malformed input"
