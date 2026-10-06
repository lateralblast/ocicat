#!/usr/bin/env python3
"""Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information into more useful formats

Currently reads iDRAC temperature, fan and power data (web interface or SNMP) and graphs it in the terminal.

License: Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
(CC BY-NC-SA 4.0), see the LICENSE file or https://creativecommons.org/licenses/by-nc-sa/4.0/
"""

import argparse
import asyncio
import atexit
import calendar
import ctypes
import ctypes.util
import collections
import concurrent.futures
import contextlib
import fnmatch
import copy
import csv
import datetime as dt
import getpass
import importlib.util
import io
import json
import logging
import os
import platform
import plistlib
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zoneinfo
from pathlib import Path

# Modules from requirements.txt: import name -> pip requirement
REQUIRED_MODULES = {"requests": "requests", "urllib3": "urllib3", "paramiko": "paramiko", "termgraph": "termgraph",
                    "plotext": "plotext==5.3.2", "pysnmp": "pysnmp", "keyring": "keyring",
                    "terminaltables": "terminaltables", "xlsxwriter": "XlsxWriter", "xlwt": "xlwt",
                    "dracclient": "python-dracclient", "six": "six"}


def install_missing_modules():
    """Install any module in REQUIRED_MODULES that cannot be found, with this interpreter's pip.

    find_spec only looks for the module, so the check costs nothing when everything is installed. --no-install or
    RAGDOLL_NO_INSTALL=1 turns it off; the modules that are optional keep falling back as before."""
    if "--no-install" in sys.argv[1:] or os.environ.get("RAGDOLL_NO_INSTALL"):
        return
    missing = [req for mod, req in REQUIRED_MODULES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"Installing missing Python modules: {' '.join(missing)}", file=sys.stderr)
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
    if sys.prefix == sys.base_prefix and not os.access(sys.prefix, os.W_OK):
        cmd.append("--user")  # system Python outside a virtual environment: do not write to its site-packages
    result = subprocess.run(cmd + missing)
    importlib.invalidate_caches()
    if result.returncode != 0:
        print(f"Warning: could not install {' '.join(missing)}; install them with "
              f"'{sys.executable} -m pip install -r requirements.txt' (or use a virtual environment)", file=sys.stderr)


install_missing_modules()

import requests
import urllib3

__version__ = "0.5.2"


class SourceError(Exception):
    """A data source could not provide what was asked for."""


DELL = "1.3.6.1.4.1.674.10892.5"  # Dell's iDRAC MIB (IDRAC-MIB-SMIv2)
IF_XTABLE = "1.3.6.1.2.1.31.1.1.1"  # IF-MIB ifXTable: .1 ifName, .6 ifHCInOctets, .10 ifHCOutOctets
# Columns shared by the Dell probe tables (temperature, voltage, amperage and cooling devices)
SNMP_COL_READING, SNMP_COL_NAME = 6, 8
SNMP_COL_LIMITS = {"upper_critical": 10, "upper_warning": 11, "lower_warning": 12, "lower_critical": 13}


class Reading(collections.namedtuple("Reading", "key value limits")):
    """One sensor from an SNMP poll: the OID it was read from, its value, and its limits (dict or None)."""


def num(value):
    """Format a reading without losing precision: 23.0 -> '23', 0.2 -> '0.2', 2224015267.0 -> '2224015267'."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _strip(name, *patterns):
    n = name.strip().lower()
    for pat in patterns:
        n = re.sub(pat, "", n)
    return re.sub(r"\s+", "-", n.strip())


def _power_name(name):
    if re.search(r"pwr consumption", name, re.I):
        return "system-power"
    return _strip(name, r" \d+$")  # "PS1 Current 1" -> "ps1-current"


def _clean(name):
    """'PERC H730P Mini (Embedded)' -> 'perc-h730p-mini'."""
    return _strip(re.sub(r"\s*\(.*?\)", "", name))


def _disk_name(name):
    m = re.match(r"Disk (\d+) in Backplane", name)
    return f"disk-{m.group(1)}" if m else _clean(name)


# Rows of the power usage table (4.600.60) that become sensors of the power metric: column -> (sensor, divisor)
POWER_USAGE = {7: ("energy", 1), 9: ("peak-power", 1), 12: ("peak-current", 10), 15: ("idle-power", 1),
               16: ("max-power", 1), 20: ("headroom", 1), 21: ("peak-headroom", 1)}

# Status columns of the components in the health metric: (table, name column, status column, name -> sensor).
# Most use Dell's status codes (3 = ok); the two redundancy tables use full(3) degraded(4) lost(5) ...
HEALTH_TABLES = [
    (f"{DELL}.4.1100.32.1", 7, 5, lambda n: _strip(n, r" status$")),  # CPUs: cpu1, cpu2
    (f"{DELL}.4.1100.50.1", 8, 5, lambda n: "dimm-" + _strip(n, r"^dimm\.socket\.")),  # memory: dimm-a1 ...
    (f"{DELL}.4.600.12.1", 8, 5, lambda n: _strip(n, r" status$")),  # power supplies: ps1, ps2
    (f"{DELL}.4.300.70.1", 8, 5, lambda n: _strip(n, r"^system board ")),  # chassis intrusion
    (f"{DELL}.4.600.50.1", 7, 5, lambda n: _strip(n, r"^system board ")),  # batteries: cmos-battery ...
    (f"{DELL}.4.700.10.1", 7, 5, lambda n: _strip(n, r"^system board ")),  # fan-redundancy
    (f"{DELL}.4.600.10.1", 7, 5, lambda n: _strip(n, r"^system board ")),  # ps-redundancy
    (f"{DELL}.5.1.20.130.4.1", 55, 24, _disk_name),  # physical disks: disk-0 ...
    (f"{DELL}.5.1.20.140.1.1", 2, 20, lambda n: "vdisk-" + _strip(n)),  # virtual disks
    (f"{DELL}.5.1.20.130.15.1", 21, 6, lambda n: "raid-battery"),
    (f"{DELL}.5.1.20.130.1.1", 2, 38, _clean),  # storage controllers
]


# --- inventory: the static description of the hardware, read from Dell's tables (none of it changes between polls) ---
STATUS_NAMES = {1: "other", 2: "unknown", 3: "ok", 4: "non-critical", 5: "critical", 6: "non-recoverable"}
LINK_NAMES = {1: "connected", 2: "disconnected", 3: "driver bad", 4: "driver disabled", 10: "hardware initializing",
              11: "hardware resetting", 12: "hardware closing", 13: "hardware not ready"}
BUS_NAMES = {1: "unknown", 2: "scsi", 3: "sas", 4: "sata", 5: "fibre", 6: "pcie", 7: "nvme"}
MEDIA_NAMES = {1: "unknown", 2: "hdd", 3: "ssd"}
LAYOUT_NAMES = {1: "other", 2: "RAID 0", 3: "RAID 1", 4: "RAID 5", 5: "RAID 6", 6: "RAID 10", 7: "RAID 50", 8: "RAID 60",
                9: "concat RAID 1", 10: "concat RAID 5"}
DISK_STATE_NAMES = {1: "unknown", 2: "ready", 3: "online", 4: "foreign", 5: "offline", 6: "blocked", 7: "failed",
                    8: "non-raid", 9: "removed", 10: "read-only"}
VDISK_STATE_NAMES = {1: "unknown", 2: "online", 3: "failed", 4: "degraded"}
BATTERY_STATE_NAMES = {1: "unknown", 2: "ready", 3: "failed", 4: "degraded", 5: "missing", 6: "charging",
                       7: "below threshold"}


def _text(value):
    return str(value).strip().strip('"') or None


def _mac(value):
    """A MAC address arrives as six raw bytes; show it as 24:6E:96:74:E1:EC."""
    raw = bytes(value.asOctets()) if hasattr(value, "asOctets") else b""
    return ":".join(f"{b:02X}" for b in raw) if len(raw) == 6 else _text(value)


def _natural(text):
    """Sort key that orders 'DIMM.Socket.A2' before 'DIMM.Socket.A10'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _enum(names):
    return lambda value: names.get(int(value), str(value))


def _gib(per_gib):
    """Size in GiB from a value in KiB (per_gib 1048576) or MiB (per_gib 1024); 0 or unknown means not present."""
    def convert(value):
        n = int(value)
        return None if n <= 0 or n == 2147483647 else f"{num(round(n / per_gib, 2))} GiB"
    return convert


def _mhz(value):
    return f"{int(value)} MHz" if int(value) > 0 else None


def _mb(value):
    return f"{int(value)} MB" if int(value) > 0 else None


# Each inventory category: (category, table OID, column holding the item's name or None for a fixed name,
# {attribute: (column, how to show it)}). Scalar groups (system, idrac) have no table; their columns are the scalars.
INVENTORY = [
    ("system", f"{DELL}.1.3", None, {"model": (12, _text), "name": (1, _text), "service-tag": (2, _text)}),
    ("idrac", f"{DELL}.1.1", None, {"product": (2, _text), "firmware": (8, _text), "manufacturer": (4, _text)}),
    ("bios", f"{DELL}.4.300.50.1", None,
     {"version": (8, _text), "released": (7, _text), "manufacturer": (11, _text), "status": (5, _enum(STATUS_NAMES))}),
    ("firmware", f"{DELL}.4.300.60.1", 8, {"version": (11, _text), "status": (5, _enum(STATUS_NAMES))}),
    ("cpu", f"{DELL}.4.1100.30.1", 26,
     {"manufacturer": (8, _text), "brand": (23, _text), "model": (16, _text), "cores": (17, _text),
      "enabled-cores": (18, _text), "threads": (19, _text), "max-speed": (11, _mhz), "speed": (12, _mhz),
      "status": (5, _enum(STATUS_NAMES))}),
    ("memory", f"{DELL}.4.1100.50.1", 8,
     {"size": (14, _gib(1048576)), "speed": (15, _mhz), "manufacturer": (21, _text), "part-number": (22, _text),
      "serial": (23, _text), "status": (5, _enum(STATUS_NAMES))}),
    ("nic", f"{DELL}.4.1100.90.1", 30,
     {"product": (6, _text), "vendor": (7, _text), "mac": (16, _mac), "link": (4, _enum(LINK_NAMES)),
      "status": (3, _enum(STATUS_NAMES))}),
    ("pci", f"{DELL}.4.1100.80.1", 12,
     {"manufacturer": (8, _text), "description": (9, _text), "status": (5, _enum(STATUS_NAMES))}),
    ("controller", f"{DELL}.5.1.20.130.1.1", 2,
     {"firmware": (8, _text), "cache": (9, _mb), "status": (38, _enum(STATUS_NAMES))}),
    ("disk", f"{DELL}.5.1.20.130.4.1", 2,
     {"manufacturer": (3, _text), "model": (6, _text), "serial": (7, _text), "firmware": (8, _text),
      "size": (11, _gib(1024)), "bus": (21, _enum(BUS_NAMES)), "media": (35, _enum(MEDIA_NAMES)),
      "state": (4, _enum(DISK_STATE_NAMES)), "status": (24, _enum(STATUS_NAMES))}),
    ("virtual-disk", f"{DELL}.5.1.20.140.1.1", 2,
     {"size": (6, _gib(1024)), "layout": (13, _enum(LAYOUT_NAMES)), "media": (33, _text),
      "state": (4, _enum(VDISK_STATE_NAMES)), "status": (20, _enum(STATUS_NAMES))}),
    ("raid-battery", f"{DELL}.5.1.20.130.15.1", 21,
     {"state": (4, _enum(BATTERY_STATE_NAMES)), "status": (6, _enum(STATUS_NAMES))}),
]


async def read_inventory(args, only_category=None):
    """Return [(category, item name, [(attribute, shown value), ...])] in category order.

    only_category limits it to one category, and then only that category's tables are read.
    """
    specs = [entry for entry in INVENTORY if only_category in (None, entry[0])]
    limit = asyncio.Semaphore(6)  # many walks at once, but not so many that the iDRAC drops requests

    async def walk(oid):
        async with limit:
            return await snmp_walk(args, oid)

    await snmp_connection(args)  # created before any concurrent walks start
    try:
        walks = await asyncio.gather(*(
            asyncio.gather(*(walk(f"{table}.{c}") for c in
                             ([name_column] if name_column else []) + [col for col, _ in attributes.values()]))
            for _, table, name_column, attributes in specs))
    finally:
        snmp_close(args)
    records = []
    for (category, table, name_column, attributes), columns in zip(specs, walks):
        names = columns[0] if name_column else None
        values = columns[1 if name_column else 0:]
        rows = sorted(names) if name_column else sorted({row for col in values for row in col}) or []
        for row in rows:
            details = []
            for (attribute, (_, show)), column in zip(attributes.items(), values):
                if row in column:
                    shown = show(column[row])
                    if shown:
                        details.append((attribute, shown))
            if not details:
                continue
            name = _text(names[row]) if name_column else category
            records.append((category, name or f"{category}-{row}", details))
    order = {category: i for i, (category, *_) in enumerate(INVENTORY)}
    records.sort(key=lambda r: (order[r[0]], _natural(r[1])))  # categories as defined, items in natural order
    if not records:
        what = f"{only_category} items" if only_category else "inventory"
        raise SourceError(f"no {what} returned by {args.host} (check host and --community)")
    return records


def probe_unit(sensor):
    return " A" if sensor.endswith("-current") else " Wh" if sensor == "energy" else " W"


# Per-metric details. read(args) -> {sensor: Reading}; unit(sensor) is shown in titles and headings; label ends the
# chart title; counter=True means the stored values are ever-growing counters that outputs turn into rates.
# Dell reports temperatures and power supply currents in tenths, voltages in millivolts, fans in RPM and watts as is.
METRICS = {
    "temperature": {
        "label": "Temperature", "default": "inlet", "unit": lambda sensor: "°C",
        "read": lambda args: read_probe_table(
            args, f"{DELL}.4.700.20.1", lambda n: _strip(n, r"^system board ", r" temp(erature)?$"), lambda s: 10)},
    "fan": {
        "label": "Speed", "default": "fan1a", "unit": lambda sensor: " RPM",
        "read": lambda args: read_probe_table(
            args, f"{DELL}.4.700.12.1", lambda n: _strip(n, r"^system board "), lambda s: 1)},
    "power": {
        "label": "", "default": "system-power", "unit": probe_unit,
        "read": lambda args: read_power(args)},
    "voltage": {
        "label": "", "default": "ps1-voltage", "unit": lambda sensor: " V",
        "read": lambda args: read_probe_table(
            args, f"{DELL}.4.600.20.1", lambda n: _strip(n, r" \d+$"), lambda s: 1000)},
    "health": {
        "label": "Status", "default": "system", "unit": lambda sensor: " code",
        "read": lambda args: read_health(args)},
    "network": {
        "label": "Traffic", "default": "bond0-in", "unit": lambda sensor: " B/s", "counter": True,
        "read": lambda args: read_network(args)},
}
METRIC_NAMES = tuple(METRICS)
URL_TEMPLATE = "https://{host}/sysmgmt/2012/server/temperature/statistics/{sensor}?format=csv"

DEFAULT_WIDTH = 80
DEFAULT_HEIGHT = 24
TERMGRAPH_LABEL_COLS = 28  # label + value + suffix columns termgraph adds around the bars
MODULES_NAMES = ("plotext", "termgraph")
CHARTS = ("vertical", "horizontal", "stacked", "histogram", "line", "scatter")

# termgraph flag for each chart type (horizontal is termgraph's default)
CHART_FLAGS = {"horizontal": [], "vertical": ["--vertical"],
               "stacked": ["--stacked"], "histogram": ["--histogram"]}


# Times are kept in UTC everywhere inside ragdoll: as ISO 8601 text in the cache and --raw output, and as Unix
# timestamps in the database and in memory. They are converted to local time only for display.
CSV_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# The iDRAC's own CSV uses its wall clock with no time zone, e.g. "Wed Sep 19 10:12:52 2018"
TIME_FORMAT = "%a %b %d %H:%M:%S %Y"
UNITS = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
UNIT_NAMES = {"hour": "h", "day": "d", "week": "w", "month": "m", "year": "y"}


# The iDRAC writes -128 for both Average and Peak when a sample has no reading. Such rows are dropped wherever data
# is read, so they never appear in charts, --raw output, the cache or the database.
NO_READING = -128


def is_reading(avg, peak):
    return NO_READING not in (avg, peak)


def epoch_to_csv(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(CSV_TIME_FORMAT)


def csv_to_epoch(text):
    return calendar.timegm(dt.datetime.strptime(text, CSV_TIME_FORMAT).timetuple())


def parse_offset(value):
    """Parse an offset from UTC such as +11:00, -05:00, +0530, 10 or UTC; return seconds."""
    v = value.strip().lower()
    if v in ("utc", "z", "gmt"):
        return 0
    m = re.fullmatch(r"([+-]?)(\d{1,2})(?::?(\d{2}))?", v)
    if not m or int(m.group(2)) > 14 or int(m.group(3) or 0) > 59:
        raise argparse.ArgumentTypeError(f"invalid offset {value!r}: use something like +10:00, -05:00 or UTC")
    seconds = int(m.group(2)) * 3600 + int(m.group(3) or 0) * 60
    return -seconds if m.group(1) == "-" else seconds


def parse_zone(name):
    """Return a tzinfo for a time zone name such as Australia/Sydney, America/New_York or UTC."""
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise argparse.ArgumentTypeError(
            f"unknown time zone {name!r}: use a name such as Australia/Sydney, America/New_York or UTC")


def parse_last(value):
    """Return ('rows', N) or ('seconds', N)."""
    v = value.strip().lower()
    if v.isdigit():
        return ("rows", int(v))
    m = re.fullmatch(r"(\d*)\s*(h|d|w|m|y|hour|day|week|month|year)s?", v)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid value {value!r}: use a number of rows or a period like hour, 2d, week, month")
    n = int(m.group(1) or 1)
    unit = UNIT_NAMES.get(m.group(2), m.group(2))
    return ("seconds", n * UNITS[unit])


def to_rates(rows):
    """Turn cumulative counter readings into bytes (or units) per second between consecutive readings.

    An interval is dropped if the counter went down (the device restarted) or no time passed. The first reading
    has nothing before it, so n readings give at most n - 1 rates.
    """
    out = []
    for (t0, v0, _), (t1, v1, _) in zip(rows, rows[1:]):
        if t1 > t0 and v1 >= v0:
            rate = round((v1 - v0) / (t1 - t0), 3)
            out.append((t1, rate, rate))
    return out


def select_last(data, last):
    """Trim rows to the last N rows, or to the period ending at the newest sample."""
    kind, n = last
    if kind == "rows":
        return data[-n:] if n else data
    cutoff = max(t for t, _, _ in data) - n
    return [row for row in data if row[0] >= cutoff]


KEYRING_SERVICE = "ragdoll:{host}"
CREDENTIAL_ENV = {"user": "IDRAC_USER", "password": "IDRAC_PASS", "community": "IDRAC_COMMUNITY"}
DEFAULT_COMMUNITY = "public"


def keyring_get(host, field):
    """Read one saved credential for a host, or None if there is none or no keyring backend."""
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE.format(host=host), field)
    except Exception:  # missing module, no backend, locked keyring
        return None


