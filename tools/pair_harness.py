"""Run one pairing experiment at a time, and record what actually happened.

Run it on the server, as root::

    sudo python -m tools.pair_harness --state
    sudo python -m tools.pair_harness --reset
    sudo python -m tools.pair_harness --setup 1
    sudo python -m tools.pair_harness --start 1      # only when the operator is ready
    sudo python -m tools.pair_harness --stop 1

Why this exists
---------------
Every wrong conclusion in this subsystem has come from reasoning over a partial
capture: a console that "never pages us" measured across 75 s when the console
cycles links on a longer period, an eviction story a capacity test later
disproved, a detector that flagged healthy hardware because it read an instant
rather than an interval. The fix is not more inference. It is running one
variable at a time and keeping the bytes.

**There is no timed capture window.** The operator may be away from the console,
and a countdown is a window to miss. ``--start`` opens an open-ended capture and
``--stop`` closes it, so the experiment lasts exactly as long as the operator
needs. Nothing here arms pairing or drops a link except on an explicit command.

What it reads, and from where
-----------------------------
State is read from the **radio and the filesystem**, never from what we asked
for earlier -- the same discipline as ``bt_link_probe``:

* links and handles: the kernel connection-list ioctl
* settings: the management socket
* bonds: ``/var/lib/bluetooth``, because ``org.bluez`` has been observed
  reporting an empty bond list for an adapter whose key file exists

Actions go through the **running server's API** rather than behind its back.
The server owns these adapters; a harness that clears a bond or flips an
advertisement directly would be a second writer, which is the fight this
project keeps losing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from server.bt import mgmt

#: Where captures and per-test notes are kept. Deliberately outside the repo:
#: these are megabytes of btmon output belonging to one debugging session.
CAPTURE_ROOT = Path("/tmp/rbgc-pair")

#: bluetoothd's bond store. A directory named like a BD_ADDR is a bond.
BLUEZ_STATE = Path("/var/lib/bluetooth")

#: Where the packaged service keeps the client password.
PASSWORD_FILE = Path("/etc/rbgc/password")
SERVER_CONFIG = Path("/etc/rbgc/server.json")

#: Lines worth counting in a capture, in the order they should occur. A test
#: that stops partway through this list has told you exactly how far it got,
#: which is the whole point -- "it did not connect" and "it connected and we
#: tore it down" are different faults with the same symptom.
CAPTURE_MARKERS = (
    ("advertising enabled", "LE Set Advertise Enable"),
    ("connections", "LE Connection Complete"),
    ("connections (ext)", "LE Enhanced Connection Complete"),
    ("SMP pairing request", "SMP: Pairing Request"),
    ("SMP pairing response", "SMP: Pairing Response"),
    ("SMP pairing confirm", "SMP: Pairing Confirm"),
    ("SMP pairing failed", "SMP: Pairing Failed"),
    ("SMP encryption info", "SMP: Encryption Information"),
    ("encryption changes", "Encryption Change"),
    ("LTK requests", "LE Long Term Key Request"),
    ("LTK negative replies", "LE Long Term Key Request Negative Reply"),
    ("ATT writes from host", "ATT: Write"),
    ("disconnects", "Disconnect Complete"),
)


# -- the tests -------------------------------------------------------------
#
# Each entry says what state the adapters must be in and what the operator
# does. `enabled` is a count: test 1 runs one adapter so nothing else can
# answer, and later tests add adapters back one at a time. That is the only
# way to tell "pairing a second controller broke the first" from "the console
# rotates between whatever it can see".

TESTS: dict[int, dict[str, object]] = {
    1: {
        "name": "one adapter, no bonds, console pairing mode",
        "enabled": 1,
        "clear_bonds": True,
        "operator": "Put the console into pairing mode and let it pair.",
        "question": "Does a single adapter pair cleanly at all?",
    },
    2: {
        "name": "sleep then wake the bonded adapter",
        "enabled": 1,
        "clear_bonds": False,
        "operator": "Nothing on the console. The harness sleeps and wakes it.",
        "question": "Does a bonded adapter reconnect on its own?",
    },
    3: {
        "name": "all four advertising, one bonded",
        "enabled": 4,
        "clear_bonds": False,
        "operator": "Nothing. Leave the console on and idle.",
        "question": "Does the console still find the bonded one among four?",
    },
    4: {
        "name": "pair a second adapter",
        "enabled": 2,
        "clear_bonds": False,
        "operator": "Put the console into pairing mode and pair the new one.",
        "question": "Does the first adapter's bond survive?",
    },
    5: {
        "name": "pair the third and fourth",
        "enabled": 4,
        "clear_bonds": False,
        "operator": "Put the console into pairing mode and pair one more.",
        "question": "At what count does it break?",
    },
    6: {
        "name": "restart the server with everything bonded",
        "enabled": 4,
        "clear_bonds": False,
        "operator": "Nothing. The harness restarts the server.",
        "question": "Do all bonded adapters come back?",
    },
}


# -- reading state ---------------------------------------------------------


def _adapters() -> list[dict[str, object]]:
    """Every adapter, from MGMT, with its bonds and live links."""
    try:
        with mgmt.MGMTSocket() as sock:
            settings = sock.read_all()
    except Exception as exc:                       # pragma: no cover - hardware
        print(f"could not open the management socket: {exc}", file=sys.stderr)
        return []

    rows = []
    for info in sorted(settings.values(), key=lambda s: s.index):
        rows.append({
            "index": info.index,
            "hci": f"hci{info.index}",
            "bd_addr": info.bd_addr,
            "powered": info.powered,
            "bondable": info.bondable,
            "bredr": info.bredr,
            "le": info.le,
            "bonds": bonds_on_disk(info.bd_addr),
            "links": links(info.index),
            "advertising": advertising(info.index),
        })
    return rows


def bonds_on_disk(bd_addr: str) -> list[str]:
    """Bonds as bluetoothd actually stored them.

    Read from the filesystem rather than D-Bus: ``org.bluez`` has been observed
    reporting an empty bond list for an adapter whose key file existed and
    which a console was actively resuming against.
    """
    directory = BLUEZ_STATE / bd_addr.upper()
    if not directory.is_dir():
        return []
    return sorted(
        entry.name for entry in directory.iterdir()
        if entry.is_dir() and len(entry.name) == 17 and entry.name.count(":") == 5
    )


def links(index: int) -> list[str]:
    """Live connections, via the kernel's own list."""
    try:
        from tools.bt_link_probe import list_connections
    except Exception:                              # pragma: no cover - hardware
        return []
    return [
        str(conn.get("peer", "?"))
        for conn in list_connections(index)
        if conn.get("type") == "LE"
    ]


