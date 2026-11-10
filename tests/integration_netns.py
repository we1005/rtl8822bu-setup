#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ljc
"""Root-only real iproute2 test inside a disposable namespace; no USB changes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

SOURCE = Path(__file__).resolve().parents[1] / "rtl8822bu_setup.py"
SPEC = importlib.util.spec_from_file_location("component", SOURCE)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def run(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


class NamespaceSystem(app.System):
    """Physical sysfs is simulated; ip commands and /proc net sysctls are real."""
    def path(self, name):
        if str(name).startswith("/proc/sys/net/"):
            return Path(name)
        return super().path(name)


def inside():
    # These dummy interfaces exist exclusively in the disposable namespace.
    with tempfile.TemporaryDirectory(prefix="rtl8822bu-netns-") as folder:
        system = NamespaceSystem(Path(folder))
        for name, addr, mac in [("usb-test", "192.0.2.10/24", "02:00:00:00:00:10"),
                                ("peer-test", "192.0.2.11/24", "02:00:00:00:00:11"),
                                ("wired-test", "198.51.100.10/24", "02:00:00:00:00:12")]:
            run("ip", "link", "add", name, "type", "dummy")
            run("ip", "link", "set", name, "address", mac)
            run("ip", "link", "set", name, "up")
            run("ip", "addr", "add", addr, "dev", name)
            # Only ancestry/type metadata is synthetic. Test the routing engine,
            # not hardware discovery (covered separately on actual USB hardware).
            path = system.path("/sys/class/net/" + name)
            (path / "device").mkdir(parents=True)
            (path / "type").write_text("1")
            (path / "address").write_text(mac)
        for name, gateway, metric in [("wired-test", "198.51.100.1", "100"),
                                      ("peer-test", "192.0.2.1", "600"),
                                      ("usb-test", "192.0.2.1", "700")]:
            run("ip", "route", "add", "default", "via", gateway, "dev", name, "metric", metric)
        run("ip", "route", "add", "203.0.113.0/24", "dev", "wired-test", "table", "30000")
        before = run("ip", "-j", "route", "show", "table", "main")
        foreign = run("ip", "-j", "route", "show", "table", "30000")
        app.reconcile_routes(system, {"usb-test"})
        state = system.read_json(app.RUNTIME, {})
        assert set(state["routes"]) == {"usb-test", "peer-test"}
        assert all(r["table"] != 30000 for r in state["routes"].values())
        lookup = json.loads(run("ip", "-j", "route", "get", "203.0.113.8", "from", "192.0.2.10"))
        assert lookup[0]["dev"] == "usb-test", lookup
        assert Path("/proc/sys/net/ipv4/conf/usb-test/arp_ignore").read_text().strip() == "1"
        assert run("ip", "-j", "route", "show", "table", "main") == before
        app.reconcile_routes(system, {"usb-test"})  # idempotent
        rules = json.loads(run("ip", "-j", "rule"))
        assert len([r for r in rules if 20000 <= r["priority"] < 20064]) == 2
        app.cleanup_routes(system, state)
        assert run("ip", "-j", "route", "show", "table", "30000") == foreign
        assert run("ip", "-j", "route", "show", "table", "main") == before
        assert len(json.loads(run("ip", "-j", "rule"))) == 3
        assert Path("/proc/sys/net/ipv4/conf/usb-test/arp_ignore").read_text().strip() == "0"
        print("PASS: real source routing, ARP sysctls, idempotence, exact cleanup, foreign table and main default preserved")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        inside()
        return
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run on Linux as root; requires network namespace support.")
    name = "rtw-test-" + uuid.uuid4().hex[:8]
    run("ip", "netns", "add", name)
    try:
        proc = subprocess.run(["ip", "netns", "exec", name, sys.executable, __file__, "--inside"], check=False)
        if proc.returncode:
            raise SystemExit(proc.returncode)
    finally:
        run("ip", "netns", "del", name)


if __name__ == "__main__":
    main()