def keyring_set(host, field, value):
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE.format(host=host), field, value)
    except ImportError:
        raise SourceError("keyring not found; install with: pip install keyring")
    except Exception as e:
        raise SourceError(f"could not use the OS keyring: {e}")


def keyring_forget(host):
    try:
        import keyring
    except ImportError:
        raise SourceError("keyring not found; install with: pip install keyring")
    removed = []
    for field in CREDENTIAL_ENV:
        try:
            keyring.delete_password(KEYRING_SERVICE.format(host=host), field)
            removed.append(field)
        except keyring.errors.PasswordDeleteError:
            pass  # nothing saved under that name
        except Exception as e:
            raise SourceError(f"could not use the OS keyring: {e}")
    return removed


def resolve_credentials(args):
    """Fill in missing credentials, in place. Order: command line, environment, keyring, prompt."""
    if SOURCES.get(args.source, {}).get("local"):
        return  # this computer's own sensors need no login
    fields = ("community",) if args.source == "snmp" else ("user", "password")
    for field in fields:
        if getattr(args, field):
            continue
        value = os.environ.get(CREDENTIAL_ENV[field]) or keyring_get(args.host, field)
        setattr(args, field, value)
    if args.source == "snmp":
        args.community = args.community or DEFAULT_COMMUNITY
    elif args.user and not args.password and sys.stdin.isatty():
        args.password = getpass.getpass(f"Password for {args.user}@{args.host}: ")


def save_credentials(args):
    fields = ("community",) if args.source == "snmp" else ("user", "password")
    for field in fields:
        keyring_set(args.host, field, getattr(args, field))


def default_cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "ragdoll"


def default_db_path():
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "ragdoll" / "ragdoll.db"


DB_VERSION = 2  # 1 stored local-time text; 2 stores UTC Unix timestamps
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    host    TEXT NOT NULL,
    source  TEXT NOT NULL,
    metric  TEXT NOT NULL,
    sensor  TEXT NOT NULL,
    time    INTEGER NOT NULL,  -- Unix time of the sample (seconds since 1970-01-01 UTC)
    average REAL NOT NULL,
    peak    REAL NOT NULL,
    PRIMARY KEY (host, source, metric, sensor, time)
) WITHOUT ROWID
"""


def open_db(path):
    """Open the database, creating it if needed; refuse one written by an older version."""
    conn = sqlite3.connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != DB_VERSION and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'readings'").fetchone():
        conn.close()
        raise ValueError(f"{path} was written by an older version, which did not store times in UTC; "
                         "delete it and run again to rebuild it")
    conn.execute(DB_SCHEMA)
    conn.execute(f"PRAGMA user_version = {DB_VERSION}")
    return conn


def store_rows(args, rows):
    """Insert (unix time, average, peak) rows for the current host, source, metric and sensor.

    Rows already in the database are skipped, so the whole history can be passed on every run.
    "No reading" rows left by earlier versions, which stored them, are deleted and the space is reclaimed.
    Returns the number of rows added.
    """
    args.db.parent.mkdir(parents=True, exist_ok=True)
    key = (args.host, args.source, args.metric, args.sensor)
    values = [key + row for row in rows]
    with contextlib.closing(open_db(args.db)) as conn, conn:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?, ?, ?, ?)", values)
        added = conn.total_changes - before
        purged = conn.execute("DELETE FROM readings WHERE average = ? OR peak = ?",
                              (NO_READING, NO_READING)).rowcount
    if purged:
        # VACUUM cannot run inside a transaction, so it gets its own connection
        with contextlib.closing(sqlite3.connect(args.db, isolation_level=None)) as conn:
            conn.execute("VACUUM")
    return added


def load_rows(args):
    """Return every stored (unix time, average, peak) row for the current sensor, oldest first."""
    with contextlib.closing(open_db(args.db)) as conn:
        return conn.execute(
            "SELECT time, average, peak FROM readings "
            "WHERE host = ? AND source = ? AND metric = ? AND sensor = ? AND average != ? AND peak != ? ORDER BY time",
            (args.host, args.source, args.metric, args.sensor, NO_READING, NO_READING)).fetchall()


def to_csv(rows):
    return "Average,Peak,Time\n" + "".join(f"{num(avg)},{num(peak)},{epoch_to_csv(t)}\n" for t, avg, peak in rows)


def read_cache(path):
    """Return the cached CSV, or None if there is none or it is in the old format (local time, no zone)."""
    if not path.exists():
        return None
    text = path.read_text()
    lines = text.splitlines()
    if len(lines) > 1 and not lines[1].endswith("Z"):
        return None
    return text


def stored_data(args):
    """Return (csv text or None, rows) from what is already stored, without contacting the iDRAC."""
    if args.db:
        if not args.db.exists():
            sys.exit(f"--no-fetch: no database at {args.db}; run once without --no-fetch to create it")
        try:
            return None, load_rows(args)
        except (sqlite3.Error, ValueError) as e:
            sys.exit(f"Could not read readings from {args.db}: {e}")
    path = cache_path(args)
    text = read_cache(path)
    if text is None:
        why = "is in an old format" if path.exists() else "is missing"
        sys.exit(f"--no-fetch: the cache for this host, source, metric and sensor {why} ({path}); "
                 "run once without --no-fetch")
    return text, parse_rows(text)


def cache_path(args):
    """One file per host and metric, e.g. 192.168.8.98_web_temperature-inlet.csv."""
    safe = lambda v: re.sub(r"[^\w.-]", "_", v)
    return args.cache_dir / f"{safe(args.host)}_{safe(args.source)}_{safe(args.metric)}-{safe(args.sensor)}.csv"


def load_data(args):
    """Return CSV text from the cache if fresh, otherwise fetch and cache it.

    Sources that accumulate (snmp, which only reports the current value) are polled on
    every run and append the new reading to the cached history.
    """
    path = cache_path(args)
    cached = read_cache(path)
    accumulate = SOURCES[args.source]["accumulate"]
    if not accumulate and not args.refresh and cached is not None and time.time() - path.stat().st_mtime < args.max_age:
        return cached
    try:
        text = SOURCES[args.source]["fetch"](args)
    except (requests.RequestException, SourceError) as e:
        if cached is not None:
            print(f"warning: fetch failed ({e}); using stale cache {path}", file=sys.stderr)
            return cached
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return text


def parse_args():
    paragraphs = __doc__.strip().split("\n\n")
    p = argparse.ArgumentParser(description=". ".join(q.replace("\n", " ") for q in paragraphs[:2]),
                                epilog=paragraphs[2].replace("\n", " "))
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--no-install", action="store_true",
                   help="do not install missing Python modules with pip at startup (or set RAGDOLL_NO_INSTALL=1)")
    p.add_argument("--host", help="iDRAC address, e.g. 192.168.8.98. With --source lmsensors or system it is optional: without it "
                                  "this computer is read, with it ragdoll runs the commands on that computer over ssh")
    p.add_argument("--user", "--username", dest="user", help="iDRAC username (web source; else $IDRAC_USER or the keyring)")
    p.add_argument("--pass", "--password", dest="password",
                   help="iDRAC password (web source; else $IDRAC_PASS, the keyring, or a prompt)")
    p.add_argument("--community",
                   help="SNMP v2c community string (snmp source; else $IDRAC_COMMUNITY, the keyring, or the standard default)")
    p.add_argument("--save-credentials", action="store_true",
                   help="after a successful login, save the credentials for this host in the OS keyring")
    p.add_argument("--forget-credentials", action="store_true",
                   help="delete this host's credentials from the OS keyring and exit")
    p.add_argument("--source", choices=sorted(SOURCES), type=lambda v: SOURCE_ALIASES.get(v.lower(), v.lower()),
                   help="where sensor data comes from: web is the iDRAC web interface (history), "
                        "snmp reads the current value and builds history in the cache, lmsensors does the same for this "
                        "computer's own sensors (lm-sensors), redfish, racadm and wsman provide only the inventory, and "
                        "system gives this computer's inventory (system_profiler on macOS, dmidecode on Linux) "
                        "(gui is an alias for web; default: web; "
                        "with --list, all sources)")
    p.add_argument("--metric", choices=METRIC_NAMES,
                   help="what to read: temperature, fan (RPM), power (W, A, Wh), voltage (V), health (status "
                        "codes) or network (bytes per second); all but temperature need --source snmp "
                        "(default: temperature; with --list, all metrics)")
    p.add_argument("--sensor", help="sensor to graph, see --list (default: inlet, fan1a or system-power depending on --metric)")
    p.add_argument("--get", nargs="?", const="sensor", metavar="inventory",
                   help="poll one snmp sensor now and print its current value, for example 16 °C; nothing is "
                        "stored. Uses --source snmp by default, with --metric and --sensor choosing the sensor "
                        "(network counters are sampled twice, 2 seconds apart, to give bytes per second). "
                        "--get inventory prints the hardware inventory, the same as --list inventory")
    p.add_argument("--detail", metavar="DETAIL",
                   help="with --list inventory or --get inventory: only this detail of each item, for example "
                        "version for the bios, returned as just its value. Plain text prints the bare values, "
                        "one per line. Not case-sensitive; a wrong name lists the details the items have")
    p.add_argument("--field", metavar="FIELD",
                   help="with --list or --get: print only this field (column) of the output, for example value, "
                        "unit or key for a sensor, or details for the inventory. Plain text prints just the values, "
                        "one per line, with no heading. Not case-sensitive; a wrong name lists the fields")
    p.add_argument("--category", choices=[entry[0] for entry in INVENTORY], metavar="CATEGORY",
                   help="with --list inventory or --get inventory: only this category of the inventory ("
                        + ", ".join(entry[0] for entry in INVENTORY) + ")")
    p.add_argument("--name", metavar="NAME",
                   help="with --list inventory or --get inventory: only the items with this name, for example "
                        "DIMM.Socket.A1; not case-sensitive, and * and ? match any characters")
    p.add_argument("--list", nargs="?", const="all", metavar="sensors|inventory|SOURCE|METRIC",
                   help="list the available sensors and exit: sensors (the default) lists every sensor of every "
                        "source and metric, with its source; or give a source (web, snmp) or a metric "
                        "(temperature, fan, power, voltage, health, network) to list only those; "
                        "inventory lists the hardware (CPUs, memory, disks, firmware ...) over snmp, or over redfish "
                        "with --source redfish, racadm or wsman; "
                        "--source and --metric also narrow the sensor listing")
    p.add_argument("--secure", action="store_true",
                   help="verify the TLS certificate (iDRACs are usually self-signed, so off by default)")
    p.add_argument("--cachedir", "--cache-dir", dest="cache_dir", type=Path, default=default_cache_dir(),
                   help="directory for cached data (default: %(default)s)")
    p.add_argument("--max-age", type=int, default=3600,
                   help="seconds before cached web data is considered stale (default: 3600)")
    p.add_argument("--tz-offset", type=parse_offset, metavar="OFFSET",
                   help="offset of the iDRAC's own clock from UTC, e.g. +10:00, -05:00 or UTC (web source; "
                        "default: measured by comparing the iDRAC's clock with this computer's, via Redfish)")
    p.add_argument("--tz", type=parse_zone, metavar="ZONE",
                   help="time zone for chart times, for example Australia/Sydney or America/New_York "
                        "(default: this computer's local time zone)")
    p.add_argument("--utc", action="store_true", help="show chart times in UTC (same as --tz UTC)")
    p.add_argument("--refresh", action="store_true", help="ignore the cache and fetch fresh data")
    p.add_argument("--no-fetch", action="store_true",
                   help="do not contact the iDRAC: chart what is already stored, from the --db database "
                        "if given, otherwise from the cache however old it is")
    p.add_argument("--db", nargs="?", const=default_db_path(), type=Path, metavar="PATH",
                   help="also store readings in a SQLite database, and chart from it so history from "
                        "earlier runs is included (default path if no PATH: %s)" % default_db_path())
    p.add_argument("--raw", action="store_true", help="print the raw CSV and exit")
    p.add_argument("--tail", action="store_true",
                   help="keep running and poll the --source for the sensor, like tail -f, until Ctrl-C; "
                        "each reading is sent to --output (text, csv, json, raw or db). Uses --source snmp by default")
    p.add_argument("--poll", type=float, metavar="SECONDS",
                   help="seconds between polls in --tail mode (at least 1; default 30, about how often an iDRAC "
                        "refreshes its sensors); giving --poll starts --tail mode")
    p.add_argument("--output", choices=sorted(set(OUTPUTS) | set(LIST_OUTPUTS)),
                   type=lambda v: {"database": "db"}.get(v.lower(), v.lower()),
                   help="what to produce. For readings: chart, table, raw (cache format CSV, UTC times), csv "
                        "(CSV with host, sensor and unit columns, times in --tz/local), json (the same as a JSON array), or "
                        "xlsx / xls spreadsheets "
                        "(see --file); --module, --chart, --width and --height apply to chart (default: chart). "
                        "With --list or --get: text, table, csv, json, xlsx or xls (default: text). db (or database) stores the "
                        "readings in the SQLite database (--db PATH, or the default one); with --tail the choices "
                        "are text, csv, json (JSON Lines), raw or db (default: text)")
    p.add_argument("--file", metavar="FILE",
                   help="file to write for --output xlsx or xls; if not given, a name is made from the host, "
                        "source, sensor and --last period in the current directory, with the right extension")
    p.add_argument("--limits", action="store_true",
                   help="draw the sensor's warning and critical limits on a plotext line or scatter chart "
                        "(limits are saved when the sensor is polled over snmp)")
    p.add_argument("--module", choices=sorted(MODULES_NAMES), default="plotext",
                   help="graphing module (default: plotext)")
    p.add_argument("--chart", choices=CHARTS, default="vertical",
                   help="chart type; line and scatter are plotext only (default: vertical)")
    p.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                   help="total graph width in columns (default: %(default)s, a standard terminal)")
    p.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                   help="total graph height in rows, plotext only (default: %(default)s, a standard terminal)")
    p.add_argument("--last", type=parse_last, default="10",
                   help="how much to graph: a row count (10) or a period such as "
                        "hour, day, week, month, year, 6h, 2days (default: 10 rows)")
    p.set_defaults(remote=False)  # True when a local source is run on another computer over ssh
    args = p.parse_args()
    local = SOURCES.get(args.source, {}).get("local") or SOURCES.get(args.list, {}).get("local")
    if local:  # without --host, this computer, named by its own hostname; with one, that computer over ssh
        if args.host:
            args.remote = True
            if args.host.startswith("-") or (args.user and (args.user.startswith("-") or re.search(r"\s", args.user))):
                p.error("--host and --user must not start with '-' or contain spaces")
            if args.password:
                p.error(f"ssh logs in with a key or the ssh agent, so --pass does not apply to --source "
                        f"{args.source or args.list}")
        else:
            args.host = socket.gethostname()
    elif not args.host:
        p.error("the following arguments are required: --host")
    if args.tz and args.utc:
        p.error("--tz and --utc cannot be used together")
    if args.get not in (None, "sensor", "inventory"):
        p.error(f"--get takes no value or 'inventory', not {args.get!r}; use --metric and --sensor to choose a sensor")
    if args.field and not (args.list or args.get):
        p.error("--field only applies to --list or --get")
    if (args.category or args.name or args.detail) and "inventory" not in (args.list, args.get):
        p.error("--category, --name and --detail only apply to --list inventory or --get inventory")
    if args.detail and args.field:
        p.error("--detail and --field cannot be used together: --detail already picks what to show")
    if args.poll is not None:
        args.tail = True  # giving an interval means polling
        if args.poll < 1:
            p.error("--poll must be at least 1 second")
    if args.tail and (args.list or args.get or args.raw or args.no_fetch):
        p.error("--tail polls the source, so it cannot be used with --list, --get, --raw or --no-fetch")
    if args.list and args.get:
        p.error("--list and --get cannot be used together")
    if args.tail:
        args.output = args.output or "text"
        if args.output not in TAIL_OUTPUTS:
            p.error(f"--output {args.output} does not work with --tail; use one of {', '.join(TAIL_OUTPUTS)}")
        args.poll = args.poll or DEFAULT_POLL_SECONDS
    elif args.list or args.get:  # plain text unless another format is asked for
        args.output = args.output or "text"
        if args.output not in LIST_OUTPUTS:
            p.error(f"--output {args.output} does not apply to --list or --get; "
                    f"use one of {', '.join(sorted(LIST_OUTPUTS))}")
    else:
        args.output = args.output or "chart"
        if args.output not in OUTPUTS:
            p.error(f"--output {args.output} only applies to --list, --get or --tail")
    if args.output == "db":
        args.db = args.db or default_db_path()  # --output db is the database, so it needs a path
    if args.file:
        if args.output not in ("xlsx", "xls"):
            p.error("--file only applies to --output xlsx or xls")
        suffix = Path(args.file).suffix.lower()
        if suffix in (".xls", ".xlsx") and suffix != "." + args.output:
            p.error(f"--file {args.file} does not match --output {args.output}")
    if args.list:  # --list gui means --list web, and --list sensors means every sensor
        args.list = SOURCE_ALIASES.get(args.list.lower(), args.list)
        if args.list.lower() == "sensors":
            args.list = "all"
    return args


@contextlib.contextmanager
def idrac_session(args):
    """Log in with a session (basic auth is not accepted); yield (session, base_url, headers)."""
    resolve_credentials(args)
    if not args.user or not args.password:
        raise SourceError("the web source needs credentials: --user/--pass, $IDRAC_USER/$IDRAC_PASS, "
                          "or ones saved with --save-credentials")
    if not args.secure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    base = f"https://{args.host}"
    s = requests.Session()
    s.verify = args.secure
    r = s.post(f"{base}/data/login", data={"user": args.user, "password": args.password}, timeout=30)
    r.raise_for_status()
    m = re.search(r"ST2=(\w+)", r.text)
    if not m:
        raise requests.RequestException("login failed (check --user/--pass)")
    try:
        yield s, base, {"ST2": m.group(1)}
    finally:
        s.get(f"{base}/data/logout", timeout=10)


def web_sensors_from_session(session, base, headers):
    """Return {short name: full iDRAC key}, e.g. {'inlet': 'iDRAC.Embedded.1#Inlet.1#ThermalHistory'}."""
    resp = session.get(f"{base}/sysmgmt/2012/server/temperature/statistics", headers=headers, timeout=30)
    resp.raise_for_status()
    sensors = {}
    for key in resp.json().get("Statistics", {}):
        m = re.search(r"#([A-Za-z]+)\.\d+#", key)
        sensors[(m.group(1) if m else key).lower()] = key
    return sensors


def web_clock_offset(args):
    """Seconds the iDRAC's wall clock is ahead of UTC: --tz-offset, else measured against this computer's clock.

    The configured time zone is deliberately not used: an iDRAC whose clock shows UTC but whose time zone is set
    to something else would be converted wrongly.
    """
    if args.tz_offset is not None:
        return args.tz_offset
    try:
        r = requests.get(f"https://{args.host}/redfish/v1/Managers/iDRAC.Embedded.1",
                         auth=(args.user, args.password), verify=args.secure, timeout=30)
        r.raise_for_status()
        wall = dt.datetime.fromisoformat(r.json()["DateTime"]).replace(tzinfo=None)
    except (requests.RequestException, KeyError, ValueError) as e:
        raise SourceError(f"could not read the iDRAC's clock from Redfish ({e}); "
                          "give its offset from UTC with --tz-offset")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return round((wall - now).total_seconds() / 900) * 900  # nearest quarter hour


def web_csv_to_utc(text, offset):
    """Convert the iDRAC's CSV (wall clock times) to the cache format with UTC times."""
    lines = ["Average,Peak,Time"]
    for row in csv.DictReader(io.StringIO(text)):
        try:
            epoch = calendar.timegm(dt.datetime.strptime(row["Time"].strip(), TIME_FORMAT).timetuple()) - offset
            avg, peak = float(row["Average"]), float(row["Peak"])
        except (KeyError, TypeError, ValueError):
            continue
        if is_reading(avg, peak):
            lines.append(f"{num(avg)},{num(peak)},{epoch_to_csv(epoch)}")
    return "\n".join(lines) + "\n"


