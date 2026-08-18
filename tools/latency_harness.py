"""End-to-end latency harness.

Runs a real server and a real client in one process, drives synthetic input at
known timestamps, and reports where the time actually goes. No Bluetooth
hardware and no gamepad required.

This exists so "latency is bad" can be answered with a per-stage breakdown
instead of a guess. It is also the regression check for the sub-millisecond
software budget: if a change makes our own code slower, the numbers here move.

Usage:
    python -m tools.latency_harness
    python -m tools.latency_harness --duration 30 --poll-hz 1000
"""

from __future__ import annotations

import argparse
import sys
import time

from client.input.synthetic import SyntheticBackend
from client.loop import InputLoop, SlotRuntime
from client.net.transport import ClientTransport
from common.protocol import ControlOp
from common.state import Button, ControllerState
from common.timing import high_resolution_timers, now_ns
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager

PASSWORD = "harness-test-password"
PORT = 47899


def build_server(controllers: int) -> tuple[Datapath, Router, list[MockSink]]:
    router = Router()
    sinks: list[MockSink] = []

    for index in range(controllers):
        sink = MockSink(name=f"harness{index}")
        sinks.append(sink)
        router.add_channel(
            OutputChannel(
                bd_addr=f"00:00:00:00:00:{index:02X}",
                hci_name=f"harness{index}",
                profile=create_profile("generic"),
                sink=sink,
            )
        )

    sessions = SessionManager(PASSWORD, auto_approve=True)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=PORT, realtime=False
    )
    datapath.start()

    return datapath, router, sinks


def measure_report_build(iterations: int = 20000) -> dict[str, float]:
    """Time report generation alone -- pure CPU, no I/O.

    This is the part of the latency budget entirely under our control, so it is
    worth measuring in isolation from anything that can be blamed on the network.
    """
    results = {}

    for profile_name in ("generic", "switch_pro"):
        profile = create_profile(profile_name)
        state = ControllerState(
            buttons=Button.A | Button.DPAD_UP, left_x=12345, right_y=-4242,
            left_trigger=200, right_trigger=17,
        )
        buf = bytearray(128)

        # Warm up so we measure steady state, not first-call overhead.
        for _ in range(1000):
            profile.build_input_report(state, buf)

        start = now_ns()
        for _ in range(iterations):
            profile.build_input_report(state, buf)
        elapsed = now_ns() - start

        results[profile_name] = (elapsed / iterations) / 1000.0  # microseconds

    return results


def run_harness(duration_s: float, poll_hz: int, controllers: int) -> int:
    print("\n  RBGC latency harness")
    print(f"    controllers   {controllers}")
    print(f"    poll rate     {poll_hz} Hz")
    print(f"    duration      {duration_s:.0f} s")
    print()

    print("  Measuring report generation (CPU only)...")
    build_times = measure_report_build()
    for name, microseconds in build_times.items():
        print(f"    {name:<12} {microseconds:.2f} us per report")
    print()

    datapath, router, sinks = build_server(controllers)

    backend = SyntheticBackend(count=controllers, animate=True)
    backend.open()

    transport = ClientTransport(PASSWORD, client_name="harness")
    print(f"  Connecting to 127.0.0.1:{PORT} ...")

    try:
        transport.connect("127.0.0.1", PORT)
    except Exception as exc:
        print(f"  error: {exc}", file=sys.stderr)
        datapath.stop()
        return 1

    print(f"  Connected. Server capacity {transport.server_capacity}.\n")

    slots = []
    for index in range(controllers):
        backend.acquire(index)
        slots.append(
            SlotRuntime(
                slot=index,
                instance_id=index,
                username=f"harness{index}",
                device_name=f"Synthetic {index}",
            )
        )

    # Announce the controllers exactly as a real client does. Without this the
    # server only knows about slot 0 (inferred at session creation) and every
    # other slot's packets are counted unroutable.
    transport.queue_control(
        ControlOp.SET_CONTROLLERS,
        {
            "client_name": "harness",
            "controllers": [
                {"slot": s.slot, "username": s.username, "device_name": s.device_name}
                for s in slots
            ],
        },
    )

    loop = InputLoop(backend, transport, poll_hz=poll_hz, axis_deadband=0)
    loop.set_slots(slots)

    with high_resolution_timers():
        loop.start()
        print(f"  Running for {duration_s:.0f} s...")

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            time.sleep(0.25)

        loop.stop()

    report(transport, datapath, loop, sinks, build_times)

    transport.close()
    datapath.stop()
    backend.close()
    return 0


