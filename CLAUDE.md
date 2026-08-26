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
- Video travels the other way: a **video server** captures the console's output and
  streams it to each player. See "Video streaming".

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
                  │                                          │
                  │                            VideoRegistry ┴── video server
                  │                          (control plane only; media goes
                  ▼                           straight to the clients)
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

**Two independent transports**, each with its own on/off and its own visibility:

| | accepts | announces via |
|---|---|---|
| `lan_enabled` / `lan_discoverable` | anything reaching us directly | the UDP discovery beacon |
| `internet_enabled` / `internet_discoverable` | peers the broker introduced | the broker's `list` op |

Both default to **false**. A freshly installed server binds its port but drops
every datagram before parsing it, stays silent to discovery probes, and does not
register with the broker. The operator turns a path on in the web GUI, or passes
`--accept-clients` for a scripted run.

The gates are separate all the way down: closing Internet drops only the sessions
the broker introduced (tracked in `RendezvousClient.was_introduced`), leaving LAN
clients streaming. With LAN off and Internet on, a machine on the same subnet must
come in via the broker — surprising at a glance, but it is what "accept Internet
clients only" has to mean.

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
  enable, up to the ceiling of 4. An adapter that was **never enabled** is left completely
  untouched — no SDP registration, no L2CAP bind, nothing written — so an adapter the Pi
  uses for something else is not hijacked.
- **Disabling** one we *had* enabled is a different thing, and used to be wrong. It dropped
  the L2CAP listeners but left the radio advertising our name, the gamepad class and the
  HID UUID, so a host kept finding a controller that could never answer — Windows reports
  that as *"We didn't get any response from the device"*. `_quiet_adapter` now clears
  `Discoverable`/`Pairable` too, scoped to adapters we configured. A persisted `number` is
  durable proof we brought one up in a past run, so an adapter stranded by an earlier
  session is cleaned up after a restart. `_quieted` latches it: the hot-plug watcher
  reconciles every 10 s, and without the latch it re-quieted forever.
- **Hot-plug**: a `pyudev` watcher with a periodic reconcile handles dongles appearing and
  disappearing. Losing an adapter marks its controller `unassigned` and notifies both GUIs;
  it must never take down the datapath.

## Bluetooth HID targets

| Target | Status |
|---|---|
| Generic BT HID gamepad | Supported. Works with 8BitDo/Mayflash-class receivers, PCs, Android, Steam Deck. |
| Nintendo Switch Pro Controller | Supported. Requires the "Change Grip/Order" pairing flow. |
| Analogue 3D | **Out of reach from this stack.** Its controller is BLE-only (HID over GATT, `0x1812`, `BR/EDR Not Supported`) -- measured, see below. Needs a GATT peripheral, not a Classic one. |
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

The first report after an idle gap used to cost ~70 ms, recorded here as "normal and not
worth optimizing". **That was wrong**, and it is worth knowing why the wrong conclusion was
so easy to reach: the cost is real, it is reproducible, and it genuinely is Bluetooth
parking the link — but *we were asking it to*. Two things we controlled were telling the
host to park us, and neither is visible from any counter. See "Link tuning" below.

The remaining floor after tuning is the host's own poll interval, which we do not control.
Measure with `tools/bt_link_probe.py` before concluding anything about an idle gap.

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

### A console with no pairing list filters us before we ever connect

Some consoles do not offer a list of nearby devices. You press their pairing
button and they scan, then connect to whatever they recognise as a controller —
the Analogue 3D works this way. A controller they do not recognise is not
rejected with a message; it is simply never connected to, which is
indistinguishable from being out of range or switched off.

Two filters, at two different moments, and which one is biting decides what to
change:

| when | what the console can see | what we control |
|---|---|---|
| during inquiry, before any connection | class of device, advertised **name**, EIR UUIDs | the name — **per adapter** |
| after connecting, over SDP | the **DeviceID** record: vendor and product ids | server-wide |

**We published no DeviceID (PnP Information, UUID `0x1200`) record at all.** The
`vendor_id`/`product_id` on each profile were passed into `build_hid_record` and
then never used — dead parameters. Most hosts do not care, which is why it went
unnoticed: a PC will drive anything with a sane HID descriptor. A console that
only pairs with controllers it recognises does care.

`server/bt/identities.py` holds the presets (generic, 8BitDo, Xbox, DualSense,
DualShock 4, Switch Pro, Razer, GameSir). **Identity is not profile**: the
profile owns the HID report descriptor, the identity owns who we claim to be.
Switching identity to satisfy a picky console must never change the report
layout underneath it, and a test asserts an identity carries no descriptor.

**Identity is server-wide, and not by choice.** `ProfileManager1` keeps one SDP
database per machine, so there is exactly one DeviceID record however many
dongles are plugged in — the same constraint that stops adapters running
different profiles. Only the *name* is per adapter, since each appends its own
number.

DeviceID registration is deliberately **non-fatal**: a host that never reads it
is perfectly happy without one, so failing Bluetooth bring-up because the extra
record was refused would trade a working controller for a missing nicety. It
warns instead.

The trade to state plainly when someone asks for impersonation: a host that
applies vendor-specific quirks may then expect behaviour our HID layer does not
implement. That is why `generic` remains the default and the others are opt-in.

### Link tuning: everything BlueZ gives you no interface for

For a long time this project had **no link-layer control at all** — no link policy, no
flush timeout, no supervision timeout, no L2CAP socket options. `SOL_L2CAP` and
`L2CAP_OPTIONS` were declared in `hid.py` and never used. Every over-the-air
characteristic was whatever BlueZ's defaults happened to be, and two of those defaults are
actively wrong for a gamepad.

We are the **peripheral**. The central schedules the ACL slots, so the poll rate is not
ours and no amount of application tuning changes it. What is ours is everything deciding
how far the link degrades *away* from that rate. None of it has a D-Bus property, a
`bluetoothctl` verb, or an MGMT opcode, so `server/bt/hci.py` opens an `HCI_CHANNEL_RAW`
socket — the same channel `hcitool cmd` uses, which coexists with `bluetoothd` rather than
seizing the adapter the way `HCI_CHANNEL_USER` would.

| Lever | Default | What the default costs |
|---|---|---|
| Automatic flush timeout | **infinite** | A packet caught in an interference burst is retransmitted by the baseband until it succeeds, blocking the channel head-of-line. Every fresh report queues behind a stale one nobody wants. This is the tail latency. |
| Link policy (sniff bit) | **permitted** | Either end may park the link when it looks idle. Waking it costs a full sniff-exit negotiation. |
| `HIDSSRHostMaxLatency` | **0x0640 = 1 s** | We were telling the host, in the SDP record it reads *before connecting*, that a full second between polls was fine. |
| Link supervision timeout | **20 s** | A console that was switched off holds the channel for twenty seconds before reconnect starts. |

The SSR value is the one worth dwelling on: `0x0640` is the HID specification's own
*example* value, which is why it appears in essentially every BlueZ HID implementation. It
is copied, not chosen. `tests/test_sdp_record.py` now asserts it is not that value.

**A flush timeout does nothing unless the packets are flushable.** Linux sends L2CAP data
non-flushable unless the socket asks otherwise, so `BT_FLUSHABLE` on the interrupt socket
and the flush timeout on the connection are a **pair**. Either alone is inert — and the
half that fails silently is the socket option, so the symptom is a tail that does not move
while every read-back looks correct. Both are applied together in `_prepare_interrupt`.

**A tight flush timeout is only safe because the state is re-sent.** Flushing discards a
report, and with send-on-change alone the console would hold that stale state until the
player next changed something — a stuck button, far worse than the jitter being fixed. The
sink re-sends the current state at `keepalive_hz` (50 Hz), which bounds a lost report to
one interval and denies the peer an idle period to park in. Real controller firmware
streams continuously for the same two reasons. **The flush timeout and the keepalive are
one design, not two features**; raising one or removing the other reintroduces the bug.

Sniff is *exited* before the policy is written. Clearing the sniff bit stops the link
entering sniff again but does not pull it out of a sniff it is already in, and a link
tuned while parked would stay parked — precisely the case that hurts.

Tuning is **never fatal**. A controller that refuses these commands still carries a
perfectly good HID link; it just runs on the defaults. Failing bring-up over a latency
optimisation would trade a working controller for a better p99, which is the wrong way
round. The failure is logged rather than swallowed, because "untuned" and "fine" are
otherwise indistinguishable until somebody notices the jitter days later.

### Measured on hardware: the Phase 0 baseline and what tuning changed

Reference Pi (`controller-server`, Pi 5, kernel 6.18, BlueZ 5.82, Python 3.13.5),
four adapters: hci0 built-in Cypress, hci2/hci3/hci4 Realtek USB-BT500.

Read with `tools/bt_link_probe.py` **over HCI**, from the controller, before any
of this work was deployed:

| | before | after |
|---|---|---|
| default link policy (all four) | `0x000F` role-switch, hold, **sniff**, park | `0x0001` role-switch only |
| per-connection link policy | `0x000F` | `0x0001` |
| automatic flush timeout | **0 — infinite** | 48 slots = **30 ms** |
| link supervision timeout | 32000 slots = 20 s | unchanged, see below |
| adapters answering page scan | **2 of 4** | **4 of 4** |

The last row was not something this work set out to fix. Two adapters — hci0 and
hci4 — were sitting with **scan enable `0x00`**: not connectable, not
discoverable, unreachable by any host. That is the trap described under
"`Connectable` is page scan" happening live, on half the fleet, with the server
reporting capacity 4 and every management layer looking healthy.

### Two HCI details that cost a round each, and would have shipped broken

**The event filter must be 16 bytes, not 14.** `struct hci_ufilter` is
`__u32 type_mask; __u32 event_mask[2]; __le16 opcode` — 14 bytes of fields, 16
with the trailing padding — and the kernel rejects anything shorter than the
full structure. Packing the significant bytes is the obvious thing to do and it
fails on every adapter with `EINVAL`, which is indistinguishable from a bad
device index or a permissions problem. Measured: len 14 rejected, 16 and 18
accepted. **And the filter is not optional** — without one, no events are
delivered at all, so the socket opens, the command sends, and nothing ever
comes back.

**`socket.bind` cannot reach any channel but `HCI_CHANNEL_RAW`.** CPython 3.13
accepts only a one-element `(device_id,)` tuple for `BTPROTO_HCI`; the
`(device_id, channel)` form the older docs describe is rejected outright with
"bind(): wrong format". Raw HCI happens to want the default channel so it is
fine, but **MGMT lives on `HCI_CHANNEL_CONTROL` and is therefore unreachable
through the socket module** — `server/bt/mgmt.py` binds via `ctypes` and
`libc.bind` with a hand-packed `sockaddr_hci`.

### Two tuning commands are refused on every healthy link, correctly

Measured against a live ACL link, so these are not guesses:

- **`Exit Sniff Mode` returns `0x0C` "command disallowed" when the link is
  already active.** We send it unconditionally because the link may be parked
  when we arrive and there is no command to ask which mode it is in.
- **`Write Link Supervision Timeout` returns `0x0C` in the peripheral role**,
  which is the role we are in whenever a console connects *to us*. The central
  owns that timeout by specification. So an incoming link keeps whatever the
  console chose — 20 s by default — and only links we initiate get 5 s. **That
  asymmetry is a real constraint on how fast reconnect can start** after a
  console vanishes, and it is not something we can tune away.

Counting either as a failure made every healthy link log a warning naming two
commands that could never have succeeded, which is worse than silence: the same
warning is used for real faults. `LinkTuner` reports them as `skipped` with the
reason, separately from `failed`.

### The class of device: compare major and minor, not the whole word

`0x000508` and `0x002508` are **the same controller**. They differ only in the
Limited Discoverable service bit, which bluetoothd toggles by itself. Measured
on the reference Pi: four adapters, two reading each value at the same instant,
all four peripheral (major 5) / gamepad (minor 2), all four working.

Anything checking the class must compare `(major, minor)` against `(5, 2)`.
Comparing the full word sends someone chasing a class-of-device problem that is
not there — which this rewrite did to itself once, in the probe tool, after
`state.py` already had a test asserting the correct behaviour.

HCI and MGMT were also checked against each other on all four adapters at the
same instant and **agreed exactly**, so the desync warned about elsewhere in
this document is not currently happening — worth knowing before blaming it.

### A controller reset silently discards the link policy

`hciconfig hciX reset`, a USB re-enumeration, or a firmware reload puts the
**default link policy** back to whatever the dongle ships with — `0x000F` on the
Realtek USB-BT500, meaning hold, sniff and park all permitted — and nothing
anywhere reports it.