def web_fetch(args):
    """Download the CSV for args.sensor, refusing sensors the iDRAC does not list."""
    with idrac_session(args) as (s, base, headers):
        # the iDRAC ignores unknown sensor names and returns inlet data, so check first
        sensors = web_sensors_from_session(s, base, headers)
        if args.sensor not in sensors:
            raise requests.RequestException(
                f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(sensors)) or 'none'}")
        resp = s.get(URL_TEMPLATE.format(host=args.host, sensor=args.sensor), headers=headers, timeout=30)
        resp.raise_for_status()
    return web_csv_to_utc(resp.text, web_clock_offset(args))


def web_list(args):
    """Return {sensor: Reading}; the web source has no live value or limits to give here, only the key."""
    with idrac_session(args) as (s, base, headers):
        return {name: Reading(key, None, None) for name, key in web_sensors_from_session(s, base, headers).items()}


async def snmp_connection(args):
    """Return the (engine, target) shared by every walk of a poll, creating them on first use.

    Creating an engine is slow (it loads MIB data), so a poll makes one and reuses it. Call snmp_close afterwards.
    """
    if getattr(args, "snmp_state", None) is None:
        resolve_credentials(args)
        try:
            from pysnmp.hlapi.v3arch.asyncio import SnmpEngine, UdpTransportTarget
        except ImportError:
            raise SourceError("pysnmp not found; install with: pip install pysnmp")
        engine = SnmpEngine()
        target = await UdpTransportTarget.create((args.host, 161), timeout=3, retries=1)
        args.snmp_state = (engine, target)
    return args.snmp_state


def snmp_close(args):
    if getattr(args, "snmp_state", None) is not None:
        args.snmp_state[0].close_dispatcher()
        args.snmp_state = None


async def snmp_walk(args, oid):
    """Return {sub-identifiers after oid, for example '1.3': value} for every value under oid."""
    from pysnmp.hlapi.v3arch.asyncio import CommunityData, ContextData, ObjectIdentity, ObjectType, walk_cmd
    engine, target = await snmp_connection(args)
    out = {}
    async for err, status, _, binds in walk_cmd(
            engine, CommunityData(args.community, mpModel=1), target, ContextData(),
            ObjectType(ObjectIdentity(oid)), lexicographicMode=False):
        if err or status:
            raise SourceError(f"SNMP error from {args.host}: {err or status.prettyPrint()}")
        for found, value in binds:
            out[str(found)[len(oid) + 1:]] = value
    return out


async def read_probe_table(args, table, short, divisor):
    """Read a Dell probe table: every probe that returns a reading, with its limits.

    Discrete probes (for example the power-good checks in the voltage table) return no reading and are skipped.
    """
    columns = [SNMP_COL_NAME, SNMP_COL_READING] + list(SNMP_COL_LIMITS.values())
    names, readings, *limit_columns = await asyncio.gather(*(snmp_walk(args, f"{table}.{c}") for c in columns))
    if not names:
        raise SourceError(f"no {args.metric} sensors returned by {args.host} (check host and --community)")
    out = {}
    for row, name in names.items():
        if row not in readings:
            continue
        sensor = short(str(name))
        div = divisor(sensor)
        limits = {key: int(col[row]) / div for key, col in zip(SNMP_COL_LIMITS, limit_columns) if row in col}
        out[sensor] = Reading(f"{table}.{SNMP_COL_READING}.{row}", int(readings[row]) / div, limits or None)
    return out


async def read_power(args):
    """Power: the amperage probes (supply currents and system watts) plus the power usage table (energy, peaks)."""
    usage_table = f"{DELL}.4.600.60.1"
    probes, *columns = await asyncio.gather(
        read_probe_table(args, f"{DELL}.4.600.30.1", _power_name, lambda s: 10 if s.endswith("-current") else 1),
        *(snmp_walk(args, f"{usage_table}.{c}") for c in POWER_USAGE))
    for (column, (sensor, divisor)), values in zip(POWER_USAGE.items(), columns):
        if values:
            row = sorted(values)[0]  # one row, "System Power Consumption data"
            probes[sensor] = Reading(f"{usage_table}.{column}.{row}", int(values[row]) / divisor, None)
    return probes


async def read_health(args):
    """Status codes of the components in HEALTH_TABLES, plus the overall system status."""
    out = {}
    limit = asyncio.Semaphore(6)  # walk many tables at once, but not so many that the iDRAC drops requests

    async def walk(oid):
        async with limit:
            return await snmp_walk(args, oid)

    overall, *walked = await asyncio.gather(
        walk(f"{DELL}.2.1"),
        *(walk(f"{table}.{column}") for table, name_column, status_column, _ in HEALTH_TABLES
          for column in (name_column, status_column)))
    if overall:
        out["system"] = Reading(f"{DELL}.2.1.0", int(next(iter(overall.values()))), None)
    for (table, _, status_column, short), names, statuses in zip(HEALTH_TABLES, walked[0::2], walked[1::2]):
        for row, name in names.items():
            if row not in statuses:
                continue
            sensor, n = short(str(name)), 2
            while sensor in out:  # two components with the same name: add a number
                sensor, n = f"{short(str(name))}-{n}", n + 1
            out[sensor] = Reading(f"{table}.{status_column}.{row}", int(statuses[row]), None)
    if not out:
        raise SourceError(f"no health data returned by {args.host} (check host and --community)")
    return out


async def read_network(args):
    """Byte counters of the iDRAC's own network interfaces (IF-MIB), as <interface>-in and <interface>-out."""
    names, received, sent = await asyncio.gather(
        *(snmp_walk(args, f"{IF_XTABLE}.{c}") for c in (1, 6, 10)))
    out = {}
    for row, name in names.items():
        if str(name) == "lo":  # loopback traffic says nothing about the network
            continue
        for direction, column, values in (("in", 6, received), ("out", 10, sent)):
            if row in values:
                out[f"{_strip(str(name))}-{direction}"] = Reading(f"{IF_XTABLE}.{column}.{row}", int(values[row]), None)
    if not out:
        raise SourceError(f"no network interfaces returned by {args.host} (check host and --community)")
    return out


async def snmp_read_table(args):
    """Return {sensor: Reading} for the chosen metric."""
    try:
        await snmp_connection(args)  # created here, before any concurrent walks start
        return await METRICS[args.metric]["read"](args)
    finally:
        snmp_close(args)


def snmp_list(args):
    """Return {sensor: Reading}: the key, the current value and the limits of every sensor of the metric."""
    return asyncio.run(snmp_read_table(args))


def limits_path(args):
    """Where a sensor's limits are kept. Not per source, so web charts of the same host and sensor can use them."""
    safe = lambda v: re.sub(r"[^\w.-]", "_", v)
    return args.cache_dir / f"{safe(args.host)}_{safe(args.metric)}-{safe(args.sensor)}.limits.json"


def save_limits(args, limits):
    path = limits_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(limits))
    tmp.replace(path)


def load_limits(args):
    try:
        return json.loads(limits_path(args).read_text())
    except (OSError, ValueError):
        return None


# --- lmsensors: the sensors of the computer ragdoll runs on, read with `sensors -j` (from the lm-sensors package) ---
LM_TYPES = {"temp": "temperature", "fan": "fan", "in": "voltage", "power": "power", "curr": "power"}
# lm-sensors limits and the limit each one becomes
LM_LIMITS = {"crit": "upper_critical", "max": "upper_warning", "min": "lower_warning", "lcrit": "lower_critical"}


def lm_slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# --- ssh, for lmsensors on another computer ---
# paramiko is used so that one connection can serve every poll. It checks host keys strictly (against ~/.ssh/known_hosts,
# and a host that is not there is refused, never accepted automatically), logs in with the ssh agent and key files and
# never a password, and reads HostName, User, Port, IdentityFile and ProxyCommand from ~/.ssh/config. The ssh command is
# used instead when paramiko is not installed or the config for the host uses ProxyJump, which paramiko does not apply.
_SSH_CLIENTS = {}  # (host, user) -> connected paramiko client, kept for the life of the process


def close_ssh_clients():
    for client in _SSH_CLIENTS.values():
        try:
            client.close()
        except Exception:
            pass
    _SSH_CLIENTS.clear()


atexit.register(close_ssh_clients)


def ssh_config_for(host):
    """What ~/.ssh/config says about host (a dict with lower-case keys), or {} if there is no config."""
    import paramiko
    path = Path.home() / ".ssh" / "config"
    return dict(paramiko.SSHConfig.from_path(str(path)).lookup(host)) if path.exists() else {}


def lm_ssh_command(args, command="sensors -j"):
    """The ssh command that runs command on args.host (the fallback; batch mode, so it never asks for a password)."""
    target = f"{args.user}@{args.host}" if args.user else args.host
    # `--` keeps a host from being read as an ssh option
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "--", target, command]


class SshUnreachable(SourceError):
    """The ssh connection could not be made at all (as opposed to being refused)."""


def ssh_connect(args, password=None):
    """Return a connected paramiko client for args.host, reusing one from an earlier poll if it is still alive.

    Login is by agent and key files, or by password when one is given (the iDRAC's own ssh shell takes passwords).
    """
    import paramiko
    cfg = ssh_config_for(args.host)
    user = args.user or cfg.get("user") or getpass.getuser()
    key = (args.host, user)
    client = _SSH_CLIENTS.get(key)
    if client and client.get_transport() and client.get_transport().is_active():
        return client
    client = paramiko.SSHClient()
    client.load_system_host_keys()  # ~/.ssh/known_hosts
    client.set_missing_host_key_policy(paramiko.RejectPolicy())  # a host key that is not known is refused
    try:
        client.connect(
            hostname=cfg.get("hostname", args.host), port=int(cfg.get("port", 22)), username=user,
            key_filename=[os.path.expanduser(f) for f in cfg.get("identityfile", [])] or None if not password else None,
            sock=paramiko.ProxyCommand(cfg["proxycommand"]) if cfg.get("proxycommand") else None, password=password,
            allow_agent=not password, look_for_keys=not password, timeout=10, banner_timeout=10, auth_timeout=10)
    except paramiko.BadHostKeyException:
        raise SourceError(f"the ssh host key of {args.host} is different from the one in ~/.ssh/known_hosts: "
                          f"it may have been reinstalled, or this may be an attack; check it, then `ssh-keygen -R {args.host}`")
    except paramiko.AuthenticationException:
        if password:
            raise SourceError(f"ssh login to {user}@{args.host} failed: the user name or password was refused")
        raise SourceError(f"ssh login to {user}@{args.host} failed: no key or agent identity was accepted (ssh logs in with "
                          f"a key or the ssh agent, never a password: check that `ssh {user}@{args.host}` works without a prompt)")
    except paramiko.SSHException as e:
        if "not found in known_hosts" in str(e):
            raise SourceError(f"the ssh host key of {args.host} is not in ~/.ssh/known_hosts, and ragdoll will not accept it "
                              f"on its own: run `ssh {user}@{args.host}` once and check the key it shows")
        raise SourceError(f"ssh to {args.host} failed: {e}")
    except (OSError, EOFError) as e:  # no route, refused, timed out, name not found
        raise SshUnreachable(f"could not connect to {args.host} over ssh: {e}")
    _SSH_CLIENTS[key] = client
    return client


def lm_run_remote(args):
    """Run `sensors -j` on args.host: returns (exit status, stdout, stderr)."""
    return ssh_run(args, "sensors -j")


