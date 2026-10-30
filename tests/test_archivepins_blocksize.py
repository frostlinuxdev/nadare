# SPDX-License-Identifier: GPL-3.0-or-later
"""The block-size rule and the document size cap are shared by the node job and the client."""

from __future__ import annotations

import pytest

from nadare.core import archivepins


@pytest.mark.parametrize("block_size", [4096, 65536, 1 << 20, 16 << 20])
def test_valid_block_sizes(block_size):
    archivepins.check_block_size(block_size)


@pytest.mark.parametrize(
    "block_size", [0, 1, 4095, 4097, (16 << 20) + 1, -4096, True, False, 4096.0, "4096", None]
)
def test_invalid_block_sizes(block_size):
    with pytest.raises(archivepins.PinError):
        archivepins.check_block_size(block_size)


def test_the_client_reads_documents_up_to_the_published_cap():
    assert archivepins.MAX_DOCUMENT_BYTES == 16 << 20


def test_a_pin_url_must_be_https_or_loopback():
    assert archivepins.is_safe_pin_url("https://cdn.example/a.zip")
    assert archivepins.is_safe_pin_url("http://127.0.0.1:8080/a.zip")
    assert not archivepins.is_safe_pin_url("http://cdn.example/a.zip")
    assert not archivepins.is_safe_pin_url(None)


def test_an_invalid_parsed_block_size_is_refused():
    pin = archivepins.pin_from_bytes("https://cdn.example/a.zip", b"x" * 8192, 4096)
    doc = archivepins.document({"a.zip": pin})
    doc["archives"]["a.zip"]["block_size"] = 4097
    with pytest.raises(archivepins.PinError):
        archivepins.parse_document(doc)
