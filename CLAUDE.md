# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A two-part system for playing a game console remotely. Gamepads plug into **client PCs**
anywhere on the internet; their inputs stream over UDP to a **Linux server** (target:
Raspberry Pi 5) which impersonates Bluetooth game controllers to a console or BT receiver.

- Up to **4 client PCs**, each with **1–4 gamepads**.
- Active controller capacity equals the number of **enabled Bluetooth adapters** on the
  server (design ceiling 4). It is *not* hardcoded — see "Dynamic adapter capacity".
- Server is headless, controlled by a **web GUI**.
- A shared **password** gates all client connections.

## The latency budget — read before optimizing anything

The original goal was 2–5 ms input-to-console. **That is not physically reachable.** The
dominant costs sit outside our code:

| Stage | Typical | Whose cost |
|---|---|---|
| Gamepad → client PC | 1–8 ms (USB 1000→125 Hz); 5–15 ms if the pad is itself Bluetooth | hardware |
| Client capture → encrypt → send | **0.05–0.3 ms** | **ours** |
| Network, one way | ~0.2 ms LAN; 10–60 ms WAN | physics |
| Server recv → route → BT write | **0.05–0.3 ms** | **ours** |
| BT HID → console | 5–15 ms (connection/sniff interval) | Bluetooth spec |
| **Total, LAN** | **~8–25 ms** | |
| **Total, internet** | **~25–90 ms** | |

**The engineering target is: software adds < 1 ms, with minimal jitter.** Optimize for
that and for tail latency (p99), not for a number the radio layer makes impossible.

When someone reports "latency is bad", get the measured per-stage breakdown from
`tools/latency_harness.py` or the GUIs before changing code. The answer is usually a
Bluetooth-connected gamepad on the client, or a cheap BT dongle on the server.

### Reading the RTT number correctly

**Reported RTT is biased upward by up to one client poll period.** Acks are read once
per input-loop tick, so a returning ack can wait in the socket buffer before we timestamp
it: `measured RTT = true RTT + [0, 1/poll_hz]`. At the default 500 Hz that is up to 2 ms.

This is why a loopback test reports ~2 ms RTT when the network actually costs ~0.1 ms. The
bias is in the measurement, not the input path — input is sent the moment a change is
detected, with no wait.

Trust these instead when measuring our own overhead, since each is a difference between two
timestamps on one clock with no polling in between:

- `bt_write` — server-side report generation + sink write
- `process_ms` — full server-side packet handling (datapath `stats_snapshot`)

Measured on loopback, those come in around **0.03–0.09 ms**, comfortably inside the
sub-millisecond software budget.

## Architecture

```
Client PC
 gamepads ──SDL2──> input thread ──> UDP (ChaCha20-Poly1305)
                                          │
              ┌───────────────────────────┴───────────────────────────┐
      MODE A: DIRECT                                        MODE B: HOLE-PUNCH
   LAN / VPN / port-forward                          Rendezvous broker (public VPS)
   mDNS + broadcast discovery                        endpoint swap → punch
   or manual host:port                               → relay fallback
              └───────────────────────────┬───────────────────────────┘
                                          ▼
Linux server ── datapath thread (SCHED_FIFO, GC off) ── web GUI thread (aiohttp+WS)
                  │
                  ▼
        router: (client, slot) → adapter
                  │
      ┌───────────┼───────────┬───────────┐
    hci0        hci1        hci2        hci3
   L2CAP PSM 17/19 HID server, bound per adapter BD_ADDR
      │            │            │            │
   Console / BT receiver
```

### Non-negotiable design decisions

These were chosen for latency reasons. Do not "simplify" them away without measuring.

- **UDP only on the data path.** TCP's head-of-line blocking and Nagle are exactly wrong
  for a stream of idempotent state snapshots. A stalled retransmit would freeze the
  controller.
- **One UDP socket per client↔server pair**, multiplexing reliable control messages and
  unreliable input. One socket = one NAT mapping to punch and keep alive.