def ssh_run(args, command):
    """Run a shell command on args.host over ssh: returns (exit status, stdout, stderr)."""
    try:
        import paramiko
        use_paramiko = "proxyjump" not in ssh_config_for(args.host)
    except ImportError:
        use_paramiko = False
    if not use_paramiko:  # paramiko is missing, or ~/.ssh/config needs ProxyJump: use the ssh command
        try:
            run = subprocess.run(lm_ssh_command(args, command), capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise SourceError("neither the paramiko module nor the ssh command is available; pip install paramiko")
        except subprocess.TimeoutExpired:
            raise SourceError(f"{command} took too long on {args.host}")
        if run.returncode == 255:  # ssh's own failure
            raise SourceError(f"could not ssh to {args.host}: {run.stderr.strip() or 'connection failed'} (ssh logs in with "
                              "a key or the ssh agent, never a password)")
        return run.returncode, run.stdout, run.stderr
    for attempt in (1, 2):  # a connection kept from an earlier poll may have dropped: reconnect once
        client = ssh_connect(args)
        try:
            _, out, err = client.exec_command(command, timeout=30)
            text, errors = out.read().decode(errors="replace"), err.read().decode(errors="replace")
            return out.channel.recv_exit_status(), text, errors
        except (paramiko.SSHException, EOFError, OSError) as e:
            for known, other in list(_SSH_CLIENTS.items()):  # forget the dead connection
                if other is client:
                    del _SSH_CLIENTS[known]
            client.close()
            if attempt == 2:
                raise SourceError(f"{command} failed over ssh on {args.host}: {e}")


# --- macOS: there is no lm-sensors, so --source lmsensors reads what macOS itself exposes (this computer only) ---
# temperature: the HID temperature sensors (die, board, SSD, battery), read from IOKit with ctypes so that no module has to
#   be installed, and without root. The same sensor can be exposed twice, so readings with one name and location are averaged.
# voltage, power: the battery and charger, from `ioreg -a` (AppleSmartBattery), without root; and CPU, GPU and ANE power from
#   `powermetrics`, which needs root: it is run with `sudo -n` (never a password prompt) and left out if that is refused.
# system power, adapter figures and fans: the SMC (mac_smc), with no root; ioreg's copies of the system and adapter figures
#   are only refreshed about once a minute, so they are not used.
_MAC_IOKIT = {}  # the loaded libraries and their function signatures, filled in by mac_iokit


def mac_iokit():
    """Load CoreFoundation and IOKit once and declare the functions used to read the HID temperature sensors."""
    if _MAC_IOKIT:
        return _MAC_IOKIT["cf"], _MAC_IOKIT["io"]
    paths = ctypes.util.find_library("CoreFoundation"), ctypes.util.find_library("IOKit")
    if not all(paths):
        raise SourceError("could not find the CoreFoundation and IOKit frameworks")
    cf, io = ctypes.CDLL(paths[0]), ctypes.CDLL(paths[1])
    vp, i32, i64 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64
    for lib, name, restype, argtypes in (
            (cf, "CFStringCreateWithCString", vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
            (cf, "CFNumberCreate", vp, [vp, ctypes.c_int, vp]),
            (cf, "CFDictionaryCreate", vp, [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]),
            (cf, "CFArrayGetCount", ctypes.c_long, [vp]), (cf, "CFArrayGetValueAtIndex", vp, [vp, ctypes.c_long]),
            (cf, "CFGetTypeID", ctypes.c_ulong, [vp]), (cf, "CFStringGetTypeID", ctypes.c_ulong, []),
            (cf, "CFNumberGetTypeID", ctypes.c_ulong, []), (cf, "CFRelease", None, [vp]),
            (cf, "CFStringGetCString", ctypes.c_bool, [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
            (cf, "CFNumberGetValue", ctypes.c_bool, [vp, ctypes.c_int, vp]),
            (io, "IOHIDEventSystemClientCreate", vp, [vp]), (io, "IOHIDEventSystemClientSetMatching", None, [vp, vp]),
            (io, "IOHIDEventSystemClientCopyServices", vp, [vp]),
            (io, "IOHIDServiceClientCopyProperty", vp, [vp, vp]),
            (io, "IOHIDServiceClientCopyEvent", vp, [vp, i64, i32, i64]),
            (io, "IOHIDEventGetFloatValue", ctypes.c_double, [vp, i32])):
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    _MAC_IOKIT.update(cf=cf, io=io)
    return cf, io


def mac_hid_temperatures():
    """Return [(sensor name, location id, degrees C)] from every HID temperature sensor that has a reading."""
    cf, io = mac_iokit()
    utf8 = 0x08000100  # kCFStringEncodingUTF8

    def string(text):
        return cf.CFStringCreateWithCString(None, text.encode(), utf8)

    def number(value):
        c = ctypes.c_int32(value)
        return cf.CFNumberCreate(None, 9, ctypes.byref(c))  # kCFNumberSInt32Type

    def prop(service, key):
        k = string(key)
        p = io.IOHIDServiceClientCopyProperty(service, k)
        cf.CFRelease(k)
        if not p:
            return None
        try:
            kind = cf.CFGetTypeID(p)
            if kind == cf.CFStringGetTypeID():
                buf = ctypes.create_string_buffer(128)
                return buf.value.decode(errors="replace") if cf.CFStringGetCString(p, buf, 128, utf8) else None
            if kind == cf.CFNumberGetTypeID():
                c = ctypes.c_int64()
                return c.value if cf.CFNumberGetValue(p, 4, ctypes.byref(c)) else None  # kCFNumberSInt64Type
        finally:
            cf.CFRelease(p)

    objects = [string("PrimaryUsagePage"), string("PrimaryUsage"), number(0xFF00), number(5)]  # vendor page, temperature
    match = cf.CFDictionaryCreate(None, (ctypes.c_void_p * 2)(*objects[:2]), (ctypes.c_void_p * 2)(*objects[2:]), 2, None, None)
    client = io.IOHIDEventSystemClientCreate(None)
    if not client:
        raise SourceError("could not open the HID event system to read the temperature sensors")
    found = []
    try:
        io.IOHIDEventSystemClientSetMatching(client, match)
        services = io.IOHIDEventSystemClientCopyServices(client)
        if services:
            try:
                for i in range(cf.CFArrayGetCount(services)):
                    service = cf.CFArrayGetValueAtIndex(services, i)
                    event = io.IOHIDServiceClientCopyEvent(service, 15, 0, 0)  # kIOHIDEventTypeTemperature
                    if not event:
                        continue  # a sensor with no reading (the ambient light sensor, for one)
                    try:
                        degrees = io.IOHIDEventGetFloatValue(event, 15 << 16)
                    finally:
                        cf.CFRelease(event)
                    name = prop(service, "Product")
                    if name and -40 < degrees < 150:
                        found.append((name, prop(service, "LocationID") or 0, degrees))
            finally:
                cf.CFRelease(services)
    finally:
        cf.CFRelease(client)
        cf.CFRelease(match)
        for obj in objects:
            cf.CFRelease(obj)
    return found


def mac_temperatures():
    """{sensor: Reading}: one sensor per product, or per product and location when it appears at several (-1, -2 ...)."""
    places = {}
    for name, location, degrees in mac_hid_temperatures():
        places.setdefault(lm_slug(name), {}).setdefault(location, []).append(degrees)
    found = {}
    for name, locations in places.items():
        for n, location in enumerate(sorted(locations), 1):
            values = locations[location]
            found[name if len(locations) == 1 else f"{name}-{n}"] = Reading(
                f"hid/{location}", round(sum(values) / len(values), 2), None)
    return found


def mac_battery():
    """The AppleSmartBattery registry entry as a dict, or {} on a Mac without a battery."""
    try:
        run = subprocess.run(["ioreg", "-a", "-r", "-n", "AppleSmartBattery"], capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise SourceError(f"could not run ioreg: {e}")
    if run.returncode != 0 or not run.stdout.strip():
        return {}
    try:
        entries = plistlib.loads(run.stdout)
    except Exception as e:  # plistlib raises several kinds
        raise SourceError(f"ioreg did not return a property list: {e}")
    return entries[0] if entries else {}


def mac_powermetrics():
    """{sensor: Reading} of CPU, GPU, ANE and combined power in watts. Needs root, so sudo -n; {} when that is refused."""
    command = ["powermetrics", "--samplers", "cpu_power,gpu_power,ane_power", "-n", "1", "-i", "500"]
    if os.geteuid() != 0:
        command = ["sudo", "-n"] + command
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if run.returncode != 0:
        return {}
    found = {}
    for label, name in (("CPU", "cpu-power"), ("GPU", "gpu-power"), ("ANE", "ane-power"),
                        (r"Combined Power \(CPU \+ GPU \+ ANE", "package-power")):
        m = re.search(rf"^{label}(?: Power)?\)?: (\d+) mW", run.stdout, re.M)
        if m:
            found[name] = Reading(f"powermetrics/{name}", int(m.group(1)) / 1000, None)
    return found


def _signed(value):
    """ioreg prints a negative 64-bit current (the battery discharging) as a huge unsigned number."""
    return value - (1 << 64) if value >= 1 << 63 else value


def mac_read(args):
    """Return {sensor: Reading} for args.metric on a Mac (see the comment above mac_iokit)."""
    if args.metric == "temperature":
        return mac_temperatures()
    if args.metric == "fan":
        return mac_fans()
    battery = mac_battery()
    found = {}

    def add(name, value, divisor, source):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found[name] = Reading(f"{source}/{name}", round(value / divisor, 4), None)

    try:  # live system and adapter figures; ioreg's own copies of them refresh only about once a minute
        with mac_smc() as read:
            if args.metric == "voltage":
                add("adapter-voltage", read("VD0R"), 1, "smc")
            else:
                add("system-power", read("PSTR"), 1, "smc")
                add("adapter-power", read("PDTR"), 1, "smc")
                add("adapter-current", read("ID0R"), 1, "smc")
    except SourceError as e:
        print(f"lmsensors: {e}; the adapter and system figures are left out", file=sys.stderr)
    if args.metric == "voltage":
        add("battery-voltage", battery.get("Voltage"), 1000, "ioreg")
    else:
        amps = battery.get("InstantAmperage", battery.get("Amperage"))
        if isinstance(amps, int) and isinstance(battery.get("Voltage"), int):
            amps = _signed(amps) / 1000  # negative while the battery discharges
            found["battery-current"] = Reading("ioreg/battery-current", round(amps, 4), None)
            found["battery-power"] = Reading("ioreg/battery-power", round(amps * battery["Voltage"] / 1000, 4), None)
        if getattr(args, "sensor", None) not in found:  # powermetrics needs sudo and half a second: only when it is wanted
            found.update(mac_powermetrics())
    return found


# The SMC is read with one struct call (IOConnectCallStructMethod, selector 2): data8 9 asks for a key's type and size,
# data8 5 for its bytes. The struct is 80 bytes (the keys are four-character codes such as F0Ac, fan 0's actual speed).
class _SmcVers(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint8), ("minor", ctypes.c_uint8), ("build", ctypes.c_uint8),
                ("reserved", ctypes.c_uint8), ("release", ctypes.c_uint16)]


class _SmcLimits(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint16), ("length", ctypes.c_uint16), ("cpu", ctypes.c_uint32),
                ("gpu", ctypes.c_uint32), ("mem", ctypes.c_uint32)]


class _SmcInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("type", ctypes.c_uint32), ("attributes", ctypes.c_uint8)]


class _SmcKey(ctypes.Structure):
    _fields_ = [("key", ctypes.c_uint32), ("vers", _SmcVers), ("limits", _SmcLimits), ("info", _SmcInfo),
                ("result", ctypes.c_uint8), ("status", ctypes.c_uint8), ("data8", ctypes.c_uint8),
                ("data32", ctypes.c_uint32), ("bytes", ctypes.c_uint8 * 32)]


@contextlib.contextmanager
def mac_smc():
    """Open the SMC and yield read(key): the number in that key, or None if this Mac does not have it."""
    cf, io = mac_iokit()
    libsystem = ctypes.CDLL(ctypes.util.find_library("System"))
    vp, uint = ctypes.c_void_p, ctypes.c_uint
    io.IOServiceMatching.restype, io.IOServiceMatching.argtypes = vp, [ctypes.c_char_p]
    io.IOServiceGetMatchingService.restype, io.IOServiceGetMatchingService.argtypes = uint, [uint, vp]
    io.IOServiceOpen.restype, io.IOServiceOpen.argtypes = ctypes.c_int, [uint, uint, uint, ctypes.POINTER(uint)]
    io.IOServiceClose.restype, io.IOServiceClose.argtypes = ctypes.c_int, [uint]
    io.IOObjectRelease.restype, io.IOObjectRelease.argtypes = ctypes.c_int, [uint]
    io.IOConnectCallStructMethod.restype = ctypes.c_int
    io.IOConnectCallStructMethod.argtypes = [uint, uint, vp, ctypes.c_size_t, vp, ctypes.POINTER(ctypes.c_size_t)]
    service = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
    if not service:
        raise SourceError("this Mac has no AppleSMC to read")
    connection = uint()
    try:
        status = io.IOServiceOpen(service, uint.in_dll(libsystem, "mach_task_self_").value, 0, ctypes.byref(connection))
    finally:
        io.IOObjectRelease(service)
    if status != 0:
        raise SourceError(f"could not open the SMC (IOServiceOpen returned {status:#x})")

    def call(request):
        reply, size = _SmcKey(), ctypes.c_size_t(ctypes.sizeof(_SmcKey))
        failed = io.IOConnectCallStructMethod(connection.value, 2, ctypes.byref(request), ctypes.sizeof(_SmcKey),
                                              ctypes.byref(reply), ctypes.byref(size))
        return None if failed or reply.result else reply

    def read(name):
        request = _SmcKey(key=struct.unpack(">I", name.encode())[0], data8=9)
        info = call(request)
        if info is None:
            return None
        request.info.size, request.data8 = info.info.size, 5
        reply = call(request)
        if reply is None:
            return None
        kind, raw = struct.pack(">I", info.info.type).decode(errors="replace"), bytes(reply.bytes[:info.info.size])
        if kind == "flt " and len(raw) == 4:  # Apple silicon: a little-endian float
            return struct.unpack("<f", raw)[0]
        if kind == "fpe2" and len(raw) == 2:  # Intel: unsigned fixed point with two fraction bits
            return int.from_bytes(raw, "big") / 4
        if kind in ("ui8 ", "ui16", "ui32", "si8 ", "si16", "si32"):
            return int.from_bytes(raw, "big", signed=kind.startswith("si"))
        return None

    try:
        yield read
    finally:
        io.IOServiceClose(connection.value)


def mac_fans():
    """{fan1: Reading, ...} in RPM; none on a Mac without fans. A stopped fan reads 0, which is real (idle MacBook Pro)."""
    with mac_smc() as read:
        return {f"fan{n}": Reading(f"smc/F{n - 1}Ac", round(read(f"F{n - 1}Ac") or 0), None)
                for n in range(1, int(read("FNum") or 0) + 1) if read(f"F{n - 1}Ac") is not None}


def lm_read(args):
    """Return {sensor: Reading} for args.metric from `sensors -j`. A sensor is named <chip>-<label>."""
    if platform.system() == "Darwin" and not args.remote:
        return mac_read(args)
    where = f"on {args.host}" if args.remote else "on this computer"
    if args.remote:
        status, stdout, stderr = lm_run_remote(args)
    else:
        try:
            run = subprocess.run(["sensors", "-j"], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise SourceError("the sensors command was not found; install lm-sensors (for example: sudo apt install lm-sensors)")
        except subprocess.TimeoutExpired:
            raise SourceError("sensors -j took too long")
        status, stdout, stderr = run.returncode, run.stdout, run.stderr
    if status == 127:
        raise SourceError(f"the sensors command was not found {where}; install lm-sensors there")
    if status != 0:
        raise SourceError(f"sensors -j failed {where}: {stderr.strip() or status}")
    try:
        chips = json.loads(stdout)
    except ValueError:
        raise SourceError(f"sensors -j {where} did not return JSON (this lm-sensors may be too old; it needs version 3.5 or newer)")
    found = {}
    for chip, features in chips.items():
        for label, subfeatures in features.items():
            if not isinstance(subfeatures, dict):
                continue  # the "Adapter" entry is a string
            for key, value in subfeatures.items():
                kind = re.fullmatch(r"([a-z]+)\d+_(input|average)", key)
                if not kind or LM_TYPES.get(kind.group(1)) != args.metric:
                    continue
                prefix = key.rsplit("_", 1)[0]
                name = lm_slug(f"{chip}-{label}") + ("-current" if kind.group(1) == "curr" else "")
                limits = {LM_LIMITS[s]: float(subfeatures[f"{prefix}_{s}"]) for s in LM_LIMITS if f"{prefix}_{s}" in subfeatures}
                found[name] = Reading(f"{chip}/{label}", float(value), limits or None)
    return found


def lm_list(args):
    return lm_read(args)


def default_sensor(args, metric):
    """The sensor to use when --sensor is not given: the metric's default, or for lmsensors a sensible local one."""
    if args.source != "lmsensors":
        return metric["default"]
    names = sorted(lm_read(args))
    if not names:
        raise SourceError(f"lm-sensors {'on ' + args.host if args.remote else 'on this computer'} has no {args.metric} sensors")
    for hint in ("package-id-0", "tctl", "composite", "pmu-tdie0", "system-power", "battery-voltage", "cpu"):  # the CPU, or the main drive
        for name in names:
            if hint in name:
                return name
    return names[0]


def read_current(args):
    """Read every sensor of args.metric right now from a source that reports current values: {sensor: Reading}."""
    if args.source == "snmp":
        return asyncio.run(snmp_read_table(args))
    if args.source == "lmsensors":
        return lm_read(args)
    raise SourceError(f"the {args.source} source has no live value to read")


def record_current(args, table):
    """Append the current reading of args.sensor to the cached history; return the CSV (these sources keep none)."""
    if args.sensor not in table:
        raise SourceError(f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(table))}")
    reading = table[args.sensor]
    if reading.limits:
        save_limits(args, reading.limits)
    history = read_cache(cache_path(args)) or "Average,Peak,Time\n"
    if not history.endswith("\n"):
        history += "\n"
    return history + f"{num(reading.value)},{num(reading.value)},{epoch_to_csv(int(time.time()))}\n"


def lm_fetch(args):
    """Take one reading from this computer's sensors and append it to the cached history."""
    return record_current(args, lm_read(args))


def snmp_fetch(args):
    """Take one reading and append it to the cached history (SNMP has no history of its own)."""
    return record_current(args, asyncio.run(snmp_read_table(args)))


# Each source provides list(args) -> {sensor: Reading} and fetch(args) -> CSV text
# ("Average,Peak,Time" rows). accumulate=True means fetch() returns only a current reading
# appended to the cached history. Others (redfish, ipmi, ...) can be added here.
SOURCE_ALIASES = {"gui": "web"}  # alternative names accepted for --source and --list
SOURCES = {
    "web": {"list": web_list, "fetch": web_fetch, "accumulate": False, "metrics": ("temperature",)},
    "snmp": {"list": snmp_list, "fetch": snmp_fetch, "accumulate": True, "metrics": METRIC_NAMES},
    # the sensors of this computer: no host, no credentials, and only listed when asked for by name
    "lmsensors": {"list": lm_list, "fetch": lm_fetch, "accumulate": True, "local": True,
                  "metrics": ("temperature", "fan", "power", "voltage")},
    # Redfish provides the inventory only (see read_inventory_redfish), so it has no metrics
    "redfish": {"list": lambda args: redfish_list(args), "fetch": lambda args: redfish_fetch(args),
                "accumulate": False, "metrics": ()},
    # racadm, too, provides the inventory only (see read_inventory_racadm)
    "racadm": {"list": lambda args: racadm_list(args), "fetch": lambda args: racadm_fetch(args),
               "accumulate": False, "metrics": ()},
    # and so does wsman (see read_inventory_wsman)
    "wsman": {"list": lambda args: wsman_list(args), "fetch": lambda args: wsman_fetch(args),
              "accumulate": False, "metrics": ()},
    # the inventory of this computer (or --host over ssh) from system_profiler or dmidecode (see read_inventory_system)
    "system": {"list": lambda args: system_list(args), "fetch": lambda args: system_fetch(args),
               "accumulate": False, "local": True, "metrics": ()},
}


def parse_rows(text):
    """Parse 'Average,Peak,Time' CSV (UTC times) into [(unix time, average, peak)]."""
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        try:
            parsed = (csv_to_epoch(row["Time"].strip()), float(row["Average"]), float(row["Peak"]))
        except (KeyError, TypeError, ValueError):
            continue
        if is_reading(parsed[1], parsed[2]):
            out.append(parsed)
    return out


def short_label(when):
    """'Sep 19 10:12:52' for a datetime."""
    return when.strftime("%b %d %H:%M:%S")


def graph_termgraph(data, width, height, chart, title, unit, limits=None):
    if chart not in CHART_FLAGS:
        sys.exit(f"termgraph does not support --chart {chart}; use one of {', '.join(sorted(CHART_FLAGS))}")
    if shutil.which("termgraph") is None:
        sys.exit("termgraph not found; install with: pip install termgraph")
    bar_width = max(width - TERMGRAPH_LABEL_COLS, 10)
    flat = len({v for _, avg, peak in data for v in (avg, peak)}) == 1
    extra = []
    with tempfile.NamedTemporaryFile("w", suffix=".dat") as f:
        f.write("@ Average,Peak\n")
        for when, avg, peak in data:
            # termgraph splits on whitespace, so keep each label to one token
            name = short_label(when).replace(" ", "_")
            if flat:
                # termgraph 0.7.6 does not scale a series whose values are all equal (it draws one
                # block per unit), so draw a full-width bar and carry the real value in the label
                name += f"_{num(avg)}{unit.replace(' ', '_')}"
                avg = peak = bar_width
            f.write(f"{name},{avg},{peak}\n")
        if flat:
            extra = ["--no-values"]
        f.flush()
        subprocess.run(["termgraph", f.name, "--width", str(bar_width),
                        "--suffix", unit, "--title", title, *extra,
                        *CHART_FLAGS[chart]], check=True)


LIMIT_LINES = [("upper_critical", "Upper critical", "red"), ("upper_warning", "Upper warning", "orange"),
               ("lower_warning", "Lower warning", "orange"), ("lower_critical", "Lower critical", "red")]


def graph_plotext(data, width, height, chart, title, unit, limits=None):
    try:
        import plotext as plt
    except ImportError:
        sys.exit("plotext not found; install with: pip install plotext")
    labels = [short_label(when) for when, _, _ in data]
    avg = [a for _, a, _ in data]
    peak = [p for _, _, p in data]
    names = ["Average", "Peak"]

    if chart in ("line", "scatter"):
        plot = plt.plot if chart == "line" else plt.scatter
        # plotext's own date axis shifts every label by this computer's UTC offset, so plot Unix times and
        # label the axis ourselves, in the zone the datetimes are already in
        epochs = [when.timestamp() for when, _, _ in data]
        plot(epochs, avg, label=names[0])
        plot(epochs, peak, label=names[1])
        zone = data[0][0].tzinfo  # None means this computer's local time
        usable = min(width, plt.terminal_width())  # plotext never draws wider than the terminal
        count = max(2, (usable - 8) // 24)  # labels are 19 characters wide, so 3 fit in 80 columns
        ticks = [epochs[0] + (epochs[-1] - epochs[0]) * i / (count - 1) for i in range(count)]
        plt.xticks(ticks, [dt.datetime.fromtimestamp(t, zone).strftime("%d/%m/%Y %H:%M:%S") for t in ticks])
        for key, text, colour in LIMIT_LINES:
            if limits and key in limits:  # horizontal lines; the chart's scale widens to include them
                plot([epochs[0], epochs[-1]], [limits[key]] * 2, label=f"{text} {num(limits[key])}{unit}", color=colour)
    elif chart == "vertical":
        plt.multiple_bar(labels, [avg, peak], labels=names)
    elif chart == "horizontal":
        plt.multiple_bar(labels, [avg, peak], labels=names, orientation="horizontal")
    elif chart == "stacked":
        plt.stacked_bar(labels, [avg, peak], labels=names)
    elif chart == "histogram":
        plt.hist(avg, bins=min(20, max(len(set(avg)), 1)), label=names[0])
    plt.title(title)
    plt.ylabel(unit.strip()) if chart != "horizontal" else plt.xlabel(unit.strip())
    # leave one row free for the shell prompt
    plt.plot_size(width, height - 1)
    plt.theme("clear")
    plt.show()


MODULES = {"plotext": graph_plotext, "termgraph": graph_termgraph}


def display_rows(args, metric, rows):
    """Return (rows as (datetime, average, peak), time zone name, title) for the chart and table outputs.

    Stored times are UTC; they are shown in --tz, UTC with --utc, or this computer's local time.
    """
    zone = dt.timezone.utc if args.utc else args.tz
    data = [(dt.datetime.fromtimestamp(t, zone), avg, peak) for t, avg, peak in rows]
    zone_name = data[-1][0].astimezone().tzname() if zone is None else data[-1][0].tzname()
    name = " ".join(w.upper() if re.match(r"(cpu|ps)\d*$", w) else w.title() for w in args.sensor.split("-"))
    title = f"{args.host} {name} {metric['label']} (Average / Peak) [{zone_name}]".replace("  ", " ")
    return data, zone_name, title


def output_chart(args, metric, rows):
    """Draw the readings as a chart in the terminal."""
    data, _, title = display_rows(args, metric, rows)
    limits = None
    if args.limits:
        if args.module != "plotext" or args.chart not in ("line", "scatter"):
            print("warning: --limits only applies to plotext line and scatter charts", file=sys.stderr)
        else:
            limits = load_limits(args)
            if not limits:
                print(f"warning: no limits saved for {args.sensor}; poll it with --source snmp first "
                      "(not every sensor has limits)", file=sys.stderr)
    MODULES[args.module](data, args.width, args.height, args.chart, title, metric["unit"](args.sensor), limits)


def output_raw(args, metric, rows):
    """Print the selected readings in the cache format: Average,Peak,Time with UTC times."""
    sys.stdout.write(to_csv(rows))


def json_number(value):
    """A reading as a JSON number: 23.0 -> 23, 0.2 -> 0.2, a missing value -> null."""
    if value is None:
        return None
    value = float(value)
    return int(value) if value.is_integer() else value


def iso_time(args, t):
    """ISO 8601 time with its UTC offset, in --tz, UTC with --utc, or this computer's local time."""
    when = dt.datetime.fromtimestamp(t, dt.timezone.utc if args.utc else args.tz)
    return (when if when.tzinfo else when.astimezone()).isoformat(timespec="seconds")


def dump_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def output_json(args, metric, rows):
    """Print the selected readings as a JSON array, one object per reading, times as in --output csv."""
    unit = metric["unit"](args.sensor).strip()
    dump_json([{"time": iso_time(args, t), "host": args.host, "source": args.source, "metric": args.metric,
                "sensor": args.sensor, "average": json_number(avg), "peak": json_number(peak), "unit": unit}
               for t, avg, peak in rows])


def output_csv(args, metric, rows):
    """Print the selected readings as CSV with one self-describing row per reading, times in --tz/--utc/local."""
    zone = dt.timezone.utc if args.utc else args.tz
    unit = metric["unit"](args.sensor).strip()
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["time", "host", "source", "metric", "sensor", "average", "peak", "unit"])
    for t, avg, peak in rows:
        when = dt.datetime.fromtimestamp(t, zone)
        if when.tzinfo is None:  # this computer's local time: attach its offset so the row is unambiguous
            when = when.astimezone()
        writer.writerow([when.isoformat(timespec="seconds"), args.host, args.source, args.metric, args.sensor,
                         num(avg), num(peak), unit])


def period_label(last):
    """Describe --last for a file name: 10 rows -> '10rows', 0 rows -> 'all', a week -> '1w'."""
    kind, n = last
    if kind == "rows":
        return f"{n}rows" if n else "all"
    for suffix, seconds in (("y", 365 * 86400), ("mo", 30 * 86400), ("w", 7 * 86400), ("d", 86400), ("h", 3600)):
        if n % seconds == 0:
            return f"{n // seconds}{suffix}"
    return f"{n}s"


def spreadsheet_path(args, extension):
    """Return the output file for a spreadsheet: --file, or a name made from the host, source, sensor and period."""
    if args.file:
        path = Path(args.file)
        return path if path.suffix else path.with_suffix(extension)
    safe = lambda v: re.sub(r"[^\w.-]", "_", v)
    return Path(f"{safe(args.host)}_{safe(args.source)}_{safe(args.metric)}-{safe(args.sensor)}"
                f"_last-{period_label(args.last)}{extension}")


def spreadsheet_rows(args, metric, rows):
    """Return (column headings, rows of [time in the display zone, time in UTC, average, peak]) with naive datetimes.

    Spreadsheets have no time zone type, so both times are given and the zone is named in the heading.
    """
    data, zone_name, _ = display_rows(args, metric, rows)
    unit = metric["unit"](args.sensor).strip()
    head = [f"Time ({zone_name})", "Time (UTC)", f"Average ({unit})", f"Peak ({unit})"]
    utc = dt.timezone.utc
    body = [[when.replace(tzinfo=None), dt.datetime.fromtimestamp(t, utc).replace(tzinfo=None), avg, peak]
            for (when, avg, peak), (t, _, _) in zip(data, rows)]
    return head, body


SPREADSHEET_TIME_FORMAT = "yyyy-mm-dd hh:mm:ss"
XLS_MAX_ROWS = 65535  # the old .xls format allows 65,536 rows including the heading


def output_xlsx(args, metric, rows):
    """Write the readings to an Excel .xlsx workbook with XlsxWriter."""
    try:
        import xlsxwriter
    except ImportError:
        sys.exit("XlsxWriter not found; install with: pip install XlsxWriter")
    path = spreadsheet_path(args, ".xlsx")
    head, body = spreadsheet_rows(args, metric, rows)
    try:
        with xlsxwriter.Workbook(path) as book:
            sheet = book.add_worksheet("Readings")
            bold = book.add_format({"bold": True})
            when = book.add_format({"num_format": SPREADSHEET_TIME_FORMAT})
            sheet.write_row(0, 0, head, bold)
            for r, row in enumerate(body, start=1):
                sheet.write_datetime(r, 0, row[0], when)
                sheet.write_datetime(r, 1, row[1], when)
                sheet.write_number(r, 2, row[2])
                sheet.write_number(r, 3, row[3])
            sheet.set_column(0, 1, 20)
            sheet.set_column(2, 3, 16)
            sheet.freeze_panes(1, 0)
    except (OSError, xlsxwriter.exceptions.XlsxWriterException) as e:
        sys.exit(f"Could not write {path}: {e}")
    print(f"Wrote {len(body)} readings to {path}")


def output_xls(args, metric, rows):
    """Write the readings to an Excel 97-2003 .xls workbook with xlwt."""
    try:
        import xlwt
    except ImportError:
        sys.exit("xlwt not found; install with: pip install xlwt")
    if len(rows) > XLS_MAX_ROWS:
        sys.exit(f"{len(rows)} readings do not fit in an .xls file (limit {XLS_MAX_ROWS}); "
                 "use --output xlsx or choose fewer with --last")
    path = spreadsheet_path(args, ".xls")
    head, body = spreadsheet_rows(args, metric, rows)
    book = xlwt.Workbook()
    sheet = book.add_sheet("Readings")
    bold = xlwt.easyxf("font: bold on")
    when = xlwt.easyxf(num_format_str=SPREADSHEET_TIME_FORMAT.upper())
    for c, text in enumerate(head):
        sheet.write(0, c, text, bold)
    for r, row in enumerate(body, start=1):
        sheet.write(r, 0, row[0], when)
        sheet.write(r, 1, row[1], when)
        sheet.write(r, 2, row[2])
        sheet.write(r, 3, row[3])
    for c, chars in enumerate((20, 20, 16, 16)):
        sheet.col(c).width = 256 * chars
    try:
        book.save(str(path))
    except OSError as e:
        sys.exit(f"Could not write {path}: {e}")
    print(f"Wrote {len(body)} readings to {path}")


def output_table(args, metric, rows):
    """Print the readings as a table, oldest first, using terminaltables."""
    try:
        from terminaltables import AsciiTable
    except ImportError:
        sys.exit("terminaltables not found; install with: pip install terminaltables")
    data, zone_name, title = display_rows(args, metric, rows)
    unit = metric["unit"](args.sensor)
    table = AsciiTable([[f"Time ({zone_name})", f"Average ({unit.strip()})", f"Peak ({unit.strip()})"]]
                       + [[when.strftime("%Y-%m-%d %H:%M:%S"), num(avg), num(peak)] for when, avg, peak in data])
    table.justify_columns = {0: "left", 1: "right", 2: "right"}
    # printed above the table: terminaltables silently drops a title that is wider than the table
    print(title)
    print(table.table)


# Each output is a function output(args, metric, rows) that presents the selected readings, where rows is a list of
# (unix time in UTC, average, peak) and metric is the METRICS entry. Add new kinds of output (a table, JSON, ...)
# here and they become available through --output.
def output_db(args, metric, rows):
    """--output db: the readings are in the database (main stores them); say so. Never draws anything."""
    print(f"{len(rows)} readings of {args.sensor} ({args.metric}, {args.source}) are in {args.db}")


OUTPUTS = {"chart": output_chart, "table": output_table, "raw": output_raw, "csv": output_csv, "json": output_json,
           "xlsx": output_xlsx, "xls": output_xls, "db": output_db}


def format_limits(limits):
    """'warn 3..42 crit -7..47' (lower..upper, '-' where a limit is missing), or '-' if there are none."""
    if not limits:
        return "-"
    g = lambda key: num(limits[key]) if key in limits else "-"
    return f"warn {g('lower_warning')}..{g('upper_warning')} crit {g('lower_critical')}..{g('upper_critical')}"


def cached_value(args):
    """The newest reading in the cache for the current host, source, metric and sensor, or None."""
    text = read_cache(cache_path(args))
    rows = parse_rows(text) if text else []
    return rows[-1][1] if rows else None


LIST_HEADER = ("SOURCE", "METRIC", "SENSOR", "UNIT", "VALUE", "LIMITS", "KEY")
LIMIT_KEYS = ("lower_critical", "lower_warning", "upper_warning", "upper_critical")


def list_display_rows(records):
    """Rows of strings for the text and table outputs: '-' for a missing value, limits as 'warn a..b crit c..d'."""
    return [(source, metric, sensor, unit, "-" if value is None else num(value), format_limits(limits), key)
            for source, metric, sensor, unit, value, limits, key in records]


def list_data_rows(records):
    """Rows for the CSV and spreadsheet outputs: numbers kept as numbers and the limits in four columns of their own.

    Returns (headings, rows); a missing value or limit is None.
    """
    head = ("source", "metric", "sensor", "unit", "value") + LIMIT_KEYS + ("key",)
    rows = [(source, metric, sensor, unit, value, *[(limits or {}).get(k) for k in LIMIT_KEYS], key)
            for source, metric, sensor, unit, value, limits, key in records]
    return head, rows


def select_field(args, head, rows):
    """Keep only the column named by --field (not case-sensitive); return (head, rows) unchanged without --field."""
    if not args.field:
        return head, rows
    names = [str(h).lower() for h in head]
    if args.field.lower() not in names:
        sys.exit(f"--field {args.field!r} is not a field of this output; the fields are: {', '.join(names)}")
    i = names.index(args.field.lower())
    return [head[i]], [[row[i]] for row in rows]


def emit_text(head, rows, right=()):
    """Aligned columns of plain text. The last column is not padded; columns listed in right are right-aligned.

    A single column (from --field) is printed as bare values, one per line, with no heading, so it can be used in a script.
    """
    if len(head) == 1:
        for row in rows:
            print(row[0])
        return
    widths = [max(len(r[i]) for r in [head] + rows) for i in range(len(head) - 1)]
    for row in [head] + rows:
        cells = [c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(row, widths))]
        print("  ".join(cells) + "  " + row[-1])


def emit_table(head, rows, right=()):
    """A bordered table using terminaltables (plain ASCII, so it survives pipes and redirects)."""
    try:
        from terminaltables import AsciiTable
    except ImportError:
        sys.exit("terminaltables not found; install with: pip install terminaltables")
    table = AsciiTable([list(head)] + [list(r) for r in rows])
    table.justify_columns = {i: "right" for i in right}
    print(table.table)


def emit_csv(head, rows):
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(head)
    for row in rows:
        writer.writerow(["" if v is None else num(v) if isinstance(v, (int, float)) else v for v in row])


def column_width(heading, values):
    """Width in characters for a spreadsheet column: the longest of the heading and the values, plus a margin."""
    return max([len(heading)] + [len(str(v)) for v in values if v is not None]) + 2


def emit_spreadsheet(kind, path, sheet_name, head, rows, noun):
    """Write head and rows to an .xlsx or .xls workbook with one sheet; a None cell is left empty."""
    if kind == "xlsx":
        try:
            import xlsxwriter
        except ImportError:
            sys.exit("XlsxWriter not found; install with: pip install XlsxWriter")
        try:
            with xlsxwriter.Workbook(path) as book:
                sheet = book.add_worksheet(sheet_name)
                sheet.write_row(0, 0, head, book.add_format({"bold": True}))
                for r, row in enumerate(rows, start=1):
                    for c, v in enumerate(row):
                        if v is not None:
                            sheet.write(r, c, v)
                for c, h in enumerate(head):
                    sheet.set_column(c, c, column_width(h, [row[c] for row in rows]))
                sheet.freeze_panes(1, 0)
        except (OSError, xlsxwriter.exceptions.XlsxWriterException) as e:
            sys.exit(f"Could not write {path}: {e}")
    else:
        try:
            import xlwt
        except ImportError:
            sys.exit("xlwt not found; install with: pip install xlwt")
        book = xlwt.Workbook()
        sheet = book.add_sheet(sheet_name)
        bold = xlwt.easyxf("font: bold on")
        for c, h in enumerate(head):
            sheet.write(0, c, h, bold)
            sheet.col(c).width = 256 * column_width(h, [row[c] for row in rows])
        for r, row in enumerate(rows, start=1):
            for c, v in enumerate(row):
                if v is not None:
                    sheet.write(r, c, v)
        try:
            book.save(str(path))
        except OSError as e:
            sys.exit(f"Could not write {path}: {e}")
    print(f"Wrote {len(rows)} {noun}{'s' if len(rows) != 1 else ''} to {path}")


def list_text(args, records):
    """The default: aligned columns of plain text, with VALUE right-aligned."""
    head, rows = select_field(args, list(LIST_HEADER), list_display_rows(records))
    emit_text(head, rows, right=tuple(i for i, h in enumerate(head) if h == "VALUE"))


def list_table(args, records):
    head, rows = select_field(args, list(LIST_HEADER), list_display_rows(records))
    emit_table(head, rows, right=tuple(i for i, h in enumerate(head) if h == "VALUE"))


def list_csv(args, records):
    emit_csv(*select_field(args, *list_data_rows(records)))


def list_json(args, records):
    """The sensors as a JSON array. The fields are those of the text output, with the limits as an object."""
    head = [h.lower() for h in LIST_HEADER]
    rows = [[source, metric, sensor, unit, json_number(value),
             {k: json_number(limits.get(k)) for k in LIMIT_KEYS} if limits else None, key]
            for source, metric, sensor, unit, value, limits, key in records]
    head, rows = select_field(args, head, rows)
    dump_json([dict(zip(head, row)) for row in rows])


def list_spreadsheet_path(args, extension):
    """--file, or <host>_sensors[_<source>][_<metric>] in the current directory."""
    if args.file:
        path = Path(args.file)
        return path if path.suffix else path.with_suffix(extension)
    safe = lambda v: re.sub(r"[^\w.-]", "_", v)
    source = args.source or (args.list if args.list in SOURCES else None)
    metric = args.metric or (args.list if args.list in METRIC_NAMES else None)
    return Path("_".join([safe(args.host), "sensors"] + [safe(p) for p in (source, metric) if p]) + extension)


def list_xlsx(args, records):
    head, rows = select_field(args, *list_data_rows(records))
    emit_spreadsheet("xlsx", list_spreadsheet_path(args, ".xlsx"), "Sensors", head, rows, "sensor")


def list_xls(args, records):
    head, rows = select_field(args, *list_data_rows(records))
    emit_spreadsheet("xls", list_spreadsheet_path(args, ".xls"), "Sensors", head, rows, "sensor")


LIST_OUTPUTS = {"text": list_text, "table": list_table, "csv": list_csv, "json": list_json, "xlsx": list_xlsx,
                "xls": list_xls}


# --- Redfish: the same inventory over the iDRAC's Redfish API ---
# Every request takes 5-9 s with basic authentication on an iDRAC8 (it checks the password each time), but only
# 0.3-0.9 s with a session token, so one session is made per run (about 10 s) and used for all requests.
REDFISH_HEALTH = {"OK": "ok", "Warning": "non-critical", "Critical": "critical"}
REDFISH_VOLUME_TYPES = {"NonRedundant": "RAID 0", "Mirrored": "RAID 1", "StripedWithParity": "RAID 5 or RAID 6",
                        "SpannedMirrors": "RAID 10", "SpannedStripesWithParity": "RAID 50 or RAID 60",
                        "RawDevice": "raw device"}


@contextlib.contextmanager
def redfish_session(args):
    """Log in to Redfish with a session token and yield get(path) -> parsed JSON; log out afterwards."""
    resolve_credentials(args)
    if not args.user or not args.password:
        raise SourceError("the redfish source needs credentials: --user/--pass, $IDRAC_USER/$IDRAC_PASS, "
                          "or ones saved with --save-credentials")
    if not args.secure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    base = f"https://{args.host}"
    session = requests.Session()
    session.verify = args.secure
    try:
        # the iDRAC8 answers this POST with 405 although the session is created, so the token is what counts
        reply = session.post(f"{base}/redfish/v1/SessionService/Sessions",
                             json={"UserName": args.user, "Password": args.password}, timeout=60)
    except requests.RequestException as e:
        raise SourceError(f"could not log in to Redfish: {e}")
    token, location = reply.headers.get("X-Auth-Token"), reply.headers.get("Location")
    if not token:
        raise SourceError("Redfish login failed (check --user/--pass)")
    session.headers["X-Auth-Token"] = token

    def get(path):
        try:
            resp = session.get(base + path, timeout=60)
        except requests.RequestException as e:
            raise SourceError(f"Redfish request for {path} failed: {e}")
        if resp.status_code != 200:
            raise SourceError(f"Redfish request for {path} returned {resp.status_code}")
        return resp.json()

    try:
        yield get
    finally:
        if location:
            try:
                session.delete(location if location.startswith("http") else base + location, timeout=30)
            except requests.RequestException:
                pass


def _members(get, collection):
    """The paths of the members of a Redfish collection given as a link or a path."""
    path = collection["@odata.id"] if isinstance(collection, dict) else collection
    return [m["@odata.id"] for m in get(path).get("Members", [])]


def _health(resource):
    return REDFISH_HEALTH.get((resource.get("Status") or {}).get("Health") or "")


def _rf(*pairs):
    """Build a details list from (attribute, value) pairs, leaving out the ones with no value."""
    return [(a, str(v)) for a, v in pairs if v not in (None, "")]


def _rf_gib(value, per_gib):
    return _gib(per_gib)(value) if value else None


def _rf_mhz(value):
    return f"{int(value)} MHz" if value else None


def read_inventory_redfish(args, only_category=None):
    """Return the inventory (see read_inventory) over Redfish; the categories match the SNMP inventory."""
    want = lambda category: only_category in (None, category)
    records = []
    with redfish_session(args) as get, concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        def fetch(paths):
            return list(pool.map(get, paths))

        system_path = _members(get, "/redfish/v1/Systems")[0]
        system = get(system_path)
        if want("system"):
            records.append(("system", "system", _rf(
                ("model", system.get("Model")), ("name", system.get("HostName")), ("service-tag", system.get("SKU")),
                ("manufacturer", system.get("Manufacturer")), ("power", str(system.get("PowerState") or "").lower()),
                ("status", _health(system)))))
        if want("idrac"):
            manager = get(_members(get, "/redfish/v1/Managers")[0])
            records.append(("idrac", "idrac", _rf(("product", manager.get("Model")),
                                                  ("firmware", manager.get("FirmwareVersion")),
                                                  ("status", _health(manager)))))
        if want("bios"):
            records.append(("bios", "bios", _rf(("version", system.get("BiosVersion")))))
        if want("firmware"):
            installed = [p for p in _members(get, "/redfish/v1/UpdateService/FirmwareInventory") if "/Installed-" in p]
            for item in fetch(installed):
                if item.get("Name") == "BIOS":  # already shown as the bios item, and would match --name bios twice
                    continue
                records.append(("firmware", item.get("Name") or item.get("Id"),
                                _rf(("version", item.get("Version")), ("status", _health(item)))))
        if want("cpu"):
            for cpu in fetch(_members(get, system["Processors"])):
                records.append(("cpu", cpu.get("Id"), _rf(
                    ("manufacturer", cpu.get("Manufacturer")), ("brand", cpu.get("Model")), ("cores", cpu.get("TotalCores")),
                    ("threads", cpu.get("TotalThreads")), ("max-speed", _rf_mhz(cpu.get("MaxSpeedMHz"))),
                    ("status", _health(cpu)))))
        if want("memory"):
            for dimm in fetch(_members(get, system["Memory"])):
                slot = re.match(r"DIMM\s+([A-Z]\d+)", dimm.get("DeviceLocator") or "")
                records.append(("memory", f"DIMM.Socket.{slot.group(1)}" if slot else dimm.get("Name"), _rf(
                    ("size", _rf_gib(dimm.get("CapacityMiB"), 1024)), ("speed", _rf_mhz(dimm.get("OperatingSpeedMhz"))),
                    ("type", dimm.get("MemoryDeviceType")), ("manufacturer", dimm.get("Manufacturer")),
                    ("part-number", dimm.get("PartNumber")), ("serial", dimm.get("SerialNumber")),
                    ("status", _health(dimm)))))
        if want("nic"):
            for nic in fetch(_members(get, system["EthernetInterfaces"])):
                records.append(("nic", nic.get("Id"), _rf(
                    ("description", nic.get("Description")), ("mac", nic.get("MACAddress")),
                    ("speed", f"{nic['SpeedMbps']} Mb/s" if nic.get("SpeedMbps") else None), ("status", _health(nic)))))
        if want("pci"):
            for device in fetch([d["@odata.id"] for d in system.get("PCIeDevices", [])]):
                records.append(("pci", device.get("Id"), _rf(
                    ("description", device.get("Name")), ("manufacturer", device.get("Manufacturer")),
                    ("status", _health(device)))))
        if any(want(c) for c in ("controller", "disk", "virtual-disk")):
            for storage in fetch(_members(get, system["Storage"])):
                controller = (storage.get("StorageControllers") or [{}])[0]
                if want("controller"):
                    records.append(("controller", controller.get("Name") or storage.get("Name"), _rf(
                        ("firmware", controller.get("FirmwareVersion")), ("manufacturer", controller.get("Manufacturer")),
                        ("status", _health(storage)))))
                if want("disk"):
                    for disk in fetch([d["@odata.id"] for d in storage.get("Drives", [])]):
                        records.append(("disk", disk.get("Name"), _rf(
                            ("manufacturer", disk.get("Manufacturer")), ("model", disk.get("Model")),
                            ("serial", disk.get("SerialNumber")), ("firmware", disk.get("Revision")),
                            ("size", _rf_gib(disk.get("CapacityBytes"), 1073741824)),
                            ("bus", str(disk.get("Protocol") or "").lower()), ("media", str(disk.get("MediaType") or "").lower()),
                            ("status", _health(disk)))))
                if want("virtual-disk") and storage.get("Volumes"):
                    for volume in fetch(_members(get, storage["Volumes"])):
                        records.append(("virtual-disk", volume.get("Name"), _rf(
                            ("size", _rf_gib(volume.get("CapacityBytes"), 1073741824)),
                            ("layout", REDFISH_VOLUME_TYPES.get(volume.get("VolumeType"), volume.get("VolumeType"))),
                            ("status", _health(volume)))))
    records = [r for r in records if r[2] and r[1]]
    order = {category: i for i, (category, *_) in enumerate(INVENTORY)}
    records.sort(key=lambda r: (order[r[0]], _natural(r[1])))
    if not records:
        what = f"{only_category} items" if only_category else "inventory"
        raise SourceError(f"no {what} returned by {args.host} over Redfish")
    return records


def redfish_list(args):
    """For --save-credentials: logging in proves the credentials. The redfish source has no sensors to list."""
    with redfish_session(args):
        return {}


def redfish_fetch(args):
    raise SourceError("the redfish source only provides the inventory: use --list inventory or --get inventory")


# --- racadm: Dell's own command line for the iDRAC, used here for the inventory ---
# The commands are run in the iDRAC's ssh shell (`racadm hwinventory` ...) with paramiko, so the password never shows in a
# process list and one connection serves every command. If ssh cannot connect at all and a local `racadm` is installed,
# `racadm -r HOST -u USER -p PASSWORD` is used instead (its password is visible in the process list while it runs).
RACADM_STATUS = {"OK": "ok", "Warning": "non-critical", "Critical": "critical", "Unknown": "unknown"}
RACADM_TIMEOUT = 180  # hwinventory takes about 30 s on an iDRAC8


IDRAC_LOCK_WAIT = 600  # seconds to queue behind another ragdoll that is using racadm on the same iDRAC


@contextlib.contextmanager
def idrac_lock(args, wait=None):
    """Let only one ragdoll at a time use an iDRAC's racadm or WS-Man logins; others queue behind it.

    An iDRAC allows few ssh and racadm sessions and can lock an account out, so these are never run in parallel, by this
    process or by another one (a cron job overlapping a manual run). The racadm and wsman sources share this lock. It is
    a file in the cache directory, one per host, released when the process ends even if it crashes.
    """
    try:
        import fcntl
    except ImportError:  # not a POSIX system: no file locks, so only this process's own commands are serialised
        yield
        return
    wait = IDRAC_LOCK_WAIT if wait is None else wait
    path = args.cache_dir / (re.sub(r"[^\w.-]", "_", args.host) + ".idrac.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as lock:
        deadline, told = time.monotonic() + wait, False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SourceError(f"another ragdoll has been using {args.host} for more than {wait:g} seconds "
                                      f"(it holds {path}); try again when it has finished")
                if not told and sys.stderr.isatty():
                    print(f"Waiting for another ragdoll that is using {args.host}...", file=sys.stderr)
                    told = True
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def racadm_local(args, subcommand):
    """Run `racadm -r HOST ... SUBCOMMAND` with the racadm installed on this computer."""
    command = ["racadm", "-r", args.host, "-u", args.user, "-p", args.password, "--nocertwarn", *subcommand.split()]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=RACADM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SourceError(f"racadm {subcommand} took too long")
    return run.stdout + run.stderr


def racadm_run(args, subcommand, tries=1):
    """Return the output of `racadm SUBCOMMAND` on args.host, over ssh or, failing that, with the local racadm."""
    resolve_credentials(args)
    if not args.user or not args.password:
        raise SourceError("the racadm source needs credentials: --user/--pass, $IDRAC_USER/$IDRAC_PASS, "
                          "or ones saved with --save-credentials")
    text = None
    try:
        import paramiko
        for attempt in (1, 2):  # a connection kept from an earlier command may have dropped: reconnect once
            client = ssh_connect(args, password=args.password)
            try:
                _, out, err = client.exec_command(f"racadm {subcommand}", timeout=RACADM_TIMEOUT)
                text = out.read().decode(errors="replace") + err.read().decode(errors="replace")
                break
            except (paramiko.SSHException, EOFError, OSError) as e:
                for known, other in list(_SSH_CLIENTS.items()):
                    if other is client:
                        del _SSH_CLIENTS[known]
                client.close()
                if attempt == 2:
                    raise SourceError(f"racadm {subcommand} failed over ssh on {args.host}: {e}")
    except (ImportError, SshUnreachable) as e:  # no paramiko, or ssh not reachable: try the racadm command
        if not shutil.which("racadm"):
            raise SourceError(f"cannot reach the iDRAC's ssh shell ({e}) and there is no local racadm to fall back on")
        text = racadm_local(args, subcommand)
    if re.match(r"\s*ERROR", text):
        raise SourceError(text.strip().splitlines()[0])
    if "No more sessions are available" in text:  # the iDRAC allows only a few racadm sessions at once: wait and retry
        if tries >= 3:
            raise SourceError("the iDRAC has no free racadm sessions (someone else is using them); try again in a moment")
        time.sleep(5 * tries)
        return racadm_run(args, subcommand, tries + 1)
    return text


def racadm_blocks(text):
    """Split hwinventory or swinventory output into one dict per block of `Key = Value` lines."""
    blocks = []
    for raw in re.split(r"\n-{20,}[^\n]*\n", text):
        pairs = {k.strip(): v.strip() for k, v in re.findall(r"^([^=\n\[]+?) = (.*)$", raw, re.M)}
        if pairs:
            blocks.append(pairs)
    return blocks


def _rstatus(value):
    return RACADM_STATUS.get(value)


def _rbytes(value):
    """'959656755200 Bytes' -> '893.75 GiB'."""
    m = re.match(r"(\d+)", value or "")
    return _gib(1073741824)(int(m.group(1))) if m else None


def _rmb(value):
    """'32768 MB' -> '32 GiB'."""
    m = re.match(r"(\d+)", value or "")
    return _gib(1024)(int(m.group(1))) if m else None


def _rmedia(value):
    return {"Solid State Drive": "ssd", "Hard Disk Drive": "hdd"}.get(value, (value or "").lower() or None)


def _rlayout(value):
    return re.sub(r"^RAID(\d+)", r"RAID \1", value) if value else None


# Device Type in hwinventory -> (category, how to name the item, {attribute: (key, how to show it)})
RACADM_HW = {
    "CPU": ("cpu", "FQDD", {"manufacturer": ("Manufacturer", None), "brand": ("Model", None),
                            "cores": ("NumberOfProcessorCores", None), "enabled-cores": ("NumberOfEnabledCores", None),
                            "threads": ("NumberOfEnabledThreads", None), "max-speed": ("MaxClockSpeed", None),
                            "speed": ("CurrentClockSpeed", None), "status": ("PrimaryStatus", _rstatus)}),
    "Memory": ("memory", "FQDD", {"size": ("Size", _rmb), "speed": ("Speed", None), "type": ("MemoryType", None),
                                  "manufacturer": ("Manufacturer", None), "part-number": ("PartNumber", None),
                                  "serial": ("SerialNumber", None), "status": ("PrimaryStatus", _rstatus)}),
    "NIC": ("nic", "FQDD", {"product": ("ProductName", None), "vendor": ("VendorName", None),
                            "mac": ("PermanentMACAddress", None), "speed": ("LinkSpeed", None)}),
    "PCIDevice": ("pci", "FQDD", {"manufacturer": ("Manufacturer", None), "description": ("Description", None)}),
    "Controller": ("controller", "FQDD", {"product": ("ProductName", None), "firmware": ("ControllerFirmwareVersion", None),
                                          "cache": ("CacheSizeInMB", None), "status": ("PrimaryStatus", _rstatus)}),
    "PCIeSSDExtender": ("controller", "FQDD", {"product": ("DeviceDescription", None),
                                               "status": ("PrimaryStatus", _rstatus)}),
    "PhysicalDisk": ("disk", "FQDD", {"manufacturer": ("Manufacturer", None), "model": ("Model", None),
                                      "firmware": ("Revision", None), "size": ("SizeInBytes", _rbytes),
                                      "bus": ("BusProtocol", lambda v: v.lower()), "media": ("MediaType", _rmedia),
                                      "state": ("RaidStatus", lambda v: v.lower()), "status": ("PrimaryStatus", _rstatus)}),
    "VirtualDisk": ("virtual-disk", "Name", {"size": ("SizeInBytes", _rbytes), "layout": ("RAIDTypes", _rlayout),
                                             "media": ("MediaType", _rmedia), "state": ("RAIDStatus", lambda v: v.lower()),
                                             "status": ("PrimaryStatus", _rstatus)}),
    "ControllerBattery": ("raid-battery", "DeviceDescription", {"state": ("RAIDState", lambda v: v.lower()),
                                                                 "status": ("PrimaryStatus", _rstatus)}),
    "iDRACCard": ("idrac", None, {"firmware": ("FirmwareVersion", None), "edition": ("Model", None)}),
}


def read_inventory_racadm(args, only_category=None):
    """Return the inventory (see read_inventory) from racadm; the categories match the SNMP and Redfish inventories."""
    want = lambda *categories: only_category is None or only_category in categories
    commands = []
    if want("system", "bios"):
        commands.append("getsysinfo -s")
    if want("firmware"):
        commands.append("swinventory")
    if any(want(entry[0]) for entry in RACADM_HW.values()):
        commands.append("hwinventory")
    # one command at a time, over the one connection, and one ragdoll at a time per iDRAC: an iDRAC allows few ssh and
    # racadm sessions and can lock an account out, so this is slower (each command takes 10 s or more on an iDRAC8)
    # but safe
    with idrac_lock(args):
        output = {command: racadm_run(args, command) for command in commands}
    records = []
    if "getsysinfo -s" in output:
        info = dict(re.findall(r"^([A-Za-z ]+?)\s+= (.*)$", output["getsysinfo -s"], re.M))
        if want("system"):
            records.append(("system", "system", _rf(("model", info.get("System Model")), ("name", info.get("Host Name")),
                                                    ("service-tag", info.get("Service Tag")),
                                                    ("power", (info.get("Power Status") or "").lower()))))
        if want("bios"):
            records.append(("bios", "bios", _rf(("version", info.get("System BIOS Version")))))
    if "swinventory" in output:
        for block in racadm_blocks(output["swinventory"]):
            name = block.get("ElementName")
            if "Current Version" in block and name != "BIOS":  # not rollback entries; the BIOS is the bios item
                records.append(("firmware", name, _rf(("version", block["Current Version"]),
                                                      ("installed", block.get("InstallationDate") if block.get("InstallationDate") != "NA" else None))))
    for block in racadm_blocks(output.get("hwinventory", "")):
        entry = RACADM_HW.get(block.get("Device Type"))
        if not entry or not want(entry[0]):
            continue
        category, name_key, attributes = entry
        details = _rf(*[(attr, (show(block[key]) if show else block[key]) if key in block else None)
                        for attr, (key, show) in attributes.items()])
        records.append((category, block.get(name_key) if name_key else category, details))
    records = [r for r in records if r[2] and r[1]]
    order = {category: i for i, (category, *_) in enumerate(INVENTORY)}
    records.sort(key=lambda r: (order[r[0]], _natural(r[1])))
    if not records:
        what = f"{only_category} items" if only_category else "inventory"
        raise SourceError(f"no {what} returned by racadm on {args.host}")
    return records


def racadm_list(args):
    """For --save-credentials: logging in proves the credentials. The racadm source has no sensors to list."""
    with idrac_lock(args):
        racadm_run(args, "getconfig -g cfgRacTuning -o cfgRacTuneIpRangeEnable")  # a quick read-only command
    return {}


def racadm_fetch(args):
    raise SourceError("the racadm source only provides the inventory: use --list inventory or --get inventory")


# --- wsman: the inventory over WS-Man (HTTPS), with the python-dracclient module ---
# No ssh and no racadm are needed. python-dracclient is OpenStack's iDRAC client; it covers part of the inventory. Its
# transport is wrapped so that --secure is honoured (it hard-codes verify=False) and a hung iDRAC cannot hang ragdoll
# (it sets no timeout). A refused login (HTTP 401) is raised at once and is never retried by the module.
WSMAN_STATUS = {"ok": "ok", "warning": "non-critical", "critical": "critical", "unknown": "unknown"}
WSMAN_TIMEOUT = 60


class _WsmanRequests:
    """Stands in for `requests` inside dracclient.wsman: its post() applies the real TLS setting and a timeout."""

    def __init__(self, verify):
        self.verify = verify

    def post(self, *args, **kwargs):
        kwargs["verify"] = self.verify
        kwargs.setdefault("timeout", WSMAN_TIMEOUT)
        return requests.post(*args, **kwargs)

    def __getattr__(self, name):  # requests.auth, requests.exceptions ...
        return getattr(requests, name)


def wsman_client(args):
    """A python-dracclient client for args.host, that makes one attempt at each request."""
    resolve_credentials(args)
    if not args.user or not args.password:
        raise SourceError("the wsman source needs credentials: --user/--pass, $IDRAC_USER/$IDRAC_PASS, "
                          "or ones saved with --save-credentials")
    try:
        import dracclient.wsman
        from dracclient import client
    except ImportError as e:
        raise SourceError(f"python-dracclient is not installed ({e}); pip install python-dracclient six")
    if not args.secure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    dracclient.wsman.requests = _WsmanRequests(args.secure)
    # ssl_retries=1 and ready_retries=1: one attempt each, so a problem is reported at once instead of being hammered
    return client.DRACClient(args.host, args.user, args.password, ssl_retries=1, ssl_retry_delay=0,
                             ready_retries=1, ready_retry_delay=1)


class _LogCollector(logging.Handler):
    """Keeps what python-dracclient logs: its exceptions carry no detail, only its log messages say what went wrong."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _wsman_call(args, call):
    """Run one python-dracclient call, turning its exceptions into SourceError."""
    from dracclient import exceptions
    collector, logger = _LogCollector(), logging.getLogger("dracclient.wsman")
    logger.addHandler(collector)  # also stops the module's own messages going to the terminal
    try:
        return call()
    except exceptions.WSManInvalidResponse as e:
        if "401" in str(e):
            raise SourceError(f"WS-Man login to {args.host} failed: the user name or password was refused")
        raise SourceError(f"WS-Man request to {args.host} failed: {e}")
    except exceptions.BaseClientException as e:
        why = collector.messages[-1] if collector.messages else str(e)
        if "SSLError" in why and args.secure:
            why += " (its certificate could not be verified; an iDRAC usually has a self-signed one, so leave --secure off)"
        raise SourceError(f"WS-Man request to {args.host} failed: {why}")
    finally:
        logger.removeHandler(collector)


def _wstatus(value):
    return WSMAN_STATUS.get(str(value).lower()) if value else None


def read_inventory_wsman(args, only_category=None):
    """Return the inventory (see read_inventory) over WS-Man. It has no BIOS, iDRAC, firmware (only the Lifecycle
    Controller), RAID battery or PCI devices other than video; the other categories match the rest."""
    want = lambda category: only_category in (None, category)
    records = []
    with idrac_lock(args):
        client = wsman_client(args)
        call = lambda f, *a: _wsman_call(args, lambda: f(*a))
        if want("system"):
            system = call(client.get_system)
            power = str(call(client.get_power_state) or "").replace("POWER_", "").lower()
            records.append(("system", "system", _rf(("model", system.model), ("service-tag", system.service_tag),
                                                    ("power", power))))
        if want("firmware"):
            version = ".".join(str(n) for n in call(client.get_lifecycle_controller_version))
            records.append(("firmware", "Lifecycle Controller", _rf(("version", version))))
        if want("cpu"):
            for cpu in call(client.list_cpus):
                records.append(("cpu", cpu.id, _rf(("brand", cpu.model), ("cores", cpu.cores), ("threads", cpu.cpu_count),
                                                   ("speed", _rf_mhz(cpu.speed_mhz)), ("status", _wstatus(cpu.status)))))
        if want("memory"):
            for dimm in call(client.list_memory):
                records.append(("memory", dimm.id, _rf(("size", _rf_gib(dimm.size_mb, 1024)), ("speed", _rf_mhz(dimm.speed_mhz)),
                                                       ("type", dimm.model), ("manufacturer", dimm.manufacturer),
                                                       ("status", _wstatus(dimm.status)))))
        if want("nic"):
            for nic in call(client.list_nics):
                records.append(("nic", nic.id, _rf(("product", nic.model), ("mac", nic.mac),
                                                   ("speed", f"{nic.speed_mbps} Mb/s" if nic.speed_mbps else None),
                                                   ("duplex", nic.duplex))))
        if want("pci"):  # python-dracclient lists video controllers only, not all PCI devices
            for video in call(client.list_video_controllers):
                records.append(("pci", video.id, _rf(("description", video.description), ("manufacturer", video.manufacturer))))
        if want("controller"):
            for ctrl in call(client.list_raid_controllers):
                records.append(("controller", ctrl.id, _rf(("product", ctrl.model), ("manufacturer", ctrl.manufacturer),
                                                           ("firmware", ctrl.firmware_version), ("status", _wstatus(ctrl.primary_status)))))
        if want("disk"):
            for disk in call(client.list_physical_disks):
                records.append(("disk", disk.id, _rf(("manufacturer", disk.manufacturer), ("model", disk.model),
                                                     ("serial", disk.serial_number), ("firmware", disk.firmware_version),
                                                     ("size", _rf_gib(disk.size_mb, 1024)), ("bus", disk.interface_type),
                                                     ("media", disk.media_type), ("state", disk.raid_status),
                                                     ("status", _wstatus(disk.status)))))
        if want("virtual-disk"):
            for vd in call(client.list_virtual_disks):
                level = str(vd.raid_level or "")
                records.append(("virtual-disk", vd.name, _rf(("size", _rf_gib(vd.size_mb, 1024)),
                                                             ("layout", f"RAID {level}" if level.isdigit() else level),
                                                             ("state", vd.raid_status), ("status", _wstatus(vd.status)))))
    records = [r for r in records if r[2] and r[1]]
    order = {category: i for i, (category, *_) in enumerate(INVENTORY)}
    records.sort(key=lambda r: (order[r[0]], _natural(r[1])))
    if not records:
        what = f"{only_category} items" if only_category else "inventory"
        raise SourceError(f"no {what} returned by WS-Man on {args.host}")
    return records


def wsman_list(args):
    """For --save-credentials: one cheap request proves the login. The wsman source has no sensors to list."""
    with idrac_lock(args):
        client = wsman_client(args)
        _wsman_call(args, client.get_lifecycle_controller_version)
    return {}


def wsman_fetch(args):
    raise SourceError("the wsman source only provides the inventory: use --list inventory or --get inventory")


# --- system: the inventory of a computer itself, from system_profiler (macOS) or dmidecode (Linux) ---
# Run on this computer, or on another one over ssh with --host (like lmsensors). dmidecode needs root, so it is tried as
# is, then with `sudo -n` (never a password prompt); without either, Linux falls back to /sys/class/dmi/id for the
# system and BIOS and leaves out the CPUs and memory. Disks come from lsblk, network ports from /sys/class/net and PCI
# devices from lspci, none of which need root.
SYSTEM_PROFILER_TYPES = ("SPHardwareDataType", "SPSoftwareDataType", "SPMemoryDataType", "SPNetworkDataType",
                         "SPEthernetDataType", "SPNVMeDataType", "SPSerialATADataType", "SPDisplaysDataType",
                         "SPPCIDataType")
LINUX_NIC_COMMAND = ('for n in /sys/class/net/*; do [ -e "$n/device" ] || continue; '
                     'd=$(readlink "$n/device/driver" 2>/dev/null); '
                     'echo "$(basename "$n")|$(cat "$n/address" 2>/dev/null)|$(cat "$n/operstate" 2>/dev/null)|'
                     '$(cat "$n/speed" 2>/dev/null)|${d##*/}"; done')
LINUX_RAID_MODELS = re.compile(r"PERC|RAID|LOGICAL VOLUME|Virtual Disk", re.I)  # lsblk models of RAID volumes
# lspci classes left out: the chipset's own bridges and internal functions (a Xeon has a hundred or more), not cards
LINUX_PCI_SKIP = re.compile(r"bridge|System peripheral|Performance counters|Signal processing|PIC|Unassigned class", re.I)


def system_run(args, command):
    """Run a shell command on this computer, or on args.host over ssh: returns (exit status, stdout, stderr)."""
    if args.remote:
        return ssh_run(args, command)
    try:
        run = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise SourceError(f"{command.split()[0]} took too long")
    return run.returncode, run.stdout, run.stderr


def _bytes_gib(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return f"{num(round(n / 2 ** 30, 2))} GiB" if n > 0 else None


def _sp(value, *prefixes):
    """A system_profiler value with its internal prefix removed: 'sppci_vendor_Apple' -> 'Apple'."""
    if value in (None, ""):
        return None
    text = str(value)
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):].replace("_", " ")
    return text


def read_inventory_macos(args, want):
    status, out, err = system_run(args, "system_profiler -json -detailLevel full " + " ".join(SYSTEM_PROFILER_TYPES))
    if status != 0:
        raise SourceError(f"system_profiler failed: {err.strip() or f'exit status {status}'}")
    try:
        data = json.loads(out)
    except ValueError as e:
        raise SourceError(f"system_profiler printed something that is not JSON: {e}")
    hardware = (data.get("SPHardwareDataType") or [{}])[0]
    software = (data.get("SPSoftwareDataType") or [{}])[0]
    records = []
    if want("system"):
        model = " ".join(filter(None, [hardware.get("machine_name"),
                                       f"({hardware['machine_model']})" if hardware.get("machine_model") else None]))
        records.append(("system", "system", _rf(
            ("model", model), ("name", args.host), ("service-tag", hardware.get("serial_number")),
            ("manufacturer", "Apple"), ("os", software.get("os_version")), ("memory", _dmi_size(hardware.get("physical_memory"))))))
    if want("bios"):
        records.append(("bios", "bios", _rf(("version", hardware.get("boot_rom_version")), ("manufacturer", "Apple"))))
    if want("cpu"):
        cores = hardware.get("number_processors")
        if isinstance(cores, str):  # Apple silicon: "proc 10:0:8:2", the total first
            cores = (re.findall(r"\d+", cores) or [None])[0]
        records.append(("cpu", hardware.get("chip_type") or hardware.get("cpu_type") or "cpu", _rf(
            ("manufacturer", "Apple" if hardware.get("chip_type") else None),
            ("brand", hardware.get("chip_type") or hardware.get("cpu_type")), ("cores", cores),
            ("packages", hardware.get("packages")), ("speed", hardware.get("current_processor_speed")))))
    if want("memory"):
        for bank in data.get("SPMemoryDataType") or []:
            if "_items" in bank:  # Intel Macs: one entry per DIMM
                for dimm in bank["_items"]:
                    if str(dimm.get("dimm_size", "")).lower() in ("", "empty"):
                        continue
                    records.append(("memory", dimm.get("_name"), _rf(
                        ("size", _dmi_size(dimm.get("dimm_size"))), ("speed", _dmi_speed(dimm.get("dimm_speed"))), ("type", dimm.get("dimm_type")),
                        ("manufacturer", dimm.get("dimm_manufacturer")), ("part-number", dimm.get("dimm_part_number")),
                        ("serial", dimm.get("dimm_serial_number")), ("status", dimm.get("dimm_status")))))
            else:  # Apple silicon: the memory is part of the chip, so there is one entry
                records.append(("memory", "memory", _rf(
                    ("size", _dmi_size(bank.get("SPMemoryDataType"))), ("type", bank.get("dimm_type")),
                    ("manufacturer", bank.get("dimm_manufacturer")))))
    if want("nic"):
        ports = {e.get("spethernet_BSD_Device_Name"): e for e in data.get("SPEthernetDataType") or []}
        for service in data.get("SPNetworkDataType") or []:
            name = service.get("interface")
            if not name:
                continue
            port = ports.get(name, {})
            records.append(("nic", name, _rf(
                ("product", port.get("spethernet_product_name") or service.get("_name")),
                ("vendor", port.get("spethernet_vendor_name")),
                ("mac", ((service.get("Ethernet") or {}).get("MAC Address") or port.get("spethernet_mac_address") or "").upper()),
                ("type", service.get("type") or service.get("hardware")),
                ("link", "connected" if (service.get("IPv4") or {}).get("Addresses") else None))))
    for bus, key, media in (("nvme", "SPNVMeDataType", "ssd"), ("sata", "SPSerialATADataType", None)):
        for controller in data.get(key) or []:
            if want("controller"):
                records.append(("controller", controller.get("_name"), _rf(("bus", bus))))
            if want("disk"):
                for disk in controller.get("_items") or []:
                    kind = disk.get("spsata_medium_type", "")
                    records.append(("disk", disk.get("bsd_name") or disk.get("_name"), _rf(
                        ("model", disk.get("device_model") or disk.get("_name")), ("serial", disk.get("device_serial")),
                        ("firmware", disk.get("device_revision")), ("size", _bytes_gib(disk.get("size_in_bytes"))),
                        ("bus", bus), ("media", media or ("ssd" if "solid" in kind.lower() else "hdd" if kind else None)),
                        ("status", disk.get("smart_status")))))
    if want("pci"):
        for gpu in data.get("SPDisplaysDataType") or []:
            records.append(("pci", gpu.get("_name"), _rf(
                ("manufacturer", _sp(gpu.get("spdisplays_vendor"), "sppci_vendor_")),
                ("description", gpu.get("sppci_model")), ("type", _sp(gpu.get("sppci_device_type"), "spdisplays_")),
                ("cores", gpu.get("sppci_cores")))))
        for card in data.get("SPPCIDataType") or []:
            records.append(("pci", card.get("_name"), _rf(
                ("manufacturer", card.get("sppci_vendor-id")), ("description", card.get("sppci_name") or card.get("_name")),
                ("type", card.get("sppci_device_type")), ("slot", card.get("sppci_slot_name")))))
    return records


def dmidecode_blocks(text):
    """Parse dmidecode output into [(DMI type, {key: value})], leaving out the indented lists (Characteristics: ...)."""
    blocks, current = [], None
    for line in text.splitlines():
        m = re.match(r"Handle 0x[0-9A-Fa-f]+, DMI type (\d+),", line)
        if m:
            current = {}
            blocks.append((int(m.group(1)), current))
        elif current is not None and line.startswith("\t") and not line.startswith("\t\t") and ":" in line:
            key, value = line.strip().split(":", 1)
            current[key.strip()] = value.strip()
    return blocks


def _dmi(value):
    """A dmidecode value, or None for its placeholders."""
    if not value or value.lower() in ("not specified", "unknown", "not provided", "to be filled by o.e.m.", "none",
                                      "no module installed", "default string", "not available"):
        return None
    return value


def _dmi_size(value):
    """'16 GB' or '16384 MB' -> '16 GiB' (dmidecode's GB and MB are binary)."""
    m = re.match(r"(\d+)\s*(MB|GB|TB)", value or "")
    if not m:
        return None
    return f"{num(round(int(m.group(1)) / {'MB': 1024, 'GB': 1, 'TB': 1 / 1024}[m.group(2)], 2))} GiB"


def _dmi_speed(value):
    """'2400 MT/s' or '2400 MHz' -> '2400 MHz', as the other sources show it."""
    m = re.match(r"(\d+)\s*(MT/s|MHz)", value or "")
    return f"{m.group(1)} MHz" if m and int(m.group(1)) > 0 else None


def read_inventory_linux(args, want):
    records = []
    if any(want(c) for c in ("system", "bios", "cpu", "memory")):
        command = "dmidecode -t 0,1,4,17"
        status, out, err = system_run(args, command)
        if status != 0:
            status, out, err = system_run(args, "sudo -n " + command)
        if status == 0:
            blocks = dmidecode_blocks(out)
        else:
            where = f"on {args.host}" if args.remote else "on this computer"
            print(f"dmidecode needs root {where} and `sudo -n dmidecode` was refused: the CPUs and memory are left out, "
                  "and the system and BIOS come from /sys/class/dmi/id", file=sys.stderr)
            status, out, _ = system_run(args, "cd /sys/class/dmi/id && grep -s . sys_vendor product_name product_serial "
                                              "bios_vendor bios_version bios_date")
            sysfs = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
            blocks = [(1, {"Manufacturer": sysfs.get("sys_vendor"), "Product Name": sysfs.get("product_name"),
                           "Serial Number": sysfs.get("product_serial")}),
                      (0, {"Vendor": sysfs.get("bios_vendor"), "Version": sysfs.get("bios_version"),
                           "Release Date": sysfs.get("bios_date")})]
        for kind, b in blocks:
            if kind == 1 and want("system"):
                records.append(("system", "system", _rf(
                    ("model", _dmi(b.get("Product Name"))), ("name", args.host),
                    ("service-tag", _dmi(b.get("Serial Number"))), ("manufacturer", _dmi(b.get("Manufacturer"))))))
            elif kind == 0 and want("bios"):
                records.append(("bios", "bios", _rf(
                    ("version", _dmi(b.get("Version"))), ("released", _dmi(b.get("Release Date"))),
                    ("manufacturer", _dmi(b.get("Vendor"))))))
            elif kind == 4 and want("cpu") and "Populated" in b.get("Status", "Populated"):
                records.append(("cpu", b.get("Socket Designation") or "cpu", _rf(
                    ("manufacturer", _dmi(b.get("Manufacturer"))), ("brand", _dmi(b.get("Version"))),
                    ("cores", _dmi(b.get("Core Count"))), ("enabled-cores", _dmi(b.get("Core Enabled"))),
                    ("threads", _dmi(b.get("Thread Count"))), ("max-speed", _dmi_speed(b.get("Max Speed"))),
                    ("speed", _dmi_speed(b.get("Current Speed"))))))
            elif kind == 17 and want("memory") and _dmi_size(b.get("Size")):
                records.append(("memory", b.get("Locator") or "memory", _rf(
                    ("size", _dmi_size(b.get("Size"))), ("speed", _dmi_speed(b.get("Speed"))), ("type", _dmi(b.get("Type"))),
                    ("manufacturer", _dmi(b.get("Manufacturer"))), ("part-number", _dmi(b.get("Part Number"))),
                    ("serial", _dmi(b.get("Serial Number"))))))
    if want("nic"):
        status, out, _ = system_run(args, LINUX_NIC_COMMAND)
        for line in out.splitlines():
            name, mac, state, speed, driver = (line.split("|") + [""] * 5)[:5]
            records.append(("nic", name, _rf(
                ("mac", mac.upper()), ("link", {"up": "connected", "down": "disconnected"}.get(state, state or None)),
                ("speed", f"{speed} Mb/s" if speed.isdigit() and int(speed) > 0 else None), ("driver", driver))))
    if want("disk") or want("virtual-disk"):
        status, out, _ = system_run(args, "lsblk -d -J -b -o NAME,TYPE,VENDOR,MODEL,SERIAL,REV,SIZE,TRAN,ROTA")
        try:
            devices = json.loads(out).get("blockdevices", []) if status == 0 else []
        except ValueError:
            devices = []
        for d in devices:
            if d.get("type") != "disk" or str(d.get("name", "")).startswith(("zram", "loop")) or not _bytes_gib(d.get("size")):
                continue  # not a disk, or an empty drive (the iDRAC's virtual floppy and CD)
            virtual = bool(LINUX_RAID_MODELS.search(d.get("model") or ""))  # a RAID controller's volume, not a disk
            if not want("virtual-disk" if virtual else "disk"):
                continue
            rota = None if virtual else d.get("rota")  # the kernel calls every RAID volume rotational
            records.append(("virtual-disk" if virtual else "disk", d.get("name"), _rf(
                ("manufacturer", (d.get("vendor") or "").strip()), ("model", (d.get("model") or "").strip()),
                ("serial", d.get("serial")), ("firmware", (d.get("rev") or "").strip()), ("size", _bytes_gib(d.get("size"))),
                ("bus", d.get("tran")), ("media", None if rota is None else "hdd" if rota in (True, "1", 1) else "ssd"))))
    if want("pci"):
        status, out, _ = system_run(args, "lspci -mm")
        for line in out.splitlines() if status == 0 else []:
            try:
                fields = [f for f in shlex.split(line) if not f.startswith("-")]
            except ValueError:
                continue
            if len(fields) >= 4 and not LINUX_PCI_SKIP.search(fields[1]):
                records.append(("pci", fields[0], _rf(("manufacturer", fields[2]), ("description", fields[3]),
                                                      ("type", fields[1]))))
    return records


def read_inventory_system(args, only_category=None):
    """Return the inventory (see read_inventory) of this computer, or of args.host over ssh."""
    want = lambda category: only_category in (None, category)
    if args.remote:
        status, out, err = ssh_run(args, "uname -s")
        if status != 0:
            raise SourceError(f"uname failed on {args.host}: {err.strip()}")
        system = out.strip()
    else:
        system = platform.system()
    if system == "Darwin":
        records = read_inventory_macos(args, want)
    elif system == "Linux":
        records = read_inventory_linux(args, want)
    else:
        raise SourceError(f"the system source supports macOS and Linux, not {system or 'this system'}")
    order = {category: i for i, (category, *_) in enumerate(INVENTORY)}
    records = [r for r in records if r[2]]  # an item with nothing known about it is left out
    records.sort(key=lambda r: (order[r[0]], _natural(r[1] or "")))
    if not records:
        what = f"{only_category} items" if only_category else "inventory"
        raise SourceError(f"no {what} found {'on ' + args.host if args.remote else 'on this computer'}")
    return records


def system_list(args):
    """The system source has no sensors to list."""
    return {}


def system_fetch(args):
    raise SourceError("the system source only provides the inventory: use --list inventory or --get inventory")


INVENTORY_HEADER = ("CATEGORY", "NAME", "DETAILS")


def inventory_display_rows(records):
    """One row per item for the text and table outputs: 'manufacturer=Samsung; size=32 GiB; ...'."""
    return [(category, name, "; ".join(f"{k}={v}" for k, v in details)) for category, name, details in records]


def inventory_data_rows(records):
    """One row per attribute (category, name, attribute, value) for CSV and spreadsheets, which suit a tidy layout."""
    return (("category", "name", "attribute", "value"),
            [(category, name, attribute, value) for category, name, details in records for attribute, value in details])


def inventory_spreadsheet_path(args, extension):
    if args.file:
        path = Path(args.file)
        return path if path.suffix else path.with_suffix(extension)
    parts = [args.host, "inventory"] + [p for p in (args.category, args.name) if p]
    return Path("_".join(re.sub(r"[^\w.-]", "_", p) for p in parts) + extension)


def only_detail(args, records):
    """Keep just the --detail of each item (the items that have it); a name that no item has is rejected."""
    wanted = args.detail.lower()
    kept = [(category, name, [(a, v) for a, v in details if a.lower() == wanted])
            for category, name, details in records]
    kept = [r for r in kept if r[2]]
    if not kept:
        have = sorted({a for _, _, details in records for a, _ in details})
        sys.exit(f"none of these items has a detail named {args.detail!r}; the details are: {', '.join(have)}")
    return kept


def inventory_text(args, records):
    if args.detail:  # just the values, one per line, so they can be used in a script
        for _, _, details in records:
            print(details[0][1])
        return
    emit_text(*select_field(args, list(INVENTORY_HEADER), inventory_display_rows(records)))


def inventory_table(args, records):
    if args.detail:  # category and name, then the detail under its own name
        emit_table(("CATEGORY", "NAME", records[0][2][0][0].upper()), [(c, n, d[0][1]) for c, n, d in records])
        return
    emit_table(*select_field(args, INVENTORY_HEADER, inventory_display_rows(records)))


def inventory_csv(args, records):
    emit_csv(*select_field(args, *inventory_data_rows(records)))


def inventory_json(args, records):
    """The inventory as a JSON array of {category, name, details}, details being an object of attribute: value."""
    head, rows = select_field(args, ["category", "name", "details"],
                              [[category, name, dict(details)] for category, name, details in records])
    dump_json([dict(zip(head, row)) for row in rows])


def inventory_xlsx(args, records):
    head, rows = select_field(args, *inventory_data_rows(records))
    emit_spreadsheet("xlsx", inventory_spreadsheet_path(args, ".xlsx"), "Inventory", head, rows, "attribute")


def inventory_xls(args, records):
    head, rows = select_field(args, *inventory_data_rows(records))
    emit_spreadsheet("xls", inventory_spreadsheet_path(args, ".xls"), "Inventory", head, rows, "attribute")


INVENTORY_OUTPUTS = {"text": inventory_text, "table": inventory_table, "csv": inventory_csv, "json": inventory_json,
                     "xlsx": inventory_xlsx, "xls": inventory_xls}


def list_inventory(args):
    """List the hardware inventory over SNMP in the chosen --output format (--list inventory or --get inventory)."""
    if args.metric or args.sensor:
        sys.exit("--metric and --sensor do not apply to the inventory")
    sub = copy.copy(args)
    # snmp is the default; redfish (and web, the iDRAC's web interface, which is where Redfish lives) use Redfish
    sub.source = args.source or "snmp"
    if sub.source not in ("snmp", "redfish", "web", "racadm", "wsman", "system"):
        sys.exit(f"the inventory needs --source snmp, redfish, racadm, wsman or system, not {sub.source}")
    try:
        if sub.source == "snmp":
            records = asyncio.run(read_inventory(sub, args.category))
        elif sub.source == "system":
            records = read_inventory_system(sub, args.category)
        elif sub.source == "wsman":
            if sys.stderr.isatty():
                print("Reading the inventory over WS-Man: about 30 seconds on an iDRAC8...", file=sys.stderr)
            records = read_inventory_wsman(sub, args.category)
        elif sub.source == "racadm":
            if sys.stderr.isatty():
                print("Reading the inventory with racadm: about a minute for all of it on an iDRAC8...", file=sys.stderr)
            records = read_inventory_racadm(sub, args.category)
        else:
            if sys.stderr.isatty():
                print("Reading the inventory over Redfish: about 10 seconds to log in, then 2 to 30 more on an iDRAC8...",
                      file=sys.stderr)
            records = read_inventory_redfish(sub, args.category)
    except SourceError as e:
        sys.exit(f"Failed to read the inventory of {args.host}: {e}")
    if args.name:
        wanted = args.name.lower()
        matching = [r for r in records if fnmatch.fnmatchcase(r[1].lower(), wanted)]
        if not matching:
            names = sorted({r[1] for r in records}, key=_natural)
            shown = ", ".join(names[:40]) + (f" ... ({len(names)} in all)" if len(names) > 40 else "")
            sys.exit(f"no {args.category + ' ' if args.category else ''}item is named {args.name!r}; "
                     f"the names are: {shown}")
        records = matching
    if args.detail:
        records = only_detail(args, records)
    INVENTORY_OUTPUTS[args.output](args, records)
    return 0


def list_all(args):
    only_inventory = args.list if args.list in SOURCES else args.source
    if only_inventory and not SOURCES[only_inventory]["metrics"] and args.list != "inventory":
        sys.exit(f"the {only_inventory} source has no sensors, only the inventory: use --list inventory or --get inventory")
    """List every matching sensor in the chosen --output format (text by default); return an exit code."""
    if args.list == "inventory":
        return list_inventory(args)
    everywhere = [s for s in sorted(SOURCES) if not SOURCES[s].get("local")]  # a local source is listed only by name
    sources, metrics = [args.source] if args.source else everywhere, [args.metric] if args.metric else None
    if args.list != "all":
        if args.list in SOURCES:
            sources = [args.list]
        elif args.list in METRIC_NAMES:
            metrics = [args.list]
        else:
            sys.exit(f"--list: {args.list!r} is not sensors, inventory, a source ({', '.join(sorted(SOURCES))}) "
                     f"or a metric ({', '.join(METRIC_NAMES)})")
    records, failed = [], 0
    for source in sources:
        for metric in (metrics or SOURCES[source]["metrics"]):
            if metric not in SOURCES[source]["metrics"]:
                continue
            sub = copy.copy(args)  # credentials are resolved per source, so don't share state
            sub.source, sub.metric = source, metric
            try:
                sensors = SOURCES[source]["list"](sub)
            except (requests.RequestException, SourceError) as e:
                failed += 1
                print(f"{source}/{metric}: {e}", file=sys.stderr)
                continue
            for name, reading in sorted(sensors.items()):
                sub.sensor = name
                value, limits = reading.value, reading.limits
                if source == "web":  # no live reading without a slow fetch: use what is already cached
                    value, limits = cached_value(sub), load_limits(sub)
                if METRICS[metric].get("counter"):  # a counter is not a value; its rate needs two readings
                    value = None
                records.append((source, metric, name, METRICS[metric]["unit"](name).strip(), value, limits, reading.key))
    if records:
        LIST_OUTPUTS[args.output](args, records)
    return 0 if records else 1 if failed else 0


DEFAULT_POLL_SECONDS = 30  # an iDRAC refreshes its sensors about this often, so polling faster repeats values
TAIL_OUTPUTS = ("text", "csv", "json", "raw", "db")  # where --tail can send each reading


def poll_once(args, state):
    """Poll the source once. Returns (rows to store, rows to show), each a list of (unix time, average, peak).

    snmp gives one reading, stamped now. web gives its whole hourly history: all of it is stored (rows already in the
    database are skipped), and the rows newer than the last one shown are displayed (the newest, the first time).
    """
    if args.source in ("snmp", "lmsensors"):
        table = read_current(args)
        if args.sensor not in table:
            raise SourceError(f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(table))}")
        reading = table[args.sensor]
        if reading.limits:
            save_limits(args, reading.limits)
        row = (int(time.time()), reading.value, reading.value)
        return [row], [row]
    rows = parse_rows(SOURCES[args.source]["fetch"](args))
    if not rows:
        return [], []
    fresh = [r for r in rows if r[0] > state["newest"]] if state.get("newest") else rows[-1:]
    return rows, fresh


def tail_emit(args, metric, rows, state):
    """Send the rows to the --output: text or csv lines on stdout, raw cache-format lines, or nothing (db)."""
    unit = metric["unit"](args.sensor).strip()
    zone = dt.timezone.utc if args.utc else args.tz
    for t, avg, peak in rows:
        if args.output == "text":
            print(f"{dt.datetime.fromtimestamp(t, zone):%Y-%m-%d %H:%M:%S}  {num(avg)} {unit}".rstrip(), flush=True)
        elif args.output == "csv":
            writer = csv.writer(sys.stdout, lineterminator="\n")
            if not state.get("header"):
                writer.writerow(["time", "host", "source", "metric", "sensor", "average", "peak", "unit"])
                state["header"] = True
            when = dt.datetime.fromtimestamp(t, zone)
            when = when if when.tzinfo else when.astimezone()
            writer.writerow([when.isoformat(timespec="seconds"), args.host, args.source, args.metric, args.sensor,
                             num(avg), num(peak), unit])
            sys.stdout.flush()
        elif args.output == "json":  # JSON Lines: one compact object per line, so a stream can be read as it arrives
            print(json.dumps({"time": iso_time(args, t), "host": args.host, "source": args.source, "metric": args.metric,
                              "sensor": args.sensor, "average": json_number(avg), "peak": json_number(peak),
                              "unit": unit}, ensure_ascii=False), flush=True)
        elif args.output == "raw":
            if not state.get("header"):
                print("Average,Peak,Time")
                state["header"] = True
            print(f"{num(avg)},{num(peak)},{epoch_to_csv(t)}", flush=True)


def tail(args, metric):
    """Poll the source every --poll seconds until Ctrl-C (or SIGTERM), sending each reading to --output."""
    if args.source not in ("snmp", "web", "lmsensors"):
        sys.exit(f"--tail needs --source snmp, web or lmsensors, not {args.source}")
    store = bool(args.db)  # --db stores as well as shows; --output db stores only (parse_args gave it a path)
    state = {"polls": 0, "stored": 0, "failed": 0}
    interactive = sys.stderr.isatty()

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    if interactive:
        where = f"stored in {args.db}" if args.output == "db" else "shown below" + (f" and stored in {args.db}" if store else "")
        print(f"Polling {args.sensor} ({args.metric}, {args.source}) on {args.host} every {args.poll:g} s, {where}; "
              "Ctrl-C to stop.", file=sys.stderr)
    next_tick = time.monotonic()
    try:
        while True:
            try:
                to_store, to_show = poll_once(args, state)
            except (requests.RequestException, SourceError) as e:
                if state["polls"] == 0:
                    sys.exit(f"Failed to poll {args.host}: {e}")  # a first poll that fails is a mistake, not an outage
                state["failed"] += 1
                print(f"warning: poll failed, will try again ({e})", file=sys.stderr)
            else:
                shown = to_show
                if metric.get("counter"):  # show a rate between polls; the database keeps the counter
                    previous = state.get("previous")
                    shown = to_rates([previous, to_show[0]]) if previous and to_show else []
                    state["previous"] = to_show[0] if to_show else previous
                if store and to_store:
                    try:
                        state["stored"] += store_rows(args, to_store)
                    except (sqlite3.Error, ValueError, OSError) as e:
                        print(f"warning: could not store in {args.db}: {e}", file=sys.stderr)
                tail_emit(args, metric, shown, state)
                if to_show:
                    state["newest"] = max(r[0] for r in to_show) if args.source == "web" else state.get("newest")
                state["polls"] += 1
                if interactive and args.output == "db" and to_store:
                    print(f"{dt.datetime.now():%H:%M:%S}  stored {num(to_store[-1][1])} {metric['unit'](args.sensor).strip()}"
                          f" ({state['stored']} new in all)", file=sys.stderr)
            next_tick += args.poll
            pause = next_tick - time.monotonic()
            if pause < 0:  # the poll took longer than the interval: carry on from now, not from the missed ticks
                next_tick = time.monotonic()
            else:
                time.sleep(pause)
    except KeyboardInterrupt:
        pass
    print(f"Stopped after {state['polls']} polls"
          + (f", {state['stored']} readings added to {args.db}" if store else "")
          + (f", {state['failed']} failed" if state["failed"] else "") + ".", file=sys.stderr)
    return 0


COUNTER_SAMPLE_SECONDS = 2  # --get on a counter metric waits this long between its two readings


def get_value(args):
    """Poll one snmp sensor now and print its value (or, with --get inventory, the inventory); return an exit code."""
    if args.get == "inventory":
        return list_inventory(args)
    args.source = args.source or "snmp"
    if args.source not in ("snmp", "lmsensors"):
        sys.exit(f"--get needs --source snmp or lmsensors: the {args.source} source has no live value to read")
    args.metric = args.metric or "temperature"
    if args.metric not in SOURCES[args.source]["metrics"]:
        sys.exit(f"--source {args.source} does not support --metric {args.metric}; "
                 f"it supports: {', '.join(SOURCES[args.source]['metrics'])}")
    metric = METRICS[args.metric]
    try:
        args.sensor = args.sensor or default_sensor(args, metric)
        table = read_current(args)
        if args.sensor not in table:
            raise SourceError(f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(table))}")
        reading = table[args.sensor]
        value = reading.value
        if metric.get("counter"):  # a counter has no value of its own: measure the rate over a short interval
            started = time.monotonic()
            time.sleep(COUNTER_SAMPLE_SECONDS)
            after = read_current(args)[args.sensor].value
            elapsed = time.monotonic() - started
            value = round((after - value) / elapsed, 3) if after >= value else None  # a lower counter means a restart
            if value is None:
                raise SourceError("the counter went down while measuring (the iDRAC restarted?); try again")
    except SourceError as e:
        sys.exit(f"Failed to read {args.sensor or 'the sensors'} from {args.host}: {e}")
    record = (args.source, args.metric, args.sensor, metric["unit"](args.sensor).strip(), value, reading.limits,
              reading.key)
    GET_OUTPUTS[args.output](args, [record])
    return 0


def get_text(args, records):
    """The default for --get: just the value and its unit, for example '16 °C'; --field picks one part of it."""
    if args.field:
        emit_text(*select_field(args, list(LIST_HEADER), list_display_rows(records)))
        return
    _, _, _, unit, value, _, _ = records[0]
    print(f"{num(value)} {unit}".strip())


def get_json(args, records):
    """--get as JSON is one object, not an array: the same fields as one row of --list."""
    source, metric, sensor, unit, value, limits, key = records[0]
    head, row = select_field(args, [h.lower() for h in LIST_HEADER],
                             [[source, metric, sensor, unit, json_number(value),
                               {k: json_number(limits.get(k)) for k in LIMIT_KEYS} if limits else None, key]])
    dump_json(dict(zip(head, row[0])))


# --get shows its one record like --list does, except that plain text is the bare value
GET_OUTPUTS = {**LIST_OUTPUTS, "text": get_text, "json": get_json}


def main():
    args = parse_args()
    if args.no_fetch and (args.list or args.get or args.save_credentials or args.refresh):
        sys.exit("--no-fetch cannot be combined with --list, --get, --save-credentials or --refresh, "
                 "which all need to contact the iDRAC")
    if args.forget_credentials:
        try:
            removed = keyring_forget(args.host)
        except SourceError as e:
            sys.exit(str(e))
        print(f"Removed from keyring: {', '.join(removed)}" if removed else "No saved credentials for this host")
        return
    if args.save_credentials:
        # credentials are saved for the chosen source; a listing needs a real login, so
        # running one proves the credentials work before they are saved
        saving = copy.copy(args)
        saving.source = args.source or (args.list if args.list in SOURCES else None) or ("snmp" if "inventory" in (args.list, args.get) else "web")
        saving.metric = args.metric or "temperature"
        try:
            SOURCES[saving.source]["list"](saving)
            save_credentials(saving)
        except (requests.RequestException, SourceError) as e:
            sys.exit(f"Not saving credentials: {e}")
        print(f"Credentials for {args.host} ({saving.source}) saved to the OS keyring", flush=True)
    if args.list:
        sys.exit(list_all(args))
    if args.get:
        sys.exit(get_value(args))
    args.source = args.source or ("snmp" if args.tail else "web")  # polling is for live values, which snmp gives
    args.metric = args.metric or "temperature"
    if not SOURCES[args.source]["metrics"]:
        sys.exit(f"the {args.source} source only provides the inventory: use --list inventory or --get inventory")
    if args.metric not in SOURCES[args.source]["metrics"]:
        sys.exit(f"--source {args.source} does not support --metric {args.metric}; "
                 f"it supports: {', '.join(SOURCES[args.source]['metrics'])}")
    metric = METRICS[args.metric]
    try:
        args.sensor = args.sensor or default_sensor(args, metric)
    except SourceError as e:
        sys.exit(f"Failed to read the sensors: {e}")
    if args.tail:
        sys.exit(tail(args, metric))
    if args.no_fetch:
        text, data = stored_data(args)
    else:
        try:
            text = load_data(args)
        except (requests.RequestException, SourceError) as e:
            sys.exit(f"Failed to fetch data from {args.host}: {e}")
        data = parse_rows(text)
        added = 0
        if args.db and data:
            try:
                added = store_rows(args, data)
            except (sqlite3.Error, ValueError, OSError) as e:
                sys.exit(f"Could not store readings in {args.db}: {e}")
        if args.output == "db":
            output_db(args, metric, data)  # the raw readings, before any counter is turned into a rate
            if sys.stderr.isatty():
                print(f"{added} new", file=sys.stderr)
            return

    if args.raw:
        print(to_csv(data) if data or text is None else text, end="")
        return

    if args.db and not args.no_fetch:
        try:
            data = load_rows(args)
        except (sqlite3.Error, ValueError) as e:
            sys.exit(f"Could not read readings from {args.db}: {e}")
    if not data and args.no_fetch and args.db:
        sys.exit(f"--no-fetch: no stored readings for {args.host} {args.source} {args.metric} {args.sensor} "
                 f"in {args.db}; run once without --no-fetch")
    if not data:
        sys.exit("No numeric data found in CSV; rerun with --raw to inspect it")
    if metric.get("counter"):
        # the stored values are ever-growing counters; show them as rates
        data = to_rates(data)
        if not data:
            sys.exit(f"{args.metric} readings are counters, and a rate needs at least two readings: "
                     "run again after a while (or from cron) to collect more")
    try:
        data = select_last(data, args.last)
    except ValueError as e:
        sys.exit(f"Could not select readings by time: {e}")
    OUTPUTS[args.output](args, metric, data)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # the reader (for example `head`) closed the pipe early; point stdout at devnull so exit is quiet
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
