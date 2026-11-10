# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ljc
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("setup_component", Path(__file__).parents[1] / "rtl8822bu_setup.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class FakeSystem(app.System):
    def __init__(self, root):
        super().__init__(root)
        self.commands = []
        self.profile_value = "enable"
        self.profile_exists = True
        self.rules = [{"priority": 0, "src": "all", "table": "local"},
                      {"priority": 32766, "src": "all", "table": "main"},
                      {"priority": 32767, "src": "all", "table": "default"}]
        self.routes = [{"dst": "192.0.2.0/24", "dev": "usb-test", "prefsrc": "192.0.2.10", "scope": "link"},
                       {"dst": "default", "gateway": "192.0.2.1", "dev": "usb-test", "metric": 700},
                       {"dst": "192.0.2.0/24", "dev": "peer-test", "prefsrc": "192.0.2.11", "scope": "link"},
                       {"dst": "default", "gateway": "192.0.2.1", "dev": "peer-test", "metric": 600}]

    def has_command(self, name):
        return True

    def run(self, args, check=True, timeout=15):
        self.commands.append(list(args))
        code, out = 0, ""
        if args[:2] == ["ethtool", "-P"]:
            out = "Permanent address: 02:00:00:00:00:10\n"
        elif args[0] == "hciconfig":
            out = "hci7: BD Address: 02:00:00:00:00:20\n UP RUNNING\n"
        elif args[:3] == ["systemctl", "is-active", "NetworkManager"]:
            out = "active\n"
        elif args[:4] == ["nmcli", "-g", "GENERAL.CON-UUID", "dev"]:
            out = "11111111-2222-3333-4444-555555555555\n"
        elif args[:3] == ["nmcli", "-g", "802-11-wireless.powersave"]:
            out = self.profile_value
            if not self.profile_exists:
                code = 10
        elif args[:3] == ["nmcli", "con", "mod"]:
            self.profile_value = args[-1]
        elif args[:5] == ["ip", "-j", "-4", "addr", "show"]:
            out = json.dumps([{"ifname": name, "addr_info": [{"family": "inet", "scope": "global", "local": addr, "prefixlen": 24}]}
                              for name, addr in [("usb-test", "192.0.2.10"), ("peer-test", "192.0.2.11")]])
        elif args[:5] == ["ip", "-j", "-4", "rule", "show"]:
            out = json.dumps(self.rules)
        elif args[:5] == ["ip", "-j", "-4", "route", "show"]:
            out = json.dumps(self.routes)
        return subprocess.CompletedProcess(args, code, out, "")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.system = FakeSystem(Path(self.temp.name))
        self.usb = self.system.path("/sys/devices/pci-test/usb9/9-2")
        self.usb.mkdir(parents=True)
        for key, value in {"idVendor": "0bda", "idProduct": "b82c", "speed": "5000", "power/control": "auto", "power/autosuspend_delay_ms": "2000"}.items():
            self.write(self.usb / key, value)
        bus = self.system.path("/sys/bus/usb/devices")
        bus.mkdir(parents=True)
        (bus / "9-2").symlink_to(self.usb)
        self.make_iface("usb-test", self.usb / "9-2:1.2", "rtw88_8822bu")
        self.make_iface("peer-test", self.system.path("/sys/devices/pci-test/internal"), "mt7921e")
        bt = self.system.path("/sys/class/bluetooth/hci7")
        bt.mkdir(parents=True)
        parent = self.usb / "9-2:1.0"
        parent.mkdir(exist_ok=True)
        (bt / "device").symlink_to(parent)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def make_iface(self, name, device, driver):
        device.mkdir(parents=True, exist_ok=True)
        driver_path = self.system.path("/sys/bus/drivers/" + driver)
        driver_path.mkdir(parents=True, exist_ok=True)
        (device / "driver").symlink_to(driver_path)
        path = self.system.path("/sys/class/net/" + name)
        (path / "wireless").mkdir(parents=True)
        (path / "device").symlink_to(device)
        self.write(path / "type", "1")
        self.write(path / "address", "02:00:00:00:00:10" if name == "usb-test" else "02:00:00:00:00:11")
        for key in ("arp_ignore", "arp_announce"):
            self.write(self.system.path(f"/proc/sys/net/ipv4/conf/{name}/{key}"), "0")

    def test_discovers_usb_and_controller_by_ancestry_not_name(self):
        data = app.inventory(self.system)
        self.assertEqual([i["name"] for i in data["wifi_interfaces"]], ["usb-test"])
        self.assertEqual([c["name"] for c in data["bluetooth_controllers"]], ["hci7"])
        self.assertTrue(data["bluetooth_controllers"][0]["up"])

    def test_same_label_other_usb_id_is_not_targeted(self):
        (self.usb / "idProduct").write_text("ffff")
        self.assertFalse(app.inventory(self.system)["wifi_interfaces"])
        self.assertFalse(app.inventory(self.system)["bluetooth_controllers"])

    def test_usb_absent_does_not_target_internal(self):
        (self.system.path("/sys/class/net/usb-test") / "device").unlink()
        self.assertFalse(app.inventory(self.system)["wifi_interfaces"])

    def test_firmware_compression_is_accepted(self):
        path = self.system.path("/lib/firmware/rtw88/rtw8822b_fw.bin.zst")
        self.write(path, "fixture")
        self.assertTrue(app.firmware_found(self.system, "rtw88/rtw8822b_fw.bin"))

    def test_payload_power_only_never_writes_networkmanager(self):
        files, config = app.payloads(b"source", True, False)
        self.assertNotIn(app.DISPATCHER, files)
        self.assertNotIn(app.NM_DEFAULTS, files)
        self.assertFalse(config["multihoming"])

    def test_defaults_use_target_permanent_mac_not_interface_or_all_drivers(self):
        value = app.connection_defaults(app.inventory(self.system)["wifi_interfaces"])
        self.assertIn(b"mac:02:00:00:00:00:10", value)
        self.assertNotIn(b"driver:", value)
        self.assertNotIn(b"usb-test", value)

    def test_no_device_defaults_do_not_match_unrelated_hardware(self):
        self.assertNotIn(b"match-device", app.connection_defaults([]))

    def test_profiles_restore_original_without_secrets(self):
        app.tune_profile(self.system, "usb-test")
        self.assertEqual(self.system.profile_value, "2")
        app.tune_profile(self.system, "usb-test")
        self.assertEqual(list(self.system.read_json(app.PROFILES, {}).values()), [3])
        app.restore_profiles(self.system)
        self.assertEqual(self.system.profile_value, "3")
        self.assertNotIn("--show-secrets", repr(self.system.commands))

    def test_profile_owner_change_is_preserved(self):
        app.tune_profile(self.system, "usb-test")
        self.system.profile_value = "ignore"
        app.restore_profiles(self.system)
        self.assertEqual(self.system.profile_value, "ignore")

    def test_deleted_profile_is_not_recreated(self):
        app.tune_profile(self.system, "usb-test")
        self.system.profile_exists = False
        count = len(self.system.commands)
        app.restore_profiles(self.system)
        self.assertFalse(any(c[:3] == ["nmcli", "con", "mod"] for c in self.system.commands[count:]))

    def test_powersave_parser_does_not_accept_invalid_prefix(self):
        self.assertEqual(app.powersave_value("2 (disable)"), 2)
        self.assertEqual(app.powersave_value("disable"), 2)
        with self.assertRaises(app.SetupError):
            app.powersave_value("20")

    def test_bluetooth_100_percent_loss_is_not_mistaken_for_zero(self):
        bad = subprocess.CompletedProcess([], 0, "3 sent, 0 received, 100% loss", "")
        self.assertFalse(app.l2ping_passed(bad))
        good = subprocess.CompletedProcess([], 0, "3 sent, 3 received, 0% loss", "")
        self.assertTrue(app.l2ping_passed(good))
        empty = subprocess.CompletedProcess([], 0, "0 sent, 0 received, 0% loss", "")
        self.assertFalse(app.l2ping_passed(empty))

    def test_http_read_timeout_does_not_falsely_report_tls_failure(self):
        class Socket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, *args): pass
            def setsockopt(self, *args): pass
            def bind(self, *args): pass
            def sendto(self, *args): pass
            def connect(self, *args): pass
            def close(self): pass
            def recvfrom(self, *args):
                return struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0), ("192.0.2.53", 53)
        class TLS(Socket):
            def sendall(self, *args): pass
            def recv(self, *args): raise TimeoutError("simulated HTTP read timeout")
        import ssl
        output = io.StringIO()
        with patch.object(app.socket, "socket", return_value=Socket()), \
                patch.object(app.socket, "SO_BINDTODEVICE", 25, create=True), \
                patch.object(app.socket, "getaddrinfo", return_value=[(None, None, None, None, ("192.0.2.99", 443))]), \
                patch.object(app.os, "urandom", return_value=b"\x12\x34"), \
                patch.object(ssl, "create_default_context") as context, \
                contextlib.redirect_stdout(output):
            context.return_value.wrap_socket.return_value = TLS()
            self.assertFalse(app.verify(self.system, "192.0.2.53", "example.com", None))
        row = json.loads(output.getvalue())[0]
        self.assertTrue(row["dns_ok"])
        self.assertTrue(row["tcp_ok"])
        self.assertTrue(row["tls_ok"])
        self.assertFalse(row["http_received"])
        self.assertEqual(row["phase"], "http")

    def test_file_backup_is_idempotent_and_restores_mode(self):
        target = "/etc/test.conf"
        self.write(self.system.path(target), "original")
        self.system.path(target).chmod(0o640)
        files = {target: (b"installed", 0o644)}
        for _ in range(2):
            app.deploy_files(self.system, files, {"mode": "test"})
        manifest = self.system.read_json(app.MANIFEST, {})
        self.assertEqual(app.restore_files(self.system, manifest), [])
        self.assertEqual(self.system.path(target).read_text(), "original")
        self.assertEqual(self.system.path(target).stat().st_mode & 0o777, 0o640)

    def test_external_edit_is_never_overwritten_or_removed(self):
        files = {"/etc/test.conf": (b"installed", 0o644)}
        app.deploy_files(self.system, files, {})
        self.system.path("/etc/test.conf").write_text("owner edit")
        with self.assertRaises(app.SetupError):
            app.deploy_files(self.system, files, {})
        self.assertEqual(app.restore_files(self.system, self.system.read_json(app.MANIFEST, {})), ["/etc/test.conf"])
        self.assertEqual(self.system.path("/etc/test.conf").read_text(), "owner edit")

    def test_symlink_destination_is_refused(self):
        self.write(self.system.path("/etc/real"), "keep")
        self.system.path("/etc/link").symlink_to(self.system.path("/etc/real"))
        with self.assertRaises(app.SetupError):
            app.deploy_files(self.system, {"/etc/link": (b"bad", 0o644)}, {})
        self.assertEqual(self.system.path("/etc/real").read_text(), "keep")

    def test_partial_file_failure_rolls_back(self):
        self.write(self.system.path("/etc/first"), "before")
        write = self.system.write
        raised = [False]
        def fail_once(name, data, mode=0o600):
            if name == "/etc/second" and not raised[0]:
                raised[0] = True
                raise OSError("simulated full filesystem")
            return write(name, data, mode)
        with patch.object(self.system, "write", side_effect=fail_once):
            with self.assertRaises(OSError):
                app.deploy_files(self.system, {"/etc/first": (b"after", 0o644), "/etc/second": (b"new", 0o644)}, {})
        self.assertEqual(self.system.path("/etc/first").read_text(), "before")
        self.assertFalse(self.system.path("/etc/second").exists())

    def options(self, **overrides):
        values = dict(dry_run=False, power_only=False, multihoming=False, install_deps=False)
        return SimpleNamespace(**{**values, **overrides})

    def prepare_firmware(self):
        for name in app.FIRMWARE:
            self.write(self.system.path("/lib/firmware/" + name + ".zst"), "fixture")

    def test_full_install_reinstall_uninstall_restores_power_and_profile(self):
        self.prepare_firmware()
        app.install(self.system, self.options())
        app.install(self.system, self.options())
        self.assertEqual((self.usb / "power/autosuspend_delay_ms").read_text(), "-1")
        self.assertEqual(self.system.profile_value, "2")
        self.assertTrue(self.system.path(app.DISPATCHER).exists())
        app.uninstall(self.system)
        self.assertEqual((self.usb / "power/autosuspend_delay_ms").read_text(), "2000")
        self.assertEqual((self.usb / "power/control").read_text(), "auto")
        self.assertEqual(self.system.profile_value, "3")
        self.assertFalse(self.system.path(app.DISPATCHER).exists())
        self.assertFalse(self.system.path(app.MANIFEST).exists())

    def test_dry_run_has_no_file_profile_or_power_mutations(self):
        app.install(self.system, self.options(dry_run=True))
        self.assertFalse(self.system.path(app.MANIFEST).exists())
        self.assertEqual((self.usb / "power/control").read_text(), "auto")
        self.assertEqual(self.system.profile_value, "enable")
        self.assertFalse(any(c[:3] == ["nmcli", "con", "mod"] for c in self.system.commands))

    def test_legacy_manager_prevents_layered_installation(self):
        self.prepare_firmware()
        self.write(self.system.path(app.LEGACY[0]), "old manager")
        with self.assertRaises(app.SetupError):
            app.install(self.system, self.options())
        self.assertFalse(self.system.path(app.MANIFEST).exists())

    def test_missing_firmware_stops_before_installation(self):
        with self.assertRaises(app.SetupError):
            app.install(self.system, self.options())
        self.assertFalse(self.system.path(app.DISPATCHER).exists())

    def test_modern_bluez_state_does_not_require_hciconfig(self):
        run = self.system.run
        def modern(args, check=True, timeout=15):
            if args[0] == "busctl":
                text = 's "02:00:00:00:00:20"' if args[-1] == "Address" else "b true"
                return subprocess.CompletedProcess(args, 0, text, "")
            return run(args, check, timeout)
        with patch.object(self.system, "run", side_effect=modern):
            data = app.inventory(self.system)
        self.assertEqual(data["bluetooth_controllers"][0]["state_source"], "BlueZ D-Bus")

    def test_overlap_is_detected_only_from_target_network(self):
        topology = {"usb": ["192.0.2.10/24"], "peer": ["192.0.2.11/24"], "other": ["198.51.100.1/24"]}
        self.assertEqual(app.overlapping_interfaces(topology, {"usb"}), {"usb", "peer"})
        self.assertEqual(app.overlapping_interfaces(topology, set()), set())

    def test_resource_allocation_avoids_foreign_rules_routes_and_names(self):
        pair = app.choose_resources([{"priority": 20000}], [{"table": 30001}], {30002}, set())
        self.assertEqual(pair, (30003, 20003))

    def test_complex_routes_are_refused(self):
        with self.assertRaises(app.SetupError):
            app.route_spec({"dev": "usb-test", "nexthops": []}, 30000)

    def test_multihoming_refuses_foreign_policy_before_mutation(self):
        self.system.rules.append({"priority": 100, "src": "all", "table": "vpn"})
        with self.assertRaises(app.SetupError):
            app.reconcile_routes(self.system, {"usb-test"})
        self.assertEqual(self.system.read_json(app.RUNTIME, {}), {})
        self.assertEqual(self.system.path("/proc/sys/net/ipv4/conf/usb-test/arp_ignore").read_text(), "0")

    def test_cleanup_deletes_full_specs_never_flushes_tables(self):
        state = {"routes": {"usb-test": {"table": 30000, "priority": 20000,
                 "sources": ["192.0.2.10/32"], "specs": [["192.0.2.0/24", "dev", "usb-test", "table", "30000"]]}}, "arp": {}}
        app.cleanup_routes(self.system, state)
        self.assertIn(["ip", "-4", "rule", "del", "priority", "20000", "from", "192.0.2.10/32", "table", "30000"], self.system.commands)
        self.assertNotIn("flush", repr(self.system.commands))

    def test_multihoming_apply_then_noop_does_not_duplicate_rules(self):
        with patch.object(app, "announce"):
            app.reconcile_routes(self.system, {"usb-test"})
            first = len(self.system.commands)
            app.reconcile_routes(self.system, {"usb-test"})
        self.assertEqual(self.system.path("/proc/sys/net/ipv4/conf/usb-test/arp_ignore").read_text(), "1")
        self.assertFalse(any(c[:4] == ["ip", "-4", "rule", "add"] for c in self.system.commands[first:]))
        state = self.system.read_json(app.RUNTIME, {})
        app.cleanup_routes(self.system, state)
        self.assertEqual(self.system.path("/proc/sys/net/ipv4/conf/usb-test/arp_ignore").read_text(), "0")

    def test_virtual_overlap_requires_manual_review(self):
        self.system.path("/sys/class/net/peer-test/device").unlink()
        with self.assertRaises(app.SetupError):
            app.reconcile_routes(self.system, {"usb-test"})

    def test_new_vpn_rule_cleans_owned_routes_instead_of_noop(self):
        with patch.object(app, "announce"):
            app.reconcile_routes(self.system, {"usb-test"})
        self.system.rules.append({"priority": 9999, "src": "all", "table": "vpn"})
        with self.assertRaises(app.SetupError):
            app.reconcile_routes(self.system, {"usb-test"})
        self.assertEqual(self.system.read_json(app.RUNTIME, {}), {})
        self.assertEqual(self.system.path("/proc/sys/net/ipv4/conf/usb-test/arp_ignore").read_text(), "0")


if __name__ == "__main__":
    unittest.main()