- **Full-state snapshots, not events.** Every packet carries complete controller state, so
  a dropped packet is self-healing — the next supersedes it. No retransmission. Packets
  with `seq` ≤ last-seen are discarded as stale.
- **Send on change + heartbeat.** Transmit the instant state differs; a ~50 Hz heartbeat
  keeps NAT mappings alive and feeds latency stats.
- **GC discipline.** `gc.freeze()` after startup, `gc.disable()` on the datapath. GC pauses
  are the single largest source of tail-latency jitter in Python.
- **Windows timer resolution.** The client calls `winmm.timeBeginPeriod(1)` via ctypes and
  spin-waits the last fraction of each interval. Without this, `time.sleep()` granularity
  on Windows is ~15.6 ms and the entire latency goal collapses.
- **Adapters are identified by BD_ADDR, never by `hciX` index.** Index numbering is
  assignment-order dependent and reshuffles across reboots and replugs. Persisting by index
  would silently move a player's controller to a different console.

### Threading model

- **Datapath thread** — UDP recv → decrypt → route → BT write. Nothing else. `SCHED_FIFO`
  on Linux where permitted.
- **asyncio thread** — web GUI, control plane, adapter management.
- They share state via atomic snapshot swaps (dict rebinding is atomic under CPython's
  GIL); the GUI reads are throttled to 10 Hz so they cannot disturb the datapath.
- If GIL contention ever shows up in p99 numbers, the documented escape hatch is splitting
  these into two processes over a Unix domain socket. Measure first.

## Wire protocol

Input packet, ~32 bytes plaintext (~56 on the wire after AEAD):

```
seq u32 · client_send_ts u64 · slot u8 · flags u8 · axes 4×i16 · triggers 2×u8 · buttons u32
```

All multi-byte fields little-endian (both ends are LE; avoids needless byte swaps).

### Security

- Password → **Argon2id** → session key via HKDF over both sides' randoms.
- Per-packet **ChaCha20-Poly1305**, nonce = direction bit ‖ counter, replay-rejected by a
  sliding window.
- Handshake: `HELLO` → `CHALLENGE(salt, server_random)` → `AUTH(proof, usernames)` →
  `ACCEPT(session_id, capacity)`.
- **Only standard libsodium primitives.** Never invent crypto here. AEAD costs ~1–2 µs per
  packet, which is irrelevant next to the 5–15 ms Bluetooth floor — there is no performance
  argument for weakening it.

## Dynamic adapter capacity

Nothing hardcodes 4. The server enumerates real adapters and capacity flows from that:

- **Fewer than 4 dongles**: capacity is what exists. The server advertises it in the
  handshake and pushes live updates; the client GUI greys out and disables slots above it.
- **More than 4**: the web GUI lists all detected adapters and the operator selects which to
  enable, up to the ceiling of 4. Unselected adapters are left completely untouched — no
  SDP registration, no L2CAP bind — so an adapter the Pi uses for something else is not
  hijacked.
- **Hot-plug**: a `pyudev` watcher with a periodic reconcile handles dongles appearing and
  disappearing. Losing an adapter marks its controller `unassigned` and notifies both GUIs;
  it must never take down the datapath.

## Bluetooth HID targets

| Target | Status |
|---|---|
| Generic BT HID gamepad | Supported. Works with 8BitDo/Mayflash-class receivers, PCs, Android, Steam Deck. |
| Nintendo Switch Pro Controller | Supported. Requires the "Change Grip/Order" pairing flow. |
| PS4 / PS5 / Xbox | **Out of scope** — proprietary authentication crypto that cannot be emulated. The profile layer is pluggable if this ever changes. |

Do not accept requests to "just add Xbox support" without flagging that the controller
authentication handshake is the blocker, not the HID layer.

### Hardware-verified status

Validated on a **Raspberry Pi 4B (8 GB), Debian 13 trixie, Python 3.13.5, BlueZ 5.82**,
paired to Windows 11 over the Pi's built-in adapter. Confirmed working end to end: input
sent from a PC reaches that same PC as OS-level gamepad input, through the Pi's Bluetooth.