Found by accident: a reset run to clear an unrelated stale connection left that
adapter sniff-capable while its three siblings stayed at `0x0001`, and only a
read showed it. Everything kept working, which is the problem — the symptom
would have been one player out of four with worse tail latency and no
explanation.

`LinkTuner.ensure_adapter_defaults()` is called from the reconcile pass and is
read-then-write, so the ordinary case costs one command and writes nothing.
Verified live: reset the adapter, and within one reconcile it is back to
`0x0001` with a warning naming what was wrong.

The per-connection `tune()` still fixes each link as it comes up, so the damage
is bounded either way. The adapter default exists to close the window *before*
that — a host that requests sniff immediately on connect would otherwise win the
race.

### The write path is coalesced, latest-wins

The two ends run at completely different rates. A client polls at up to 500 Hz and sends
the instant anything changes; the link drains at whatever the console schedules, typically
a fraction of that. The datapath used to issue **one `send()` per received UDP packet**,
which builds a queue of stale reports that each new report waits behind.

That queue is invisible from every counter: writes succeed, `dropped` stays 0, and latency
simply grows with how hard the player is moving the stick. It is the same head-of-line
problem the UDP design was built to avoid, reintroduced at the L2CAP boundary — the one
place the "full-state snapshot, latest wins, drop the stale one" discipline had never been
applied.

`L2CAPSink` now tries the write inline on the datapath exactly as before, because that is
the cheapest thing that can happen and it is what happens whenever the link keeps up. On
`EAGAIN` it keeps **only the newest state** and this adapter's writer thread transmits it
when the link drains. One report in flight, never a stale one, and no scheduling hop in the
common case. Measured cost of the extra bookkeeping and its lock: **+357 ns per report**,
against a real L2CAP `send()` of tens of microseconds.

**A superseded report is not a dropped one.** It is counted as `writes_coalesced`, which
is a sign of a saturated but healthy link — the newest state still went out on time.
Counting it as a drop, which is what the old contract did, made a link that was working
perfectly look broken.

`SO_SNDBUF` on the interrupt socket is deliberately tiny. The kernel doubles the request
and enforces its own floor, so **the effective value must be measured on the target**, but
asking small is what makes `EAGAIN` arrive while the backlog is one or two reports deep.
With the default buffer the socket swallows tens of milliseconds of reports before it ever
pushes back, and by then coalescing has nothing left to save.

### Adapter state is event-driven now, not polled

`server/bt/mgmt.py` opens the Bluetooth management socket and subscribes to the
adapter state changes the kernel broadcasts: index added and removed, settings
changed, device connected and disconnected, new link key. `AdapterManager` wakes
on those instead of discovering everything on a ten-second timer.

This replaces **every `btmgmt` subprocess call**, and with it the entire class of
bug documented under "`btmgmt` hangs on `/dev/null`" — a socket has no stdin, no
argv, and no five-second timeout to burn. Adapter enumeration goes through the
same socket, so `hciconfig -a` is no longer spawned and scraped on every
reconcile; it survives only as a fallback and as the source of the human-readable
manufacturer string, which MGMT does not provide.

The 10 s reconcile is still there, demoted to a **safety net**. A missed event
would otherwise leave the server wrong until the next operator action.

**Read and observe only.** bluetoothd owns adapter state and we are a second
MGMT client — which the kernel permits, and is how `btmgmt` coexists with the
daemon — but two clients writing one setting is exactly the desynchronisation
this project has been bitten by. `MGMTSocket.command` enforces a **read-only
opcode allowlist** and refuses anything else with a message pointing at
`org.bluez.Adapter1`. It is an allowlist rather than a denylist so that adding a
write has to be a decision, not merely something nobody forbade yet.

Events arrive on a reader thread, so `_on_mgmt_event` does almost nothing: it
flips an `asyncio.Event` through `call_soon_threadsafe` and returns. The
reconcile it triggers touches D-Bus, the router and the HID servers, none of
which belong on a socket reader thread. Events are **debounced 250 ms** — one
operator action produces a burst (setting an adapter discoverable emits several
New Settings events) and reconciling on each would run the whole pass repeatedly
for one change.

Startup on the reference Pi with four adapters now completes in **about one
second**.

### `AdapterState` outlives the rescan that used to destroy it

`server/bt/state.py` holds one object per BD_ADDR, created once and mutated in
place. `AdapterRegistry.sync()` updates fields; it never constructs.

The bug this closes: `rescan()` **replaced every `AdapterInfo` with a fresh
object** every ten seconds, so anything transient held on the old one was
silently lost. That is why the pairing countdown read zero a few seconds after
the operator armed it, and why a degraded adapter looked healthy again between
rescans. The fix at the time was to hand-copy two fields across the rebuild,
which works exactly until someone adds a third — so the property the tests pin
is not "the fields are right" but "**the object survives**".

`Phase` replaces the scattered booleans (`_configured`, `_quieted`, `hid_error`,
`pairing_until_ns`): `DETECTED → CONFIGURING → LISTENING ⇄ PAIRING ⇄ LINKED`,
plus `DEGRADED` (enabled, HID could not start — visible in the GUI but inert and
never advertising) and `QUIET` (operator disabled, radio silenced). An
unexpected transition is **logged and then taken**: hardware does surprising
things and refusing would turn a bookkeeping problem into a dead adapter, but an
unexpected transition is nearly always two code paths fighting over one radio.

`AdapterState.health()` turns adapter state into the sentence describing the
actual fault, because every one of these presents to the operator identically,
as a console that will not pair: page scan off, link security forcing legacy
PIN, SSP disabled, the wrong device class, a failed HID bind.

### Bonds come from BlueZ, not from our config

`AdapterManager._reconnect_target_for()` asks `org.bluez` which hosts are bonded to an
adapter. The persisted `paired_target` is demoted to a *preference*: it chooses between
bonds when an adapter has several, and is ignored when it names a host BlueZ has no key
for.

This makes the stale-target bug impossible to construct rather than merely fixed. There is
one record of who we are bonded to and it is the one the pairing created, so entering
pairing mode, or a host forgetting us, cannot leave an address behind that the reconnect
loop then pages every 30 s forever at debug level.

The bonds are also carried on `AdapterState.bonds` and shown in the web GUI, because "who
is this adapter paired with" is a question the operator actually asks and previously had
no way to answer.

### One D-Bus connection, not one per property write

Every call in `adapter_dbus` used to open a system-bus connection, introspect, do its work
and disconnect. With four adapters and a reconcile every ten seconds that is roughly
twenty-four connection setups a minute, forever, each a round trip that can fail
transiently under load.

The connection is now shared and **keyed by event loop** — a `MessageBus` is bound to the
loop that created it, and the test suite runs a fresh loop per test, so reusing one across
loops fails in a way that reads as a D-Bus fault rather than a lifetime bug. A connection
that has dropped (bluetoothd restarting) is replaced rather than handed back dead, since a
dead bus surfaces as writes that report success and change nothing. Introspection results
are cached per path: they describe an interface's shape, not its state.

### Two silent bugs in the accept path

A HID link is two L2CAP connections and is only up once both exist. The old loop accepted
control and then **blocked** on the interrupt listener:

- A host that opened control and vanished **wedged that adapter for the life of the
  process**. Nothing else could connect on it, and nothing was logged.
- The interrupt peer address was discarded, so a *second* host opening interrupt while the
  first was mid-connect got spliced onto the first host's control channel.

The loop never blocks on an accept now: sockets are filed under the peer that opened them
and a session starts only when one peer supplies both, with unmatched halves closed on a
5 s deadline. `tests/test_hid_accept.py` covers both.

### The interrupt socket must not be closed underneath a write

`detach()` closes the interrupt socket while the datapath may be inside `send()` on that
same descriptor — and if the descriptor number is reused in between, a HID report goes
into whatever unrelated socket now owns it. `_io_lock` guards both.

It is affordable because it wraps **a syscall we were already making**, not a queue: an
uncontended acquire is tens of nanoseconds against a send of tens of microseconds. The
teardown that follows a fatal write error therefore happens *outside* the lock —
`threading.Lock` is not reentrant, so tearing down in place would deadlock the datapath
thread on the first console disconnect, taking every other controller with it.
`tests/test_hid_write_path.py` has a test that fails by hanging if that is undone.

### The Analogue 3D is BLE, and this server is Bluetooth Classic

**Measured, and it settles the question this document previously left open.** The
Analogue 3D's official controller — an 8BitDo 64 Bluetooth Controller — was put into
pairing mode and captured from the Pi:

```
Address: E4:17:D8:E7:EE:F2 (8BITDO TECHNOLOGY HK LIMITED)
Flags: 0x05
  LE Limited Discoverable Mode
  BR/EDR Not Supported                      <-- LE only. No Classic radio at all.
Appearance: Gamepad (0x03c4)
16-bit Service UUIDs (complete): 1 entry
  Human Interface Device (0x1812)           <-- HID over GATT, not the Classic 0x1124
Name (complete): 8BitDo 64 BT
```

`BR/EDR Not Supported` appeared 57 times across the capture, and the pad produced
**zero** BR/EDR inquiry responses. It is a **BLE HOGP** device.

Everything in `server/bt/` is Bluetooth **Classic**: L2CAP on PSM 17/19, an SDP record
carrying HID UUID `0x1124`, the Classic HID profile. A BLE-only host will never page a
Classic device however perfect its advertisement — different radio mode, different
discovery, different transport, different service.

That is why the pairing attempts failed, and the failure was invisible for exactly the
reason this section always warned about: the console does not reject you, it simply never
connects. Before the capture, everything measurable on our side looked correct:

| what a scanning console can see | ours | verdict |
|---|---|---|
| class of device | `0x002508`, major 5 / minor 2 | correct |
| EIR service UUID | `0x1124` HID (**Classic**) | correct — for the wrong protocol |
| advertised name | `8BitDo 64 gamepad` | plausible |
| page + inquiry scan | both on | correct |

Twenty minutes discoverable, console confirmed in pairing mode, **not one connection
request**. Two things had looked like candidate causes and were both red herrings: the
adapter number appended to the name (`8BitDo 64 gamepad 4`, where the real pad is
`8BitDo 64 BT`), and the dongle's ASUSTek OUI where a real pad has 8BitDo's. Neither
matters when the radio protocol is wrong.

**Supporting the Analogue 3D means a second, parallel stack**, not a tweak to this one:

- A GATT server publishing **HID Service `0x1812`** — Report Map, Report characteristics
  with CCCD notifications, HID Control Point, Protocol Mode — via
  `org.bluez.GattManager1`, plus Device Information and Battery services, which HOGP
  hosts expect.
- LE advertising via `org.bluez.LEAdvertisingManager1` carrying Appearance `0x03c4`,
  the `0x1812` UUID, and `BR/EDR Not Supported`.
- LE pairing and bonding, which is its own flow — not the Classic SSP path in `agent.py`.
- A different latency model entirely. There is no sniff mode and no flush timeout to set;
  latency is governed by the **connection interval** (7.5 ms minimum) and **peripheral
  latency**, which the *central* grants in response to a Connection Parameter Update
  Request. So `server/bt/link.py` does not carry over — the levers are different ones.

The profile layer already separates "what we pretend to be" from "how we talk", so the
report-generation code is reusable. The transport is not.

**Do not spend more time tuning the Classic path for this console.** It is not a
discovery, naming, class-of-device or DeviceID problem. Anything reached over Classic HID
— PC, Switch, 8BitDo and Mayflash receivers — is unaffected by this and continues to work.

### The BLE transport, and two platform facts that shape it

`server/bt/ble/` publishes an adapter as a **HID-over-GATT** gamepad. It is a
second transport beside the Classic stack, not a layer on it: the two share the
profile layer -- the report descriptor and the bytes of every report are
identical -- and nothing else. `controller_transport` selects one (`"classic"`
or `"ble"`); `BLESink` implements `HIDSink`, so the router and the datapath are
untouched by the choice.

`server/bt/ble/hogp.py` is stdlib-only wire format, testable anywhere. The rest
needs dbus-next, the same split `common/video.py` uses against PyAV.

**BLE is per adapter, and Classic is not.** `GattManager1` and advertising both
live on the adapter object, so four dongles are four independent peripherals
with their own services and names -- verified, four BLE gamepads live at once.
The Classic side cannot do this: one SDP database per machine.

#### bluetoothd cannot advertise on this platform

`org.bluez.LEAdvertisingManager1` takes the **extended** advertising path
(MGMT `Add Extended Advertising Parameters`/`Data`, 0x0054/0x0055) and the
kernel rejects the data with `Invalid Parameters (0x0d)`. Measured on a Pi 5,
kernel 6.18, BlueZ 5.82, on the built-in Broadcom adapter *and* the Realtek
dongles, with a **minimal** advertisement -- so it is not our payload.

