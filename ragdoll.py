#!/usr/bin/env python3
"""Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information into more useful formats

Currently reads iDRAC temperature, fan and power data (web interface or SNMP) and graphs it in the terminal.

License: Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
(CC BY-NC-SA 4.0), see the LICENSE file or https://creativecommons.org/licenses/by-nc-sa/4.0/
"""

import argparse
import asyncio
import calendar
import contextlib
import copy
import csv
import datetime as dt
import getpass
import io
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

__version__ = "0.3.0"


class SourceError(Exception):
    """A data source could not provide what was asked for."""


SNMP_COL_READING, SNMP_COL_NAME = 6, 8  # same layout in all the Dell probe tables below


def _strip(name, *patterns):
    n = name.strip().lower()
    for pat in patterns:
        n = re.sub(pat, "", n)
    return re.sub(r"\s+", "-", n.strip())


def _power_name(name):
    if re.search(r"pwr consumption", name, re.I):
        return "system-power"
    return _strip(name, r" \d+$")  # "PS1 Current 1" -> "ps1-current"


# Per-metric SNMP details. divisor converts the raw integer to the unit; label ends the chart title.
# Power mixes units: power supply probes report tenths of an amp, system consumption is in watts.
METRICS = {
    "temperature": {
        "table": "1.3.6.1.4.1.674.10892.5.4.700.20.1", "label": "Temperature", "default": "inlet",
        "short": lambda n: _strip(n, r"^system board ", r" temp(erature)?$"),
        "divisor": lambda sensor: 10, "unit": lambda sensor: "°C"},
    "fan": {
        "table": "1.3.6.1.4.1.674.10892.5.4.700.12.1", "label": "Speed", "default": "fan1a",
        "short": lambda n: _strip(n, r"^system board "),
        "divisor": lambda sensor: 1, "unit": lambda sensor: " RPM"},
    "power": {
        "table": "1.3.6.1.4.1.674.10892.5.4.600.30.1", "label": "", "default": "system-power",
        "short": _power_name,
        "divisor": lambda sensor: 10 if sensor.endswith("-current") else 1,
        "unit": lambda sensor: " A" if sensor.endswith("-current") else " W"},
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
    return "Average,Peak,Time\n" + "".join(f"{avg:g},{peak:g},{epoch_to_csv(t)}\n" for t, avg, peak in rows)


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
                   help="what to read: temperature, fan (RPM) or power (watts and amps); "
                        "fan and power need --source snmp (default: temperature; with --list, all metrics)")
    p.add_argument("--sensor", help="sensor to graph, see --list (default: inlet, fan1a or system-power depending on --metric)")
    p.add_argument("--list", nargs="?", const="all", metavar="SOURCE|METRIC",
                   help="list the available sensors and exit: every sensor from every source by default, "
                        "or only one source (web, snmp) or metric (temperature, fan, power); "
                        "--source and --metric also narrow the listing")
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
    p.add_argument("--output", choices=sorted(OUTPUTS), default="chart",
                   help="what to produce from the readings: chart, table, raw (cache format CSV, UTC times) or csv "
                        "(CSV with host, sensor and unit columns, times in --tz/local), or xlsx / xls spreadsheets "
                        "(see --file); --module, --chart, --width and --height apply to chart (default: chart)")
    p.add_argument("--file", metavar="FILE",
                   help="file to write for --output xlsx or xls; if not given, a name is made from the host, "
                        "source, sensor and --last period in the current directory, with the right extension")
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
    if args.file:
        if args.output not in ("xlsx", "xls"):
            p.error("--file only applies to --output xlsx or xls")
        suffix = Path(args.file).suffix.lower()
        if suffix in (".xls", ".xlsx") and suffix != "." + args.output:
            p.error(f"--file {args.file} does not match --output {args.output}")
    if args.list:  # --list gui means --list web
        args.list = SOURCE_ALIASES.get(args.list.lower(), args.list)
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
            lines.append(f"{avg:g},{peak:g},{epoch_to_csv(epoch)}")
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
    with idrac_session(args) as (s, base, headers):
        return web_sensors_from_session(s, base, headers)


async def snmp_walk_column(args, column):
    """Return {row index: value} for one column of the chosen metric's probe table."""
    resolve_credentials(args)
    try:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType,
            SnmpEngine, UdpTransportTarget, walk_cmd)
    except ImportError:
        raise SourceError("pysnmp not found; install with: pip install pysnmp")
    table = METRICS[args.metric]["table"]
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create((args.host, 161), timeout=3, retries=1)
        out = {}
        async for err, status, _, binds in walk_cmd(
                engine, CommunityData(args.community, mpModel=1), target, ContextData(),
                ObjectType(ObjectIdentity(f"{table}.{column}")), lexicographicMode=False):
            if err or status:
                raise SourceError(f"SNMP error from {args.host}: {err or status.prettyPrint()}")
            for oid, value in binds:
                out[str(oid).rsplit(".", 1)[-1]] = value
        return out
    finally:
        engine.close_dispatcher()


