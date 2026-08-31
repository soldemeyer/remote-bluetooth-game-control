# Playing over the internet

Three ways across, in order of latency. Measure before assuming which one you
need — the answer is decided by your NAT, and it takes two commands to find out.

| | path | needs | latency |
|---|---|---|---|
| **Port forward / VPN** | client → server, direct | a forwarded UDP port, or Tailscale/WireGuard | best |
| **Hole-punch** | client → server, direct | a broker, and a NAT that traverses | best |
| **Relay or frp** | client → VPS → server | a public VPS | one extra hop |

## First: does hole-punching stand a chance?

Ask two different STUN servers from **one** socket. If they report different
external ports, the mapping is endpoint-dependent ("symmetric") and punching
cannot work — the address STUN reports is not the one a peer would reach.

```
one local socket, port 43197
  stun.l.google.com:19302   -> 75.174.63.47:8879
  stun.cloudflare.com:3478  -> 75.174.63.47:62487   <- different, same socket
```

On such a network, do not spend time on hole-punch mode. It will fail every
time and cost about ten seconds doing it. Use relay or frp — or better, a port
forward, which beats both because nothing bounces off a third machine.

## Option A: the broker's relay

Nothing new to deploy if you already run the broker. In the client, choose
**"Over the Internet (relay via broker)"**. It registers, gets introduced, and
goes straight to relaying without the punch it knows will fail.

The broker allocates a UDP port per peer of each relayed pair, which is what
lets more than one player be relayed at once. **Open `47910-47949/udp` on the
VPS firewall** alongside the signalling port — see `packaging/docker/`.

## Option B: frp

Use this if you would rather not run the broker, or already have frp. Copy
`frps.toml` to the VPS and `frpc.toml` to the Pi, change both tokens and
`serverAddr`, and start them. Then in the server's web GUI:

1. **Client connections → "Through a tunnel"** on.
2. **Visibility → "Tunnel delivers from"** = `127.0.0.1`.

Players choose **"Through a tunnel or port forward"** and enter the VPS address.

### Why there is a separate toggle for it

Tunnelled traffic reaches the server directly, so before this existed the only
way to admit it was to switch LAN on — which admits the entire subnet in order
to let one forwarder in. Naming the source address narrows it back to the
forwarder, and it is also what lets the server tell tunnelled sessions apart
from LAN ones when you switch one off.

Leaving the source blank still works; it just accepts a tunnelled client from
any address, and those sessions count as LAN.

### UDP, not TCP

Both proxies are `type = "udp"` and must stay that way. The input path is a
stream of full-state snapshots where a lost packet is superseded by the next
one. TCP would hold every fresh controller state behind the retransmit of a
stale one — the exact head-of-line stall the UDP design exists to avoid.

### One client per flow

frp must give each remote client its own local flow. It does, but a proxy that
multiplexes every client onto one local socket makes them all arrive at the
server from one address, and sessions keyed by address then collapse into one:
the second player to connect evicts the first, with every counter still
reporting a healthy session.

Check with `ss -unp | grep 47800` on the Pi while two players are connected —
two distinct source ports means it is fine.

## What the broker never sees

Either way the middle machine forwards datagrams it cannot read. Sessions are
end-to-end AEAD-sealed with a key derived from the password, which never
reaches the VPS. It learns endpoints, and the room name if you list it.