The legacy single-step `Add Advertising` (0x003e) accepts the identical bytes.
That is what `btmgmt add-adv` uses, and it is what we use, through our own MGMT
socket. **The GATT half still goes through bluetoothd**, which works fine.

That is the one place `server/bt/mgmt.py` writes, and the read-only rule it
otherwise holds is intact. The rule is "do not write adapter **settings**",
because those are shared state bluetoothd owns and two writers desynchronise
them. An advertising instance is not that: the kernel records **which socket
added it** and removes it when that socket closes, so it is a per-client
resource the kernel arbitrates. The ownership is a feature -- our advertisement
dies with our process, which is the lifecycle we wanted anyway.

Getting there cost three separate silent failures, all reported by BlueZ as the
same "Failed to register advertisement":

- An undeclared `TxPower` property. BlueZ reads it whether or not you want it,
  and dbus-next answers an undeclared property with an error BlueZ treats as
  fatal. Dropping the deprecated `IncludeTxPower` does not stop the probe.
- `TxPower` declared **read-only**. BlueZ writes back the power the controller
  actually selected.
- The extended-advertising rejection above, which no amount of property
  fiddling fixes.

In every case the real cause was visible only as a traceback on our own bus.

#### The kernel rewrites the advertisement but not the scan response

Appearance must go in the **scan response**. Put it in the advertising data and
MGMT accepts it, reports `Advertising data length: 8`, returns Success -- and
then transmits seven bytes with no appearance among them. The kernel manages
that AD type itself and rebuilds the advertisement from its own model. The scan
response is passed through verbatim.

For the same reason the advertising data must **not** contain a Flags
structure: the kernel adds one under `ADV_FLAG_MANAGED_FLAGS`, and a duplicate
fails `tlv_data_is_valid`, taking the whole advertisement with it under -- once
again -- `Invalid Parameters`.

What we put on air, against the real 8BitDo 64 captured in pairing mode:

| | real pad | ours |
|---|---|---|
| ADV | Flags 0x05, Appearance, UUID 0x1812 | Flags 0x02, UUID 0x1812 |
| SCAN_RSP | Name | Appearance, Name |
| Flags detail | LE Limited Discoverable, **BR/EDR Not Supported** | LE General Discoverable |

**The `BR/EDR Not Supported` difference is not currently fixable and may
matter.** Our adapters are dual-mode and the kernel sets the flags from the
controller's capabilities, so an adapter cannot advertise "BLE only" while also
serving as a Classic HID gamepad. Whether a console cares is unknown and worth
measuring before anything is built to work around it.

#### A dual-mode adapter cannot advertise "BR/EDR Not Supported"

This is what stopped the first BLE attempt connecting, and nothing in the
advertising data could have fixed it: the kernel derives the advertisement's
Flags from the **controller's capabilities**, not from anything we send.

| adapter mode | flags on air |
|---|---|
| dual mode (default) | `0x1a` LE General Discoverable, **Simultaneous LE and BR/EDR** |
| LE only | `0x06` LE General Discoverable, **BR/EDR Not Supported** |
| the real 8BitDo 64 | `0x05` LE *Limited* Discoverable, **BR/EDR Not Supported** |

That bit is how a BLE-only host tells a controller it can drive from one it
cannot, so a console looking for a pure-BLE gamepad has good reason to ignore a
device advertising Classic support. Everything else about the two
advertisements was already identical.

`AdapterManager._ensure_radio_mode()` therefore switches an adapter to LE-only
when the BLE transport is selected, and back when Classic is. It is
read-then-write and the only adapter *setting* the server writes -- justified
because the transport choice is meaningless without it. The power cycle is
required: a controller will not change mode while it is up.

**Secure Simple Pairing does not apply to an LE-only adapter.**
`_ensure_pairing_settings` skips it there. SSP is a BR/EDR concept, so checking
for it on an LE radio reports a fault that cannot happen -- and tells the
operator that hosts will be prompted for a PIN, on a transport with no PIN
pairing to fall back to.

#### bluetoothd's GATT *client* was killing the link every 34 seconds

The single longest-running bug in this subsystem, and the cause was ours.

An Analogue 3D paired, subscribed, drove the game -- and lost input roughly
every 35 seconds, for a couple of seconds, indefinitely. Every counter on both
sides stayed healthy throughout, which is why it survived so many wrong
diagnoses: APTO, WiFi coexistence, the client, the datapath, the video link,
bluetoothd's debug logging. All eliminated by measurement, all wrong.

**Measure from the right reference point.** Drop-to-drop the interval looked
approximate -- 34.3 to 58.9 s, median 39.7 -- which suggests something
accumulating or an external trigger. Measured from **Encryption Change** it is
rigid:

| | |
|---|---|
| encryption -> drop | 34.06-35.27 s over 22 drops, spread **1.21 s** |
| encryption -> traffic stops | **exactly 30.000 s** |

The varying interval was only reconnection taking different amounts of time.
Every short capture in this investigation was 12-30 s long -- about one period
-- which is enough to see that it recurs and not enough to tell a fixed timer
from a scattered one. A 15-minute run answered it immediately.

**The mechanism.** We are the peripheral, but bluetoothd creates a GATT
*client* for every LE connection and sends `Exchange MTU Request` microseconds
after encryption completes. This console never answers -- not even the Error
Response the specification requires. Measured: **23 requests sent, 0 answered**.
ATT gives a transaction 30 seconds before it must be considered failed and the
bearer closed, and BlueZ then drops the link. Notifications stop the instant
the bearer closes, which is why input dies ~4 s *before* the visible
disconnect.

Note the asymmetry, because it is what made this look like the console's fault:
the console sends **its** MTU request and we answer all 23 of those correctly.
Only our outgoing one goes unanswered, and only we tear the link down over it.

**The fix is one documented BlueZ option**, in `packaging/bluetooth-main.conf.snippet`:

    [General]
    ReverseServiceDiscovery = false     # "for LE this disables the GATT
                                        #  client functionally so it can be
                                        #  used in system which can only
                                        #  operate as peripheral"
    [GATT]
    Client = false                      # parsed by 5.82, undocumented there

Verified: 0 MTU requests sent, 0 disconnects across 4.3 minutes and 5,879
notifications, where previously no link survived 36 seconds.

**Do not reach for `[GATT] ExchangeMTU = 23`.** It would also work --
`gatt_client_init` has `if (mtu == BT_ATT_DEFAULT_LE_MTU) goto discover;` -- but
on 5.82 the BR/EDR ATT listener passes `gatt_mtu` to `bt_io_listen`
unconditionally, BR/EDR L2CAP has a 48-byte minimum, and the failure aborts
`btd_gatt_database_new` for **every** adapter: `setsockopt(L2CAP_OPTIONS):
Invalid argument`, no GATT database anywhere. Upstream master guards it with
`btd_adapter_get_bredr()`; 5.82 does not. Tried on hardware, reverted.

`AdapterManager._check_reverse_discovery` verifies the setting at startup and
warns -- naming the **symptom**, since "input stops every ~35 seconds" is what
somebody watching a console will search for, and the setting name is not.

#### A bond has two halves, and one-sided bonds do not recover

Neither end recovers when only one half survives, and it presents two ways:

    peer kept the key, we did not:  it sends LE Long Term Key Request,
                                    we answer negative, it disconnects
    we kept the key, peer did not:  we send SMP Security Request,
                                    it answers Pairing Failed, we disconnect

Either way the link comes up and dies in well under a second, several times a
second, forever, with nothing in any log to explain it -- the GUI shows a
controller flickering between connected and not. Measured 18-30 cycles per
capture.

`_note_auth_failure` treats four `EV_AUTH_FAILED` events for one peer inside 20
seconds as conclusive and repairs what it can: our orphaned half is deleted so
the next attempt pairs cleanly, and the unfixable direction is logged as an
error saying the stale half is on the other device. A console generally offers
no way to forget a controller, so that distinction is the whole difference
between a five minute fix and an evening.

**"Forget pairing" now refuses over BLE while a bond exists**, and offers an
override only after explaining. It was a tooltip, then a confirm() dialog
spelling out the exact consequence, and it still caused this four times in one
evening -- including twice by the person who wrote the warning. A warning its
own author ignores is not a control.

**Bond presence is read from `/var/lib/bluetooth`, not D-Bus.** `org.bluez`
reported an empty bond list for an adapter whose key file existed and which the
console was actively resuming against. Anything deciding whether to *delete* a
bond must not act on a view that can wrongly answer "none".

#### What was verified on hardware

Against our own peripheral, over a real LE link:

- GATT discovery finds Generic Access, Generic Attribute, **HID (0x1812)**,
  Device Information and Battery.
- The HID service exposes all six characteristics with the right properties:
  Report Map and HID Information readable, HID Control Point
  write-without-response, Protocol Mode read/write-without-response, an input
  Report with **notify**, and an output Report.
- The Report Map reads back as the profile's own HID descriptor.

Not yet verified: a host subscribing to notifications, and input actually
reaching a console.

**Testing this from another adapter on the same Pi has a confound.** Both are
dual-mode with one public address, so bluetoothd on the scanning side merges
the BR/EDR SDP record with the LE advertisement and prefers BR/EDR --
`Device1.Connect` then fails with `br-connection-profile-unavailable`, which
looks like a BLE fault and is not one. Use `gatttool -t public`, which is
LE-only, or a host that has never seen the adapter over Classic.

#### The report ID is not in the payload

The HOGP counterpart of the Classic report-ID trap, and it fails the same
silent way in the opposite direction. Over Classic every input report begins
with its report ID; over HOGP the ID lives in the **Report Reference descriptor
(0x2908)** and the notification value is the body *without* it. Leaving it in
shifts every field by one byte -- axes read as garbage, buttons land on the
wrong bits, nothing errors at either end. `hogp.build_ble_payload` strips it,
and a test asserts the BLE payload is exactly one byte shorter than the Classic
one for both profiles.

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
  setting: a server that silently resumed auto-approving strangers after a reboot,
  because someone enabled it for one evening months ago, is a posture nobody chose. The
  persisted value is the *startup* default, from the config file or `--auto-approve`.
  `handle_settings` therefore does **not** mirror it into `state.config` — it used to,
  and since that handler never saves, the value reached disk whenever some *unrelated*
  change called `_persist`. It survived a restart or not depending on what the operator
  happened to touch next, which is worse than either answer. `rumble_enabled` sits beside
  it in the same handler and *is* persisted, because it is an ordinary preference.
- **Switch Pro profile is unverified against a real Switch.** Report generation is tested,
  the pairing handshake is not.
- **Three layers write the same adapter state** — `hciconfig` (raw HCI, below MGMT),
  `btmgmt` (a second MGMT client), and `bluetoothd` (the owner). This is why
  `hciconfig class` reverts: bluetoothd recomputes the class via MGMT and overwrites it.
  Class of device belongs in `main.conf`; everything else belongs on
  `org.bluez.Adapter1`.

  It is **no longer silent**, which was the harmful part. `_set_device_class` reads the
  class back and, if it did not stick, warns once per adapter with the `main.conf` line
  to add. Writing that file ourselves would mean editing a system-wide daemon config on
  the operator's behalf — and it is machine-wide, so it cannot express a per-adapter
  choice anyway. The symptom this rescues is nasty out of proportion to the cause: a
  console filters its pairing list on the class, so the only visible effect is never
  being offered as a controller, which reads as a pairing fault.

### `Connectable` is page scan, and BlueZ will not set it for you

**An enabled adapter must be explicitly set `Connectable=True`.** `set_properties` does
this at bring-up. Without it a radio that has never been bonded sits with page scan off and
cannot accept *any* incoming connection.

BlueZ only keeps an adapter connectable on its own when it has **bonded devices that might
reconnect**. So the adapters already paired to a host look perfect and a fresh one is
unreachable — and it is a trap that cannot open itself: the adapter cannot accept a
connection, so it can never gain the bond that would have made BlueZ keep it connectable.
Measured, four adapters on one Pi, three bonded and one not:

```
hci0/2/3  Connectable=true   scan enable 0x02   (page scan)
hci4      Connectable=false  scan enable 0x00   (nothing)
```

The host says only *"We didn't get any response from the device"*, which is literally true
and matches half a dozen unrelated faults. It cost several rounds of looking at pairing,
bonds and SSP before anyone read scan enable.

Three properties, three different radio behaviours — do not treat them as one switch:

| property | HCI effect | symptom when wrong |
|---|---|---|
| `Connectable` | page scan | host cannot connect at all |
| `Discoverable` | inquiry scan | host cannot *find* it (but a remembered one still connects) |
| `Pairable` | bondable | host finds and connects, then pairing is rejected |

