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

## The server starts switched off

`server_enabled` defaults to **false**. A freshly installed server binds its port
but drops every datagram before parsing it, stays silent to discovery probes, and
does not register with the broker. The operator turns it on in the web GUI, or
passes `--accept-clients` for a scripted run.

**Off never touches Bluetooth.** Adapters stay registered, HID listeners stay
bound, and paired consoles stay connected. Turning players off mid-session must
not make a console's controllers disappear, so `set_accepting()` closes client
sessions and clears assignments but leaves the entire BT stack alone. There is a
test for exactly this (`test_turning_off_leaves_bluetooth_alone`).

The gate sits at the very top of `_handle_datagram`, ahead of punch replies and
all crypto, so a switched-off server does no work on behalf of a stranger.

### Visibility is two independent switches

- **`discoverable`** — answer LAN discovery probes, and send a name to the broker.
  Hidden servers still *register* with the broker (so anyone holding the room code
  reaches them) but are never enumerated. Hidden mode is implemented by simply not
  replying and not sending a name; there is no separate code path to get wrong.
- **`internet_enabled`** — opt in to the rendezvous broker at all. Off by default:
  registering with a third-party host is the operator's decision, not a default.

Changing the client password drops every session, because the session key is
derived from it — an existing session is by construction using the old one.
`SessionManager.set_password` also rolls the salt and clears pending handshakes.

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
paired to Windows 11 — first over the built-in adapter alone, later with **four adapters**
(built-in + 3 ASUS USB-BT500 dongles) serving four independent controllers. Confirmed working end to end: input
sent from a PC reaches that same PC as OS-level gamepad input, through the Pi's Bluetooth.

**Four simultaneous HID links confirmed**: all four adapters paired to the same Windows 11
host at once, each on its own radio, via Secure Simple Pairing — 8 `IO Capability Request`,
4 `Simple Pairing Complete`, **0 `PIN Code Request`**. All four also re-established
themselves automatically after a server restart, via the outgoing reconnect path.

With all four streaming concurrently to live L2CAP links (543 packets per slot, 0 write
failures), per-slot cost stayed flat — there is no measurable interference between radios:

| slot | RTT p50 | RTT p99 | `bt_write` p50 |
|---|---|---|---|
| 1 | 2.1 ms | 2.2 ms | 0.32 ms |
| 2 | 2.1 ms | 2.2 ms | 0.28 ms |
| 3 | 2.0 ms | 2.1 ms | 0.29 ms |
| 4 | 2.0 ms | 2.2 ms | 0.27 ms |

`bt_write` here is higher than the ~0.05 ms measured against `--mock-bt` because it is a
real radio submission, not a mock sink. RTT carries the usual poll-period bias (see
"Reading the RTT number correctly"). Measured with the client on the Pi itself, so this
isolates server-side cost — it does **not** include the WiFi hop.

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

### When a host demands a PIN, measure the host before touching the Pi

A host that prompts "Enter the PIN for …" instead of pairing silently looks exactly like a
server misconfiguration, and it is very easy to spend a long time fixing the Pi. **The
cause is often on the host.** This cost eight rounds of wrong fixes once already.

Both sides must advertise Secure Simple Pairing or the spec *requires* legacy PIN pairing.
The host's half of that is one bit, and it is directly measurable — **extended features
page 1, bit 0 (SSP Host Support)**, visible in `btmon` on every incoming connection:

```
> HCI Event: Read Remote Extended Features   Page: 1/2
        Features: 0x00 0x00 ...      <- host has SSP OFF: legacy PIN is mandatory, not our doing
        Features: 0x0f 0x00 ...      <- "Secure Simple Pairing (Host Support)": SSP will be used
```

A real case: a Windows 11 machine reported `0x00` across two independent captures and
demanded a PIN; **rebooting it** flipped the field to `0x0f` and all four adapters then
paired via SSP with zero PIN requests. Nothing on the Pi changed.

Diagnose in this order — the first two take two minutes and settle which machine is at
fault, which is the whole question:

1. **Isolate.** Stop the server and make a bare adapter discoverable (`Alias`, `Pairable`,
   `Discoverable` via `busctl` on `org.bluez.Adapter1`). No HID UUID, no agent, no L2CAP
   listeners. If the host *still* asks for a PIN, the fault cannot be in this codebase.
2. **Control.** Read page 1 from a device known to be SSP-capable (another adapter on the
   same Pi) to prove the read itself works, before concluding anything about the host:
   `hcitool -i hciX cc <addr>` then `hcitool -i hciX cmd 0x01 0x1c <handle_lo> <handle_hi> 0x01`.
3. Only then look at our side.

Read controller state over HCI rather than trusting MGMT's cached view, because raw
`hciconfig` writes desynchronise the two: `hcitool -i hciX cmd 0x03 0x55` reads
Simple Pairing Mode, `0x03 0x1f` reads Authentication Enable, `0x03 0x23` reads the class.

Things that look causal here and are **not** — all verified innocent on hardware:
link security, class of device, the SDP record's attribute ordering, the agent's IO
capability, and the outgoing reconnect loop.

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

### Reconnection

`HIDServer` runs two threads: one accepting incoming connections, one **initiating
outgoing** ones. The outgoing path is what makes the link recover by itself after either
end restarts — verified on hardware, reconnecting in **under 2 seconds**.

