# Remote Bluetooth Game Control

Play a console remotely. Gamepads plug into **client PCs** anywhere on the internet;
their input streams to a **Linux server** (typically a Raspberry Pi 5) which impersonates
Bluetooth game controllers to a console or Bluetooth receiver.

- Up to **4 client PCs**, 1–4 gamepads each
- Up to **4 emulated controllers**, one per Bluetooth adapter
- Headless server with a **web GUI** for approval, routing, pairing and latency
- Shared **password** on all connections, ChaCha20-Poly1305 on every packet
- **Direct/LAN** and **NAT hole-punching** connection modes

---

## Read this before you start: latency

The original design goal was 2–5 ms input-to-console. **That is not physically
achievable**, and no software can make it so:

| Stage | Typical | Whose cost |
|---|---|---|
| Gamepad → client PC | 1–8 ms (USB); 5–15 ms if the pad is itself Bluetooth | hardware |
| Client capture → send | **0.05–0.3 ms** | ours |
| Network, one way | ~0.2 ms LAN; 10–60 ms WAN | physics |
| Server recv → BT write | **0.05–0.3 ms** | ours |
| BT HID → console | 5–15 ms (connection interval) | Bluetooth spec |
| **Total, LAN** | **~8–25 ms** | |
| **Total, internet** | **~25–90 ms** | |

What this project *does* deliver is **under 1 ms of software-added latency** — measured
at ~0.25 ms on x86 and ~0.57 ms on a Pi 4. Run `python -m tools.latency_harness` to see
the breakdown on your own hardware.

**Measured on real hardware** (Pi 4B, Debian 13, BlueZ 5.82, built-in adapter → Windows 11,
Pi on 5 GHz WiFi; 60 samples, 0 timeouts), from client send to OS-level gamepad input:

| min | median | p90 | p99 |
|---|---|---|---|
| 4.14 ms | **5.79 ms** | 7.74 ms | 12.45 ms |

Better than the LAN estimate above, because 5 GHz WiFi does not contend with 2.4 GHz
Bluetooth. Add 1–8 ms for a wired USB gamepad's own polling. The first input after an idle
gap costs ~70 ms while the Bluetooth link wakes — normal, and not fixable in software.

**The two things that matter most are yours to control:** use **wired USB gamepads**
(avoids a second Bluetooth hop, saves 5–15 ms), and keep the client close to the server.

## Supported Bluetooth targets

| Target | Status |
|---|---|
| Generic BT HID gamepad | **Supported.** 8BitDo/Mayflash-class receivers, PCs, Android, Steam Deck |
| Nintendo Switch Pro Controller | **Supported.** Pairs via "Change Grip/Order" |
| PS4 / PS5 / Xbox | **Not supported.** Proprietary authentication crypto that cannot be emulated |

---

## Quick start

### Try it with no hardware at all

Everything below runs on one machine, no Bluetooth dongle and no gamepad needed.

```bash
pip install -e ".[dev]"

# Terminal 1 — server with 4 fake adapters
python -m server.main --mock-bt --password test123 --auto-approve -v

# Terminal 2 — client with 2 fake controllers
python -m client.main --headless --direct 127.0.0.1 --password test123 \
                      --backend synthetic --controllers 0,1 --usernames alice,bob
```

You should see live latency lines within a couple of seconds. Then open
<http://localhost:8080> and sign in with `test123` to see the web GUI.

### Real server (Raspberry Pi)

```bash
sudo apt install bluez python3-pip
git clone <this repo> /opt/rbgc && cd /opt/rbgc
python3 -m venv venv && ./venv/bin/pip install -e ".[server]"

# REQUIRED: stop bluetoothd claiming the HID role
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo cp packaging/bluetooth-noinput.conf /etc/systemd/system/bluetooth.service.d/rbgc.conf
sudo systemctl daemon-reload && sudo systemctl restart bluetooth

sudo mkdir -p /etc/rbgc
sudo sh -c 'printf "your-password" > /etc/rbgc/password' && sudo chmod 600 /etc/rbgc/password

sudo cp packaging/rbgc-server.service /etc/systemd/system/
sudo systemctl enable --now rbgc-server
```

Then open `http://<pi-address>:8080`.

### Real client

```bash
pip install -e ".[client]"
python -m client.main                  # GUI
python -m client.main --list-controllers
```

### Standalone client executable (no Python needed)