async def snmp_read_table(args):
    """Return {short name: (row index, reading in the metric's unit)}."""
    metric = METRICS[args.metric]
    names = await snmp_walk_column(args, SNMP_COL_NAME)
    readings = await snmp_walk_column(args, SNMP_COL_READING)
    if not names:
        raise SourceError(f"no {args.metric} sensors returned by {args.host} (check host and --community)")
    out = {}
    for i, n in names.items():
        if i in readings:
            sensor = metric["short"](str(n))
            out[sensor] = (i, int(readings[i]) / metric["divisor"](sensor))
    return out


def snmp_list(args):
    table = asyncio.run(snmp_read_table(args))
    base = METRICS[args.metric]["table"]
    return {name: f"{base}.{SNMP_COL_READING}.1.{i}" for name, (i, _) in table.items()}


def snmp_fetch(args):
    """Take one reading and append it to the cached history (SNMP has no history of its own)."""
    table = asyncio.run(snmp_read_table(args))
    if args.sensor not in table:
        raise SourceError(f"unknown sensor {args.sensor!r}; available: {', '.join(sorted(table))}")
    value = table[args.sensor][1]
    history = read_cache(cache_path(args)) or "Average,Peak,Time\n"
    if not history.endswith("\n"):
        history += "\n"
    return history + f"{value:g},{value:g},{epoch_to_csv(int(time.time()))}\n"


# Each source provides list(args) -> {sensor: key} and fetch(args) -> CSV text
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


def graph_termgraph(data, width, height, chart, title, unit):
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
                name += f"_{avg:g}{unit.replace(' ', '_')}"
                avg = peak = bar_width
            f.write(f"{name},{avg},{peak}\n")
        if flat:
            extra = ["--no-values"]
        f.flush()
        subprocess.run(["termgraph", f.name, "--width", str(bar_width),
                        "--suffix", unit, "--title", title, *extra,
                        *CHART_FLAGS[chart]], check=True)


def graph_plotext(data, width, height, chart, title, unit):
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
    MODULES[args.module](data, args.width, args.height, args.chart, title, metric["unit"](args.sensor))


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
                         f"{avg:g}", f"{peak:g}", unit])


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
                       + [[when.strftime("%Y-%m-%d %H:%M:%S"), f"{avg:g}", f"{peak:g}"] for when, avg, peak in data])
    table.justify_columns = {0: "left", 1: "right", 2: "right"}
    # printed above the table: terminaltables silently drops a title that is wider than the table
    print(title)
    print(table.table)


# Each output is a function output(args, metric, rows) that presents the selected readings, where rows is a list of
# (unix time in UTC, average, peak) and metric is the METRICS entry. Add new kinds of output (a table, JSON, ...)
# here and they become available through --output.
OUTPUTS = {"chart": output_chart, "table": output_table, "raw": output_raw, "csv": output_csv,
           "xlsx": output_xlsx, "xls": output_xls}


def list_all(args):
    """Print an aligned table of source, metric, sensor and key for every matching sensor; return an exit code."""
    sources, metrics = [args.source] if args.source else sorted(SOURCES), [args.metric] if args.metric else None
    if args.list != "all":
        if args.list in SOURCES:
            sources = [args.list]
        elif args.list in METRIC_NAMES:
            metrics = [args.list]
        else:
            sys.exit(f"--list: {args.list!r} is not a source ({', '.join(sorted(SOURCES))}) "
                     f"or a metric ({', '.join(METRIC_NAMES)})")
    rows, failed = [], 0
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
            rows += [(source, metric, name, key) for name, key in sorted(sensors.items())]
    if rows:
        header = ("SOURCE", "METRIC", "SENSOR", "KEY")
        widths = [max(len(r[i]) for r in [header] + rows) for i in range(3)]
        for row in [header] + rows:
            print("  ".join(c.ljust(w) for c, w in zip(row, widths)) + "  " + row[3])
    return 0 if rows else 1 if failed else 0


def main():
    args = parse_args()
    if args.no_fetch and (args.list or args.save_credentials or args.refresh):
        sys.exit("--no-fetch cannot be combined with --list, --save-credentials or --refresh, "
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
        saving.source = args.source or (args.list if args.list in SOURCES else None) or "web"
        saving.metric = args.metric or "temperature"
        try:
            SOURCES[saving.source]["list"](saving)
            save_credentials(saving)
        except (requests.RequestException, SourceError) as e:
            sys.exit(f"Not saving credentials: {e}")
        print(f"Credentials for {args.host} ({saving.source}) saved to the OS keyring", flush=True)
    if args.list:
        sys.exit(list_all(args))
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
