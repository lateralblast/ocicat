# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are `MAJOR.MINOR.PATCH`, but no number goes above 9: when a number would pass 9 it rolls over into the
next one, so 0.0.9 is followed by 0.1.0 and 0.9.9 by 1.0.0. Versions are therefore sequential release numbers
and do not follow the semantic versioning rules for what each number means.

The project had no version history before this file was written, so versions 0.0.1 to 0.5.9 were assigned
afterwards, one per step of development, all on 2026-10-06. The script reports its version with `--version`.

## [Unreleased]

### Changed
- README: added a Capabilities section summarising sources, metrics, outputs, modes, storage, time, credentials and
  safety, and corrected the current version to 0.5.3.

## [0.5.9] - 2026-10-15

### Changed
- `--add user` without `--new-pass` now generates a password instead of asking for one. It says so on stderr
  ("No --new-pass given: generating a password for NAME.") and prints `Generated password for NAME: ...` on stdout
  before anything is changed, so the password is not lost if the change fails half way or the user declines. It is 16
  random characters (from the `secrets` module): at least one upper case letter, lower case letter, digit and one of
  `-_.+=`, with no spaces, quotes or backslashes, so racadm accepts it too, and within an iDRAC8's 20 characters.
  Anyone running with `--yes` in a log or cron job should know the password then ends up in that output.

## [0.5.8] - 2026-10-15

### Added
- `--add user` creates an iDRAC account, from `--new-user NAME`, `--new-pass PASSWORD` (asked for twice on the terminal
  if missing) and `--privs`. `--user` and `--pass` stay the login that does the work, so the new account has its own
  flags. `--privs` takes `read` (Read Only, 0x1), `operator` (0x1f3), `write` or `admin` (Administrator, 0x1ff), `none`,
  or a number from 0 to 0x1ff; it has no default, so the level is always chosen. The account goes in the first free slot
  (2 to 16) and is enabled. An existing name is refused.
- It works like `--enable`: Redfish first (a PATCH of `Users.N.UserName`, `Password`, `Privilege` and `Enable`, then a
  read-back of the name), racadm `set iDRAC.Users.N.*` if Redfish cannot, under `idrac_lock`, a refused login never
  retried. It lists what it will do (never the password) and asks first unless `--yes` is given. Over racadm a password
  with spaces, quotes or a backslash is refused.
- `--add` cannot be combined with `--enable`, `--disable`, `--list`, `--get` and the other options that exclude them, and
  `--yes` applies to it. IPMI-over-LAN privilege and SNMPv3 settings of the new account are not touched.
- Not yet run against a real iDRAC: the `Users.N.*` attribute names and the privilege numbers are Dell's documented ones,
  tested against stand-ins only. Needs a login with the Configure Users privilege.

## [0.5.7] - 2026-10-15

### Changed
- `--enable snmp --version 3` now uses the strongest authentication and privacy types the iDRAC accepts when
  `--snmp-auth` or `--snmp-priv` is not given, instead of fixed `SHA` and `AES`. It tries `SHA512`, `SHA384`, `SHA256`,
  `SHA`, `MD5` and then `AES256`, `AES192`, `AES`, `DES` (never `None`), each as its own change, keeps the first the
  iDRAC accepts, and prints the flags to read with, for example `--snmp-auth sha256 --snmp-priv aes`. A value the
  attribute already has stops the search. The confirmation shows "the strongest of ...". A type given explicitly is set as given.
- `--snmp-auth` takes `md5`, `sha`, `sha256`, `sha384` or `sha512`, and `--snmp-priv` takes `none`, `des`, `aes`, `aes192`
  or `aes256`, for reading too. Reading still defaults to `sha` and `aes`, so after enabling the strongest, pass the
  flags that were printed.
- Not yet run against a real iDRAC: which of these values it accepts, and their exact spelling, are unknown (a rejected
  one only moves the search down). A run on an account that already has the strongest type still tries the stronger
  ones first, and asks again.

## [0.5.6] - 2026-10-15

### Added
- `--disable FUNCTION` switches functions of an iDRAC off, the opposite of `--enable`, by the same route (Redfish first,
  racadm if Redfish cannot, under `idrac_lock`, a refused login never retried). The names are `snmp`, `redfish`, `ssh`,
  `web` (also `wsman`) and `ipmi`, and each must be given by name: `all` and `list` are refused, so nothing is switched
  off by accident (`--enable list` shows the state). It lists the changes and asks first unless `--yes` is given, and
  warns when you turn off `web`, `redfish` or `ssh`, which are the ways in that ocicat and other tools use.
- `--disable snmp --version 1|2` leaves the agent on and sets the iDRAC's SNMP protocol to `SNMPv3`, so v1 and v2c
  stop. `--disable snmp --version 3` switches SNMPv3 off for the `--user` account only. `--disable snmp` alone turns
  the agent off.
- `--enable` and `--disable` cannot be used together, and `--yes` applies to both.
- Not yet run against a real iDRAC (attribute names as for 0.5.4 and 0.5.5); tested against stand-ins only.

## [0.5.5] - 2026-10-15

### Added
- SNMP versions: `--version 1`, `--version 2` (or `2c`) and `--version 3` choose the SNMP version of the snmp source
  (default 2c, as before). Bare `--version` still prints ocicat's version. v1 and v2c use `--community`. v3 logs in as
  the iDRAC user (`--user`/`--pass`, the same credentials as the web source: an iDRAC uses the user's password as both the
  authentication and the privacy key), with `--snmp-auth sha|md5` (default sha) and `--snmp-priv aes|des|none` (default
  aes). Saved credentials follow the version: a community for v1/v2c, the user and password for v3.
- `--enable snmp --version N` also sets the iDRAC's SNMP protocol: `All` for 1 and 2 (the iDRAC cannot serve only one of
  them) and `SNMPv3` for 3, which makes it stop answering v1 and v2c. For 3 it also switches on SNMPv3 for the `--user`
  account and sets its authentication and privacy types from `--snmp-auth` and `--snmp-priv`. Each change is listed and
  confirmed first. `--enable list` shows the protocol. The account slot is found from the user names in Redfish, or by
  asking racadm slot by slot.
