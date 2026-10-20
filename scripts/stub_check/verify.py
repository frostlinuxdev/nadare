#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decrypt the packets captured by client.exe and compare with the session file.

usage: verify.py <priv.pem> <session.json> <got.txt>
"""
import base64
import json
import sys

from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

priv = serialization.load_pem_private_key(open(sys.argv[1], "rb").read(), None)
want = open(sys.argv[2]).read()
lines = open(sys.argv[3]).read().splitlines()
if not lines or len(lines) % 2:
    sys.exit(f"expected pairs of 2000/2001 packets, got {len(lines)} lines")
for n in range(0, len(lines), 2):
    v2000 = json.loads(lines[n].split(" ", 1)[1])["value"]
    ct = base64.b64decode(v2000.replace("\\n", "").replace("\n", ""))
    key = json.loads(priv.decrypt(ct, padding.PKCS1v15()))["aesKey"].encode()
    v2001 = json.loads(lines[n + 1].split(" ", 1)[1])["value"]
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    pt = dec.update(bytes.fromhex(v2001)) + dec.finalize()
    un = sympad.PKCS7(128).unpadder()
    pt = un.update(pt) + un.finalize()
    if pt.decode() != want:
        sys.exit(f"client {n // 2}: session mismatch")
    print(f"client {n // 2}: session round-trips")