def report(transport, datapath, loop, sinks, build_times) -> None:
    print("\n" + "=" * 68)
    print("  LATENCY BREAKDOWN")
    print("=" * 68)

    server_stats = datapath.stats_snapshot()
    process = server_stats["process_ms"]

    print("\n  Server-side, directly measured (trustworthy):")
    if process["count"]:
        print(f"    packet handling      p50 {process['p50']:.3f} ms   "
              f"p99 {process['p99']:.3f} ms   worst {process['worst']:.3f} ms")

    for index, sink in enumerate(sinks):
        stats = sink.write_stats()
        if stats["count"]:
            print(f"    sink write [{index}]      p50 {stats['p50']:.3f} ms   "
                  f"p99 {stats['p99']:.3f} ms   ({stats['count']} reports)")

    print("\n  Report generation (CPU only):")
    for name, microseconds in build_times.items():
        print(f"    {name:<20} {microseconds:.2f} us")

    print("\n  Round-trip, as measured by the client:")
    print("    NOTE: biased upward by up to one poll period, because acks are")
    print(f"          read once per tick. At {loop.stats_snapshot()['poll_hz']} Hz "
          f"that is up to {1000.0 / loop.stats_snapshot()['poll_hz']:.1f} ms.")

    latency = transport.latency_snapshot()
    for slot, stats in sorted(latency.items()):
        rtt = stats["rtt"]
        if rtt["count"]:
            print(f"    slot {slot}   RTT p50 {rtt['p50']:.2f} ms   "
                  f"p99 {rtt['p99']:.2f} ms   ({rtt['count']} samples)")

    loop_stats = loop.stats_snapshot()
    tick = loop_stats["tick_ms"]
    print("\n  Client input loop:")
    if tick["count"]:
        print(f"    tick duration        p50 {tick['p50']:.3f} ms   "
              f"p99 {tick['p99']:.3f} ms   worst {tick['worst']:.3f} ms")
    for entry in loop_stats["slots"]:
        encode = entry["encode_ms"]
        if encode["count"]:
            print(f"    slot {entry['slot']} encode+send  p50 {encode['p50']:.3f} ms   "
                  f"p99 {encode['p99']:.3f} ms   ({entry['packets_sent']} packets)")

    print("\n  Throughput:")
    print(f"    packets received     {server_stats['packets_received']}")
    print(f"    dropped              {server_stats['packets_dropped']}")
    print(f"    unroutable           {server_stats['packets_unroutable']}")
    print(f"    decrypt failures     {server_stats['decrypt_failures']}")

    software_ms = _software_budget(process, loop_stats)
    print("\n" + "-" * 68)
    print(f"  SOFTWARE-ADDED LATENCY (our code, both sides):  ~{software_ms:.3f} ms")
    print("  Budget is 1.000 ms.  ", end="")
    print("PASS" if software_ms < 1.0 else "OVER BUDGET")
    print("-" * 68)
    print("\n  Not included, and outside our control:")
    print("    gamepad -> client PC      1-8 ms   (USB polling rate)")
    print("    network, one way          ~0.2 ms LAN / 10-60 ms WAN")
    print("    server -> console         5-15 ms  (Bluetooth interval)")
    print()


def _software_budget(process: dict, loop_stats: dict) -> float:
    """Our own overhead: client encode+send plus server packet handling."""
    client_side = 0.0
    for entry in loop_stats["slots"]:
        encode = entry["encode_ms"]
        if encode["count"]:
            client_side = max(client_side, float(encode["p50"]))

    server_side = float(process["p50"]) if process["count"] else 0.0
    return client_side + server_side


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="latency-harness",
        description="Measure where RBGC latency actually goes. No hardware needed.",
    )
    parser.add_argument("--duration", type=float, default=10.0, help="Seconds to run")
    parser.add_argument("--poll-hz", type=int, default=500, help="Client poll rate")
    parser.add_argument("--controllers", type=int, default=2, help="Controllers to simulate")
    args = parser.parse_args(argv)

    try:
        return run_harness(args.duration, args.poll_hz, max(1, min(4, args.controllers)))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
