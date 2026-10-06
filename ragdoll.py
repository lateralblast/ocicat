#!/usr/bin/env python3
"""Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information into more useful formats

Currently reads iDRAC temperature, fan and power data (web interface or SNMP) and graphs it in the terminal.

License: Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
(CC BY-NC-SA 4.0), see the LICENSE file or https://creativecommons.org/licenses/by-nc-sa/4.0/
"""

import argparse
import asyncio
import contextlib
import copy
import csv
import datetime as dt
import getpass
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import urllib3

__version__ = "0.1.7"


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


TIME_FORMAT = "%a %b %d %H:%M:%S %Y"
UNITS = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
UNIT_NAMES = {"hour": "h", "day": "d", "week": "w", "month": "m", "year": "y"}


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
    stamps = [dt.datetime.strptime(label, TIME_FORMAT) for label, _, _ in data]
    cutoff = max(stamps) - dt.timedelta(seconds=n)
    return [row for row, t in zip(data, stamps) if t >= cutoff]


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
    accumulate = SOURCES[args.source]["accumulate"]
    if not accumulate and not args.refresh and path.exists() and time.time() - path.stat().st_mtime < args.max_age:
        return path.read_text()
    try:
        text = SOURCES[args.source]["fetch"](args)
    except (requests.RequestException, SourceError) as e:
        if path.exists():
            print(f"warning: fetch failed ({e}); using stale cache {path}", file=sys.stderr)
            return path.read_text()
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
    p.add_argument("--user", help="iDRAC username (web source; else $IDRAC_USER or the keyring)")
    p.add_argument("--pass", dest="password",
                   help="iDRAC password (web source; else $IDRAC_PASS, the keyring, or a prompt)")
    p.add_argument("--community",
                   help="SNMP v2c community string (snmp source; else $IDRAC_COMMUNITY, the keyring, or the standard default)")
    p.add_argument("--save-credentials", action="store_true",
                   help="after a successful login, save the credentials for this host in the OS keyring")
    p.add_argument("--forget-credentials", action="store_true",
                   help="delete this host's credentials from the OS keyring and exit")
    p.add_argument("--source", choices=sorted(SOURCES),
                   help="where sensor data comes from: web is the iDRAC web interface (history), "
                        "snmp reads the current value and builds history in the cache (default: web; "
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
    p.add_argument("--refresh", action="store_true", help="ignore the cache and fetch fresh data")
    p.add_argument("--raw", action="store_true", help="print the raw CSV and exit")
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
    return p.parse_args()


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
    return resp.text


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
    path = cache_path(args)
    history = path.read_text() if path.exists() else "Average,Peak,Time\n"
    if not history.endswith("\n"):
        history += "\n"
    return history + f"{value:g},{value:g},{dt.datetime.now().strftime(TIME_FORMAT)}\n"


# Each source provides list(args) -> {sensor: key} and fetch(args) -> CSV text
# ("Average,Peak,Time" rows). accumulate=True means fetch() returns only a current reading
# appended to the cached history. Others (redfish, ipmi, ...) can be added here.
SOURCES = {
    "web": {"list": web_list, "fetch": web_fetch, "accumulate": False, "metrics": ("temperature",)},
    "snmp": {"list": snmp_list, "fetch": snmp_fetch, "accumulate": True, "metrics": METRIC_NAMES},
}


def parse_rows(text):
    """Parse 'Average,Peak,Time' CSV into [(time, average, peak)]."""
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        try:
            out.append((row["Time"].strip(), float(row["Average"]), float(row["Peak"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def short_label(label):
    """Drop the weekday and year: 'Wed Sep 19 10:12:52 2018' -> 'Sep 19 10:12:52'."""
    return label.split(None, 1)[-1].rsplit(" ", 1)[0]


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
        for label, avg, peak in data:
            # termgraph splits on whitespace, so keep each label to one token
            name = short_label(label).replace(" ", "_")
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
    labels = [short_label(label) for label, _, _ in data]
    avg = [a for _, a, _ in data]
    peak = [p for _, _, p in data]
    names = ["Average", "Peak"]

    if chart in ("line", "scatter"):
        plot = plt.plot if chart == "line" else plt.scatter
        stamps = [dt.datetime.strptime(label, TIME_FORMAT).strftime("%d/%m/%Y %H:%M:%S")
                  for label, _, _ in data]
        plt.date_form("d/m/Y H:M:S")
        plot(stamps, avg, label=names[0])
        plot(stamps, peak, label=names[1])
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
    try:
        text = load_data(args)
    except (requests.RequestException, SourceError) as e:
        sys.exit(f"Failed to fetch data from {args.host}: {e}")

    if args.raw:
        print(text)
        return

    data = parse_rows(text)
    if not data:
        sys.exit("No numeric data found in CSV; rerun with --raw to inspect it")
    try:
        data = select_last(data, args.last)
    except ValueError as e:
        sys.exit(f"Could not parse timestamps in CSV: {e}")
    name = " ".join(w.upper() if re.match(r"(cpu|ps)\d*$", w) else w.title() for w in args.sensor.split("-"))
    title = f"{args.host} {name} {metric['label']} (Average / Peak)".replace("  ", " ")
    MODULES[args.module](data, args.width, args.height, args.chart, title, metric["unit"](args.sensor))


if __name__ == "__main__":
    main()