`Connectable` is cleared **only** by `_quiet_adapter`, when the operator disables an
adapter — that is what finally silences a disabled radio properly, since scan enable goes
to `0x00` and kills inquiry *and* page scan in one write, through bluetoothd rather than a
second MGMT client. It is deliberately **not** cleared when a pairing window ends: a host
that has just bonded reconnects by paging us, so switching page scan off there would undo
the pairing the window existed to create.

**But not clearing it is not enough — BlueZ clears it for you.** It only keeps an adapter
connectable on its own while it holds a bond that might reconnect, so ending a window on
an adapter that did not manage to bond drops page scan to `0x00` regardless of what we
did or did not write. Measured live: arm a window on an unbonded hci3, stop it, and the
radio reads scan enable `0x00` with nothing in any log to say so. The adapter is then
unreachable — and this is the trap that cannot open itself, because it cannot accept a
connection and so can never gain the bond that would have kept it connectable.

**Ordering, and it cuts both ways.** When *arming*, set `Connectable` before
`Discoverable`: BlueZ will not hold `Discoverable` on a non-connectable adapter, so the
wrong order silently drops the request. When *disarming*, `Connectable` has to be
re-checked **after** `Discoverable` goes false, because that is what perturbs it — and the
read-then-write no-op skip makes the obvious version fail silently: at the moment we write
`Connectable=True` it is still true, so the write is skipped, and BlueZ takes it away
immediately afterwards.

Because that is a behaviour of BlueZ rather than of any one code path, the reconcile pass
also holds it as an invariant: `AdapterManager._ensure_connectable()` puts page scan back
on any enabled, non-degraded adapter that has lost it, read-then-write so the ordinary
pass writes nothing. `AdapterState.health()` reports the condition in the web GUI, which
is how this was caught at all — the check was written for the *startup* case and fired on
a case nobody knew existed.

**A window that expires on its own does not clean up after itself.** When BlueZ's
`DiscoverableTimeout` ends the window, MGMT emits `New Settings` without `discoverable`
but never writes scan enable back to `0x02` — the controller stays at `0x03`, still
answering inquiries, so the gamepad keeps appearing in a host's *Add a device* list long
after the window closed. Nothing in MGMT or D-Bus reports this; read the radio with
`hcitool -i hciX cmd 0x03 0x0019` and do not trust either layer to tell you what it is
actually doing.

`AdapterManager._expire_pairing_windows()` closes this: the reconcile pass notices a
`pairing_until_ns` in the past and re-asserts `Discoverable=False` through bluetoothd,
which is what actually rewrites scan enable — the same call the operator's "stop pairing"
button makes, so the timeout now behaves like the button. `Connectable` is deliberately
left alone, for the reason above. It fires once per window (the deadline is cleared before
the await, so the 10 s reconcile cannot start a second teardown), and clears the deadline
even when the write fails, since the window is over either way and retrying forever would
only add noise.

### `btmgmt` hangs on `/dev/null`, so every call from the service timed out

**A helper must never inherit the service's stdin.** `_run` passes `input=""`, giving each
child its own empty pipe. That one keyword is load-bearing.

Under systemd stdin is `/dev/null` (`StandardInput=null` is the default), and **`btmgmt`
hangs on `/dev/null` forever.** It is built on BlueZ's `bt_shell`, which watches stdin even
for a one-shot command; `/dev/null` is permanently read-ready and never delivers the EOF
event that would end it. Measured on the Pi, same command, same user, same instant:

```
stdin=DEVNULL      TIMEOUT after 5.00s
stdin=<pipe, EOF>  rc=0 in 0.00s
```

So **every `btmgmt` invocation the server ever made burned its full 5 s timeout and
returned failure** — `_read_mgmt_settings` returned `None`, `discov off` "timed out", and
each failure surfaced only as `log.debug` or a single warning naming one adapter. It is
invisible from a shell, where stdin is a terminal or a socket and the command returns in
under a millisecond, which is why every manual check said the adapter was fine.

Two things it was mistaken for, both wrong:

- *"Adapter hci4 is faulty."* The warning named whichever adapter was touched most often.
  All four failed identically.
- *"Reconcile is slow."* `_ensure_pairing_settings` runs per adapter under
  `_reconcile_lock`, so four adapters cost ~20 s of stalled reconcile at every startup.
  Startup now completes in about 1 s.

**`bondable` is deliberately not required by `_pairing_settings_ok`.** It is the MGMT face
of `org.bluez.Adapter1.Pairable`, which is held **false** outside a pairing window — so off
is the correct resting state, and a healthy adapter reads
`powered connectable ssp br/edr le secure-conn` with no `bondable`. Requiring it meant the
check could only ever fail, and its "correction" would have written `bondable on` through
`btmgmt` behind bluetoothd's back on every reconcile, leaving all four adapters
permanently bondable to anyone. The hang was the only thing preventing that, so fixing the
hang alone would have shipped it. The lever for bondable is `set_pairable`, which sets
`Pairable` over D-Bus and lets bluetoothd move MGMT itself — verified live: arming a window
flips the adapter to `discoverable bondable ssp` and back.

### Reconciling adapters must be serialised

`_reconcile_channels` awaits inside its loop and is driven from two independent
places: the operator enabling an adapter in the web GUI, and the hot-plug watcher
rescanning every 10 s. Overlapping, both saw "no channel for this adapter yet",
both called `_start_hid`, and the second hit **EADDRINUSE against our own
listener** — which the error message blamed on bluetoothd's input plugin, because
the two are indistinguishable from the errno.

That race is how three adapters ended up bound to **PSM 17 with no PSM 19**: one
pass won both PSMs, the other won control and lost interrupt. Before the leak was
fixed the loser's control socket then stayed bound for the life of the process,
holding the PSM so no retry could succeed, while the adapter went on advertising a
HID service it could not serve. From the host that looks like *"Try connecting
your device again"* with nothing to explain it.

An `asyncio.Lock` now serialises it, `_start_hid` refuses to start twice for one
adapter, and the EADDRINUSE message names both causes. `tests/test_bt_setup.py`
reproduces the race with two concurrent `_reconcile_channels()` calls — it fails
if the lock is removed.

**`set_enabled` must persist.** It updated the in-memory config and reconciled but
never wrote the file, so an adapter the operator enabled came back disabled after
a restart with nothing to explain it.

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

**So the GUI must not offer a per-adapter profile, and no longer does.** It used to put an
*Emulate* dropdown on every adapter card, which failed in the worst available way:
`channel.profile.build_input_report()` genuinely *is* per channel, so the setting really
did change the bytes that adapter sent — while the console was still told to expect the
other descriptor. A controller sending reports in a format nothing advertised, with one
log line as the only trace.

The choice now sits once, server-wide, in the **What the console sees** card beside the
identity: `controller_profile` in the config, `AdapterManager.set_profile_all()` applying
it to every channel, `POST /api/bluetooth/profile`. Removing the control rather than
fixing it was the point — there was nothing to fix, the capability never existed.

`--profile` therefore takes **no argparse default**. With one it always carried a value and
silently beat the saved setting, so applying a profile in the web GUI worked until the next
restart. It is a per-run override now: `args.profile or cfg.controller_profile`. Third time
this shape has bitten — see also `--backend` and the preview-demand flag.

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

## Video streaming

The return path: a **video server** captures the console (capture card), encodes, and
unicasts to each player. It runs on the capture PC, or as a subprocess on the Pi itself.

```
Video server (capture PC, or a subprocess on the Pi)
  capture ─ encode H.264 ─┬─ slice ─ AEAD ─ UDP ──────────────► each client, directly
  audio ─── Opus ─────────┘                                     (never via the Pi)
      ▲
      │  Bluetooth server connects IN (role "bt-server", video password):
      │    VIDEO_CONFIG down -- settings, tickets, the players' password
      │    VIDEO_STATUS up   -- what it is doing, plus a small JPEG preview
      │
Bluetooth server ── VideoRegistry ──► VIDEO_SOURCE advert ──► players
```

**The Bluetooth server is a control plane, not a relay.** It knows where the video is and
tells players; the media itself never passes through it. The two exceptions are embedded
mode (where the source *is* the Pi) and the preview, which is a few small JPEGs a second
for the web GUI.

### The Bluetooth server dials the video server, not the other way round

The operator enters the video server's address and password in the Pi's web GUI — or
picks it from a LAN scan — and the Pi connects out and drives it from there. The video
server is a passive appliance: it binds its port, announces itself, and waits.

Everything is then configured in one place, and the video server needs no idea where the
Pi is. That matters because the capture PC is the machine most likely to be rebooted,
moved or replaced mid-session, and because it removes the loopback accept-gate exemption
embedded mode used to need — nothing connects *in* to the Bluetooth server any more.

**Two passwords, and keeping them apart is the point.**

- The **video password** is the operator's, set on the video server and entered on the Pi.
  It authenticates the Pi to the video server, and it is the only credential that admits
  the control role.
- The **players' password** is the one every client already has. The Pi sends it over the
  encrypted control link so the video server can admit viewers.

A viewer therefore never learns the video password. If it shared one, a client the
operator *denied* would hold the credential for the one role that is exempt from viewing
tickets, and revocation would last only until they tried it. `SessionManager` carries a
second `_viewer_key` for this; both are derived at startup, so telling them apart at AUTH
is two HMACs rather than two Argon2id runs, and a proof matching the viewer key is forced
to the viewer role whatever the payload claims.

### Discovery: `RBGCV?` / `RBGCV!` on UDP 47811

Same shape as the gameplay beacon and deliberately separate — one answers "which consoles
can I play on?", the other "which machines can send me a picture?". Sharing a beacon would
mean a player's probe returning video servers, and a video server having to know about
controller capacity.

The reply carries name, port, resolution and viewer count. **No credential**, so finding a
video server still gets you nothing without its password — exactly the position a
discovered Bluetooth server is in.

### Only the link may consume the config-push flag

`VideoRegistry.needs_config_push()` both answers the question *and* records that a push
happened. Any second caller that consumes it and then sends nothing silently swallows the
link's next attempt.

That is not hypothetical: the datapath used to ask on every `VIDEO_QUERY`, left over from
when the source connected inwards. With a client querying twice a second the link almost
never won the race, so a freshly minted ticket sat unsent and the player waited on an
advert that never turned available — intermittently, which is the worst way to meet it.
`Datapath` no longer has a `_push_video_config` at all, and a test asserts the attribute
stays gone.

### Why the video server is a separate process, always

Embedded mode runs the *same program* the standalone install runs, as a subprocess. This
is not a packaging convenience:

- **The datapath calls `configure_gc_for_realtime()`, which disables the cyclic collector
  for the whole process, and `restore_gc()` is never called.** PyAV allocates per frame,
  so encoding in that process would accumulate uncollectable cycles for as long as the
  server runs.
- An encoder saturating a core is a scheduling problem for whatever shares its process.
- One code path: embedded and external sources are configured, authenticated and
  monitored identically. Only the question of who started the process differs.

`server/videohost.py` supervises it: restart backoff 1→30 s, the password via the child's
environment (never argv — the process list is world-readable), the initial settings on
stdin (never a file), and the child's output re-logged under `rbgc.videohost`.

**The child must not outlive its parent.** `stop()` only runs on a graceful shutdown, so
a SIGKILL, a crash, or anything at all on Windows leaves it encoding forever and holding
the media port. That is not untidiness: the next server to start finds a stranger already
bound there, and on Windows `SO_REUSEADDR` lets both bind, so clients are served by
whichever wins. It cost an afternoon once — six orphans accumulated across test runs, and
the oldest, several fixes out of date, was quietly answering every client with video that
could not be decoded, while the *current* server reported zero viewers. The child is now
passed `--supervised-by <pid>` and exits when that process disappears.

### Embedded mode after the inversion

The child no longer connects to us, so the loopback accept-gate exemption it once needed
is gone: `allow_loopback_video` stays false and nothing is let past the gates. The parent
spawns the child on a known port and then dials it like any other video server. (The
`_is_loopback` / `_session_transport` machinery is still there and still tested, because
the exemption can be armed — it simply is not, now that nothing needs it.)

There is no operator to agree a password with for a local subprocess, so the server
**generates one** at startup when `video_password` is unset and hands it to the child
through the environment. An ephemeral `video_port` is not usable here: the parent has to
know in advance where to dial.

### Two sockets, never one

Video gets its **own socket and its own transport**, on both ends. Sharing the gameplay
socket would put 1.2 kB video slices in the same receive queue as 29-byte input packets,
drained on the thread with a sub-millisecond budget — the exact head-of-line problem the
input path was designed around. Two sockets means two NAT mappings, which is much cheaper:
both sides heartbeat anyway.

