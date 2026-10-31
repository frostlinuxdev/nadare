# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import ssl

import pytest

from nadare.core import tls


def _no_default_store(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    missing = ssl.DefaultVerifyPaths(
        cafile=str(tmp_path / "nope.pem"),
        capath=str(tmp_path / "nodir"),
        openssl_cafile_env="SSL_CERT_FILE",
        openssl_cafile=str(tmp_path / "nope.pem"),
        openssl_capath_env="SSL_CERT_DIR",
        openssl_capath=str(tmp_path / "nodir"),
    )
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: missing)


def test_picks_first_existing_bundle_when_default_store_missing(monkeypatch, tmp_path):
    _no_default_store(monkeypatch, tmp_path)
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text("x")
    chosen = tls.ensure_ca_bundle([str(tmp_path / "absent.crt"), str(bundle)])
    assert chosen == str(bundle)
    import os

    assert os.environ["SSL_CERT_FILE"] == str(bundle)


def test_leaves_explicit_user_setting_alone(monkeypatch, tmp_path):
    _no_default_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text("x")
    assert tls.ensure_ca_bundle([str(bundle)]) is None
    import os

    assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"


def test_does_nothing_when_default_store_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    cafile = tmp_path / "default.pem"
    cafile.write_text("x")
    present = ssl.DefaultVerifyPaths(
        cafile=str(cafile),
        capath=None,
        openssl_cafile_env="SSL_CERT_FILE",
        openssl_cafile=str(cafile),
        openssl_capath_env="SSL_CERT_DIR",
        openssl_capath="",
    )
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: present)
    assert tls.ensure_ca_bundle([str(cafile)]) is None


def test_returns_none_when_no_bundle_exists(monkeypatch, tmp_path):
    _no_default_store(monkeypatch, tmp_path)
    assert tls.ensure_ca_bundle([str(tmp_path / "a"), str(tmp_path / "b")]) is None


@pytest.mark.network
def test_real_clients_reject_the_forged_emudhra_chains():
    """Opt-in: both live HTTPS stacks must refuse the compromised eMudhra chains.

    Run with ``-m network``. A page/200 here means this machine's CA store still
    trusts the forged roots; see docs/TROUBLESHOOTING.md for the remediation.
    """
    import urllib.request

    for host in tls.DIAGNOSTIC_HOSTS:
        try:
            with urllib.request.urlopen(  # noqa: S310 - intentional probe
                host, timeout=15, context=tls.ssl_context()
            ) as response:
                response.read(1)
        except Exception as exc:  # noqa: BLE001 - only a cert failure is a pass
            assert tls._is_certificate_failure(exc), (
                f"urllib could not reach {host} for a non-certificate reason: {exc}"
            )
        else:
            pytest.fail(
                f"urllib ACCEPTED the forged chain for {host}: remove the emSign* and "
                "Comodo_AAA_Services_root roots from the CA store"
            )

    curl_cffi = pytest.importorskip("curl_cffi")
    for host in tls.DIAGNOSTIC_HOSTS:
        with pytest.raises(curl_cffi.requests.errors.RequestsError) as info:
            curl_cffi.requests.get(host, impersonate="chrome", timeout=15)
        assert "certificate" in str(info.value).lower() or "ssl" in str(info.value).lower(), (
            f"curl_cffi failed for {host} but not on certificate verification: {info.value}"
        )
