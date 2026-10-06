#!/usr/bin/env python3
"""Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information into more useful formats

Currently reads iDRAC temperature, fan and power data (web interface or SNMP) and graphs it in the terminal.

License: Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
(CC BY-NC-SA 4.0), see the LICENSE file or https://creativecommons.org/licenses/by-nc-sa/4.0/
"""

import argparse
import asyncio
import calendar
import collections
import contextlib
import fnmatch
import copy
import csv
import datetime as dt
import getpass
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zoneinfo
from pathlib import Path

import requests
import urllib3

__version__ = "0.3.8"


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
    p.add_argument("--host", required=True, help="iDRAC address, e.g. 192.168.8.98")
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
                        "snmp reads the current value and builds history in the cache (gui is an alias for web; default: web; "
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
                        "inventory lists the hardware (CPUs, memory, disks, firmware ...) over snmp; "
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
    p.add_argument("--output", choices=sorted(set(OUTPUTS) | set(LIST_OUTPUTS)),
                   help="what to produce. For readings: chart, table, raw (cache format CSV, UTC times), csv "
                        "(CSV with host, sensor and unit columns, times in --tz/local), or xlsx / xls spreadsheets "
                        "(see --file); --module, --chart, --width and --height apply to chart (default: chart). "
                        "With --list: text, table, csv, xlsx or xls (default: text)")
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
    args = p.parse_args()
    if args.tz and args.utc:
        p.error("--tz and --utc cannot be used together")
    if args.get not in (None, "sensor", "inventory"):
        p.error(f"--get takes no value or 'inventory', not {args.get!r}; use --metric and --sensor to choose a sensor")
    if (args.category or args.name) and "inventory" not in (args.list, args.get):
        p.error("--category and --name only apply to --list inventory or --get inventory")
    if args.list and args.get:
        p.error("--list and --get cannot be used together")
    if args.list or args.get:  # plain text unless another format is asked for
        args.output = args.output or "text"
        if args.output not in LIST_OUTPUTS:
            p.error(f"--output {args.output} does not apply to --list or --get; "
                    f"use one of {', '.join(sorted(LIST_OUTPUTS))}")
    else:
        args.output = args.output or "chart"
        if args.output not in OUTPUTS:
            p.error(f"--output {args.output} only applies to --list or --get")
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


def snmp_fetch(args):
    """Take one reading and append it to the cached history (SNMP has no history of its own)."""
    table = asyncio.run(snmp_read_table(args))
    if args.sensor not in table:
        raise SourceError(f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(table))}")
    reading = table[args.sensor]
    if reading.limits:
        save_limits(args, reading.limits)
    history = read_cache(cache_path(args)) or "Average,Peak,Time\n"
    if not history.endswith("\n"):
        history += "\n"
    return history + f"{num(reading.value)},{num(reading.value)},{epoch_to_csv(int(time.time()))}\n"