### The bug that will bite you again: picture type carry-over

**A frame that came out of a decoder carries the picture type it was decoded as, and
`reformat()` preserves it.** Hand that frame to an encoder and it treats the set
`pict_type` as *"emit this kind of frame"*. Capture sources deliver I on every frame, so
leaving it alone makes the entire stream intra-only.

Measured on a test pattern: **90/90 keyframes at 15.6 kB each, against 2/90 at 3.2 kB**
once cleared. Five times the bitrate for identical quality — and every counter looks
perfect, the picture is flawless, and it reads as "video streaming is just expensive"
rather than as a bug. `VideoEncoder` clears `pict_type` on every frame except a
deliberate keyframe request. `tests/test_videoserver_pipeline.py` and the end-to-end
suite both assert the keyframe ratio.

Two related settings in the same area:

- **`scenecut=0` on libx264.** A scene cut emits an unscheduled full keyframe, which is
  the one thing guaranteed to burst the uplink. With `rc-lookahead=0` (required for low
  latency) the detector has nothing to look ahead at and fires constantly on moving
  content. Periodic IDR plus an explicit request from a client that lost one is the design.
- **`realtime` on a lavfi test source.** A lavfi source generates frames as fast as the
  CPU allows — measured at ~10,000 fps for 320×240. Without the filter the test source is
  nothing like a capture card and every latency figure measured against it is fiction.

### Latency measurement

Capture timestamps are on the **source's** clock, so the client needs an offset. The
exchange is NTP cut to its essentials (`MEDIA_HEARTBEAT` / `_ACK`, `t0`..`t3`), filtered
on round-trip time: a sample taken while a burst was in flight has an asymmetric path,
and asymmetry maps straight into offset error. `ClockSync` keeps a rolling minimum and
discards anything much worse.

The client shows three figures: **video** (capture→present, one way), **controller**
(existing RTT), and **combined** = controller RTT/2 + video p50, labelled as excluding
console processing and the capture card's own delay — both of which are real and neither
of which we can see.

`clock_locked` gates the display, **not** a non-zero offset. Zero is an ordinary offset
(same machine, or two clocks that agree), and treating it as "not ready" hid the numbers
exactly where they were easiest to check.

### Self-healing, like the input path

No retransmission. A frame too large for one datagram is sliced; a lost slice is a lost
frame, and the client asks for a keyframe rather than for the missing piece — by the time
a retransmission arrived the frame would be stale. `FrameAssembler` is latest-wins: a
newer `frame_id` discards an incomplete older one immediately, because holding it only
adds latency to the frame behind it. `capture_ts` rides in **every** slice so losing
slice 0 does not cost the timestamp.

Keyframe requests are rate-limited on both ends — 250 ms per client, 500 ms at the source
across *all* clients. A burst of requests from one lossy path would otherwise make the
encoder emit nothing but expensive frames, worsening the loss that caused them.

**The client also watches for frames that decode to nothing.** A broken reference chain
usually fails silently: frames arrive, decode raises nothing, no picture comes out.
Watching only for exceptions leaves the window frozen with nothing asking for a way out.

### The video leg of a broker room

A room now holds **two independent pairs**: gameplay (`server`/`client`) and video
(`video-source`/`video-client`). The `peer` message's `role` field had been on the wire
since the beginning and was read by nobody; it is now the discriminator, so the change is
backward compatible and an older broker simply refuses the new roles (video falls back to
LAN-only with a loud log).

**A peer must filter introductions by role.** A viewer registering as a plain `client`
gets introduced to the *game server*, punches at it, and then fails to handshake against
a socket serving something else — reported as "the other side never appeared". `HolePuncher`
takes a `peer_role` and ignores anything else.

Relay works unchanged: the video socket is a distinct source address, so the broker's
`addr → addr` table carries both pairs at once. A relayed path costs somebody else's
bandwidth, so the source **caps its bitrate** (`relay_bitrate_kbps`, default 3000) when
the broker reports relaying.

### Encoding for latency, not for quality

Every encoder setting exists to stop the encoder buffering: no B-frames (a B-frame refers
forward, so it cannot be emitted until the next frame arrives — one guaranteed frame of
delay), no lookahead, a VBV about 1.5 frames deep, and in-band SPS/PPS so a client
joining mid-stream needs one IDR and nothing else.

Encoder choice is a **probe, not a guess**: `pick_encoder` encodes a throwaway frame
before declaring a winner, because hardware encoders routinely open fine and fail on the
first real frame. Chain is `h264_nvenc → h264_qsv → h264_amf → libx264` on a desktop,
`h264_v4l2m2m → libx264` on ARM.

**The probe must run on a context that is then thrown away.** It originally probed
through the very context it returned, so the stream's opening IDR *and its SPS/PPS* went
into the probe's discarded output — every context handed back began mid-GOP, describing
itself with parameter sets no client ever received.

Nothing on the source side notices: frames are produced, counters climb, the encoder
reports itself healthy, and every client decodes exactly nothing. Measured on NVENC
before the fix, 70 frames out, **zero IDRs and zero pictures decoded**, with no error
logged anywhere. libx264 hid it, because it recovers at the next GOP boundary — which is
precisely why the regression test runs against *every* encoder present, not just the
software one the other tests pin.

**"Built in" is not "usable".** FFmpeg ships NVENC, QSV and AMF support whatever silicon
the machine has, so `available_encoders()` lists all three on a box with one of them or
none. `usable_encoders()` is the honest one — it opens each and pushes a frame through,
and caches the answer. Conflating them is not academic: the encoder tests skipped on the
build list, so the QSV and AMF cases quietly fell back to NVENC and re-tested it under
names claiming coverage they did not have, and the GUI offered encoders that cannot open.

**A hardware encoder also ignores a keyframe request unless told not to.** Setting
`pict_type = I` is advisory: measured on NVENC mid-stream, it produced neither an IDR nor
parameter sets, so the second viewer to join saw a black window forever while both sides'
counters looked right. `keyframe_options()` adds `forced-idr` and `repeat_headers` for
NVENC (verified) and `forced_idr` for QSV/AMF (from documentation — neither could be
opened on the machine this was written on). Because those last two are a guess,
`_build_context` **retries without them** if the encoder objects: an unverified option
must never cost somebody an encoder that would otherwise work.

**The Pi 5 has no H.264 hardware encoder at all** — embedded mode there is software
encoding on the machine also serving the input datapath. `VideoRegistry.cap_for_embedded`
holds embedded settings to 720p30 / 6 Mbps, and the web GUI *says so* when it clamps:
an operator who asks for 1080p60 and silently gets 720p30 reasonably concludes the
control is broken.

### An audio plane is sized for the codec's largest frame, not for the frame

**Never `bytes(frame.planes[0])` on decoded audio.** FFmpeg allocates the plane for the
biggest frame the codec can produce, and for Opus that is **120 ms** — 23040 bytes at
48 kHz stereo s16. A 10 ms packet carries **1920 bytes of audio inside that buffer**.

The client did exactly that, so every 10 ms of sound was followed by 110 ms of padding.
Nothing raises, `packets_received` and `decode_errors` both look perfect, and the padding
is normally zeroed — so the only symptom is the sound being wrong. It was reported as
*"I hear nothing or just crackling"*, which is precisely what 8% duty-cycle audio is.

`AudioPlayout._pcm_from` slices to `frame.samples * BYTES_PER_FRAME`. It also converts a
planar frame rather than taking plane 0, which would be the left channel alone — half the
audio at half the speed. libopus decodes to packed s16 today, so that path is insurance.

The encode side was innocent and worth not chasing: `AudioResampler` hands the encoder
1024-sample frames while libopus wants exactly 480, and PyAV re-buffers internally, so the
packet count comes out right. Verified before touching anything —
`tests/test_client_audio_decode.py` asserts the plane really is larger than the frame, so
the slice cannot be "simplified" back out.

### The jitter buffer's capacity is not its target

**`BURST_HEADROOM_MS` exists because those were once the same constant.** The buffer was
capped at `MAX_TARGET_MS`, so it could never hold more than the largest target the
governor might pick — leaving nothing above the target to absorb the late burst a jitter
buffer exists for. A WiFi stall delivers its backlog all at once, and all of it past the
cap was discarded as an overrun.

Measured on a 6 s tone with packets jittered ±40 ms: **102 overruns, 17% of the audio
thrown away**, heard as constant chopping. With headroom, the same run drops nothing and
plays 100% of what was sent. It costs nothing when the path is clean — a ceiling, not a
target; the buffer still sits at `target_ms` in steady state.

**The hold-back in `_take` is the cushion, and it applies to every read.** It reads like a
one-time priming step and it is not: releasing freely once primed lets the sink drain the
whole buffer — it asks for everything it has room for — leaving nothing for the next late
packet. Measured at ±8 ms jitter: holding back gives 4 underruns and one gap; priming-only
gives **40 underruns and gaps up to 259 ms**. That was tried, measured, and reverted.

### The capture level meter answers a question no counter can

`audio: on` means a thread is alive. A muted input, a capture card on the wrong channel,
and a console with its volume down all satisfy it while sending pure silence, and every
other indicator stays green. `AudioEncoder._measure_level` measures peak and RMS on the
audio that has *already been resampled to what we encode*, so the meter answers "is sound
reaching the stream" rather than "is the device open".

It travels in `status` so the web GUI shows the same meter as the video server's own
window. Three states, and telling them apart is the point: **off**, **live but silent**
(the fault worth catching), and a level. `level_fresh` separates silence from nothing
arriving at all — both read zero.

Sampled every 16th sample, which is 60 samples per 10 ms frame: ample for a meter and
cheap enough to sit on the encode path without numpy, which the video server does not
otherwise require.

### A/V sync

Video presents immediately, always. Audio rides a small fixed jitter buffer (30 ms
target, 20–60 bounds). At these latencies both land inside the ~45 ms window where
lip-sync error becomes noticeable, and neither ever waits for the other.

The textbook approach — present audio against the video clock — would be *worse* here:
it works by delaying whichever stream is early, and the early one is almost always video.
Deliberately adding video latency to match audio is exactly backwards for a game. A 1 Hz
governor nudges the audio buffer a few milliseconds at a time if the two drift apart.

### Video config is owned by the server — but only once the operator has chosen

Once a source attaches, the Bluetooth server pushes `VIDEO_CONFIG` and the source adopts
it — whatever it had locally. That is what makes the web GUI and the standalone GUI show
the same settings.

**Until the operator configures video on the server, there is nothing worth pushing.** A
video server is set up in front of the machine its capture card is plugged into, and it is
usually already running when the Pi first reaches it. Pushing defaults at that moment
silently undid that work: the operator selected a camera and a resolution, connected, and
watched both revert. It reads as the video server losing its settings, not as the server
asserting its own.

So `VideoRegistry._configured` gates it, seeded from whether `video_config` was actually
saved to disk:

