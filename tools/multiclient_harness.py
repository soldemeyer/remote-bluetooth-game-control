"""Multi-client controller harness: does player 2 cost player 1 anything?

``tools/latency_harness.py`` answers "how fast is one client?" and cannot answer
this one -- it builds a single transport, and ``--controllers`` only adds slots
inside that one session. The question here is different and needs genuinely
independent sessions: separate client ids, separate crypto, separate sockets,
separate router entries, separate adapters.

What it measures, and why these and not others
----------------------------------------------
``rtt`` carries a known upward bias of up to one poll period, because acks are
read once per tick (see ``ControllerLatency.record_ack``). It is still the
number a player feels, so it is reported -- but the headline is:

``bt_write``
    ``server_bt_ts - server_recv_ts``, both taken on the server's clock with no
    polling in between. On the BLE transport this is time spent inside
    ``BLESink.send_input_report``, which is where the D-Bus marshalling lives.
    It is the unbiased measure of server-side cost per report.

The phases mirror the reproduction plan:

  A  player 1 alone                     -- the baseline
  B  player 1 + player 2 connected idle -- separates setup cost from load
  C  both players active                -- the reported failure

Phase A and phase C are the comparison that matters. Anything extra in phase B
is setup cost; anything beyond that in C is load.

Patterns are synthetic on purpose so the offered rate is a known quantity: a
real stick's rate depends on how hard someone is pushing it, which is exactly
the variable this test has to control.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

from client.net.transport import ClientTransport
from common.state import Button, ControllerState
from common.timing import high_resolution_timers, now_ns

#: Offered input rate per player. Matches the client default poll rate, so the
#: harness reproduces what a player with a moving stick actually generates.
DEFAULT_HZ = 500

#: Ask for an ack this often. The real client asks at 20 Hz, which over a
#: 20 s phase is 400 samples -- so p99 is the fourth-worst of them and moves
#: by a millisecond on noise alone. 100 Hz gives 2000 samples per 20 s and a
#: percentile worth comparing, at the cost of 100 small return packets a
#: second. That cost is identical in every phase, so it cannot bias the
#: comparison the harness exists to make.
ACK_INTERVAL_NS = 10_000_000


@dataclass
class Player:
    """One independent client session, driven from its own thread."""

    name: str
    host: str
    port: int
    password: str
    slot: int = 0

    transport: ClientTransport | None = None
    thread: threading.Thread | None = None

    #: Set while the player should be generating input. Clearing it leaves the
    #: session up and the heartbeat running -- which is exactly phase B.
    active: threading.Event = field(default_factory=threading.Event)
    stop_flag: threading.Event = field(default_factory=threading.Event)

    #: Which pattern this player drives. Distinct per player on purpose: it is
    #: only possible to prove player 1's input never reached player 2's adapter
    #: if the two are distinguishable on the wire.
    button: int = 0
    axis: str = "left"

    sent: int = 0
    hz: int = DEFAULT_HZ

    def connect(self, timeout_ns: int = 10_000_000_000) -> None:
        self.transport = ClientTransport(self.password, client_name=self.name)
        self.transport.connect(self.host, self.port, timeout_ns=timeout_ns)

    def close(self) -> None:
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def start(self) -> None:
        self.stop_flag.clear()
        self.thread = threading.Thread(
            target=self._run, name=f"player-{self.name}", daemon=True
        )
        self.thread.start()

    def _state(self, tick: int) -> ControllerState:
        """A distinguishable, continuously-changing pattern.

        Continuous change is the point: send-on-change means a still stick
        produces almost no traffic, and a load test that generates no load
        would report that everything is fine.
        """
        # Full-scale sweep, so every packet differs by more than the client's
        # axis deadband and none is filtered out as noise.
        phase = (tick % 200) - 100
        value = int(phase * 327)
        state = ControllerState()
        if self.axis == "left":
            state.left_x = value
        else:
            state.right_y = value
        # Square wave on the button, ~5 Hz at a 500 Hz offered rate.
        if (tick // 50) % 2 == 0:
            state.buttons = self.button
        return state

    def _run(self) -> None:
        interval_ns = 1_000_000_000 // max(1, self.hz)
        next_tick = now_ns()
        last_ack = 0
        tick = 0

        with high_resolution_timers():
            while not self.stop_flag.is_set():
                now = now_ns()
                if now < next_tick:
                    remaining = (next_tick - now) / 1e9
                    if remaining > 0.001:
                        time.sleep(remaining - 0.0005)
                    continue
                next_tick += interval_ns
                if now - next_tick > interval_ns * 10:
                    next_tick = now + interval_ns  # resync after a long stall

                transport = self.transport
                if transport is None:
                    return

                if self.active.is_set():
                    want_ack = now - last_ack >= ACK_INTERVAL_NS
                    if want_ack:
                        last_ack = now
                    try:
                        transport.send_input(
                            self.slot, self._state(tick), request_ack=want_ack
                        )
                        self.sent += 1
                    except Exception:
                        return
                    tick += 1

                try:
                    transport.service()
                except Exception:
                    return

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict:
        if self.transport is None:
            return {}
        return self.transport.latency_snapshot().get(self.slot, {})

    def reset_stats(self) -> None:
        """Clear samples so each phase is measured on its own, not cumulatively."""
        if self.transport is None:
            return
        entry = self.transport._latency.get(self.slot)  # noqa: SLF001
        if entry is not None:
            entry.stats.rtt.clear()
            entry.stats.bt_write.clear()
        self.sent = 0


class Operator:
    """The web GUI, driven the way a person would drive it.

    Approval and assignment are operator actions, and on a production server
    they are the only way a client's input reaches a console: `auto_approve`
    is false by default, and is runtime-only by design. Doing them here means
    the harness measures the same configuration the operator actually runs,
    rather than requiring the server be restarted into a different one.
    """

    def __init__(self, host, password, port=8080):
        self._base = f"https://{host}:{port}"
        self._password = password
        # The server's certificate is self-signed and its fingerprint is
        # printed at startup for the operator to check. Verifying it here would
        # mean pinning that fingerprint into a test tool; this connection
        # carries no secret the password does not already protect.
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx),
            urllib.request.HTTPCookieProcessor(CookieJar()),
        )

    def _post(self, path, payload):
        request = urllib.request.Request(
            self._base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(request, timeout=10) as response:
            return json.loads(response.read() or b"{}")

    def _get(self, path):
        with self._opener.open(self._base + path, timeout=10) as response:
            return json.loads(response.read() or b"{}")

    def login(self):
        self._post("/api/login", {"password": self._password})

    def status(self):
        return self._get("/api/status")

    def approve(self, client_id):
        try:
            self._post("/api/approve", {"client_id": client_id})
            return True
        except urllib.error.HTTPError:
            return False

    def assign(self, bd_addr, client_id, slot):
        try:
            self._post(
                "/api/assign",
                {"bd_addr": bd_addr, "client_id": client_id, "slot": slot},
            )
            return True
        except urllib.error.HTTPError:
            return False

    def place(self, players):
        """Approve every player and give each one its own adapter.

        Returns the (player, bd_addr) pairs actually placed. One adapter per
        player, taken in the order the server reports them, which is the
        persisted controller number -- so player 1 lands on Controller 1.
        """
        for player in players:
            self.approve(player.transport._client_id.hex())  # noqa: SLF001

        adapters = [a["bd_addr"] for a in self.status().get("adapters", [])]
        placed = []
        for player, bd_addr in zip(players, adapters):
            client_id = player.transport._client_id.hex()  # noqa: SLF001
            if self.assign(bd_addr, client_id, player.slot):
                placed.append((player, bd_addr))
        return placed


def _row(label: str, stats: dict, sent: int, seconds: float) -> str:
    rtt = stats.get("rtt", {})
    bt = stats.get("bt_write", {})
    return (
        f"  {label:<24}"
        f"rtt p50 {rtt.get('p50', 0):6.2f} p95 {rtt.get('p95', 0):6.2f}"
        f" p99 {rtt.get('p99', 0):6.2f}  |  "
        f"bt_write p50 {bt.get('p50', 0):6.3f} p99 {bt.get('p99', 0):6.3f}"
        f"  |  sent {sent:6d} ({sent / max(seconds, 0.001):5.0f}/s)"
    )


def _measure(players: list[Player], seconds: float, label: str) -> dict:
    for player in players:
        player.reset_stats()
    started = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - started

    print(f"\n{label}")
    result = {}
    for player in players:
        stats = player.stats()
        state = "active" if player.active.is_set() else "idle"
        print(_row(f"{player.name} ({state})", stats, player.sent, elapsed))
        result[player.name] = stats
    return result


def _verdict(results: dict, first: str) -> None:
    """State plainly whether player 1 was hurt, and by how much."""

    def get(phase: str, metric: str, stat: str = "p99") -> float:
        return results.get(phase, {}).get(first, {}).get(metric, {}).get(stat, 0.0)

    print("\n" + "=" * 78)
    for stat in ("p50", "p95", "p99"):
        print(
            f"Player 1 {stat} RTT:      alone {get('A', 'rtt', stat):6.2f} ms"
            f"   +idle peer {get('B', 'rtt', stat):6.2f} ms"
            f"   +active peer {get('C', 'rtt', stat):6.2f} ms"
        )
    print(
        f"Player 1 p50 bt_write: alone {get('A', 'bt_write', 'p50'):6.3f} ms"
        f"                             +active peer"
        f" {get('C', 'bt_write', 'p50'):6.3f} ms"
    )

    baseline = get("A", "rtt")
    if baseline <= 0:
        print("\nNo samples. Is the client approved and assigned an adapter?")
        print("=" * 78)
        return

    print()
    verdicts = []
    for stat, budget in (("p50", 15.0), ("p95", 20.0), ("p99", 25.0)):
        alone, loaded = get("A", "rtt", stat), get("C", "rtt", stat)
        if alone <= 0:
            continue
        change = (loaded - alone) / alone * 100
        # Only degradation counts. An improvement is not a regression -- an
        # earlier version of this check failed a run that got 50% *better*,
        # which is exactly the kind of confidently-wrong report this project
        # keeps having to unpick.
        ok = change <= budget
        verdicts.append(ok)
        print(
            f"  {stat}: {change:+6.1f}%   (budget +{budget:.0f}%)   "
            f"{'ok' if ok else 'DEGRADED'}"
        )

    # p50 is the number a player feels and the one worth trusting: p99 over a
    # short phase is a handful of samples and moves on background load.
    print()
    print(
        "PASS -- player 1 is not materially degraded by player 2"
        if all(verdicts)
        else "FAIL -- player 1 is materially degraded by player 2"
    )
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-client latency harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47800)
    parser.add_argument("--password", default=None, help="defaults to $RBGC_PASSWORD")
    parser.add_argument("--hz", type=int, default=DEFAULT_HZ)
    parser.add_argument(
        "--duration", type=float, default=15.0, help="seconds per phase"
    )
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument(
        "--web-port", type=int, default=8080,
        help="web GUI port, for approving and assigning the test clients",
    )
    parser.add_argument(
        "--no-operator", action="store_true",
        help="do not approve/assign via the web GUI (use with --auto-approve)",
    )
    args = parser.parse_args()

    password = args.password or os.environ.get("RBGC_PASSWORD", "")
    if not password:
        print("No password: pass --password or set RBGC_PASSWORD", file=sys.stderr)
        return 2

    patterns = [
        (Button.A, "left"),
        (Button.B, "right"),
        (Button.X, "left"),
        (Button.Y, "right"),
    ]

    players: list[Player] = []
    for index in range(args.clients):
        button, axis = patterns[index % len(patterns)]
        players.append(
            Player(
                name=f"p{index + 1}",
                host=args.host,
                port=args.port,
                password=password,
                slot=0,
                button=int(button),
                axis=axis,
                hz=args.hz,
            )
        )

    print(
        f"Connecting {len(players)} independent client session(s) to "
        f"{args.host}:{args.port} at {args.hz} Hz offered\n"
    )

    try:
        for player in players:
            player.connect()
            player.start()
            print(
                f"  {player.name}: client_id "
                f"{player.transport._client_id.hex()[:12]}"  # noqa: SLF001
                f"  capacity {player.transport.server_capacity}"
            )
    except Exception as exc:
        print(f"Connect failed: {exc}", file=sys.stderr)
        for player in players:
            player.close()
        return 1

    # Approve and assign, the way the operator does. Skipped only when the
    # server was started with --auto-approve.
    if not args.no_operator:
        try:
            operator = Operator(args.host, password, port=args.web_port)
            operator.login()
            placed = operator.place(players)
            print()
            for player, bd_addr in placed:
                print(f"  {player.name} -> adapter {bd_addr}")
            if len(placed) < len(players):
                print(
                    f"\nOnly {len(placed)} of {len(players)} players got an "
                    f"adapter. Is capacity lower than --clients?",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"\nCould not drive the web GUI ({exc}).", file=sys.stderr)
            print("Pass --no-operator if the server runs with --auto-approve.",
                  file=sys.stderr)

    # Let the assignment settle before measuring anything.
    time.sleep(2.0)

    first, rest = players[0], players[1:]
    results = {}
    try:
        # --- Phase A: player 1 alone --------------------------------------
        first.active.set()
        for player in rest:
            player.active.clear()
        results["A"] = _measure(
            [first], args.duration, "Phase A -- player 1 alone (baseline)"
        )

        # --- Phase B: others connected but silent -------------------------
        results["B"] = _measure(
            players,
            args.duration,
            "Phase B -- player 1 active, others connected but idle",
        )

        # --- Phase C: everyone active -------------------------------------
        for player in players:
            player.active.set()
        results["C"] = _measure(
            players, args.duration, "Phase C -- all players active"
        )

        _verdict(results, first.name)
    finally:
        for player in players:
            player.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