**Measured end-to-end latency (real hardware, 60 samples, 0 timeouts):**

| | |
|---|---|
| min | 4.14 ms |
| **median** | **5.79 ms** |
| p90 / p99 | 7.74 / 12.45 ms |
| stdev | 1.54 ms |

That is client-send → WiFi → Pi → Bluetooth → Windows HID, excluding the gamepad's own
USB hop. Better than the 8–25 ms LAN estimate above, because the Pi was on 5 GHz WiFi (no
contention with 2.4 GHz Bluetooth) and Windows negotiated a fast connection interval.
Software-added latency on ARM measured **0.566 ms**, inside the 1 ms budget.

The first report after an idle gap costs ~70 ms — the Bluetooth link parks when idle. This
is normal and not worth optimizing.

### The report ID: the bug that will bite you again

**Every HID input report must begin with its report ID byte.** Both profiles declare a
report ID in their descriptor (`0x01` generic, `0x30` Switch), so `build_input_report`
writes it as byte 0 and the L2CAP layer prepends only the `0xA1` transaction header.

Omitting it produces the nastiest possible failure mode: reports are delivered over L2CAP
without error, every server-side counter looks perfect (`reports_sent` climbing,
`dropped=0`, `unroutable=0`), and the host **silently discards all of them** because the
first byte parses as an unknown report ID. There is no error anywhere to follow.

If input stops reaching a console while the server insists it is sending, check byte 0
first. `tests/test_profiles.py` has a regression test per profile.

### Known gaps

- **No reconnect.** `HIDServer` only *accepts* incoming L2CAP connections; it never
  initiates. After a server restart the host must reconnect manually, and the Pi cannot
  re-establish the link itself — BlueZ refuses an outgoing connect with
  `br-connection-profile-unavailable` because we disabled its input plugin. A real
  controller reconnects by initiating outgoing L2CAP to PSM 17/19; implementing that is
  the fix.
- **`auto_approve` is runtime-only** and resets on restart. Deliberate for a security
  setting, but surprising if you restart mid-session.
- **Multi-adapter routing is unverified.** The Pi 4 has one adapter, so capacity was
  always 1. The dynamic-capacity path was exercised (client correctly greys out slots
  1–3); the 2–4 adapter routing path was not.
- **Switch Pro profile is unverified against a real Switch.** Report generation is tested,
  the pairing handshake is not.

### BlueZ requirements

- `bluetoothd` **must** run with the input plugin disabled (`--noplugin=input`), or BlueZ
  grabs the HID role and our L2CAP binds fail. Shipped as a systemd drop-in. Verified on
  hardware: binding PSM 17/19 returns `EADDRINUSE` with the plugin active and succeeds
  once it is disabled.
- **rfkill soft-blocks Bluetooth by default on Raspberry Pi OS.** `hciconfig up` then fails
  with a bare "Operation not possible due to RF-kill (132)". `adapter.py` detects this via
  `/sys/class/rfkill/` and prints the `sudo rfkill unblock bluetooth` fix — reading sysfs
  rather than shelling out, because the `rfkill` binary lives in `/usr/sbin` and is
  routinely absent from a non-root PATH.
- **Pairing needs `Adapter1.Pairable`, not just scan mode.** `hciconfig piscan` makes the
  adapter visible, but bluetoothd still *rejects* pairing unless `Pairable` is set, and the
  host reports only "Couldn't connect". `_set_discoverable` sets both, plus the
  discoverable timeout (which otherwise defaults to 180 s regardless of what was asked).
- **Stale bonds break re-pairing.** If the host forgets us — device removed in Windows,
  console reset — it generates a fresh link key while we keep the old one, and
  authentication fails with no useful diagnostic on either side. Entering pairing mode
  therefore clears existing bonds by default (`forget_bonds=True`), which is also what a
  real controller does.
- On Debian 13 the daemon is at `/usr/libexec/bluetooth/bluetoothd`. Other distributions
  use `/usr/lib/bluetooth/bluetoothd`; the drop-in must match or the service fails to start.