- **Not configured** — `config_message()` omits the `config` key entirely, and the first
  `VIDEO_STATUS` (which carries the source's full `settings`) is adopted as the server's
  own. The key is *omitted*, not empty: an empty dict parses as a complete set of defaults
  and is exactly the reset we are avoiding.
- **Configured** — the server is authoritative from then on, and `set_config` latches the
  flag, so a later status can never talk it back out of the operator's choice.

Tickets and the viewer password are in that same message and are **never** withheld, or a
player would sit waiting on an advert while the settings question was being settled.

**A blank `device` means "keep using yours", never "reset to the first one found."** The
capture device is a property of the machine holding the card, so `_merge_local_device` on
the source keeps the local one when a push does not name one — likewise `audio_device`,
and `backend` when it is `auto`. Naming a device explicitly from the web GUI still wins.
The guard is deliberately on the **control path only**: `apply_config()` straight from the
local GUI or `--config-stdin` is a deliberate local act, where blank legitimately means
"first available".

Because the server→client control direction has **no retransmit**, `VIDEO_CONFIG` is
re-pushed every 2 s until the source echoes its `cfg_seq` in a status, and `VIDEO_SOURCE`
is re-answered to every `VIDEO_QUERY`. Do not add a server-side retransmit mechanism for
this; the full-state/ask-again discipline is the same one the input path uses.

**The advert waits for the first status.** A session exists from the moment the source
authenticates, but the media port it actually bound arrives with that status. Advertising
earlier hands every client the *default* port and they all fail to connect — which looks
like a broken video server rather than a premature advert.

### Watching requires a ticket, because the password cannot say who is approved

The media socket authenticates with **the same password every player has**, so it cannot
by itself tell an approved client from a denied one. Left at that, pressing *Deny* took
someone's controller away and left the picture running — the button only half worked.

So the Bluetooth server, which is the only party that knows who is approved, issues a
**ticket**: an unguessable token minted per client, carried in that client's `VIDEO_SOURCE`
advert and in the `tickets` list of `VIDEO_CONFIG`. The source admits nobody without a
current one (`SessionManager(require_tickets=True)`).

Four details that are load-bearing:

- **Mint before you advertise.** `ticket_for()` marks the config stale and clears the
  re-push timer, and the datapath pushes to the source *before* sending the advert. The
  client acts on an advert the instant it arrives, and a source that has not yet been
  told the ticket refuses it — costing the player a retry for no reason.
- **Revocation reaches live sessions.** `set_tickets()` returns the sessions that no
  longer qualify and `VideoNet` drops them. The operator pressed deny to stop someone
  *now*, not at their next connection.
- **A ticket lasts as long as the gameplay session.** Any departure revokes it, not only
  an explicit denial. The client already tears its own video down when its controller
  session drops, so nothing is lost — and it keeps the rule short enough to state: you
  may watch while you are a connected, approved player. It also stops tickets
  accumulating for the life of the process.
- **Standalone requires no ticket.** There is no Bluetooth server to issue one, which is
  what standalone means. `require_tickets` is `not config.standalone`, so a source that is
  *supposed* to be governed and has not yet been reached fails closed rather than open.

### FFmpeg reports the DirectShow device list through its *log*

Not a return value, not the exception. The dummy open always fails — that part is
documented and intended — but the exception says only *"Immediate exit requested"* while
the device names go to the logging callback. Parsing the exception text found nothing,
every time, on every machine: the dropdown stayed empty and read as a driver or
permissions problem rather than as a parser reading the wrong string.

Two details are load-bearing in `_enumerate_dshow`:

- **The level has to be raised to INFO** for the duration. The listing is emitted at INFO,
  so at the default level it is discarded before the capture ever sees it — which is
  indistinguishable from a machine with no capture devices.
- **The capture is thread-local**, so a concurrent encode keeps its own log lines instead
  of having them diverted into the device scan.

FFmpeg also emits one device across *four* separate log records — the quoted name, then
`(video`, then `)`, then the alternative name — so a parser that assumes one line per
device finds nothing. `_parse_dshow_listing` works over the joined text and is pure, so
`tests/test_capture_devices.py` exercises it against captured real output on any machine.

### The preview flows only while somebody is looking at it

**Preview slices are decoded and reassembled on the datapath thread** — the one with a
sub-millisecond budget. That is the real constraint on preview quality, and it is paid by
the *Bluetooth server*, not by the video server.

It used to stream from the moment a source connected, whether or not any browser had the
panel open. The cost was therefore permanent, which is why it had to be 320 px at 5 fps to
be affordable — and the operator saw a postage stamp and reasonably concluded the stream
itself was poor.

`VideoRegistry.preview_wanted()` gates it: asking for a frame over `/api/video/preview`
records demand, which lapses `PREVIEW_DEMAND_NS` (5 s) after the last request — comfortably
longer than the browser's poll interval. A change in demand is its own reason to push
(`needs_config_push`), since it carries no new `cfg_seq`.

**Demand travels as its own field, `preview_wanted`, never as `config.preview_enabled`.**
Overriding the setting looked tidier and killed the preview outright. The source adopts
whatever we push and reports it back in its status; a server with nothing saved adopts the
source's settings on connect. So a restart while nobody had the panel open adopted
`preview_enabled: false` as the operator's own choice — permanently, with no control in
either GUI able to undo it. The source therefore gates on the live flag alone and ignores
its own `preview_enabled` on this path.

The general rule, and the second time it has bitten here: **a transient signal must never
round-trip through a persisted one.** (See also `--backend` and `ClientConfig.backend_override`.)

With the cost gone when idle, the defaults are `preview_width` 640 (up to 1280) and
`preview_fps` 10 (up to 30), both operator-settable in the web GUI.

**An `<img>` with no `src` is a broken-image icon, not an empty box.** The preview element
carries `class="hidden"` **in the markup** and is unhidden only when a frame lands. Hiding
it from JavaScript alone was not enough: on first page load neither `startPreview` nor
`stopPreview` has run, so the operator met a broken link with its alt text sitting on top
of the hint — before touching anything.

**The picture is sized `width/height: 100%` with `object-fit: contain`.** With
`max-width`/`max-height` a 640-wide preview sat at its natural size in the middle of a
wider black box. `contain` rather than `cover`: this is a monitoring picture, and cropping
it would hide the edges of what the capture card is actually seeing.

**The browser's poll interval is the real frame rate.** It was fixed at 200 ms, which caps
the picture at 5 fps however fast the source is told to send — raising the setting appeared
to do nothing. `setPreviewRate()` derives the interval from `preview_fps`.

The preview is deliberately still **not** the stream: handing a browser H.264 means a
container (which buffers) or a JS decoder (heavy and fragile), and either way the
operator's browser would pull on the path players depend on. Players get the real thing
directly from the video server over their own socket.

### A capture that has not let go is kept, never replaced

A settings change that alters the capture format closes the device and opens it again, and
**the two devices release at their own pace** — audio is routinely still held when video
has already gone, so this is the ordinary path on a resolution change, not a rare one.

For both halves: if `stop()` reports the thread did not exit, keep the object. Assigning a
new capture over it orphans a thread that still holds the device, so the replacement
cannot open it either — and now nothing holds a reference to the one that must be stopped
first. `VideoCapture.stop()` returns that answer honestly for exactly this reason.

The video half had this guard; **the audio half did not**, and audio is the one that
usually needs it. `_start_audio_locked` is split out so the picture can come back
immediately and the sound a moment later.

Whichever half was skipped is retried from the governor tick, and **audio needs its own
recovery pass** — `_recover_capture_if_needed` only ever watched video, so when only the
microphone was still held nothing retried it. That failure is quiet in the worst way: the
picture is fine, so a permanently silent stream does not look broken enough to investigate.
`_recover_audio_if_needed` defers while video is also down, since video's recovery starts
both and two starters would race.

### Never call `frame.reformat()` — own a `VideoReformatter`

**`VideoCapture._publish` puts one `CapturedFrame` into both `latest` and the encoder's
queue.** That single PyAV frame object is therefore reformatted by *three* threads: the
encoder scaling it for H.264, the GUI drawing its 640 px preview, and the responder
encoding the 320 px one for the web GUI.

`frame.reformat()` runs all of them through **a reformatter cached on the frame**, and that
cached scaler — not the pixel data — is the shared state.

**The failure is not an exception.** One thread wedges inside the call and *never returns*
while the others run on untouched. Reproduced directly and repeatably: two threads doing
800 reformats of one shared frame, the first finishing in 0.13 s and the second stopping at
iteration 1, forever. Nothing is logged, and which thread loses is a coin toss — so it
presents either as the video server window going *Not Responding* (Windows offers to close
it, which reads as a crash) or as the web preview frozen on one picture.

It appears **only after a Bluetooth server connects**, because that is what starts the
second preview encoder — which is why it looked like a networking fault. Raising the local
preview from 4 fps to 15 made it far likelier.

Controls, so the fix is not "simplified" away later — same shared frame, two threads:

| | result |
|---|---|
| one thread, alternating both sizes | fine — size switching is innocent |
| two threads, `frame.reformat()` | **wedges** — 800 vs 1 |
| two threads, one `VideoReformatter` each | fine (0.12 s) — **the fix** |
| two threads, separate frames | fine — sharing is the trigger |

A same-size, same-format reformat takes a fast path that never touches the scaler, so a
control using one proves nothing; make both targets differ from the source.

`PreviewEncoder` and `VideoEncoder._prepare` each own one. A lock cannot substitute: the
encoder reformats the same object and putting it behind a preview lock would drag preview
work onto the hot path. `VideoServerApp.encode_preview()` keeps a lock anyway as cheap
belt-and-braces across the two previews, and is the one place to take "newest frame, then
encode". `tests/test_video_preview_race.py` runs all three consumers on one frame and
separately reproduces the raw wedge.

### Video gotchas worth keeping

- **The preview is the only path where a client's bytes are retained** rather than acted
  on and dropped. It is gated on `role == "video-source"` and capped at 1 MB, and both
  must stay in the same commit as the handler. The two caps
  (`videoserver.preview.MAX_PREVIEW_BYTES` and `server.video.MAX_PREVIEW_BYTES`) move
  together — a reassembler smaller than the sender drops a large frame *after* it has
  crossed the network.
- **A QImage built on `width*3` instead of the real stride shears the picture
  diagonally** — the scaler pads rows. `PresentFrame` carries `stride` for this reason.
- **QImage does not copy the buffer it wraps**, so the window holds a reference to the
  frame's bytes; the decoder publishes a fresh object per frame and never writes back
  into one it has handed over.
- **Qt's `findData` will not match a Python tuple** through its QVariant wrapper — the
  resolution dropdown silently stayed on its first entry. Compare `itemData()` in Python.
- **Keyboard capture had to be widened to the video window.** The filter checked only
  `self.isActiveWindow()`, so opening the stream silently killed capture — precisely when
  a keyboard player needs it.
- **`VideoWindow` emits `closed`; `QObject.destroyed` is useless here.** The window has a
  parent and the main window holds a reference, so closing it only hides it and the C++
  object is never deleted — the connection fired nothing and the button stayed on "Close
  video" for good. Fixing that alone was not enough: `_tick_video` opens the window
  whenever the stream is up and none exists, *every tick*, so it reopened the instant it
  was shut. `_video_window_dismissed` records a deliberate close, and `_stop_video` clears
  it so a retry or reconnect still brings the picture back by itself.
- **`streaming` means frames are flowing**, not that a thread is alive. On a machine
  whose capture device is missing the encoder opens fine and then sits there; reporting
  that as streaming sends the operator looking at the network.

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

### Discovery must not overwrite what the player configured

LAN discovery runs **by itself, 150 ms after the window opens**, so anything it does to
the form happens without the player asking. Selecting the first result unconditionally —
which `_populate_server_list` used to do — writes that server's address into the host and
port fields, and the next `_save_ui_into_config()` persists it.

The address the player typed is then gone. It bites exactly the setups that cannot be
discovered in the first place: a server reached over a VPN, or one running hidden, is
silently replaced by whichever machine on the LAN answered a broadcast first.

`_preferred_server_index()` decides instead: select the entry matching what is already
configured; failing that, leave **Custom** selected whenever there is something to
preserve. Only a fresh install — nothing configured, nothing to lose — auto-selects the
first result, which is where that behaviour was actually helpful.

### A one-off CLI flag must never change saved settings

`--backend synthetic` used to be copied into `input_backend` and then persisted by
the GUI. One test run therefore switched the client to fabricated controllers
**permanently**, and every launch afterwards showed "Synthetic Controller 0" and no
real hardware — which reads as "my controller isn't detected", not as a stuck
setting. It cost a full round of debugging the wrong thing.

`--backend` is now `ClientConfig.backend_override`: applied for the run, excluded
from `save()`, read through `effective_backend()`. `load()` also repairs configs
already poisoned by the old behaviour, because they will not fix themselves.

The general rule: **a transient override belongs in its own field, not on top of
the persisted one.**

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

### Keyboard capture must be explicit, and filtered at the application

`client/input/keyboard_backend.py` is fed key events by the GUI rather than
reading the keyboard itself. Two consequences that are easy to get wrong:

- **A `keyPressEvent` override on the main window never fires.** Focused child
  widgets — the controller table, combo boxes, text fields — consume keys first.
  The filter has to be installed on the **QApplication**, which sees events ahead
  of every widget. The first version overrode the window and silently received
  nothing.
- **Capture is armed by an explicit toggle**, not implied by focus. Once keys are
  intercepted the player cannot type a password or a player name, so the two
  states have to be a deliberate choice rather than a side effect of clicking.

The mapping dialog does the same thing while binding: Qt activates a focused
button on Space and answers Enter with the dialog's default button, so binding
Space re-armed Bind and binding Enter dismissed the dialog. During capture it
installs its own filter, swallows every key, and makes the Bind buttons
non-focusable.

Reading the keyboard globally would need a system-wide hook, indistinguishable
from a keylogger. Keys are released on `ActivationChange`, or one held when focus
is lost would latch down forever.

### Two app icons, one family

`client/gui/assets/icon.svg` (a gamepad) and `videoserver/assets/icon.svg` (a display) are
**generated into `.png` and `.ico` by `python -m tools.build_icon`**, outputs committed so
neither app needs QtSvg or Pillow at runtime and PyInstaller has a real `.ico` path at
build time.

They deliberately share the badge, the palette and the flat-silhouette style — one bold
shape with dark cut-outs — so the two read as one product in a taskbar while still being
told apart at 16×16. **Check both at 16 and 32 after any art change**; that is where a
detailed icon turns to mush, and it is the size the taskbar actually uses.

Each app sets its **own** `AppUserModelID` on Windows. Sharing one would group them under a
single taskbar button with a single icon despite being separate applications; setting none
at all groups them under the host interpreter and shows Python's icon.

### Controller artwork is generated SVG, not drawn in code

`client/gui/assets/controllers/*.svg` is **generated** by
`tools/build_controller_art.py`; the outputs are committed so the app needs no
tooling at runtime. Style is deliberately **flat silhouette** — solid body,
controls as light cut-outs. Two earlier attempts failed differently: drawing
everything with QPainter from coordinates in Python made every visual fix a
coordinate edit and looked hand-cut, and adding gradients and highlight
crescents on top of that read as cheap 3D clip-art.

Each control is a group `id="c_<name>"`. The preview renders the SVG once into a
cached pixmap, then resolves each held control with
`QSvgRenderer.boundsOnElement` and draws a glow there — so **art and hit boxes
cannot drift apart**, and improving the art never touches the widget.

Qt renders a subset of SVG: shapes, paths, gradients. Filters, masks and CSS are
silently ignored. And a double hyphen inside an XML comment makes the whole file
invalid with no error beyond `isValid() == False` — that has cost time three
times now, so `tests/test_client_config.py` parses every asset.

### Controller configurations

A configuration (`client/gui/controller_config.py`) is a named bundle holding a
**mapping per controller type**, because bindings only mean anything relative to
a target — an N64 wants the C buttons on the right stick and has no X/Y at all.
The bindable button list is derived from the layout, so an NES offers eight
bindings and an Xbox seventeen; switching type in the mapping screen swaps both
the list and the bindings. Slots reference a configuration by name, so one pad
can have several. Files export/import as plain JSON.

The controller type is a **binding and preview concept only.** It does not change
what the server emulates. Real 8BitDo receivers do the per-console translation on
the console side, not over Bluetooth, and BlueZ permits only one system-wide SDP
record anyway (see above) — so per-adapter emulated profiles are not possible.

**The active type is per slot, not per configuration.** Slots reference a
configuration by *name*, so two slots can hold the same one; storing the active
type on the configuration meant they fought over it, and changing one player's
controller type silently changed another's. It lives in `ControllerConfig.layout`
and the table has its own "Controller type" column beside "Configuration".

`client/config.py:load()` rebuilds `ControllerConfig` field by field. **A new
field must be added to that literal too**, or it saves and never loads.

### Presets are symbolic, and resolved against the pad in front of you

`client/gui/controller_presets.py` ships seven built-in configurations — Xbox,
PlayStation, Switch Pro, 8BitDo Ultimate, 8BitDo Bluetooth, 8BitDo DIY, generic
USB — each covering all eight controller types.

A preset says *"the bottom face button"*, never *"raw joystick button 0"*.
That distinction is the whole design, because `DeviceMapping` stores **raw
indices** and `_poll_mapped_with_override` reads the raw joystick even for pads
SDL recognises. A raw index is a property of one device on one platform, so a
shipped table of them is wrong on a good fraction of setups — the same trap
`default_joystick_mapping` documents.

Resolution happens when a preset meets a device, in two steps:

1. **SDL's own database**, via `SDL_GameControllerGetBindForButton` /
   `GetBindForAxis`, surfaced as `SDL2Backend.pad_bindings()`. That returns the
   raw index this control occupies on this device on this platform.
   `BINDTYPE_NONE` means the pad has no such control — omit the binding rather
   than invent one.
2. **The existing heuristic** when SDL has no entry (the 8BitDo 64, the mod
   kits, most no-name pads). `default_joystick_mapping` is inverted into the
   same shape so there is one resolver, and the result is flagged
   `approximate` so the UI can say so.

**A family is not a capability list.** An earlier version described each family
by which controls it has and pruned bindings to match, which silently dropped
the N64 kit's C cluster and analog stick because the family was written down as
stickless. The rule now binds **every** button its layout offers, and whether
the control exists is decided once, at resolution, from the real device. The
test for it is direct: same pad, same layout — Xbox to Xbox, an N64-shaped pad
to N64 — must leave nothing unbound.

Built-ins are **markers**: `mappings` is empty and `family` names the rule. They
are re-seeded on every load and never persisted, so an improved preset reaches
existing installs and a deleted one comes back.

### Editing configurations: Save, Save as…, and why a built-in has no Save

`Configure…` on a slot opens **whatever the Configuration column is showing**,
built-in included. `materialise(..., keep_builtin=True)` resolves a built-in's
bindings for the pad so there is something to edit, while keeping the flag so
the editor knows what it is holding.

- **Save** updates the open configuration and closes. **Disabled for built-ins**
  — they are regenerated from a rule on every launch, so writing to one would be
  silently undone at the next start, taking the player's work with it.
  `_on_save` refuses as well as disabling the button, because a dialog's default
  button is still reachable by Enter.
- **Save as…** copies under a new name, stores it *immediately*, and **carries on
  editing the copy**. `MappingDialog` therefore needs the `ConfigurationStore`
  itself: the new configuration has to exist while the dialog is still open. The
  copy's mappings are deep copies, so the editor repoints `self._mapping` at
  them — otherwise later edits would land on the configuration just left.
- Because the copy is stored before the dialog closes, `created_copy` is exposed
  and callers must honour it **even on Cancel**. Discarding it because the last
  click was Cancel would lose work the player was told had been saved.

### Guided binding

`MappingDialog` has a wizard mode — **"Bind this type…"** walks every control of
the type on screen, **"Bind all types…"** walks all eight. It is a *mode*, not a
separate dialog, so it reuses the same capture, preview and live-apply code as
the individual Bind buttons; only the sequencing is new.

- A landed binding calls `_finish_capture()`, which advances — the player never
  reaches for the mouse between controls.
- `_wizard_targets()` excludes the trigger *bits*: the trigger axis step
  captures the control whether it is analog or digital, so queueing the bit as
  well asked for LT and RT twice and the second answer overwrote the first.
  Anything filtered from the binding rows must be filtered here too — they are
  parallel views of the same control set.
- **Esc and Skip both clear the control and move on.** Skipping means "this pad
  has no such control", and the starting mapping is a *guess*, so leaving its
  invented binding in place is exactly what the player was skipping past.
- Switching type mid-run must happen **before** capture starts, or the binding
  lands in the previous type's mapping.
- `_populate_bindings()` builds fresh widgets, so it re-disables them while a
  wizard is running; otherwise the Bind buttons quietly come back to life at the
  first type change and can steal the next press.

`_starting_mapping()` is trimmed to the layout. The generic defaults describe a
full modern pad, so seeding an NES configuration with them bound fifteen
controls to a type that has eight — **and a binding the list does not show is
still sent to the console.**

### One row per physical control

**A trigger with no analog travel is a button.** `ControlRef.analog=False` marks
one — the N64's Z is a switch, and listing it under "Sticks and triggers" asked
the player to pull something part-way that only clicks, then bound it as an axis
the pad can never report travel on. `Layout.has_axis()` returns False for it, so
it falls through to the button list and the wizard asks for it as a plain press.
Switch ZL/ZR are digital on real hardware too, but SDL reports them as axes and
the Switch Pro profile only reads their *bits*, so they are left as-is.

An **analog** trigger appears **once**, under "Sticks and triggers", never also
in the button list. It used to be in both, which read as two separate things to bind, and the
button half was pointless: `apply_trigger_buttons` derives that bit from the
analog value anyway. The row is labelled with the name the system prints — `Z` on
an N64, `ZL` on a Switch, `L2` on a PlayStation.

**Either kind of control can drive a trigger.** Move an axis and it binds as an
axis, with travel. Press a button and it binds as the logical bit, which the
poll path turns into a full-scale pull (255) while held and nothing when
released — the `left_trigger_is_analog` path above. Binding one clears the
other; two sources for one control conflict.

### Sticks are asked for one direction at a time

In the walk-through each direction is **its own step**. They were one step with
an internal two-half flow, and Skip/Esc then dropped the axis's *other*
direction as well — skipping "Left" silently skipped "Right", so the right
stick appeared to only ever ask for Left and Up. A step the player can see is a
step the player can skip alone. The positive step recovers `_axis_pending` from
whatever the negative step bound, so the same-axis verification still runs; if
the negative step was skipped, the positive push stands alone and infers the
wiring from itself.

Outside the walk-through, capture is two halves in one go: **Left then Right**,
**Up then Down** — never "X+/Y−",
and the prompt names the *stick*, never the axis: "Left stick: push Down". The
axis letter is our bookkeeping, and showing it asked the player to translate it
into a direction while implying X and Y were separate controls.

**Sticks are detected by absolute deflection** (`_deflected_axis`), not against
a baseline — a stick self-centres, so "is it pushed?" is answerable from the
reading alone, and going via a baseline was what allowed the stall above.
Triggers keep the baseline comparison, because a raw joystick may rest one at
full negative rather than at zero. Asking for both halves is also how
inversion is learned: if pushing *Left* reads positive the axis is wired
backwards, and that beats letting the player discover it mid-game. A second
push on a different axis restarts the pair rather than binding something that
cannot work.

Between halves the stick must return to centre (`_AXIS_REARM_LEVEL`). Releasing
a fully deflected stick is itself a large movement in the opposite direction,
and without the wait it is read as the next push — so the binding would complete
from one flick with the wrong direction recorded.

**A stick that does not click offers directions only.** `ControlRef.clickable`
is False for the N64's, whose element exists to anchor the axes; without the
flag the artwork could not carry a stick's axes without also claiming a button
the pad cannot press. L3/R3 on modern pads are real buttons and stay bindable.

### Capture guards

Two settings stop a binding being recorded by accident. Both were added after
real use, and both trade a moment of the player's time for a whole class of
wrong bindings that look deliberate afterwards.

- **`_AXIS_CAPTURE_DELTA` (26000, ~80% of travel)** — an axis must be pushed
  near its extent. At the old third-of-travel setting, brushing a stick while
  reaching for a face button was enough to bind it. Deliberately short of the
  maximum, because a worn stick may no longer reach it.
- **A trigger additionally requires *deflection*, not merely change.** A stick
  released from full travel produces a large change while landing on nothing,
  and a trigger prompt read that as a pull: the N64 walk-through bound Z to the
  left stick's Y axis, because the stick was still over from the step before.
  `_changed_axis(require_deflection=True)` also insists the axis ends up away
  from centre. A trigger that rests at full negative still works — at rest it
  has no change from the baseline, and pulling it satisfies both at once.

There is deliberately **no cooldown between bindings.** An earlier version
paused half a second after each one, which created a window where input was
silently ignored: a control pressed during it went dead until released and
pressed again. Hold-to-confirm already covers what the pause protected —
a control held over from the previous binding is in the new prompt's baseline,
and a fresh press must persist.
- **`_HOLD_TO_BIND_S` (0.6 s)** — and it must be *held*. This applies to
  **every** control, not just axes: a button clipped while reaching past it is
  indistinguishable from a deliberate press for a single frame, and only
  persistence separates them. `_hold_input()` keys on an *identity* — an
  `(axis, sign)` pair, or the `InputSource` for a button or hat — so changing
  control mid-hold restarts the clock and a swept control never accumulates
  time. **Keyboard keys stay instant**: typing is already deliberate, and half
  a second per key would make binding a chore. The *digital* trigger path was
  missed at first and bound instantly — the common case on a retro pad, where
  Z is a plain button, so it fired on the lightest brush.

### Two sources per button

`DeviceMapping.buttons_alt` holds an optional **second** control for the same
logical button, so one action can be driven from two places. Kept as its own
dict rather than making `buttons` hold lists: `compile()` emits both into the
same tuple list and the poll loop ORs bits, so nothing downstream changes, and
every configuration written before it still loads. `bind_button(bit, None)`
clears **both** — a cleared button that kept firing from its alternate would be
the worst of both. Binding an alt with no primary promotes it, rather than
leaving it somewhere the primary lookup never reads.

Each binding row has **×** to clear it and **+** to add the second source.
Clearing a *trigger* row also drops its digital `buttons` entry, since a
trigger with no analog travel lives there rather than in `axes`.

`set_highlight(..., direction=(dx, dy))` marks **which way** a stick is being
asked for: the cap is thrown to that side, out of its well, with a chevron
pointing the way. Without it "click the left stick" and "push the left stick
up" drew identically — same ring, same element — and only the text separated
them. `_update_preview_highlight()` is the single place that decides all of
which control, lit, progress and direction, because updating one from a
separate call used to reset the others.

`ControllerPreview.set_highlight(button, *, lit, progress)` carries both cues:

- **`progress`** draws a **circular loader** around the target — a faint full
  ring plus a bright arc sweeping from twelve o'clock. Without it the control
  simply sits there for most of a second, which reads as broken rather than as
  a deliberate wait. Always circular, even on an oblong control: a partly swept
  rounded rectangle does not read as progress.
- **`lit`** draws the target *as pressed*. The walk-through uses it to show
  **the control being set**, and suppresses live input entirely (`_neutral`)
  while it runs. Mid-rebind, the control the player's press currently drives is
  not the one being configured, and lighting the old one tells them their press
  went somewhere it did not. Outside the wizard the live preview is the useful
  thing, so it stays.

`ConfigurationsDialog` ("Manage configurations…") lists only *custom* entries and
offers Edit / Rename / Delete / Export / Import. Built-ins are absent because
there is nothing to manage — deleting one brings it back next launch, and editing
one means opening it and choosing Save as…. It replaced a bare Save…/Load… pair
that exported *everything* to one file and gave no way to see what existed.

Slots reference configurations **by name**, so deleting or renaming one has to
carry the slots with it — `_configurations_changed()` clears any slot pointing at
a name that no longer exists, rather than leaving it on stale bindings.

`tools/build_controller_presets.py` writes the rule's output to
`client/gui/assets/presets/*.json` for review and sharing. Those files are
**documentation, not runtime data** — the client applies the rule directly. A
test regenerates and compares them, so a stale commit is caught.

### The trigger bits, and four the N64 borrows

`apply_trigger_buttons()` recomputes `LEFT_TRIGGER` and `RIGHT_TRIGGER` from the
analog values on **every poll**. Two consequences:

- **Never bind an unrelated control to those bits.** It is erased between polls,
  so the mapping screen shows a correct binding and nothing reaches the game —
  far harder to diagnose than a control that plainly does not work.
- **A digital trigger needs a synthesized analog value.** A pad whose Z or LT is
  a plain button (an N64 mod kit) sets the bit and has it cleared microseconds
  later. `CompiledMapping.left_trigger_is_analog` / `right_trigger_is_analog`
  record whether an axis drives each one; where none does, the poll path writes
  255 into the analog field so the bit survives and the console sees a pressed
  trigger. Both flags default to `True`, so an ordinary analog pad is unaffected.

`pad_bindings()` therefore reports each trigger **twice**: as an axis, and as a
digital source under the same name — the axis' pressed half on a modern pad, the
button itself on a retro one. Presets bind the bit through that name.

The N64's C buttons used to share the face-button bits (C-down *was* A), which
made it impossible to drive both — pushing the C stick down was indistinguishable
from pressing A. They now have their own bits, chosen from the four the N64 does
not otherwise use: C-down `RIGHT_STICK`, C-right `BACK`, C-up `CAPTURE`, C-left
`GUIDE`. All four are plain HID buttons under `generic_gamepad` (the default,
hardware-verified profile). **Under the Switch Pro profile `GUIDE` is Home and
`CAPTURE` is screenshot** — the one combination where this bites.

## Deploying the broker

`packaging/docker/` holds a Dockerfile, a compose file and deployment notes. The image is
a Python base plus `rendezvous/` — the broker imports only the standard library and
nothing else from this repo, and `tests/test_broker_relay.py` asserts that stays true.

### Peers report their own address; the broker does not have to observe it

The broker originally *observed* each peer's public source address and handed it to the
other side to punch at. That made the deployment topology a correctness question: an L4
proxy, an frp tunnel or Docker's userland proxy all re-originate the datagram, so the
broker learned the proxy and every Internet session silently fell back to relay.

A peer cannot know its own NAT mapping from the inside — that is what STUN is for — but it
can ask a directly-reachable STUN server and **report** the answer. `common/stun.py` is a
minimal RFC 5389 client for exactly that, and the introduction now carries three
candidates:

| candidate | where it comes from | what it is for |
|---|---|---|
| `local` | the peer's own LAN address | two peers behind the same NAT |
| `public` | STUN | the only one that survives a proxy |
| `address` | what the broker observed | the proxy's, when there is one |

`HolePuncher` tries them in that order, skipping duplicates — and on a directly reachable
broker `public` equals `address`, so the common path is byte-for-byte what it always was.
The two share one punch budget rather than one each, so a peer that cannot be reached
still falls back to relay just as quickly.

**Discovery must run on the socket that will carry traffic.** A NAT mapping belongs to one
local port, so a public address learned on a scratch socket describes a mapping nobody will
use. The client does it before REGISTER, when nothing else is expected on the socket. The
server cannot — the datapath owns its socket and must never block on a read — so it sends
the binding request through the same `send` callback and picks the reply up in
`handle_datagram`, recognised by content (`stun.is_stun_response`) and gated on an
outstanding transaction so it costs one attribute read per packet.

That check sits **ahead of the accept gates**: a STUN reply comes from neither the broker
nor a peer, so the gates would drop it exactly in the Internet-only case that needs it.
Safe because it is a reply to our own request and the transaction ID is verified.

### Relay is routed by token, not by address

`_relay_routes` was keyed on the observed source address, which behind a proxy collapses
two peers into one entry and *misroutes* their traffic rather than merely slowing it. Each
peer now gets a token at registration and frames relayed packets as
`RELAY_MAGIC ‖ token ‖ payload`; the broker routes on the token, strips the header, and
learns each peer's current address as it goes — so a NAT rebind or a proxy re-flowing keeps
the route. The address-keyed path remains as a fallback for un-upgraded peers.

The token must **survive re-registration** — peers renew every 20 s, and a fresh token each
time would drop a live relay every 20 seconds. It is looked up *before* `_handle_register`
replaces the `Peer`, or the lookup finds the new blank one and mints another.

Framing applies to session traffic only. Signalling is JSON the broker parses, and punch
probes go to the peer rather than through anyone — hence `Datapath.send_raw` deliberately
bypasses it while `_sendto` applies it.

`RELAY_MAGIC` is defined in **both** `common/protocol.py` and `rendezvous/broker.py`, the
same way the two `MAX_PREVIEW_BYTES` are, because the broker must stay importable with
nothing but the standard library. A test pins them together.

### What this does not fix

- **Symmetric NAT** still needs relay: the mapping differs per destination, so the STUN
  answer is not the one the peer would reach. Unchanged, and why the relay fix matters.
- **A proxy that multiplexes every peer onto one socket** breaks even the replies to
  signalling. Measure before assuming: distinct source ports per peer in `docker logs`
  means it is fine.
- **A peer can assert an address the broker cannot verify.** The observed address is kept
  alongside rather than replaced, candidates are bounded, and a punch is 10 probes a second
  for a few seconds with no amplification — but it is a real difference from observe-only.

## Layout

```
common/       protocol.py  crypto.py  state.py  timing.py  video.py   (both sides)
client/       main.py  input/  net/  gui/  media/  config.py
server/       main.py  datapath.py  sessions.py  router.py  video.py  videolink.py
              videohost.py  bt/  web/  config.py
server/bt/ble/ gatt.py  hid_service.py  advertising.py  peripheral.py
              hogp.py  HOGP wire format, stdlib only (no dbus-next)
server/bt/    adapter.py  hid.py  sdp.py  agent.py  adapter_dbus.py  identities.py
              hci.py   raw HCI command channel (link tuning has no BlueZ interface)
              link.py  LinkPolicy / LinkTuner -- flush timeout, sniff, supervision
              mgmt.py  management socket: read-only settings + the event stream
              state.py AdapterState / AdapterRegistry -- one object per BD_ADDR
videoserver/  main.py  pipeline.py  capture.py  encode.py  net.py  control.py
              preview.py  discovery.py  gui.py  config.py
rendezvous/   broker.py                                      (public VPS service)
packaging/docker/  Dockerfile  docker-compose.yml  healthcheck.py  README.md
tools/        latency_harness.py  bt_link_probe.py  build_controller_art.py
              build_controller_presets.py
tests/
```

`common/video.py` holds the media **wire format only** — stdlib `struct`, no PyAV. The
codec layer lives in `videoserver/` (capture and encode) and `client/media/` (decode and
playback), both of which import PyAV lazily so the apps still start and explain themselves
where the media extras are missing.

`common/` is imported by both sides and must stay dependency-light and platform-neutral —
no SDL2, no BlueZ, no Qt.

## Commands

```bash
# Setup (dev machine)
pip install -e ".[client,dev]"          # Windows/Linux client work
pip install -e ".[server,dev]"          # Linux server work
pip install -e ".[video,dev]"           # video server work (adds PyAV)

# Tests -- 1403, none need hardware (GUI tests run offscreen, video uses a
# lavfi test pattern). Video tests skip cleanly without the media extras.
pytest tests/ -v

# Regenerate committed assets after changing the rules that produce them
python -m tools.build_controller_art        # client/gui/assets/controllers/*.svg
python -m tools.build_controller_presets    # client/gui/assets/presets/*.json
python -m tools.build_icon                  # both apps' icon.png / icon.ico

# Full pipeline on one machine, no Bluetooth hardware needed
python -m server.main --mock-bt --password test123 --auto-approve -v
python -m client.main --headless --direct 127.0.0.1 --password test123 \
                      --backend synthetic --controllers 0,1

# Video, no capture card needed. It binds and waits for the Bluetooth server to
# dial in; point the web GUI's Video panel at it (Detect, or 127.0.0.1) using
# this password -- the video server's own, not the players'.
RBGC_PASSWORD=video123 python -m videoserver.main --headless --test-source \
                           --media-bind 0.0.0.0:47810 -v

# --standalone skips the Bluetooth server and serves anyone holding the
# password. Testing only: nothing issues viewing tickets in that mode.

# ...or let the server run one itself, configured from the web GUI:
python -m server.main --mock-bt --password test123 --video-mode embedded -v

# What capture devices this machine can see
python -m videoserver.main --list-devices

# What encoders this machine actually has
python -c "from videoserver.encode import available_encoders; print(available_encoders())"

# Latency breakdown
python -m tools.latency_harness

# What the radios are ACTUALLY doing -- read over HCI, not from MGMT or D-Bus.
# Run on the server with a console connected. Needs root.
sudo python -m tools.bt_link_probe

# Build the standalone executables
pyinstaller packaging/client.spec           # → dist/rbgc-client/  (~166 MB)
pyinstaller packaging/videoserver.spec      # → dist/rbgc-video/
```

### Packaging gotchas

Both of these produce a bundle that builds fine and then fails at runtime, so they are
easy to reintroduce:

- **PyNaCl needs `cffi` and `_cffi_backend` as hidden imports**, plus
  `collect_dynamic_libs("nacl")` for libsodium. Without them the exe dies on import with
  `No module named '_cffi_backend'`.
- **SDL2 ships inside `pysdl2-dll`** and is loaded through ctypes, so it needs
  `collect_dynamic_libs("sdl2dll")`.
- **PyAV needs `collect_dynamic_libs("av")` for the bundled FFmpeg, plus
  `collect_submodules("av")`** — it is a large set of Cython extensions that import each
  other dynamically, and enumerating them is the only way PyInstaller finds them all.
- **`PySide6.QtMultimedia` is no longer excluded** from the client bundle: `QAudioSink`
  plays the stream's audio. Everything around it stays excluded, so the cost is bounded.
- The whole encode path assumes the PyAV wheel ships **libx264 and libopus**. It does
  today; `tests/test_videoserver_pipeline.py` asserts it so a future LGPL-only wheel
  fails a test rather than a player's evening.
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
timestamps (`server/bt/sink.py:MockSink`). The entire pipeline — input, crypto,
transport, routing, report generation — is exercised on any machine. Use this for all
development; only Phase 4 BT work genuinely needs a Pi with dongles.

`client/input/synthetic.py` fabricates gamepads (`--backend synthetic`) so input
measurements have a ground truth.

**Video needs no capture hardware either.** `--test-source` (or `test_source` in the
settings) opens a lavfi test pattern, and `tests/test_video_e2e.py` runs the entire
chain — Bluetooth server, video server and a player, each the real implementation over
real loopback sockets — in about eight seconds. That test is the one to reach for when
changing anything across the video boundary: each side looks correct alone.

## Conventions

- Type hints throughout. `from __future__ import annotations` at the top of every module.
- Datapath code (`server/datapath.py`, `client/input/`, `common/protocol.py`) is
  **allocation-sensitive**: prefer preallocated buffers, `struct.pack_into`, and avoid
  creating objects per packet. Comment any non-obvious micro-optimization with the reason.
- Everything else favors clarity over speed — the GUI and control plane are not hot.
- No `print()` in library code; use the module logger. The datapath logs at most on state
  transitions, never per packet.