It has to be done ourselves. `bluetoothctl connect` fails with
`br-connection-profile-unavailable`, because asking BlueZ to connect a *profile* is
meaningless once `--noplugin=input` has removed its HID profile. Opening raw L2CAP
channels to the host's PSM 17 then 19 sidesteps BlueZ entirely, and is exactly what real
controller firmware does — it is what the `HIDReconnectInitiate` SDP attribute advertises.

- The host address is learned from any incoming connection and persisted as
  `AdapterConfig.paired_target`, so it survives a restart.
- Backoff is `_RECONNECT_DELAYS` (2 s → 30 s cap). A dropped session sets `_retry_now` to
  cut the wait short; failures log at debug so an overnight-off host does not fill the log.
- `_session_lock` keeps an incoming connection and a reconnect attempt from both attaching
  — they genuinely race after a restart.
- Both directions converge on `_serve_session`, so there is one code path for a live link.

### Known gaps

- **`auto_approve` is runtime-only** and resets on restart. Deliberate for a security
  setting, but surprising if you restart mid-session.
- **Switch Pro profile is unverified against a real Switch.** Report generation is tested,
  the pairing handshake is not.
- **`RegisterProfile` passes `Channel`, which is the RFCOMM channel.** HID is L2CAP, so
  this should be `PSM` or (as in every working reference implementation) omitted entirely.
  Harmless in practice — hosts connect to the fixed PSMs regardless — but wrong.
- **The SSP check in `_ensure_pairing_settings` cannot fail.** It greps `btmgmt info` for
  `link-security`, which always appears in the `supported settings:` line; it never reads
  `current settings:` and never checks `ssp` at all. Every `btmgmt` write failure is
  swallowed at `log.debug`, so this code has never verified anything.
- **Three layers write the same adapter state** — `hciconfig` (raw HCI, below MGMT),
  `btmgmt` (a second MGMT client), and `bluetoothd` (the owner). This is why
  `hciconfig class` silently reverts: bluetoothd recomputes the class via MGMT and
  overwrites it. Class of device belongs in `main.conf`; everything else belongs on
  `org.bluez.Adapter1`.

### The SDP record is system-wide, not per-adapter

**BlueZ's `ProfileManager1` owns a single SDP database for the whole machine.** A UUID can
be registered once; bluetoothd then serves that record on every adapter. Registering
per-adapter — which this code originally did — fails the second and subsequent attempts
with `UUID already registered`, leaving all but one adapter inert. Which one survived
depended on dict ordering, so the failure moved between restarts.

`AdapterManager._ensure_sdp()` therefore registers once and reference-counts: the record is
released only when the last adapter stops. What *is* genuinely per-adapter is the pair of
L2CAP listeners bound to each BD_ADDR — that bind is what makes four dongles four
independent controllers.

**Consequence:** every adapter advertises the same HID descriptor. Four adapters running
the same profile is the normal case and works. **Mixed profiles are not supported** — you
cannot have one adapter emulating a generic pad and another a Switch Pro Controller
simultaneously, because there is only one record to advertise. The mismatch is logged
rather than silently serving the wrong descriptor.

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

## The two GUI traps

Both of these produced symptoms that looked like unrelated feature bugs, and both
are easy to reintroduce.

### Never rebuild a DOM node the operator is using

The server web GUI receives status at **10 Hz**. An earlier version rebuilt both
card containers with `innerHTML` on every message, which broke two things at once:

- An open `<select>` was destroyed and recreated 100 ms after opening, so the
  Emulate dropdown **closed the instant it was opened**.
- A click needs mousedown *and* mouseup on the same node. The node was routinely
  replaced between them, so **"Connection mode" silently did nothing**.

`server/web/static/app.js` now renders incrementally: cards are keyed by
`bd_addr` / `client_id`, built once, and only changed values are written.
Handlers are attached **once per container by delegation** so they survive the
rebuilds that do still happen when the adapter or client *set* changes, and no
control is written to while it is focused or while a pointer is down anywhere.

A cautionary note: the old file's header comment claimed open `<select>` elements
were preserved. They never were — the code only restored *focus*, which does not
reopen a popup. The comment described an intention, not the behaviour.

### SDL hides gamepads it has no mapping for

`client/input/sdl2_backend.py` used to skip any device where
`SDL_IsGameController()` was false. An **8BitDo 64** enumerates as a perfectly
good 18-button, 6-axis joystick with no entry in SDL's mapping database, so it
was discarded — indistinguishable, from the GUI, from the pad not being detected
at all.

Two independent faults, both fixed:

- Unmapped devices are now **listed and marked "needs mapping"**, and driven
  through `client/input/mapping.py` as raw joysticks.
- `list_devices()` calls `SDL_PumpEvents`/`SDL_JoystickUpdate` first. SDL detects
  hotplug inside `SDL_JoystickUpdate`, not in `SDL_NumJoysticks`, so enumerating
  without it returns whatever was present at startup forever — which is why
  "Refresh gamepad list" appeared to do nothing.

`default_joystick_mapping()` is deliberately a **guess, labelled as one in the
UI**, not a table of specific devices. A wrong table entry is indistinguishable
from a broken controller; an obviously-approximate default invites the player to
check it against the live preview.

### Keyboard input requires window focus

`client/input/keyboard_backend.py` is fed key events by the GUI rather than
reading the keyboard itself, so it only works while the client window is focused.
Gamepads do not have this limitation (SDL sets
`SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS`). Reading the keyboard globally would need
a system-wide hook, which is indistinguishable from a keylogger and would be
flagged as one. Focus is released on `ActivationChange`, or a key held when focus
is lost would latch down forever.

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