def advertising(index: int) -> bool | None:
    """Whether our advertising instance is on the air. None if unreadable."""
    try:
        with mgmt.MGMTSocket() as sock:
            return bool(sock.advertising_instances(index))
    except Exception:
        return None


def print_state(title: str = "state") -> list[dict[str, object]]:
    rows = _adapters()
    print(f"\n=== {title} ===")
    print(f"{'hci':<6}{'address':<20}{'adv':<6}{'bond':<6}{'link':<6}"
          f"{'powered':<9}{'bondable':<10}{'radio'}")
    for row in rows:
        adv = {True: "yes", False: "NO", None: "?"}[row["advertising"]]
        radio = "LE only" if not row["bredr"] else "dual"
        print(
            f"{row['hci']:<6}{row['bd_addr']:<20}{adv:<6}"
            f"{len(row['bonds']):<6}{len(row['links']):<6}"
            f"{'yes' if row['powered'] else 'NO':<9}"
            f"{'yes' if row['bondable'] else 'NO':<10}{radio}"
        )
    return rows


# -- talking to the server -------------------------------------------------


class ServerAPI:
    """The running server's admin API.

    Actions go through it rather than straight to BlueZ so there is one owner
    of adapter state. A harness that cleared a bond directly would be a second
    writer, and the server's next reconcile would disagree with it.
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._ctx = ssl.create_default_context()
        # Self-signed by design: the server generates its own certificate and
        # prints the fingerprint. Verification here would only ever fail.
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._cookie = ""

    def _password(self) -> str:
        if SERVER_CONFIG.is_file():
            try:
                admin = json.loads(SERVER_CONFIG.read_text()).get("admin_password")
                if admin:
                    return str(admin)
            except Exception:
                pass
        if PASSWORD_FILE.is_file():
            return PASSWORD_FILE.read_text().strip()
        raise SystemExit(
            f"no password: expected {SERVER_CONFIG} admin_password or {PASSWORD_FILE}"
        )

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        if self._cookie:
            request.add_header("Cookie", self._cookie)
        try:
            with urllib.request.urlopen(request, context=self._ctx, timeout=15) as r:
                for header, value in r.getheaders():
                    if header.lower() == "set-cookie":
                        self._cookie = value.split(";", 1)[0]
                body = r.read().decode() or "{}"
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() or "{}"
            try:
                return json.loads(body)
            except Exception:
                return {"error": body[:200]}

    def login(self) -> None:
        result = self._request("/api/login", {"password": self._password()})
        if not self._cookie:
            raise SystemExit(f"could not sign in to {self.base}: {result}")

    def status(self) -> dict:
        return self._request("/api/status")

    def enable(self, bd_addr: str, enabled: bool) -> dict:
        return self._request("/api/adapter/enable",
                             {"bd_addr": bd_addr, "enabled": enabled})

    def pair(self, bd_addr: str, duration: int = 600) -> dict:
        return self._request("/api/adapter/pair",
                             {"bd_addr": bd_addr, "pairable": True,
                              "duration": duration})

    def wake(self, bd_addr: str) -> dict:
        return self._request("/api/adapter/wake", {"bd_addr": bd_addr})

    def sleep(self, bd_addr: str) -> dict:
        return self._request("/api/adapter/disconnect", {"bd_addr": bd_addr})

    def forget(self, bd_addr: str) -> dict:
        return self._request(
            "/api/adapter/disconnect",
            {"bd_addr": bd_addr, "forget": True, "confirm_orphan": True},
        )


# -- capture ---------------------------------------------------------------


def _test_dir(number: int) -> Path:
    return CAPTURE_ROOT / f"{number:02d}"


def _pidfile(number: int) -> Path:
    return _test_dir(number) / "btmon.pids"


def start_capture(number: int, indices: list[int]) -> None:
    """One btmon per adapter, open-ended, into this test's directory."""
    if shutil.which("btmon") is None:
        raise SystemExit("btmon is not installed; nothing could be captured")

    directory = _test_dir(number)
    directory.mkdir(parents=True, exist_ok=True)

    pids = []
    for index in indices:
        target = directory / f"hci{index}.txt"
        process = subprocess.Popen(
            ["btmon", "-i", f"hci{index}"],
            stdout=target.open("w"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        pids.append(process.pid)
    _pidfile(number).write_text("\n".join(str(pid) for pid in pids))
    (directory / "started_at").write_text(str(time.time()))
    print(f"capturing {len(pids)} adapter(s) into {directory} -- open-ended")


def stop_capture(number: int) -> None:
    path = _pidfile(number)
    if not path.is_file():
        print("no capture was running for this test")
        return
    for line in path.read_text().split():
        try:
            os.kill(int(line), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    path.unlink(missing_ok=True)
    time.sleep(1)


def summarise(number: int) -> None:
    """What each adapter saw, in the order it should have seen it."""
    directory = _test_dir(number)
    captures = sorted(directory.glob("hci*.txt"))
    if not captures:
        print(f"no captures in {directory}")
        return

    for capture in captures:
        text = capture.read_text(errors="replace")
        print(f"\n=== {capture.stem} ===")
        for label, marker in CAPTURE_MARKERS:
            count = text.count(marker)
            # Zero is a finding, not noise -- print every marker so the point
            # the exchange stopped is visible rather than inferred from gaps.
            print(f"  {label:<26} {count}")

        reasons = sorted({
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("Reason:")
        })
        if reasons:
            print(f"  {'disconnect reasons':<26} {'; '.join(reasons)}")

    started = directory / "started_at"
    if started.is_file():
        since = int(time.time() - float(started.read_text()))
        print(f"\n--- server log for the last {since + 5}s ---")
        _print_server_log(since + 5)


def _print_server_log(seconds: int) -> None:
    if shutil.which("journalctl") is None:
        return
    result = subprocess.run(
        ["journalctl", "-u", "rbgc-server", "--since", f"-{seconds}s", "--no-pager"],
        capture_output=True, text=True, input="",
    )
    interesting = (
        "agent", "pair", "auth", "bond", "orphan", "advertis", "link state",
        "output report", "vendor", "disconnect", "gamepad live",
    )
    for line in result.stdout.splitlines():
        low = line.lower()
        if any(word in low for word in interesting):
            print("  " + line.split("]: ", 1)[-1])


# -- commands --------------------------------------------------------------


def do_reset(api: ServerAPI) -> None:
    """Every adapter enabled, bonded to nobody, advertising."""
    print("Clearing every bond and putting all adapters back on the air.")
    for row in _adapters():
        api.enable(row["bd_addr"], True)
        if row["bonds"]:
            result = api.forget(row["bd_addr"])
            print(f"  {row['hci']}: {result.get('message', result)}")
        api.wake(row["bd_addr"])
    time.sleep(3)
    print_state("after reset")


def do_setup(api: ServerAPI, number: int) -> None:
    """Put the adapters where test N needs them. Captures nothing."""
    test = TESTS[number]
    rows = _adapters()
    wanted = int(test["enabled"])

    print(f"\nTest {number}: {test['name']}")
    print(f"Question: {test['question']}")

    for position, row in enumerate(rows):
        enabled = position < wanted
        api.enable(row["bd_addr"], enabled)
        if enabled and test["clear_bonds"] and row["bonds"]:
            api.forget(row["bd_addr"])
        if enabled:
            api.wake(row["bd_addr"])

    time.sleep(3)
    print_state(f"ready for test {number}")
    print(f"\nWhen you are at the console and ready:  --start {number}")
    print(f"Then: {test['operator']}")


def do_start(api: ServerAPI, number: int) -> None:
    test = TESTS[number]
    rows = [row for row in _adapters() if row["advertising"] or row["links"]]
    start_capture(number, [int(row["index"]) for row in rows])

    if number in (1, 4, 5):
        # Arm our side. Ten minutes, because the operator sets the pace.
        for row in rows:
            if not row["bonds"]:
                result = api.pair(row["bd_addr"], duration=600)
                print(f"  {row['hci']}: {result.get('message', result)}")

    print(f"\n{test['operator']}")
    print(f"Tell me when you are done, then: --stop {number}")


def do_stop(api: ServerAPI, number: int) -> None:
    stop_capture(number)
    print_state(f"after test {number}")
    summarise(number)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--api", default="https://127.0.0.1:8080",
                        help="the running server's admin API")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--state", action="store_true",
                       help="print what the radios are doing and stop")
    group.add_argument("--reset", action="store_true",
                       help="clear every bond and put all adapters on the air")
    group.add_argument("--setup", type=int, metavar="N",
                       help="prepare for test N; captures nothing")
    group.add_argument("--start", type=int, metavar="N",
                       help="begin an open-ended capture for test N")
    group.add_argument("--stop", type=int, metavar="N",
                       help="end test N's capture and summarise")
    group.add_argument("--list", action="store_true", help="list the tests")
    args = parser.parse_args(argv)

    if args.list:
        for number, test in TESTS.items():
            print(f"{number}. {test['name']}\n     {test['question']}")
        return 0

    if args.state:
        print_state()
        return 0

    for number in (args.setup, args.start, args.stop):
        if number is not None and number not in TESTS:
            parser.error(f"no test {number}; --list shows them")

    api = ServerAPI(args.api)
    api.login()

    if args.reset:
        do_reset(api)
    elif args.setup is not None:
        do_setup(api, args.setup)
    elif args.start is not None:
        do_start(api, args.start)
    elif args.stop is not None:
        do_stop(api, args.stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
