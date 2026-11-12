# SPDX-License-Identifier: GPL-3.0-or-later
"""The CDN allow-list is HTTPS on the publisher's CDN and nowhere else, ports included."""

from nadare.core.sources import nikke as source


def test_an_explicit_unlisted_port_is_not_the_publishers_cdn():
    assert source.cdn_url_allowed("https://cloud.nikke-kr.com/PC/x.wgc")
    assert not source.cdn_url_allowed("https://cloud.nikke-kr.com:8443/PC/x.wgc")