- Changed: `--enable` writes the value `Enabled` over racadm (it wrote `1`) and prints each attribute it sets.
- Not yet run against a real iDRAC: the `SNMPProtocol`, `SNMPv3Enable`, `SNMPv3AuthenticationType` and
  `SNMPv3PrivacyType` names and values, and the SNMPv3 login itself, were checked against stand-ins only.

## [0.5.4] - 2026-10-15

### Added
- `--enable FUNCTION` switches on functions of an iDRAC: `snmp` (SNMP agent), `redfish`, `ssh`, `web` (web server, also
  `wsman`, which is served by it), `ipmi` (IPMI over LAN) or `all`; repeat the flag or separate names with commas.
  `--enable list` only shows which are enabled. It tries Redfish first (a PATCH of the Dell attributes under
  `/redfish/v1/Managers/iDRAC.Embedded.1/Attributes`) and uses racadm over ssh when Redfish is unreachable or has no
  attributes resource. A refused Redfish login stops there: the same credentials are never tried over ssh, which could
  lock the account. Everything runs under `idrac_lock`, with one login per transport. Functions already on are left
  alone, and it asks before changing anything unless `--yes` is given (without a terminal and without `--yes` it only
  says what it would do).
- `--yes`, which only applies to `--enable`.
- Not yet run against a real iDRAC: the racadm object names and Redfish attribute names are Dell's documented ones, but
  were only exercised against a stand-in in tests. WS-Man having no switch of its own is also unverified.

## [0.5.3] - 2026-10-07

### Changed
- The tool is renamed from ragdoll to **ocicat**, for **O**ut-of-band **C**ollector of **I**nventory, **C**harts **A**nd
  **T**elemetry (it was "Redfish/API/GUI/DRAC/Other Log Linter"). The script is now `ocicat.py`, and the README, `--help`
  and messages say ocicat. The entries below this one keep the name they were written under.
- The environment variable is `OCICAT_NO_INSTALL`, the keyring service is `ocicat:<host>`, and the default cache and
  database are `~/.cache/ocicat` and `~/.local/share/ocicat/ocicat.db`.
- The README logo is still `ragdoll.jpg`, which is a picture of the old name.

### Added
- Nothing stored under the old name is lost: credentials saved as `ragdoll:<host>` are still read (and
  `--forget-credentials` deletes both), and `~/.cache/ragdoll` and `~/.local/share/ragdoll/ragdoll.db` are used while they
  exist and the new locations do not.

## [0.5.2] - 2026-10-06

### Added
- `--source lmsensors` works on macOS, for this computer: temperature, fan, power and voltage, with no package to install.
  Tested on an Apple silicon MacBook Pro (M1 Max). Under a full CPU load the die temperatures rose from 28 to 34 °C, CPU
  power from 0.4 to 36 W and the fans from stopped to about 2,100 RPM.
  - `temperature`: the HID temperature sensors (`pmu-tdie0`, `pmu-tdev1`, `nand-ch0-temp`, `gas-gauge-battery-1` ...), read from
    IOKit with `ctypes`, without root. A sensor at several locations is numbered (`-1`, `-2`); duplicates at one location are
    averaged.
  - `fan`: the SMC (`fan1`, `fan2`), read through the AppleSMC user client with `ctypes`, without root. Handles Apple silicon
    floats and Intel `fpe2` values (Intel is untested).
  - `power`: `system-power`, `adapter-power` and `adapter-current` from the SMC; `battery-power` and `battery-current` from
    `ioreg -a` (parsed with `plistlib`); `cpu-power`, `gpu-power`, `ane-power` and `package-power` from `powermetrics`, run
    with `sudo -n` and left out when sudo refuses. `powermetrics` is skipped when the sensor asked for is available without it.
  - `voltage`: `adapter-voltage` (SMC) and `battery-voltage` (`ioreg`).
- The default sensor on macOS is `pmu-tdie0`, `system-power` or `battery-voltage`, by metric.

### Notes
- `ioreg`'s `PowerTelemetryData` (`SystemLoad`, `SystemPowerIn`) refreshes only about every 50 seconds, so it is not used for
  live power; the SMC keys `PSTR`, `PDTR`, `ID0R` and `VD0R` change on every read.
- Thermal pressure from `powermetrics` is not a number, so it is not a sensor.
- `--source lmsensors --host` still runs `sensors -j` on the remote computer, so it needs Linux there.

## [0.5.1] - 2026-10-06

### Added
- `--source system` reads the hardware inventory of the computer ragdoll runs on (`--list inventory`, `--get inventory`):
  `system_profiler -json` on macOS, and `dmidecode` on Linux. With `--host` it runs the same commands on another computer
  over ssh, as `lmsensors` does (key or agent only, strict host keys). The categories and attributes are the same as the
  iDRAC sources', so `--category`, `--name`, `--detail`, `--field` and every `--output` work with it.
- macOS: `system`, `bios` (boot ROM), `cpu`, `memory`, `nic`, `pci` (GPU and PCI cards), `controller` and `disk`, in
  about half a second.