- SDP records are registered via D-Bus `org.bluez.ProfileManager1.RegisterProfile`.
  `sdptool add` is deprecated and does not work on modern BlueZ.
- Class of device is set to `0x002508` (gamepad/joystick peripheral).
- L2CAP sockets bind to a **specific adapter's BD_ADDR** on PSM 17 (control) and 19
  (interrupt) — that bind is how a particular dongle gets selected.
- Requires root (or `CAP_NET_RAW` + `CAP_NET_BIND_SERVICE`) for the L2CAP binds.

## Layout

```
common/       protocol.py  crypto.py  state.py  timing.py    (shared by both sides)
client/       main.py  input/  net/  gui/  config.py
server/       main.py  datapath.py  sessions.py  router.py  bt/  web/  config.py
rendezvous/   broker.py                                      (public VPS service)
tools/        latency_harness.py  mock_bt_sink.py  virtual_gamepad.py
tests/
```

`common/` is imported by both sides and must stay dependency-light and platform-neutral —
no SDL2, no BlueZ, no Qt.

## Commands

```bash
# Setup (dev machine)
pip install -e ".[client,dev]"          # Windows/Linux client work
pip install -e ".[server,dev]"          # Linux server work

# Tests -- 253, none need hardware
pytest tests/ -v

# Full pipeline on one machine, no Bluetooth hardware needed
python -m server.main --mock-bt --password test123 --auto-approve -v
python -m client.main --headless --direct 127.0.0.1 --password test123 \
                      --backend synthetic --controllers 0,1

# Latency breakdown
python -m tools.latency_harness

# Build the standalone client executable
pyinstaller packaging/client.spec           # → dist/rbgc-client/  (~166 MB)
```

### Packaging gotchas

Both of these produce a bundle that builds fine and then fails at runtime, so they are
easy to reintroduce:

- **PyNaCl needs `cffi` and `_cffi_backend` as hidden imports**, plus
  `collect_dynamic_libs("nacl")` for libsodium. Without them the exe dies on import with
  `No module named '_cffi_backend'`.
- **SDL2 ships inside `pysdl2-dll`** and is loaded through ctypes, so it needs
  `collect_dynamic_libs("sdl2dll")`.
- The build is **windowed** (`console=False`) so double-clicking does not flash a console.
  That leaves the process with no stdio, so `client/main.py:_attach_console_if_needed()`
  restores it for `--headless`, `--list-controllers` and `--help`. It runs *before*
  `parse_args`, because argparse writes usage errors to stderr.

## Measured performance

On the reference machine, via `tools/latency_harness`:

| Metric | Value |
|---|---|
| Software-added latency, both sides | **~0.25 ms** (budget: 1 ms) |
| Server packet handling | p50 0.04 ms, p99 0.12 ms |
| Generic HID report generation | ~7 µs |
| Switch Pro report generation | ~11 µs |
| Client input loop tick | p50 0.24 ms, p99 0.37 ms |

Treat a regression in these as a real bug — they are the only part of the latency budget
we control.

## Testing without hardware

`--mock-bt` swaps the real Bluetooth layer for a sink that logs HID reports with
timestamps. The entire pipeline — input, crypto, transport, routing, report generation —
is exercised on any machine. Use this for all development; only Phase 4 BT work genuinely
needs a Pi with dongles.

`tools/virtual_gamepad.py` synthesizes input at known timestamps so latency measurements
have a ground truth.

## Conventions

- Type hints throughout. `from __future__ import annotations` at the top of every module.
- Datapath code (`server/datapath.py`, `client/input/`, `common/protocol.py`) is
  **allocation-sensitive**: prefer preallocated buffers, `struct.pack_into`, and avoid
  creating objects per packet. Comment any non-obvious micro-optimization with the reason.
- Everything else favors clarity over speed — the GUI and control plane are not hot.
- No `print()` in library code; use the module logger. The datapath logs at most on state
  transitions, never per packet.
