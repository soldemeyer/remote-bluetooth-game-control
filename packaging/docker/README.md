# Running the rendezvous broker in Docker

```bash
# From the repository root
docker compose -f packaging/docker/docker-compose.yml up -d --build
docker logs -f rbgc-broker
```

The image is a Python base plus `rendezvous/` — the broker imports nothing
outside the standard library and nothing else from this repo.

Check it is answering:

```bash
docker exec rbgc-broker python /app/healthcheck.py && echo ok
```

Point the server and clients at `your-host:47900` (server web GUI →
Visibility → Rendezvous broker; client → Internet mode).

---

## The proxy question

**A proxy in front of the broker is now supported, provided STUN is enabled**
(it is, by default). It was not always: the broker used to depend on *observing*
each peer's public source address, and an L4 proxy, an frp tunnel or Docker's
userland proxy all re-originate the datagram from their own address.

What changed is where that address comes from. A peer cannot know its own NAT
mapping from the inside, but it can ask a STUN server, which is directly
reachable, and then **report** it. The broker passes that candidate through
without needing to observe, understand or verify it. Relay was fixed the same
way: each peer gets a token at registration and prefixes relayed packets with
it, so forwarding is routed by content rather than by source address.

So behind frp you get:

- **Hole-punching** — works, via the reported candidate.
- **Relay fallback** — works, via the token.
- **Listing and introductions** — unchanged; they were never address-dependent.

### What must still be true

1. **A STUN server has to be reachable from each peer.** The defaults are
   public (`stun.l.google.com:19302`, `stun.cloudflare.com:3478`). Point
   `stun_servers` at your own — coturn, or anything speaking RFC 5389 — to keep
   a third party out of it entirely.
2. **Your proxy must give each peer a distinct flow.** frp normally opens a
   separate local UDP connection per client, which is fine. If yours
   multiplexes every peer onto one socket, even the *replies* to signalling
   become ambiguous and no amount of this helps. Check `docker logs` — distinct
   source ports per peer means you are fine.
3. **STUN answers correctly only for endpoint-independent (cone) NAT.** On
   symmetric NAT the mapping differs per destination, so the reported candidate
   is not usable by the peer and the session falls back to relay. That was
   already true before any of this.

### Does this open anything in my firewall?

No — and it is worth being precise, because the honest answer is not simply
"nothing is exposed":

- **No router configuration changes.** No port forward, no UPnP mapping,
  nothing persistent. The only thing created is an ordinary outbound NAT
  mapping, exactly as when a browser fetches a page, and it expires after about
  30 s to 5 min idle.
- That mapping *is* reachable from outside — **that is the mechanism**, not a
  side effect. It is equally true of the address the broker observed before,
  and of any UDP connection. STUN changes who observes it, not whether it
  exists.
- On the **port-restricted NAT typical of home routers**, a third party who
  learns the port still cannot reach it: inbound is only accepted from an
  address you have already sent to. That is exactly why punching requires both
  sides to send. Full-cone NAT is the exposed case; symmetric NAT makes the
  learned port useless to anyone else anyway.
- **What sits behind the port matters more than the port.** The datapath drops
  any datagram that is not a valid handshake or an AEAD-authenticated packet
  from a live session, and both accept gates default to off.
- The real delta from *public* STUN is **privacy, not exposure**: Google or
  Cloudflare learn your public IP:port and that you are doing NAT traversal.
  A binding request carries nothing else — no password, no room code, no
  session traffic. Set `stun_servers` to your own to avoid even that.

## Still the simplest option: the broker on the VPS

None of the above is needed if the broker has a public address of its own. It
is tiny — signalling is a few hundred bytes per peer every 20 seconds — so
copying this directory and `rendezvous/` to the VPS and running the same compose
file remains the least moving parts. Keep frp for whatever else you publish.

## Docker's own userland proxy

The compose file uses `network_mode: host`. On a bridge network Docker may
publish the port through `docker-proxy`, a userspace forwarder that rewrites the
source address like any other. That is survivable now, but it is one more thing
between you and a working punch for no benefit, so host networking stays the
default. If you do switch to a bridge network, disable it in
`/etc/docker/daemon.json`:

```json
{ "userland-proxy": false }
```

and restart Docker, so the published port is a plain iptables DNAT.

## Checking a deployment

Read `docker logs rbgc-broker` after two peers connect:

- **Registrations showing real public addresses, and no relay warnings.**
  Everything is direct — the ideal, and what you get with the broker on a public
  address.
- **Registrations showing one repeated address** (`172.x`, `10.x`, your frpc
  host) **but no relay warnings.** The proxy is rewriting addresses and the
  reported STUN candidates are carrying the punch. Working as designed.
- **`Relaying ... (hole-punch failed)`.** Punching did not work for that pair.
  Either they are on symmetric NAT — nothing to be done, relay is the answer —
  or STUN is unreachable and there was no candidate to fall back on. Check the
  client and server logs for `STUN sees us at`; its absence is the tell.

---

## What the broker can and cannot see

Worth knowing before exposing it, and the reason it needs no secrets of its
own:

- It never receives the game or video password, and holds no key material.
- Session traffic is end-to-end encrypted; relayed packets pass through opaque.
- It learns which addresses want to talk to each other, and the name and room
  code of servers that **opted in** to being listed publicly.
- `list` returns those names only — never an endpoint. Reaching a server still
  requires registering into its room and being introduced.

There is no authentication on the broker, by design: a peer proves nothing to
it, and gains nothing from it but an introduction. Rate limits and room caps
(`MAX_ROOMS`, `MAX_MESSAGE`, `PEER_TTL_S`) bound what an unwanted guest can
consume.