- Linux: `dmidecode` for `system`, `bios`, `cpu` and `memory`, run as is and then with `sudo -n`; if both are refused, the
  system and BIOS come from `/sys/class/dmi/id` and a warning says the CPUs and memory are left out. `nic` from
  `/sys/class/net` (physical ports), `disk` and `virtual-disk` (RAID volumes such as a PERC's) from `lsblk`, and `pci` from
  `lspci` without bridges and chipset functions (16 items instead of 215 on a dual-socket R630). About a second over ssh.

### Changed
- `--list redfish` (and any other inventory-only source given to `--list`) now says the source has only the inventory,
  instead of printing nothing.
- The ssh runner used by `lmsensors` (`ssh_run`) takes any command, so other sources can use it.

## [0.5.0] - 2026-10-06

### Added
- Missing Python modules are installed at startup: ragdoll checks every module from `requirements.txt` with
  `importlib.util.find_spec` (which imports nothing, so the check adds no time) and runs `python3 -m pip install` for the
  missing ones, with the interpreter that is running it, adding `--user` for a system Python it cannot write to.
- `--no-install`, or `RAGDOLL_NO_INSTALL=1`, turns it off. A failed install prints a warning with the command to run.

## [0.4.9] - 2026-10-06

### Added
- `--source wsman` reads the hardware inventory over WS-Man with the `python-dracclient` module (`--list inventory`,
  `--get inventory`), needing neither ssh nor racadm. It covers `system`, `cpu`, `memory`, `nic`, `controller`, `disk` and
  `virtual-disk` like the other sources, `pci` for the video controller only and `firmware` for the Lifecycle Controller
  only, and has no `bios`, `idrac` or `raid-battery`. All the inventory options (`--category`, `--name`, `--detail`,
  `--field`, every `--output`) work with it. 40 items in 31 s on the test iDRAC, and 2 to 6 s for one category.
- It uses the web credentials. A refused login (HTTP 401) is reported after one attempt, in under a second.
- python-dracclient hard-codes `verify=False` and sets no request timeout, so its transport is wrapped: `--secure` is
  honoured and a request times out after 60 s. Its retries are set to one attempt (`ssl_retries=1`, `ready_retries=1`).
  Its exceptions carry no detail, so ragdoll reads the module's log message to say what failed (TLS, connection).
- `python-dracclient` and `six` in `requirements.txt` (the module imports `six` without declaring it).
- The `wsman` source has no metrics, like `racadm` and `redfish`.

### Changed
- The per-iDRAC lock is now shared by the racadm and wsman sources, so a run of one waits for a run of the other (a racadm
  run and a wsman run started together finished at 12 s and 15 s). The lock file is `<host>.idrac.lock` (it was
  `<host>.racadm.lock`; the old file is harmless and can be deleted).

### Changed
- The README now says plainly that the racadm source needs no `racadm` installed on this computer, because the commands
  run in the iDRAC's own ssh shell. I checked it by removing `racadm` from the search path: the source still worked.

## [0.4.8] - 2026-10-06

### Changed
- racadm commands now run strictly one at a time over one ssh login, so an iDRAC is never given parallel sessions. The
  whole inventory takes 41 s, the same as the 40 s with two at once, because the iDRAC runs them one at a time anyway.
- Only one ragdoll process at a time talks racadm to a given iDRAC: a per-host lock file (`<host>.racadm.lock`) in the
  cache directory makes a second process wait for the first, and give up with a message after ten minutes. The lock is
  released by the operating system if the holder crashes (checked with `kill -9`). Two simultaneous runs finished at
  8 s and 20 s, so they queued and did not overlap. Another iDRAC has its own lock.
- If the iDRAC reports "No more sessions are available" (someone else is using them), ragdoll waits 5 s, then 10 s, and
  tries again, giving up after three attempts (it was five attempts at 3 s steps).
- A refused login is still reported at once and never retried, so a wrong password costs one attempt per run.

## [0.4.7] - 2026-10-06

### Added
- `--source racadm` reads the hardware inventory with Dell's `racadm` (`--list inventory`, `--get inventory`), with the
  same categories, `--category`, `--name`, `--detail`, `--field` and `--output` options as the other inventory sources.
  It uses the web credentials.
- It runs `racadm getsysinfo -s` (system, BIOS), `racadm swinventory` (firmware) and `racadm hwinventory` (everything
  else) in the iDRAC's ssh shell with paramiko, so the password stays inside the ssh connection. Host keys are checked
  strictly, and `known_hosts` is never written.
- If ssh to the iDRAC cannot be reached, or paramiko is missing, and `racadm` is installed locally, it runs
  `racadm -r HOST -u USER -p PASSWORD --nocertwarn ...` instead (the password is then visible in the process list while
  it runs). A refused login is not retried that way.
- Two commands run at a time, because the iDRAC refuses a third racadm session ("No more sessions are available"); that
  message is also retried with a short wait. The whole inventory took 40 s on the test iDRAC, a system or BIOS lookup 11 s.
- On the test iDRAC racadm gave all 12 categories (80 items): 15 firmware items, 4 controllers, 23 PCI devices, 10 disks
  and RAID layouts such as `RAID 5` exactly.
- The `racadm` source has no metrics: a chart, a sensor listing, `--get` of a sensor and `--tail` are rejected with a
  message pointing to the inventory.

### Changed
- The ssh connection helper can log in with a password (for the iDRAC's own ssh shell) as well as with keys and the agent.

## [0.4.6] - 2026-10-06

### Added
- `--output json` for every kind of output:
  - readings: a JSON array of objects with the fields of `--output csv` (time with its UTC offset, host, source, metric,
    sensor, average, peak, unit);
  - `--list`: an array of objects with `source`, `metric`, `sensor`, `unit`, `value`, `limits` (an object, or `null`) and
    `key`;
  - `--get`: a single object, with the same fields;
  - the inventory (`--list inventory`, `--get inventory`): an array of `{category, name, details}` where `details` is an
    object of attribute and value (strings, as in the text listing);
  - `--tail`: JSON Lines, one compact object per poll.
- Numbers are JSON numbers (`15`, not `15.0`), a missing value or limit is `null`, and non-ASCII text such as `°C` is not
  escaped.
- `--field` and `--detail` work with JSON: `--get --field value --output json` prints `{"value": 15}`.
- "JSON" section in the README.

## [0.4.5] - 2026-10-06

### Changed
- `--source lmsensors --host` now connects with the `paramiko` module instead of running the `ssh` command. One
  connection is kept for the whole run and reused for every read: a four-metric `--list` took 0.8 s in all, and
  `--tail --poll 1` gives a reading every second. A dropped connection is reopened once.
- Host keys are checked strictly against `~/.ssh/known_hosts`: an unknown host is refused and a changed key is refused
  with a warning, and ragdoll never writes to the file. Login uses the ssh agent and key files, never a password.
- `~/.ssh/config` is read for `HostName`, `User`, `Port`, `IdentityFile` and `ProxyCommand`; `--user` overrides `User`.
- The `ssh` command (batch mode, with `--`) is still used when paramiko is not installed or the host's config uses
  `ProxyJump`.
- Clear messages for an unknown host key, a changed host key, a refused login, an unreachable or unknown host, and a
  missing `sensors` command.

### Added
- `paramiko` in `requirements.txt`.

## [0.4.4] - 2026-10-06

### Added
- `--source lmsensors --host HOST` runs `sensors -j` on HOST over ssh, for every command that works with the source
  (`--list lmsensors`, `--get`, `--tail`, charts, `--db`). `--user` is the ssh login name. Without `--host` it still
  reads this computer. The cache and database are named after the host.
- ssh runs in batch mode (`BatchMode=yes`, a 10 second connect timeout, `LogLevel=ERROR`), so it never waits for a
  password; it uses your keys, the ssh agent and `~/.ssh/config`. Failures give ssh's own message with a hint, and a
  missing `sensors` command on the other computer is reported as such.
- A `--host` or `--user` that starts with `-` or contains a space is rejected, and the command is run with `--`, so
  neither can be taken as an ssh option. `--pass` is rejected, since ssh is not given a password.

### Changed
- `--host` with `--source lmsensors` used to be rejected; it now selects the computer to read.

## [0.4.3] - 2026-10-06

### Added
- `--source lmsensors` reads the sensors of the computer ragdoll runs on, through `sensors -j` from lm-sensors:
  `temperature`, `fan`, `voltage` and `power` (currents are named `...-current` and shown in amps). Sensors are named
  `<chip>-<label>`, for example `coretemp-isa-0000-package-id-0`. lm-sensors' `crit`, `max`, `min` and `lcrit` become
  the limits, so `--list` and `--limits` show them.
- It needs no `--host` (and rejects one, since the readings are not from that host) and no credentials; the computer's
  hostname names it in the cache and the database.
- With no `--sensor`, it picks the CPU package, `tctl`, a drive's `composite`, or a `cpu` sensor, else the first one.
- `--get`, `--tail`, `--output`, `--db` and the cache work with it as with SNMP. It is listed only by name
  (`--list lmsensors`), not as part of a host's `--list`.
- A clear error when the `sensors` command is missing, fails, or is too old to give JSON.

### Changed
- `--host` is no longer required by the parser; it is still required for every source except a local one.
- `--get` now checks the metric against the source, so `--source lmsensors --metric health` is rejected up front, and its
  errors no longer say "Failed to read None".
- The code that takes a reading and appends it to the cache is shared by the snmp and lmsensors sources.

## [0.4.2] - 2026-10-06

### Added
- `--tail` keeps ragdoll running, polling the source for a sensor until Ctrl-C or SIGTERM. `--poll SECONDS` sets the
  interval (at least 1, default 30, about how often an iDRAC refreshes its sensors); giving `--poll` starts `--tail`.
  It defaults to `--source snmp`.
- `--output` chooses where each reading goes: `text` (a line per reading), `csv`, `raw` (the cache format) or `db`
  (also spelt `database`), which stores it in the SQLite database (`--db PATH`, or the default) and prints nothing.
  `--db` with `text` shows and stores.
- `--output db` also works for a single run, storing the fetched readings and reporting how many are in the database.
- Counters (`network`) are shown as a rate in the text output while the database keeps the raw counter.
- `--tail --source web` stores the whole hourly history on the first poll and shows only the newest sample, then shows
  new samples as they appear.
- A first poll that fails stops with an error; later failures print a warning and polling continues. Ctrl-C and SIGTERM
  print a summary. `--tail` does not write the CSV cache but does save limits.
- `--tail` is rejected with `--list`, `--get`, `--raw` and `--no-fetch`, and with `chart`, `table`, `xlsx` and `xls`.
  `--poll` below 1 second is rejected.
- "Polling with --tail" section in the README.

### Changed
- The README note on the web history's lag now says it looks to be written in batches: on one iDRAC the newest sample
  stayed put for about 12 hours and then twelve hourly samples appeared together.

## [0.4.1] - 2026-10-06

### Added
- `--source redfish` reads the hardware inventory over the iDRAC's Redfish API, for `--list inventory` and
  `--get inventory`, with the same categories, `--category`, `--name`, `--detail`, `--field` and `--output` options as
  the SNMP inventory. `--source web` (and `gui`) read the inventory over Redfish too. SNMP stays the default.
- It uses the web credentials (`--user`/`--pass`, the keyring or the environment), which the web and redfish sources
  share.
- Redfish gives more firmware (11 installed items against 2), the storage controllers (4 against 2) and drive and
  volume details, but only 12 PCI devices (against 23), no RAID battery, and `RAID 5 or RAID 6` where SNMP says
  `RAID 5`. Names of memory modules, network ports, CPUs, disks and virtual disks match SNMP's.
- A Redfish login uses a session token, made once per run and logged out afterwards (no sessions are left behind,
  checked against the iDRAC's session list), because every request with a password takes 5 to 9 seconds on an iDRAC8
  and about 0.5 s with a token. A full inventory takes about 43 seconds (10 of them logging in); one category about 12.
- The `redfish` source has no metrics: `--source redfish` with a chart, `--list` of sensors or `--get` of a sensor is
  rejected with a message pointing to the inventory.

## [0.4.0] - 2026-10-06

### Added
- `--detail DETAIL` returns just one attribute of each inventory item, as its value only: for example
  `--get inventory --name bios --detail version` prints `2.19.0`. The attribute name is not case-sensitive.
- In plain text the values are bare, one per line; `--output table` shows `CATEGORY`, `NAME` and the detail under its
  own name; `csv`, `xlsx` and `xls` give the `category,name,attribute,value` rows for just that attribute.
- Items without the detail are skipped. If none has it, the error lists the details the items do have.
- `--detail` is rejected without `--list inventory` or `--get inventory`, and together with `--field`.

## [0.3.9] - 2026-10-06

### Added
- `--field FIELD` returns just one field of what `--list` or `--get` prints, for example
  `--get inventory --category system --field details`, `--get --field value` (a bare `16`) or
  `--list fan --field sensor`. It is not case-sensitive.
- The fields are the columns of the chosen `--output`: for sensors `source`, `metric`, `sensor`, `unit`, `value`,
  `limits`, `key` in `text` and `table`, and the four limit columns in place of `limits` in `csv`, `xlsx` and `xls`;
  for the inventory `category`, `name`, `details` in `text` and `table`, and `category`, `name`, `attribute`,
  `value` in `csv`, `xlsx` and `xls`. A wrong name is rejected with the fields that exist.
- In plain text one field is printed as bare values, one per line with no heading, which suits scripts; the other
  formats keep the heading.
- `--field` is rejected without `--list` or `--get`.

## [0.3.8] - 2026-10-06

### Added
- `--category CATEGORY` limits `--list inventory` and `--get inventory` to one category (`system`, `idrac`, `bios`,
  `firmware`, `cpu`, `memory`, `nic`, `pci`, `controller`, `disk`, `virtual-disk`, `raid-battery`). Only that
  category's tables are read, so it is faster than the whole inventory (2 s against 5 s on the test server).
- `--name NAME` limits it to the items with that name, for example `--name DIMM.Socket.A1`. It is not
  case-sensitive and `*` and `?` are wildcards. With `--category` it narrows that category; without, it looks in
  every category.
- A name that matches nothing is rejected with the names that exist, and an unknown category with the valid ones.
  Both flags are rejected without `--list inventory` or `--get inventory`.
- Inventory spreadsheets are named after the filters, for example `<host>_inventory_memory_DIMM.Socket.A1.xlsx`.

## [0.3.7] - 2026-10-06

### Added
- `--get inventory` prints the hardware inventory, exactly the same as `--list inventory`, in every `--output`
  format. Plain `--get` still reads one sensor.

### Changed
- `--get` now takes an optional value. Any word other than `inventory` is rejected with a message pointing to
  `--metric` and `--sensor`.
- The inventory is also rejected when `--sensor` is given (it already rejected `--metric`), and its error messages no
  longer say "--list".

## [0.3.6] - 2026-10-06

### Added
- `--list inventory` lists the hardware over SNMP: system, iDRAC, BIOS and firmware, CPUs, memory modules, network
  ports, PCI devices, storage controllers, disks, virtual disks and the RAID battery (66 items, 314 attributes on the
  test server). Sizes are in GiB, speeds in MHz, MAC addresses are shown as `24:6E:...`, and status and state values
  are shown as words (`ok`, `online`, `RAID 5`).
- It works with `--output text|table|csv|xlsx|xls`. `text` and `table` give one row per item; `csv`, `xlsx` and `xls`
  give one row per attribute (`category,name,attribute,value`). A spreadsheet is named `<host>_inventory.xlsx` unless
  `--file` is given.
- `--list inventory` is rejected with `--source web` and with `--metric`, and `--save-credentials` with it saves for snmp.

### Changed
- The text, table, CSV and spreadsheet writers for `--list` now share common code that the inventory listing also
  uses. The sensor listings are unchanged.

### Changed
- The README is restructured. Features are grouped by task (sources and metrics, listing, getting a value, charts,
  other outputs, time zones, storage, credentials, running regularly) instead of in the order they were added, and
  the duplicated examples are merged into one "Example output" section.
- New in the README: a table of contents, a quick start, an option reference table, the default sensor of each
  metric, and the supported Python version (written and tested with 3.14; at least 3.9 is needed for `zoneinfo`,
  older versions untried).
- Corrected statements that had gone stale: the intro now lists all six metrics, and the units note includes volts,
  watt-hours and bytes per second.

## [0.3.5] - 2026-10-06

### Added
- `--get` polls one snmp sensor and prints its current value and unit (`16 °C`, `3840 RPM`, `112 W`), for quick
  checks and scripts. It defaults to `--source snmp`, and `--metric` and `--sensor` choose the sensor. Nothing is
  stored, so the cache and any `--db` database are left alone.
- For a counter metric (`network`) it reads twice, 2 seconds apart, and prints bytes per second.
- `--output table|csv|xlsx|xls` shows the sensor as a one-row listing (with unit, limits and key); `--output text`
  is the bare value and the default. `--file` names a spreadsheet.
- `--get` is rejected with `--list`, `--no-fetch` and `--source web`, and an unknown sensor is reported with the
  sensors that exist. A failure exits with status 1.

### Fixed
- Spreadsheet messages said "1 sensors"; they now say "1 sensor".

## [0.3.4] - 2026-10-06

### Added
- `--list` accepts `--output text|table|csv|xlsx|xls`. `text`, the aligned plain text, is the default and is also
  available by name. `table` uses `terminaltables`. `csv`, `xlsx` and `xls` keep numbers as numbers and split the
  limits into four columns (`lower_critical`, `lower_warning`, `upper_warning`, `upper_critical`); a missing value is
  an empty cell.
- Spreadsheet listings use `--file`, or are named `<host>_sensors[_<source>][_<metric>]` in the current directory,
  for example `192.0.2.20_sensors_snmp_fan.xlsx`.
- `--output chart` and `--output raw` are rejected with `--list`, and `--output text` without `--list`.

### Changed
- `--output` no longer has a fixed default: it is `chart` for readings and `text` for `--list`.

### Fixed
- Writing a sensor listing to `.xlsx` or `.xls` failed with a traceback when every cell in a column was empty (for
  example the upper limits of the fans). The column width is now worked out safely.

## [0.3.3] - 2026-10-06

### Added
- The `--list` table has three new columns: `UNIT` (°C, RPM, V, W, A, Wh, B/s or code), `VALUE` (the current reading)
  and `LIMITS` (`warn lower..upper crit lower..upper`, `-` where missing). The `KEY` column stays last. The `METRIC`
  column was already there.
- SNMP sensors take their value and limits from the poll that built the listing. The web source's row uses the newest
  cached reading and the saved limits, or `-` when there are none, because a live value would need a full fetch.
  `network` counters show `-`.
- `list` functions of the sources now return a `Reading` (key, value, limits) for each sensor.
- The README's "List the sensors" example shows the new columns.

## [0.3.2] - 2026-10-06

### Added
- `--list sensors` lists every sensor of every source and metric with its source, in the same aligned table as
  before (source, metric, sensor, key). It is the same as `--list` on its own, now spelt out; the help text and error
  message name it, and the value is case-insensitive.

### Changed
- The `--list` help text and its error message now name all six metrics.

## [0.3.1] - 2026-10-06

### Added
- `--metric voltage`: the power supply input voltages (`ps1-voltage`, `ps2-voltage`), in volts. The discrete
  power-good probes in the same table return no reading and are skipped.
- `--metric health`: a status code per component (system, CPUs, memory modules, power supplies, intrusion,
  batteries, RAID controller and battery, disks, virtual disks, fan and power redundancy), 41 sensors on the test
  server, so state changes can be charted. Only the *status* columns are used, because the disk, virtual disk and
  battery *state* columns are numbered differently.
- `--metric network`: byte counters of the iDRAC's own interfaces (IF-MIB `ifHCInOctets` and `ifHCOutOctets`),
  shown as bytes per second between readings. The counter is what is stored; a counter that goes down (a restart) is
  skipped, and one reading explains that two are needed.
- New `power` sensors from the power usage table: `energy` (Wh), `peak-power`, `peak-current`, `idle-power`,
  `max-power`, `headroom` and `peak-headroom`.
- `--limits` draws a sensor's warning and critical limits on plotext `line` and `scatter` charts. Each SNMP poll saves
  them next to the cache (`<host>_<metric>-<sensor>.limits.json`), shared by sources.

### Changed
- Each metric now has its own reader, so metrics need not be a single probe table. An SNMP poll shares one engine
  and connection between its walks instead of creating one per walk, which made a 41-sensor health poll take 1.8 s
  instead of 4.5 s.
- `--list` shows the new sensors, and the sensor listing keys use the full row index.
- The `--metric` help and README list the new metrics.

### Fixed
- Readings were formatted with six significant digits, so a value of seven digits or more would have been stored
  in scientific notation and lost precision (for example the 2,224,483,828 byte counter, or an energy total over
  999,999 Wh). Values are now written in full.

### Added
- "More SNMP telemetry" section in `TODO.md`: voltage, threshold lines, health status, network traffic, energy and
  peak power, amperage units from the probe type, and the event log, with their OIDs confirmed against the Dell iDRAC MIB.

## [0.3.0] - 2026-10-06

### Added
- `--output xlsx` and `--output xls` write the selected readings to an Excel workbook (`XlsxWriter` for `.xlsx`,
  `xlwt` for `.xls`). One `Readings` sheet with a bold heading row and columns `Time (<zone>)`, `Time (UTC)`,
  `Average (<unit>)` and `Peak (<unit>)`; times are real Excel dates.
- `--file FILE` names the output file. It only applies to `xlsx` and `xls`, adds the extension if the name has none,
  and rejects a name with the other spreadsheet extension.
- Without `--file`, a name is made in the current directory:
  `<host>_<source>_<metric>-<sensor>_last-<period>.<ext>`, for example
  `192.0.2.20_web_temperature-inlet_last-1w.xlsx`.
- `.xls` refuses more than 65,535 readings, with a message suggesting `.xlsx` or a smaller `--last`.
- `XlsxWriter` and `xlwt` added to `requirements.txt`; "Spreadsheet output" section in the README.

## [0.2.9] - 2026-10-06

### Added
- `--output csv` prints the selected readings as CSV with `time`, `host`, `source`, `metric`, `sensor`, `average`,
  `peak` and `unit` columns, so each row describes itself. Times are ISO 8601 with their UTC offset, in local time or
  the zone chosen with `--tz` or `--utc`.
- `--output raw` prints the selected readings in the cache format (`Average,Peak,Time`, UTC times ending in `Z`).
- Both honour `--last` (`--last 0` means every reading). The older `--raw` flag is unchanged: it prints every
  stored reading and ignores `--last`.
- "CSV output" section in the README.

### Added
- "Example output" section in the README with real output for `--list`, line, bar and termgraph charts, the table
  output, SNMP readings with `--tz`, and `--raw`.

## [0.2.8] - 2026-10-06

### Added
- `--output table` prints the readings as a table (time, average and peak, with units) using `terminaltables`, in
  the zone chosen by `--tz` or `--utc`. It uses the plain ASCII style, which also works when piped. The chart title
  is printed above the table, because `terminaltables` drops a title wider than the table.
- `terminaltables` added to `requirements.txt`.
- "Table output" section in the README.

## [0.2.7] - 2026-10-06

### Added
- `--output` selects what is produced from the readings. `chart` is the default and the only choice for now.
  Outputs are registered in an `OUTPUTS` table, so new kinds (a table, JSON, ...) can be added later. `--module`,
  `--chart`, `--width` and `--height` apply to `chart`.

### Fixed
- Piping output into a command that exits early (for example `ragdoll.py --raw | head -2`) printed a
  `BrokenPipeError` traceback. The script now exits quietly.

## [0.2.6] - 2026-10-06

### Fixed
- The time labels on the x-axis of `line` and `scatter` charts (plotext) were shifted by this computer's UTC offset,
  so on an AEDT machine every label was 11 hours late: a sample at 06:00:31 local was labelled 17:00:31. This also
  affected `--tz` and `--utc`, which showed the same shift. plotext 5.3.2's date axis adds the machine's offset to
  whatever it is given. The chart now plots Unix times and draws the axis labels itself, in the chosen time zone.
  Bar, stacked and histogram charts, and termgraph, were not affected.

### Changed
- The number of x-axis labels on `line` and `scatter` charts follows the usable width: 3 at 80 columns, more in a
  wider terminal.

## [0.2.5] - 2026-10-06

### Changed
- When `--db` stores readings, any "no reading" rows (`-128`) that earlier versions (0.1.8 to 0.2.3) saved in the
  database are deleted and the file is compacted with `VACUUM`, so the space they used is freed. A test database
  holding 23,237 such rows shrank from about 990 KB to 8 KB. A database with no such rows is not touched.
  New readings of this kind were already never stored from 0.2.4.

## [0.2.4] - 2026-10-06

### Fixed
- Rows where the iDRAC reports `-128` for both Average and Peak, its marker for "no reading", are now skipped. They
  used to be charted as -128 °C spikes (23,237 of 48,984 rows, 47%, in one iDRAC's history). They are dropped when
  the web CSV is fetched, when a cache is read, when rows are stored in the database and when they are read back from
  it, so caches and databases from earlier versions are cleaned up without being rewritten.

### Changed
- `--raw` prints the readings that would be charted, in the cache format, instead of the cache file as it is, so it
  no longer includes the skipped rows. It still prints the file unchanged if it holds no readable rows, to help you
  inspect a malformed one.

## [0.2.3] - 2026-10-06

### Added
- `--tz ZONE` shows chart times in a named time zone (for example `Australia/Sydney` or `America/New_York`), using
  Python's `zoneinfo`, instead of this computer's local time zone. An unknown name is rejected, and `--tz` cannot
  be combined with `--utc`. The stored data, cache and `--raw` output stay in UTC.

## [0.2.2] - 2026-10-06

### Added
- `gui` is accepted as an alias for the `web` source in `--source` and `--list` (case-insensitive). The alias uses
  the same cache files and database rows as `web`.

## [0.2.1] - 2026-10-06

### Added
- `--username` as an alias for `--user`, and `--password` as an alias for `--pass`.

## [0.2.0] - 2026-10-06

### Fixed
- Times from the two sources were in different, unmarked time zones: snmp readings were stamped with this
  computer's local time and the web source's CSV used the iDRAC's own wall clock, so combining them in `--db` or
  in one chart misaligned them (by 11 hours on the author's setup).

### Changed
- All times are now UTC inside ragdoll. The cache and `--raw` output use ISO 8601 with a `Z` (for example
  `2026-10-06T04:27:44Z`) and the database stores `time` as an integer Unix timestamp (schema version 2).
- The web source converts the iDRAC's wall clock times to UTC when it fetches them. The offset is measured by
  comparing the iDRAC's clock (via Redfish `DateTime`) with this computer's clock, not taken from the iDRAC's
  configured time zone, which can be wrong.
- Charts show times in local time, with the time zone name in the title (for example `[AEDT]`).
- Caches and databases from earlier versions are not read. An old cache is refetched (web) or ignored (snmp), and an
  old database is refused with a message asking you to delete it and run again.

### Added
- `--utc` shows chart times in UTC.
- `--tz-offset OFFSET` sets the offset of the iDRAC's clock from UTC for the web source (for example `UTC`, `+10:00`,
  `-05:00`), instead of measuring it.
- "Time zones" section in the README.

### Added
- `TODO.md`, with database performance measurements and planned improvements.

## [0.1.9] - 2026-10-06

### Added
- `--no-fetch` charts what is already stored without contacting the iDRAC: from the `--db` database if given,
  otherwise from the cache regardless of age. `--raw` prints the stored readings as CSV.
- "Charting without contacting the iDRAC" section in the README.

### Changed
- `--no-fetch` is rejected with `--list`, `--save-credentials` or `--refresh`, which need the iDRAC.

## [0.1.8] - 2026-10-06

### Added
- `--db [PATH]` stores readings in a SQLite database, using the built-in `sqlite3` module, and draws charts from
  it so that history from earlier runs and sources is included. Without `PATH` the database is
  `~/.local/share/ragdoll/ragdoll.db` (or `$XDG_DATA_HOME/ragdoll/ragdoll.db`).
- One `readings` table keyed on host, source, metric, sensor and time. Rows already stored are skipped, so
  repeating a run is safe; the web source's full history (about 61,000 rows) is stored in about a second.
- "Storing readings in SQLite" section in the README.

### Added
- "Chart CPU temperatures" example in the README, including how to build up SNMP history with cron.

## [0.1.7] - 2026-10-06

### Changed
- The default cache directory is `~/.cache/ragdoll` (or `$XDG_CACHE_HOME/ragdoll`) instead of `~/.cache/idrac`.
  Cached files from earlier versions are not found in the new location: move the old directory
  (`mv ~/.cache/idrac ~/.cache/ragdoll`) to keep your SNMP history.
- The option to choose the cache directory is now `--cachedir`. `--cache-dir` still works as an alias.

## [0.1.6] - 2026-10-06

### Changed
- Credentials are saved in the keyring under the service name `ragdoll:<host>` instead of `idrac-temp:<host>`.
  Credentials saved by an earlier version are not found under the new name: run `--save-credentials` again, and
  remove the old entries from your keyring if you have any.

### Fixed
- `--save-credentials` was ignored when combined with `--list` (since 0.1.2), because the listing exited first.
  Credentials are now saved before the listing runs, for `--source` or, if that is not given, the source named
  after `--list`, otherwise `web`. The confirmation message names the source.

## [0.1.5] - 2026-10-06

### Added
- "Help Support Development" section in the README.

### Changed
- The script is renamed from `idrac_temp.py` to `ragdoll.py`, and the project is now called ragdoll. Update any
  cron entries or aliases that call the old name.
- New project description: "Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information
  into more useful formats". It is used in the README and in the script's `--help`.

## [0.1.4] - 2026-10-06

### Added
- `LICENSE` file containing the Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
  (CC BY-NC-SA 4.0) legal text. The license is referenced in the script's docstring and in the README.
- "Version" and "License" sections in the README.
- `.gitignore`, ignoring `CLAUDE.md` and `__pycache__/`.

## [0.1.3] - 2026-10-06

### Changed
- `--list` prints an aligned table with a `SOURCE METRIC SENSOR KEY` header row instead of tab-separated lines.
- Added `--version`.

## [0.1.2] - 2026-10-06

### Changed
- `--list` with no value lists every sensor from every source and metric, with the source shown for each.
- `--list` takes an optional source (`web`, `snmp`) or metric (`temperature`, `fan`, `power`) to narrow the listing.
  `--source` and `--metric` narrow it too.
- `--source` and `--metric` no longer have defaults on the command line; `web` and `temperature` apply when not listing.

### Added
- A source that cannot be queried while listing is reported on stderr and skipped instead of stopping the listing.

## [0.1.1] - 2026-10-06

### Added
- `--metric temperature|fan|power`. The `snmp` source now reads fan speeds (RPM) from the Dell fan probe table
  and power from the amperage probe table: `system-power` in watts and `ps1-current`, `ps2-current` in amps.
- Default sensor per metric: `inlet`, `fan1a` and `system-power`.
- Chart titles and axes use the unit of the metric and sensor (°C, RPM, W, A).
- Cache files are named per metric, for example `<host>_snmp_fan-fan1a.csv`.

### Fixed
- termgraph draws one block per unit when every value in a series is identical (for example a fan at a constant
  speed), producing lines thousands of characters wide. The script now draws a full-width bar and shows the value
  in the label.

## [0.1.0] - 2026-10-06

### Added
- Credentials can be saved in the operating system keyring with `--save-credentials`, which saves only after a
  successful login, and removed with `--forget-credentials`.
- Missing credentials are looked up in the order: command-line flag, environment variable (`IDRAC_USER`,
  `IDRAC_PASS`, `IDRAC_COMMUNITY`), keyring, then a password prompt.
- `keyring` added to `requirements.txt`.
- "Saving credentials" section in the README.

### Changed
- `--user`, `--pass` and `--community` are no longer required on the command line.

## [0.0.9] - 2026-10-06

### Added
- `snmp` source, using the `pysnmp` module, reading the Dell temperature probe table (`inlet`, `exhaust`,
  `cpu1`, `cpu2`). SNMP reports only the current value, so each run appends a reading to the cached history.
- `--community` for the SNMP v2c community string.
- `pysnmp` added to `requirements.txt`.
- `README.md` describing the script, its capabilities and examples, using placeholder credentials.

## [0.0.8] - 2026-10-06

### Added
- `--source` option, with `web` (the iDRAC web interface) as the only source and the default. Sources are
  registered in a table so that others can be added.

### Changed
- Cache files include the source in their name, for example `<host>_web_temperature-inlet.csv`. Files from
  earlier versions are no longer used and can be deleted.

## [0.0.7] - 2026-10-06

### Added
- `--list` shows the sensors available on the iDRAC.

### Fixed
- The iDRAC web interface ignores unknown sensor names and returns inlet data, so a chart labelled `exhaust` showed
  inlet readings. `--sensor` is now checked against the iDRAC's own sensor list and unknown names are rejected.

## [0.0.6] - 2026-10-06

### Added
- `--height` (default 24 rows, plotext only) and a `--width` default of 80 columns, so charts fit a standard terminal.
  One row is left free for the shell prompt.
- Chart titles name the host and sensor, for example "192.168.8.98 Inlet Temperature (Average / Peak)", and
  values are labelled in °C.

### Changed
- `--width` is the total width of the graph. For termgraph the label and value columns are subtracted from it.

## [0.0.5] - 2026-10-06

### Added
- `--module plotext|termgraph`, with `plotext` as the default.
- `--chart line` and `--chart scatter` (plotext only). The existing chart types are also supported by plotext.
- `requirements.txt`, with `plotext` pinned to 5.3.2 because 6.x has a different API.

## [0.0.4] - 2026-10-06

### Added
- Local cache of fetched CSV data, one file per host and sensor, under `~/.cache/idrac` (or `$XDG_CACHE_HOME/idrac`).
- `--cache-dir`, `--max-age` (default 3600 seconds) and `--refresh`.
- If a fetch fails and a cache file exists, the cache is used and a warning is printed.
- Cache files are written to a temporary file first and then renamed, so an interrupted fetch cannot corrupt them.

## [0.0.3] - 2026-10-06

### Added
- `--last` accepts a period (`hour`, `day`, `week`, `month`, `year`, and forms such as `6h`, `2d`, `3 weeks`) as
  well as a row count. A period is counted back from the newest sample in the data.

### Changed
- `--last` defaults to 10 rows. It was previously all rows.

## [0.0.2] - 2026-10-06

### Added
- `--host` option for the iDRAC address. It was previously a positional argument.
- `--chart` option with `vertical` (default), `horizontal`, `stacked` and `histogram` charts (termgraph).

## [0.0.1] - 2026-10-06

### Added
- Initial script `idrac_temp.py`: downloads the inlet temperature statistics CSV from an iDRAC and graphs it in
  the terminal with termgraph.
- `--user` and `--pass` options, so no credentials are written in the script. `--sensor`, `--secure`, `--raw`,
  `--width` and `--last` (row count).
- Certificate verification is off by default because iDRACs use self-signed certificates.
- The CSV columns (`Average,Peak,Time`) are graphed as two series.

### Fixed
- The iDRAC rejects HTTP basic authentication and returns its login page. The script now logs in through
  `/data/login`, sends the session token with the request and logs out afterwards.
