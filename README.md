# Remote Bluetooth Game Control

Play a console remotely. Gamepads plug into **client PCs** anywhere on the internet;
their input streams to a **Linux server** (typically a Raspberry Pi 5) which impersonates
Bluetooth game controllers to a console or Bluetooth receiver.

- Up to **4 client PCs**, 1–4 gamepads each
- Up to **4 emulated controllers**, one per Bluetooth adapter
- **Low-latency video and audio** back to every player, with a fullscreen mode
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

# Terminal 1 — server with 4 fake adapters.
# --accept-clients matters: a fresh server starts switched off and drops every
# packet until someone turns a transport on, here or in the web GUI.
python -m server.main --mock-bt --password test123 --auto-approve \
                      --accept-clients -v

# Terminal 2 — client with 2 fake controllers
python -m client.main --headless --direct 127.0.0.1 --password test123 \
                      --backend synthetic --controllers 0,1 --usernames alice,bob
```

You should see live latency lines within a couple of seconds. Then open
<http://localhost:8080> and sign in with `test123` to see the web GUI.

To add video to that — still with no hardware — install the media extras and stream a
test pattern:

```bash
pip install -e ".[dev,video]"

# Terminal 3 — a video server with no capture card, on its own password
RBGC_PASSWORD=video123 python -m videoserver.main --headless --test-source \
                           --media-bind 0.0.0.0:47810 -v
```

In the web GUI's Video panel choose *A video server elsewhere*, press **Detect** (or type
`127.0.0.1`), enter `video123`, and press **Connect**. The preview appears. Run the client
with its GUI (drop `--headless`) and the stream opens by itself.

Note `video123` is the *video server's own* password, separate from the players' `test123`.
Once connected, its settings come from the web GUI — change resolution and bitrate there
rather than on its command line.

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

Then open `https://localhost:8080` **through an SSH tunnel** (the admin interface binds
loopback by default):

```bash
ssh -L 8080:127.0.0.1:8080 spencer@<pi-address>
```

The GUI is HTTPS with a self-signed certificate generated on first run, so your browser
shows a warning the first time. The server prints the certificate's SHA-256 fingerprint at
startup — check it matches once, then accept. To serve on the LAN directly instead, set
`web_host` in the config.

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

## Video

Players need to see the game. A **video server** captures the console's output from a
capture card, encodes it, and streams it to each player. The Bluetooth server tells
players where the stream is; **the video itself goes straight from the capture machine to
each player** and never passes through the Pi.

Two ways to run it:

- **On a PC with the capture card** (the usual setup — a desktop has hardware H.264
  encoding and bandwidth to spare):

  ```bash
  rbgc-video                 # set a password in the window, then leave it running
  ```

  It has a GUI by default, and `--headless` for a machine with no display. A Windows
  build is produced by `pyinstaller packaging/videoserver.spec`.

  The video server waits to be taken charge of: in the Bluetooth server's web GUI, press
  **Detect** to find it on the LAN (or type its address), enter the password shown on the
  video server, and press **Connect**. That password is the video server's own, *not* the
  players' one — players never learn it, so someone you denied cannot pose as the server.

- **On the Pi itself**, if the capture card is plugged in there. Set video to *This
  machine* in the web GUI, or start the server with `--video-mode embedded`. Note the
  **Raspberry Pi 5 has no hardware H.264 encoder**, so this is software encoding on the
  machine already serving your controllers: it is held to 720p30 and the web GUI says so.

Either way the settings live in the server's web GUI — capture device, resolution, frame
rate, bitrate, audio — alongside a live preview so you can confirm the capture card is
showing what you expect.

In the client, video opens by itself once a stream is available. **F11** toggles
fullscreen, **L** toggles a latency overlay showing the video path, the controller path,
and the two combined.

No capture card to hand? `--test-source` streams a test pattern, and everything else
behaves identically.

### Video latency

| Stage | Typical |
|---|---|
| Capture card | 5–15 ms (the card's own buffering; outside our control) |
| Encode | 2–5 ms hardware, 5–12 ms software |
| Network, one way | ~0.2 ms LAN; 10–60 ms WAN |
| Decode and present | 5–15 ms |
| **Software total, LAN** | **~25–55 ms** |

Audio rides a 30 ms jitter buffer, which keeps it inside the window where lip-sync error
is noticeable without ever making video wait for it.

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

**Device doesn't reconnect after a restart** — it should, automatically, within a couple of
seconds. The server remembers the last host it was connected to and dials back out. If it
doesn't:
- Check the pairing still exists on both sides (`bluetoothctl devices Paired`)
- Check `paired_target` is set for the adapter in the server config
- The host must be awake; a sleeping PC won't answer, and retries back off to 30 s

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

**No video, or "waiting for a video server"** — check the Video panel in the web GUI. If
the mode is *A video server elsewhere*, the Bluetooth server has not reached it: check the
address, and that the password matches the one shown on the video server (that is its own
password, not the players'). The panel reports the last connection error. If the mode is
*This machine*, look for `video:` lines in the server log — a missing capture device is
reported there.

**Detect finds nothing** — discovery is a LAN broadcast, so it does not cross subnets and
some Wi-Fi setups block it. Type the address in instead; nothing else depends on
discovery. Check too that the video server has "Announce this machine on the LAN" on.

**The capture device dropdown is empty** — press **Rescan**. If it stays empty, the video
server found no devices at all; `python -m videoserver.main --list-devices` prints what it
can see.

**Video connects but the picture never appears** — almost always the capture device.
The panel shows the encoder it chose and the frame rate it is achieving; 0 fps with an
encoder listed means nothing is being captured. Try **Scan devices**, or tick the test
pattern to prove the rest of the path works.

**Video is choppy from the Pi** — a Pi 5 has no hardware H.264 encoder and encodes in
software while also serving your controllers. Use a PC with the capture card if you can;
otherwise keep it at 720p30 and watch the encode figures in the panel.

---

## Development

```bash
pytest tests/ -v                      # 954 tests, no hardware needed
python -m tools.latency_harness       # per-stage latency breakdown
```

`CLAUDE.md` documents the architecture, wire protocol, threading model, and the
design decisions that exist for latency reasons — read it before changing the datapath.

## Layout

```
common/       protocol, crypto, controller state, timing, media wire format
client/       input backends, transport, GUI, poll loop, video playback
server/       datapath, sessions, router, bluetooth, web GUI, video control plane
videoserver/  capture, encode, media socket, GUI          (runs on the capture PC)
rendezvous/   NAT hole-punching broker
tools/        latency harness
packaging/    PyInstaller specs, systemd units
```

## License

MIT