```bash
pip install -e ".[client,dev]"
pyinstaller packaging/client.spec      # -> dist/rbgc-client/
```

Zip `dist/rbgc-client/` and hand it to players — they run `rbgc-client.exe` and nothing else.

---

## Using it

1. **Server:** open the web GUI, sign in with the server password.
2. **Adapters:** each detected dongle appears as a card. Enable the ones you want (up to 4)
   and choose what each emulates. Capacity follows automatically — with two dongles,
   clients see two usable slots and the rest grey out.
3. **Pair:** click **Connection mode** on an adapter, then start pairing on the console.
   For a Switch, that means the "Change Grip/Order" screen.
4. **Client:** enter the server address (or click **Find on LAN**), the password, and a
   player name per controller. Connect.
5. **Approve:** the client appears in the web GUI as pending. Approve it, then assign each
   controller to an adapter.

---

## Connection modes

| Mode | When | Notes |
|---|---|---|
| **Direct** | Same LAN, VPN (Tailscale/WireGuard), or port-forwarded | Lowest latency, no third party. Includes LAN auto-discovery. |
| **Hole-punch** | Both sides behind NAT | Needs a rendezvous broker on a public IP. Falls back to relay if traversal fails. |
| **Auto** | Default | Tries direct, then discovery, then hole-punch. |

To run a broker (any cheap VPS):

```bash
python -m rendezvous.broker --port 47900 -v
```

The broker only ever learns which addresses want to talk. It never sees the password and
cannot decrypt session traffic.

---

## Troubleshooting

**`L2CAP PSM 17/19 already in use`** — bluetoothd's input plugin has the HID role. Apply
`packaging/bluetooth-noinput.conf` (see above). This is the single most common failure.
Check the daemon path matches your distro: Debian 13 uses `/usr/libexec/bluetooth/bluetoothd`,
others use `/usr/lib/bluetooth/bluetoothd`.

**`Operation not possible due to RF-kill`** — Raspberry Pi OS soft-blocks Bluetooth by
default:
```bash
sudo rfkill unblock bluetooth      # binary lives in /usr/sbin, often off a non-root PATH
```

**Host says "Couldn't connect" when pairing** — usually a stale bond. If you removed the
device on the host, it made a new link key while the Pi kept the old one. Entering
connection mode clears bonds automatically now; to do it by hand:
```bash
bluetoothctl devices Paired
bluetoothctl remove <ADDRESS>
```

**Host reconnects but no input arrives** — check that the client is *approved* in the web
GUI and assigned to an adapter. An unapproved client authenticates fine and its input is
counted, but never routed. `auto_approve` resets on server restart.

**Device pairs but never reconnects after a server restart** — known limitation. The Pi
cannot initiate the reconnect; connect from the host side, or re-pair.

**`Permission denied` binding L2CAP** — run as root, or:
```bash
sudo setcap 'cap_net_raw,cap_net_bind_service+eip' $(readlink -f $(which python3))
```

**Console won't find the adapter** — confirm connection mode is active, and that the
adapter is enabled in the web GUI. `hciconfig -a` should show it `UP RUNNING`.

**Latency looks bad** — run `python -m tools.latency_harness` for the per-stage breakdown
before changing anything. In practice it is almost always a Bluetooth-connected gamepad on
the client (switch to USB) or a cheap dongle on the server.

**Reported RTT seems high on loopback** — expected. RTT is biased upward by up to one poll
period because acks are read once per tick; at 500 Hz that is up to 2 ms. The
directly-measured server numbers are the trustworthy ones. See CLAUDE.md.

**Controller assigned to the wrong console after a reboot** — should not happen: adapters
are tracked by BD_ADDR, not `hciX` index. If it does, file a bug.

---

## Development

```bash
pytest tests/ -v                      # 190 tests, no hardware needed
python -m tools.latency_harness       # per-stage latency breakdown
```

`CLAUDE.md` documents the architecture, wire protocol, threading model, and the
design decisions that exist for latency reasons — read it before changing the datapath.

## Layout

```
common/       protocol, crypto, controller state, timing   (shared, platform-neutral)
client/       input backends, transport, GUI, poll loop
server/       datapath, sessions, router, bluetooth, web GUI
rendezvous/   NAT hole-punching broker
tools/        latency harness
packaging/    PyInstaller spec, systemd units
```

## License

MIT
