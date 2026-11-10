#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ljc
"""Targeted RTL8822BU policy installation. No kernel or firmware is bundled."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

VERSION = "0.1.0"
USB_IDS = {("0bda", "b82c")}
NATIVE_DRIVERS = {"rtw88_8822bu", "rtw_8822bu"}
HELPER = "/usr/local/libexec/rtl8822bu-setup"
CONFIG = "/etc/rtl8822bu-setup/config.json"
MANIFEST = "/var/lib/rtl8822bu-setup/install.json"
PROFILES = "/var/lib/rtl8822bu-setup/profiles.json"
RUNTIME = "/run/rtl8822bu-setup/runtime.json"
RULES = "/etc/udev/rules.d/81-rtl8822bu-setup.rules"
DISPATCHER = "/etc/NetworkManager/dispatcher.d/85-rtl8822bu-setup"
NM_DEFAULTS = "/etc/NetworkManager/conf.d/81-rtl8822bu-setup.conf"
LEGACY = ("/etc/NetworkManager/dispatcher.d/85-12800h-wireless-multihoming",
          "/etc/udev/rules.d/81-rtl8822bu-power.rules")
FIRMWARE = ("rtw88/rtw8822b_fw.bin", "rtl_bt/rtl8822b_fw.bin",
            "rtl_bt/rtl8822b_config.bin")


class SetupError(RuntimeError):
    pass


class System:
    def __init__(self, root=Path("/")):
        self.root = Path(root)

    def path(self, name):
        return self.root / str(name).lstrip("/")

    def has_command(self, name):
        return shutil.which(name) is not None

    def run(self, args, check=True, timeout=15):
        try:
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"} if args[0] == "apt-get" else None
            proc = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if check:
                raise SetupError(f"{args[0]}: {exc}") from exc
            return subprocess.CompletedProcess(args, 127, "", str(exc))
        if check and proc.returncode:
            # Commands below never contain passwords or connection secrets.
            raise SetupError(f"{' '.join(args)}: {proc.stderr.strip()}")
        return proc

    def read_json(self, name, default):
        path = self.path(name)
        return json.loads(path.read_text()) if path.exists() else default

    def write(self, name, data, mode=0o600):
        path = self.path(name)
        if path.is_symlink():
            raise SetupError(f"Refusing symlink: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".rtl8822bu-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.chmod(temp, mode)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def json_write(self, name, data):
        self.write(name, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode())


def read_text(path, default=""):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return default


def usb_parent(path, system):
    """Follow physical sysfs ancestry, never guess from interface names."""
    path = path.resolve()
    for parent in [path, *path.parents]:
        if (read_text(parent / "idVendor").lower(),
                read_text(parent / "idProduct").lower()) in USB_IDS:
            return parent
    return None


def inventory(system):
    devices, interfaces, controllers = [], [], []
    usb_root = system.path("/sys/bus/usb/devices")
    for path in sorted(usb_root.glob("*")):
        if (read_text(path / "idVendor").lower(),
                read_text(path / "idProduct").lower()) not in USB_IDS:
            continue
        devices.append({"path": str(path.resolve()), "id": "0bda:b82c",
                        "speed_mbps": read_text(path / "speed"),
                        "control": read_text(path / "power/control"),
                        "autosuspend_delay_ms": read_text(path / "power/autosuspend_delay_ms")})
    for path in sorted(system.path("/sys/class/net").glob("*")):
        if not (path / "wireless").exists() and not (path / "phy80211").exists():
            continue
        parent = usb_parent(path / "device", system)
        if parent is None:
            continue
        driver = path / "device/driver"
        permanent = system.run(["ethtool", "-P", path.name], False).stdout
        permanent_match = re.search(r"Permanent address:\s*([0-9a-f:]{17})", permanent, re.I)
        interfaces.append({"name": path.name,
                           "mac": read_text(path / "address"),
                           "permanent_mac": permanent_match.group(1).lower() if permanent_match else None,
                           "driver": driver.resolve().name if driver.exists() else "",
                           "usb_path": str(parent)})
    for path in sorted(system.path("/sys/class/bluetooth").glob("hci*")):
        parent = usb_parent(path / "device", system)
        if parent is not None:
            prefix = ["busctl", "--system", "--auto-start=no", "get-property", "org.bluez",
                      "/org/bluez/" + path.name, "org.bluez.Adapter1"]
            address = system.run([*prefix, "Address"], False)
            powered = system.run([*prefix, "Powered"], False)
            match = re.search(r"([0-9A-F:]{17})", address.stdout, re.I)
            up, state_source = None, "unavailable"
            if match and powered.stdout.strip() in ("b true", "b false"):
                up, state_source = powered.stdout.strip() == "b true", "BlueZ D-Bus"
            else:
                proc = system.run(["hciconfig", path.name], check=False)
                match = re.search(r"BD Address:\s*([0-9A-F:]{17})", proc.stdout, re.I)
                if proc.returncode == 0 and match:
                    up, state_source = bool(re.search(r"\bUP RUNNING\b", proc.stdout)), "hciconfig"
            controllers.append({"name": path.name, "usb_path": str(parent),
                                "address": match.group(1) if match else None,
                                "up": up, "state_source": state_source})
    return {"usb_devices": devices, "wifi_interfaces": interfaces,
            "bluetooth_controllers": controllers}


def firmware_found(system, relative):
    return any(system.path("/lib/firmware/" + relative + suffix).is_file()
               for suffix in ("", ".zst", ".xz", ".gz"))


def doctor(system):
    data = inventory(system)
    data.update({"component_version": VERSION, "kernel": platform.release(),
                 "os_release": read_text(system.path("/etc/os-release")),
                 "networkmanager_active": system.run(
                     ["systemctl", "is-active", "NetworkManager"], False).stdout.strip() == "active",
                 "native_wifi_module": system.run(
                     ["modinfo", "rtw88_8822bu"], False).returncode == 0,
                 "btusb_module": system.run(["modinfo", "btusb"], False).returncode == 0,
                 "firmware": {name: firmware_found(system, name) for name in FIRMWARE},
                 "legacy_configs": [p for p in LEGACY if system.path(p).exists()],
                 "installed": system.path(MANIFEST).exists()})
    return data


def connection_defaults(interfaces):
    matches = sorted({i["permanent_mac"] for i in interfaces
                      if i.get("permanent_mac") and i["permanent_mac"] != "00:00:00:00:00:00"})
    if not matches:
        return b"# Managed by rtl8822bu-setup; no matching USB permanent MAC discovered yet.\n"
    return ("# Managed by rtl8822bu-setup\n[connection-rtl8822bu-setup]\nmatch-device="
            + ";".join("mac:" + m for m in matches) + "\nwifi.powersave=2\n").encode()


def payloads(source, power_only, multihoming, interfaces=()):
    config = {"version": VERSION, "power_only": power_only,
              "multihoming": multihoming}
    udev = ('# Managed by rtl8822bu-setup\n'
            'ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", '
            'ATTR{idProduct}=="b82c", TEST=="power/control", '
            'ATTR{power/autosuspend_delay_ms}="-1", ATTR{power/control}="on"\n')
    files = {HELPER: (source, 0o755), RULES: (udev.encode(), 0o644),
             CONFIG: ((json.dumps(config, indent=2) + "\n").encode(), 0o600)}
    if not power_only:
        files[NM_DEFAULTS] = (connection_defaults(interfaces), 0o644)
        hook = ('#!/bin/sh\n# SPDX-License-Identifier: MIT\n'
                '# Copyright (c) 2026 ljc\n'
                'case "$2" in up|down|dhcp4-change|reapply|vpn-up|vpn-down|connectivity-change)\n'
                f'  exec {HELPER} event "$1" "$2" ;;\n'
                'esac\n')
        files[DISPATCHER] = (hook.encode(), 0o755)
    return files, config


def file_hash(data):
    return hashlib.sha256(data).hexdigest()


def deploy_files(system, files, config):
    previous = system.read_json(MANIFEST, None)
    if previous and previous["options"] != config:
        raise SetupError("Installed options differ; uninstall before changing modes.")
    snapshots = {}
    for name in files:
        path = system.path(name)
        if path.is_symlink():
            raise SetupError(f"Refusing symlink: {name}")
        if path.exists() and not path.is_file():
            raise SetupError(f"Not a regular file: {name}")
        data = path.read_bytes() if path.exists() else None
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        snapshots[name] = (data, mode)
        if previous:
            expected = previous["files"].get(name, {}).get("sha256")
            if expected and (data is None or file_hash(data) != expected):
                raise SetupError(f"Installed file was changed externally: {name}")
    entries = {}
    try:
        for name, (data, mode) in files.items():
            old, old_mode = snapshots[name]
            backup = previous["files"][name] if previous else {
                "original": base64.b64encode(old).decode() if old is not None else None,
                "original_mode": old_mode}
            entries[name] = {**backup, "sha256": file_hash(data)}
            system.write(name, data, mode)
        system.json_write(MANIFEST, {"version": VERSION, "options": config, "files": entries})
    except Exception:
        for name, (data, mode) in snapshots.items():
            if data is None:
                system.path(name).unlink(missing_ok=True)
            else:
                system.write(name, data, mode)
        raise


def restore_files(system, manifest):
    changed = []
    for name, entry in manifest["files"].items():
        path = system.path(name)
        if path.is_symlink() or (path.exists() and file_hash(path.read_bytes()) != entry["sha256"]):
            changed.append(name)
            continue
        if entry["original"] is None:
            path.unlink(missing_ok=True)
        else:
            system.write(name, base64.b64decode(entry["original"]), entry["original_mode"])
    return changed


def powersave_value(text):
    value = text.strip().lower()
    for word, number in {"default": 0, "ignore": 1, "disable": 2, "enable": 3}.items():
        if value == word or re.match(r"^" + str(number) + r"(?:\s|$)", value):
            return number
    raise SetupError("Unknown NetworkManager powersave value: " + value)


def tune_profile(system, iface):
    uuid = system.run(["nmcli", "-g", "GENERAL.CON-UUID", "dev", "show", iface], False).stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return
    value = powersave_value(system.run(
        ["nmcli", "-g", "802-11-wireless.powersave", "con", "show", "uuid", uuid]).stdout)
    if value != 2:
        saved = system.read_json(PROFILES, {})
        saved.setdefault(uuid, value)
        system.json_write(PROFILES, saved)
        system.run(["nmcli", "con", "mod", "uuid", uuid, "802-11-wireless.powersave", "2"])
    system.run(["iw", "dev", iface, "set", "power_save", "off"])


def restore_profiles(system):
    for uuid, value in system.read_json(PROFILES, {}).items():
        proc = system.run(["nmcli", "-g", "802-11-wireless.powersave", "con", "show", "uuid", uuid], False)
        if proc.returncode:
            continue  # Profile was removed by its owner.
        if powersave_value(proc.stdout) == 2:
            system.run(["nmcli", "con", "mod", "uuid", uuid, "802-11-wireless.powersave", str(value)])


def ipv4_topology(system):
    rows = json.loads(system.run(["ip", "-j", "-4", "addr", "show"]).stdout)
    result = {}
    for row in rows:
        name = row["ifname"]
        if read_text(system.path(f"/sys/class/net/{name}/type")) != "1":
            continue
        addresses = [f"{a['local']}/{a['prefixlen']}" for a in row.get("addr_info", [])
                     if a.get("scope") == "global" and a.get("family") == "inet"]
        if addresses:
            result[name] = addresses
    return result


def overlapping_interfaces(topology, targets):
    selected = set()
    for target in targets:
        for address in topology.get(target, []):
            net = ipaddress.ip_interface(address).network
            for peer, addresses in topology.items():
                if peer != target and any(net.overlaps(ipaddress.ip_interface(a).network) for a in addresses):
                    selected.update((target, peer))
    return selected


def route_spec(route, table):
    if any(key in route for key in ("nexthops", "multipath", "nhid", "encap")):
        raise SetupError("Complex routes require manual review; multihoming was not applied.")
    if route.get("type", "unicast") != "unicast":
        raise SetupError("Non-unicast interface route requires manual review.")
    args = [route.get("dst", "default")]
    if route.get("gateway"):
        args += ["via", route["gateway"]]
    args += ["dev", route["dev"]]
    if route.get("prefsrc"):
        args += ["src", route["prefsrc"]]
    if route.get("scope") in ("link", "host"):
        args += ["scope", route["scope"]]
    if "metric" in route:
        args += ["metric", str(route["metric"])]
    if "onlink" in route.get("flags", []):
        args += ["onlink"]
    return args + ["table", str(table)]


def choose_resources(rules, routes, reserved_tables, used_pairs):
    used_priorities = {int(r.get("priority", -1)) for r in rules}
    used_tables = {str(r.get("table", "main")) for r in routes} | {str(t) for t in reserved_tables}
    for offset in range(64):
        table, priority = 30000 + offset, 20000 + offset
        if priority not in used_priorities and str(table) not in used_tables and (table, priority) not in used_pairs:
            return table, priority
    raise SetupError("No unused policy-routing slot available; nothing was overwritten.")


def reserved_tables(system):
    paths = [system.path("/etc/iproute2/rt_tables"), system.path("/usr/lib/iproute2/rt_tables"),
             *system.path("/etc/iproute2/rt_tables.d").glob("*.conf"),
             *system.path("/usr/lib/iproute2/rt_tables.d").glob("*.conf")]
    return {int(line.split()[0]) for path in paths for line in read_text(path).splitlines()
            if line.strip() and not line.lstrip().startswith("#") and line.split()[0].isdigit()}


def cleanup_routes(system, state):
    """Delete only complete rule/route specifications created by this component."""
    for record in state.get("routes", {}).values():
        for source in record["sources"]:
            system.run(["ip", "-4", "rule", "del", "priority", str(record["priority"]),
                        "from", source, "table", str(record["table"])], False)
        for spec in reversed(record["specs"]):
            system.run(["ip", "-4", "route", "del", *spec], False)
    for iface, record in state.get("arp", {}).items():
        if read_text(system.path(f"/sys/class/net/{iface}/address")) != record["mac"]:
            continue
        for key, old in record["values"].items():
            path = system.path(f"/proc/sys/net/ipv4/conf/{iface}/{key}")
            if path.exists() and read_text(path) == {"arp_ignore": "1", "arp_announce": "2"}[key]:
                path.write_text(old)


def announce(system, iface, addresses):
    mac = bytes.fromhex(read_text(system.path(f"/sys/class/net/{iface}/address")).replace(":", ""))
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806)) as raw:
        raw.bind((iface, 0))
        for address in addresses:
            ip = socket.inet_aton(str(ipaddress.ip_interface(address).ip))
            arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1) + mac + ip + bytes(6) + ip
            frame = bytes.fromhex("ffffffffffff") + mac + struct.pack("!H", 0x0806) + arp
            for _ in range(2):
                raw.send(frame)
                time.sleep(0.03)


def reconcile_routes(system, targets):
    old = system.read_json(RUNTIME, {})
    topology = ipv4_topology(system)
    selected = overlapping_interfaces(topology, targets)
    main = json.loads(system.run(["ip", "-j", "-4", "route", "show", "table", "main"]).stdout)
    signature = {n: {"addresses": topology[n], "routes": [r for r in main if r.get("dev") == n]}
                 for n in sorted(selected)}
    rules = json.loads(system.run(["ip", "-j", "-4", "rule", "show"]).stdout)
    def source_key(value):
        return str(ipaddress.ip_network(value, strict=False)) if value != "all" else "all"
    owned = {(int(r["priority"]), str(r["table"]), source_key(source))
             for r in old.get("routes", {}).values() for source in r["sources"]}
    foreign = [r for r in rules if int(r["priority"]) not in (0, 32766, 32767)
               and (int(r["priority"]), str(r.get("table")), source_key(r.get("src", "all"))) not in owned]
    if selected and foreign:
        # A VPN may have appeared after installation. Stop owning source rules
        # rather than letting an old no-op signature bypass the new policy.
        cleanup_routes(system, old)
        system.json_write(RUNTIME, {})
        raise SetupError("Existing custom policy rules (VPN/another manager); review before enabling multihoming.")
    if old.get("signature") == signature:
        return
    for name in selected:
        device = system.path(f"/sys/class/net/{name}/device")
        if not device.exists():
            raise SetupError("Overlapping virtual/bridge interface; automatic routing refused: " + name)
    # Validate every route before removing any existing owned state.
    for entry in signature.values():
        for route in entry["routes"]:
            route_spec(route, 30000)
    cleanup_routes(system, old)
    state = {"signature": signature, "routes": {}, "arp": {}}
    system.json_write(RUNTIME, state)
    try:
        rules = json.loads(system.run(["ip", "-j", "-4", "rule", "show"]).stdout)
        routes = json.loads(system.run(["ip", "-j", "-4", "route", "show", "table", "all"]).stdout)
        used_pairs = set()
        for iface in sorted(selected):
            table, priority = choose_resources(rules, routes, reserved_tables(system), used_pairs)
            used_pairs.add((table, priority))
            originals = {key: read_text(system.path(f"/proc/sys/net/ipv4/conf/{iface}/{key}"))
                         for key in ("arp_ignore", "arp_announce")}
            state["arp"][iface] = {"mac": read_text(system.path(f"/sys/class/net/{iface}/address")), "values": originals}
            for key, value in (("arp_ignore", "1"), ("arp_announce", "2")):
                system.path(f"/proc/sys/net/ipv4/conf/{iface}/{key}").write_text(value)
            record = {"table": table, "priority": priority, "sources": [], "specs": []}
            state["routes"][iface] = record
            system.json_write(RUNTIME, state)
            # Connected routes first so gateways are reachable in this table.
            for route in sorted(signature[iface]["routes"], key=lambda r: r.get("dst", "default") == "default"):
                spec = route_spec(route, table)
                system.run(["ip", "-4", "route", "add", *spec])
                record["specs"].append(spec)
                system.json_write(RUNTIME, state)
            for addr in topology[iface]:
                source = str(ipaddress.ip_interface(addr).ip) + "/32"
                system.run(["ip", "-4", "rule", "add", "priority", str(priority), "from", source, "table", str(table)])
                record["sources"].append(source)
                system.json_write(RUNTIME, state)
            announce(system, iface, topology[iface])
    except Exception:
        cleanup_routes(system, state)
        system.json_write(RUNTIME, {})
        raise


def refresh(system):
    config = system.read_json(CONFIG, None)
    if config is None:
        raise SetupError("Component is not installed.")
    data = inventory(system)
    for device in data["usb_devices"]:
        path = Path(device["path"])
        for key, value in (("autosuspend_delay_ms", "-1"), ("control", "on")):
            (path / "power" / key).write_text(value)
    if config["power_only"]:
        return
    defaults = connection_defaults(data["wifi_interfaces"])
    path = system.path(NM_DEFAULTS)
    if data["wifi_interfaces"] and path.read_bytes() != defaults:
        manifest = system.read_json(MANIFEST, {})
        if file_hash(path.read_bytes()) != manifest["files"][NM_DEFAULTS]["sha256"]:
            raise SetupError("NetworkManager defaults were externally edited; automatic rewrite refused.")
        system.write(NM_DEFAULTS, defaults, 0o644)
        manifest["files"][NM_DEFAULTS]["sha256"] = file_hash(defaults)
        system.json_write(MANIFEST, manifest)
        system.run(["nmcli", "general", "reload", "conf"])
    for iface in data["wifi_interfaces"]:
        tune_profile(system, iface["name"])
    if config["multihoming"]:
        reconcile_routes(system, {i["name"] for i in data["wifi_interfaces"]})


def install(system, options):
    report = doctor(system)
    files, config = payloads(Path(__file__).read_bytes(), options.power_only, options.multihoming,
                             report["wifi_interfaces"])
    if options.dry_run:
        print(json.dumps({"dry_run": True, "write_paths": list(files), "options": config,
                          "hardware": report, "note": "No files, packages, profiles or routes were changed."},
                         indent=2, ensure_ascii=False))
        return
    if report["legacy_configs"]:
        raise SetupError("Existing host-specific policies found; do not layer two managers: " + ", ".join(report["legacy_configs"]))
    if not options.power_only and not report["networkmanager_active"]:
        raise SetupError("A running NetworkManager is required. Networkd/server users may explicitly use --power-only; no renderer is changed.")
    if options.multihoming and options.power_only:
        raise SetupError("--multihoming requires NetworkManager, not --power-only.")
    if options.install_deps:
        print("Installing distro prerequisites; NetworkManager/renderer will not be installed or switched.", flush=True)
        system.run(["apt-get", "update"], timeout=300)
        system.run(["apt-get", "install", "-y", "python3", "kmod", "iproute2", "iw", "bluez",
                    "linux-firmware", "usbutils", "ethtool", "rfkill"], timeout=600)
        report = doctor(system)
    required = ["ip", "iw", "modinfo", "udevadm", "systemctl", "ethtool"]
    if not options.power_only:
        required.append("nmcli")
    missing = [name for name in required if not system.has_command(name)]
    if missing:
        raise SetupError("Missing prerequisites: " + ", ".join(missing) + "; use --install-deps or install them with the distro package manager.")
    if not report["native_wifi_module"] or not report["btusb_module"]:
        raise SetupError("Native kernel modules missing. Install modules matching the running kernel; this component does not supply .ko files.")
    if not all(report["firmware"].values()):
        raise SetupError("Firmware missing; install/update linux-firmware (or use --install-deps), then recheck.")
    unexpected = [i["driver"] for i in report["wifi_interfaces"] if i["driver"] and i["driver"] not in NATIVE_DRIVERS]
    if unexpected:
        raise SetupError("An untested competing driver is bound: " + ", ".join(unexpected))
    power = {d["path"]: {"control": d["control"], "autosuspend_delay_ms": d["autosuspend_delay_ms"]}
             for d in report["usb_devices"]}
    prior = system.read_json(MANIFEST, None)
    deploy_files(system, files, config)
    manifest = system.read_json(MANIFEST, {})
    manifest["usb_originals"] = prior.get("usb_originals", power) if prior else power
    system.json_write(MANIFEST, manifest)
    system.run(["udevadm", "control", "--reload-rules"])
    if not options.power_only:
        system.run(["nmcli", "general", "reload", "conf"])
    try:
        refresh(system)
    except Exception:
        # Leave an accurate manifest so a partial runtime failure is reversible.
        print("Runtime application failed. Installed files are tracked; run uninstall.sh to roll back.", file=sys.stderr)
        raise
    print("Installed targeted USB policy. No drivers were replaced, no connections restarted, no reboot requested.")
    print("If the adapter was absent, plug it in and connect using the system's existing network manager.")


def uninstall(system):
    manifest = system.read_json(MANIFEST, None)
    if manifest is None:
        print("Not installed; nothing changed.")
        return
    if system.read_json(PROFILES, {}) and system.run(
            ["systemctl", "is-active", "NetworkManager"], False).stdout.strip() != "active":
        raise SetupError("Start NetworkManager to restore profile values before uninstalling; backups were retained.")
    cleanup_routes(system, system.read_json(RUNTIME, {}))
    restore_profiles(system)
    changed = restore_files(system, manifest)
    if changed:
        raise SetupError("Externally edited files were preserved; manifest retained for review: " + ", ".join(changed))
    for path, values in manifest.get("usb_originals", {}).items():
        parent = Path(path)
        if usb_parent(parent, system) != parent or not parent.exists():
            continue
        for key, value in values.items():
            target = parent / "power" / key
            if target.exists() and value:
                target.write_text(value)
    system.run(["udevadm", "control", "--reload-rules"])
    if system.run(["systemctl", "is-active", "NetworkManager"], False).stdout.strip() == "active":
        system.run(["nmcli", "general", "reload", "conf"])
    for name in (MANIFEST, PROFILES, RUNTIME):
        system.path(name).unlink(missing_ok=True)
    print("Removed owned files/rules; profile values restored only if still equal to the installed value.")
    print("Replug the USB adapter to reset any live radio power state or newly enumerated device without an original snapshot.")


def l2ping_passed(proc):
    match = re.search(r"(\d+)\s+sent,\s*(\d+)\s+received,\s*([0-9.]+)%\s+loss", proc.stdout)
    return (proc.returncode == 0 and match is not None and int(match[1]) >= 3
            and match[1] == match[2] and float(match[3]) == 0)


def verify(system, dns_server, https_host, peer, timeout=10):
    import ssl
    dns_server = str(ipaddress.IPv4Address(dns_server))
    data = inventory(system)
    rows = []
    def bound(iface, address, kind):
        sock = socket.socket(socket.AF_INET, kind)
        sock.settimeout(timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, (iface + "\0").encode())
        sock.bind((address, 0))
        return sock
    topology = ipv4_topology(system)
    for iface in data["wifi_interfaces"]:
        name = iface["name"]
        row = {"interface": name, "dns_ok": False, "tcp_ok": False,
               "tls_ok": False, "http_received": False, "phase": "address"}
        try:
            addresses = topology.get(name, [])
            if not addresses:
                raise SetupError("No global IPv4 address: connect this adapter first.")
            address = str(ipaddress.ip_interface(addresses[0]).ip)
            row["phase"] = "dns"
            with bound(name, address, socket.SOCK_DGRAM) as sock:
                qid = int.from_bytes(os.urandom(2), "big")
                query = struct.pack("!HHHHHH", qid, 0x100, 1, 0, 0, 0) + b"\x07example\x03com\0" + struct.pack("!HH", 1, 1)
                sock.sendto(query, (dns_server, 53))
                packet, _ = sock.recvfrom(4096)
                header = struct.unpack("!HHHHHH", packet[:12])
                row["dns_ok"] = header[0] == qid and header[1] & 15 == 0 and header[3] > 0
            row["phase"] = "https-name-resolution"
            endpoint = socket.getaddrinfo(https_host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4]
            row["https_peer"] = endpoint[0]
            sock = bound(name, address, socket.SOCK_STREAM)
            try:
                row["phase"] = "tcp"
                sock.connect(endpoint)
                row["tcp_ok"] = True
                row["phase"] = "tls"
                with ssl.create_default_context().wrap_socket(sock, server_hostname=https_host) as tls:
                    row["tls_ok"] = True
                    row["phase"] = "http"
                    tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {https_host}\r\nConnection: close\r\n\r\n".encode())
                    row["http_status"] = tls.recv(4096).split(b"\r\n", 1)[0].decode("ascii", "replace")
                    row["http_received"] = row["http_status"].startswith("HTTP/")
                    row["phase"] = "done"
            finally:
                sock.close()
        except (OSError, SetupError, ValueError) as exc:
            row["error"] = str(exc)
        rows.append(row)
    for ctrl in data["bluetooth_controllers"]:
        row = {"bluetooth": ctrl["name"], "controller_up": ctrl["up"], "peer_tested": False}
        if peer:
            proc = system.run(["l2ping", "-i", ctrl["name"], "-c", "3", "-t", "2", "-d", "1", peer], False, 20)
            row.update(peer_tested=True, peer_ok=l2ping_passed(proc),
                       output=(proc.stdout + proc.stderr).strip())
        rows.append(row)
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    passed = (bool(data["wifi_interfaces"]) and bool(data["bluetooth_controllers"])
              and all(r.get("dns_ok") and r.get("tls_ok") and r.get("http_received") for r in rows if "interface" in r)
              and all(r.get("controller_up") for r in rows if "bluetooth" in r))
    if peer:
        passed = passed and bool(data["bluetooth_controllers"]) and all(r.get("peer_ok") for r in rows if "bluetooth" in r)
    return passed


@contextlib.contextmanager
def mutation_lock(system):
    path = system.path("/run/rtl8822bu-setup/lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="Read-only inventory; no network probe or password output")
    install_parser = subs.add_parser("install")
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--power-only", action="store_true")
    install_parser.add_argument("--multihoming", action="store_true")
    install_parser.add_argument("--install-deps", action="store_true")
    subs.add_parser("uninstall")
    subs.add_parser("refresh")
    event = subs.add_parser("event")
    event.add_argument("interface")
    event.add_argument("action")
    probe = subs.add_parser("verify")
    probe.add_argument("--dns-server", default="223.5.5.5")
    probe.add_argument("--https-host", default="www.qq.com")
    probe.add_argument("--timeout", type=float, default=10, help="Per-socket timeout in seconds (1..60)")
    probe.add_argument("--peer", help="An already-paired Bluetooth device owned/authorized by the operator")
    options = parser.parse_args(argv)
    if platform.system() != "Linux":
        parser.error("Run this component on Linux, not on the Mac used to copy it.")
    if options.command == "verify" and (not re.fullmatch(r"[A-Za-z0-9.-]+", options.https_host)
                                        or not 1 <= options.timeout <= 60
                                        or (options.peer and not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", options.peer))):
        parser.error("Invalid HTTPS hostname, timeout (1..60 seconds), or Bluetooth address.")
    system = System()
    dry = options.command == "install" and options.dry_run
    if options.command == "doctor" or dry:
        if options.command == "doctor":
            print(json.dumps(doctor(system), indent=2, ensure_ascii=False))
        else:
            install(system, options)
        return 0
    if os.geteuid() != 0:
        parser.error("This command needs root; use sudo. doctor and install --dry-run are read-only.")
    if options.command == "verify":
        # Network probes must not block dispatcher recovery behind our lock.
        return 0 if verify(system, options.dns_server, options.https_host, options.peer, options.timeout) else 1
    with mutation_lock(system):
        if options.command == "install":
            install(system, options)
        elif options.command == "uninstall":
            uninstall(system)
        elif options.command in ("event", "refresh"):
            refresh(system)  # Reconcile current facts, not a potentially stale queued event.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