# Each source provides list(args) -> {sensor: Reading} and fetch(args) -> CSV text
# ("Average,Peak,Time" rows). accumulate=True means fetch() returns only a current reading
# appended to the cached history. Others (redfish, ipmi, ...) can be added here.
SOURCE_ALIASES = {"gui": "web"}  # alternative names accepted for --source and --list
SOURCES = {
    "web": {"list": web_list, "fetch": web_fetch, "accumulate": False, "metrics": ("temperature",)},
    "snmp": {"list": snmp_list, "fetch": snmp_fetch, "accumulate": True, "metrics": METRIC_NAMES},
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
OUTPUTS = {"chart": output_chart, "table": output_table, "raw": output_raw, "csv": output_csv,
           "xlsx": output_xlsx, "xls": output_xls}


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


def emit_text(head, rows, right=()):
    """Aligned columns of plain text. The last column is not padded; columns listed in right are right-aligned."""
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
    emit_text(list(LIST_HEADER), list_display_rows(records), right=(4,))


def list_table(args, records):
    emit_table(LIST_HEADER, list_display_rows(records), right=(4,))


def list_csv(args, records):
    emit_csv(*list_data_rows(records))


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
    head, rows = list_data_rows(records)
    emit_spreadsheet("xlsx", list_spreadsheet_path(args, ".xlsx"), "Sensors", head, rows, "sensor")


def list_xls(args, records):
    head, rows = list_data_rows(records)
    emit_spreadsheet("xls", list_spreadsheet_path(args, ".xls"), "Sensors", head, rows, "sensor")


LIST_OUTPUTS = {"text": list_text, "table": list_table, "csv": list_csv, "xlsx": list_xlsx, "xls": list_xls}


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


INVENTORY_OUTPUTS = {
    "text": lambda args, records: emit_text(list(INVENTORY_HEADER), inventory_display_rows(records)),
    "table": lambda args, records: emit_table(INVENTORY_HEADER, inventory_display_rows(records)),
    "csv": lambda args, records: emit_csv(*inventory_data_rows(records)),
    "xlsx": lambda args, records: emit_spreadsheet("xlsx", inventory_spreadsheet_path(args, ".xlsx"), "Inventory",
                                                   *inventory_data_rows(records), "attribute"),
    "xls": lambda args, records: emit_spreadsheet("xls", inventory_spreadsheet_path(args, ".xls"), "Inventory",
                                                  *inventory_data_rows(records), "attribute"),
}


def list_inventory(args):
    """List the hardware inventory over SNMP in the chosen --output format (--list inventory or --get inventory)."""
    if args.source not in (None, "snmp"):
        sys.exit(f"the inventory needs --source snmp: the {args.source} source has none")
    if args.metric or args.sensor:
        sys.exit("--metric and --sensor do not apply to the inventory")
    sub = copy.copy(args)
    sub.source = "snmp"
    try:
        records = asyncio.run(read_inventory(sub, args.category))
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
    INVENTORY_OUTPUTS[args.output](args, records)
    return 0


def list_all(args):
    """List every matching sensor in the chosen --output format (text by default); return an exit code."""
    if args.list == "inventory":
        return list_inventory(args)
    sources, metrics = [args.source] if args.source else sorted(SOURCES), [args.metric] if args.metric else None
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


COUNTER_SAMPLE_SECONDS = 2  # --get on a counter metric waits this long between its two readings


def get_value(args):
    """Poll one snmp sensor now and print its value (or, with --get inventory, the inventory); return an exit code."""
    if args.get == "inventory":
        return list_inventory(args)
    args.source = args.source or "snmp"
    if args.source != "snmp":
        sys.exit(f"--get needs --source snmp: the {args.source} source has no live value to read")
    args.metric = args.metric or "temperature"
    metric = METRICS[args.metric]
    args.sensor = args.sensor or metric["default"]
    try:
        table = asyncio.run(snmp_read_table(args))
        if args.sensor not in table:
            raise SourceError(f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(table))}")
        reading = table[args.sensor]
        value = reading.value
        if metric.get("counter"):  # a counter has no value of its own: measure the rate over a short interval
            started = time.monotonic()
            time.sleep(COUNTER_SAMPLE_SECONDS)
            after = asyncio.run(snmp_read_table(args))[args.sensor].value
            elapsed = time.monotonic() - started
            value = round((after - value) / elapsed, 3) if after >= value else None  # a lower counter means a restart
            if value is None:
                raise SourceError("the counter went down while measuring (the iDRAC restarted?); try again")
    except SourceError as e:
        sys.exit(f"Failed to read {args.sensor} from {args.host}: {e}")
    record = (args.source, args.metric, args.sensor, metric["unit"](args.sensor).strip(), value, reading.limits,
              reading.key)
    GET_OUTPUTS[args.output](args, [record])
    return 0


def get_text(args, records):
    """The default for --get: just the value and its unit, for example '16 °C'."""
    _, _, _, unit, value, _, _ = records[0]
    print(f"{num(value)} {unit}".strip())


# --get shows its one record like --list does, except that plain text is the bare value
GET_OUTPUTS = {**LIST_OUTPUTS, "text": get_text}


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
    args.source = args.source or "web"
    args.metric = args.metric or "temperature"
    if args.metric not in SOURCES[args.source]["metrics"]:
        sys.exit(f"--source {args.source} does not support --metric {args.metric}; "
                 f"it supports: {', '.join(SOURCES[args.source]['metrics'])}")
    metric = METRICS[args.metric]
    args.sensor = args.sensor or metric["default"]
    if args.no_fetch:
        text, data = stored_data(args)
    else:
        try:
            text = load_data(args)
        except (requests.RequestException, SourceError) as e:
            sys.exit(f"Failed to fetch data from {args.host}: {e}")
        data = parse_rows(text)
        if args.db and data:
            try:
                store_rows(args, data)
            except (sqlite3.Error, ValueError, OSError) as e:
                sys.exit(f"Could not store readings in {args.db}: {e}")

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
