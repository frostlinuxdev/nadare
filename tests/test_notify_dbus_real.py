# SPDX-License-Identifier: GPL-3.0-or-later
"""Notifications reach a strict notification service (2026-11-11).

Found while fixing a GUI-thread freeze: ``send`` passed a plain 0, [] and {}, which went out as
int32 and untyped arrays (``dbus-monitor``). A service that checks the spec's signature
``susssasa{sv}i`` (KDE's QtDBus one, GNOME's GDBus one) answered "no slot matched", so the
"install finished" and "still running" notifications never showed there. This runs a private
session bus with such a service.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtDBus")
if shutil.which("dbus-run-session") is None:
    pytest.skip("dbus-run-session is not installed", allow_module_level=True)

SRC = str(Path(__file__).resolve().parents[1] / "src")

SERVICE = textwrap.dedent(
    """
    from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtClassInfo, pyqtSlot
    from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection

    app = QCoreApplication([])

    @pyqtClassInfo("D-Bus Interface", "org.freedesktop.Notifications")
    class Adaptor(QDBusAbstractAdaptor):
        @pyqtSlot(str, "uint", str, str, str, "QStringList", "QVariantMap", int, result="uint")
        def Notify(self, app_name, rid, icon, summary, body, actions, hints, timeout):
            print("GOT", summary, body, flush=True)
            return 7

    obj = QObject()
    Adaptor(obj)
    bus = QDBusConnection.sessionBus()
    assert bus.registerService("org.freedesktop.Notifications")
    assert bus.registerObject("/org/freedesktop/Notifications", obj)
    print("READY", flush=True)
    QTimer.singleShot(8000, app.quit)
    app.exec()
    """
)

CLIENT = textwrap.dedent(
    """
    import sys
    from PyQt6.QtCore import QCoreApplication
    app = QCoreApplication([])
    from nadare.gui import notify
    print("SENT", notify.send("Nadare test", "it works"), flush=True)
    """
)


def test_a_strict_notification_service_accepts_the_call(tmp_path):
    (tmp_path / "service.py").write_text(SERVICE)
    (tmp_path / "client.py").write_text(CLIENT)
    monitor = shutil.which("dbus-monitor")
    script = (
        f'"{sys.executable}" service.py > service.log 2>&1 &\n'
        "for i in $(seq 50); do grep -q READY service.log && break; sleep 0.1; done\n"
        + (
            f"\"{monitor}\" \"interface='org.freedesktop.Notifications'\" > wire.log 2>&1 &\n"
            "sleep 0.5\n"
            if monitor
            else ""
        )
        + f'"{sys.executable}" client.py\n'
        "sleep 0.3; kill $(jobs -p) 2>/dev/null; wait\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=SRC)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    done = subprocess.run(
        ["dbus-run-session", "--", "bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = (tmp_path / "service.log").read_text()
    if "READY" not in log:
        pytest.skip(f"no private session bus here: {log[-300:]}")
    assert "SENT True" in done.stdout, done.stdout + done.stderr
    assert "GOT Nadare test it works" in log
    if monitor:
        # Round 7 (mutation testing): the service above accepts any hint type, so only the wire
        # shows the urgency hint is a byte as the spec says (a plain int stayed green before).
        wire = (tmp_path / "wire.log").read_text()
        assert '"urgency"' in wire, wire[-2000:]
        after = wire.split('"urgency"', 1)[1][:200]
        assert "byte 1" in after, after
