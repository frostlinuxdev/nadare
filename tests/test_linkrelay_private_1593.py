# SPDX-License-Identifier: GPL-3.0-or-later
"""Sandbox link relay: a game cannot point the browser at the user's own network (#1593 item 3).

A link to a router, a NAS or a printer on the LAN is how a page in the user's browser attacks
those devices (CSRF, DNS-rebinding probes). Public hosts stay allowed, and so does loopback: the
Sandbox shares the host network, so ``http://127.0.0.1:PORT/`` reaches the game's own sign-in
listener (an OAuth loopback callback), which the relay accepted on purpose since #804.
"""

from __future__ import annotations

import pytest

from nadare.core.linkrelay import parse_request

STEAM_LINE = "'~/.steam/root/ubuntu12_32/steam' 'steam://openurl_external/{url}'\n"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.1/",  # the router's admin page
        "http://192.168.178.1:80/cgi-bin/x",
        "https://10.0.0.5/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://169.254.169.254/latest/meta-data/",  # link-local (cloud metadata)
        "http://100.64.0.1/",  # carrier-grade NAT / Tailscale
        "http://0.0.0.0:8080/",  # "this host" on every interface
        "http://224.0.0.1/",  # multicast
        "http://255.255.255.255/",
        "http://240.0.0.1/",  # reserved
        "https://[fd00::1]/",  # unique local
        "https://[fe80::1]/",  # link-local
        "https://[::]/",
        "https://[::ffff:192.168.1.1]/",  # IPv4-mapped LAN address
        "https://[ff02::1]/",
        "http://router/",  # one label: the LAN's search domain resolves it
        "http://printer.local/",  # mDNS
        "http://nas.lan/",
        "http://fritz.home.arpa/",
        "http://db.internal/",
        "http://host.localdomain/",
        "http://NAS.LOCAL./",
    ],
)
def test_a_link_into_the_users_network_is_refused(url):
    assert parse_request(STEAM_LINE.format(url=url)) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.example.com/login",
        "https://8.8.8.8/",
        "https://[2606:4700:4700::1111]/",
        "http://localhost:8080/callback",  # the game's own loopback listener
        "http://127.0.0.1:53682/?code=abc#frag",
        "http://127.8.9.1:5096/",
        "https://[::1]:8080/callback",
        "http://app.localhost:3000/",  # browsers resolve *.localhost to loopback
        "http://172.32.0.1/",  # just past 172.16/12: public
        "http://192.169.0.1/",
        "https://local.example.com/",  # "local" as a label, not the suffix
        "https://example.internal.example.com/",
    ],
)
def test_public_and_loopback_links_still_open(url):
    assert parse_request(STEAM_LINE.format(url=url)) == url
