# Security Audit

Audit of Remote Bluetooth Game Control as of 2026-08-10, covering the client, server,
rendezvous broker, and shared protocol layer. Findings are rated by realistic impact in
this system's threat model, not in the abstract.

## Threat model

Who we defend against, and what they can do:

| Actor | Position | Realistic goal |
|---|---|---|
| **LAN attacker** | Same network as server or client | Capture the password, take over controller slots, watch the operator's session |
| **Internet attacker** | Can reach a forwarded port, or the broker | Guess the password, exhaust resources, hijack a session |
| **Malicious client** | Knows the password | Escape its own slot, disrupt other players, attack the server |
| **Broker operator** | Runs the rendezvous service | Read or tamper with relayed gameplay traffic |

Explicitly out of scope: an attacker with root on the server (they own the Bluetooth stack
anyway), physical access, and attacks on Bluetooth pairing itself.

---

## Summary

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Web GUI is HTTP-only and the password crosses the LAN in cleartext | **High** | **Fixed** |
| 2 | Web login has no rate limiting — only a 1 s delay that parallel connections bypass | **High** | **Fixed** |
| 3 | One password grants both operator (web) and player (client) access | **Medium** | **Fixed** |
| 4 | No HTTP security headers — clickjacking on approve/deny controls | **Medium** | **Fixed** |
| 5 | Auth middleware fails **open** for any non-`/api` path | **Medium** | **Fixed** |
| 6 | Broker has no per-peer rate limit and no relay bandwidth cap | **Medium** | Open |
| 13 | Nonce counter increment was not atomic across threads | **High** | **Fixed** |
| 7 | `--password` on the command line is visible in `ps` | **Medium** | Mitigated |
| 8 | Web GUI binds `0.0.0.0` by default | **Low** | **Fixed** |
| 9 | `_pending_hello` is cleared wholesale, dropping in-flight handshakes | **Low** | **Fixed** |
| 10 | Handshake rate limiting is keyed on source IP alone | **Low** | Open |
| 11 | Discovery beacon answers unauthenticated probes | **Low** | Accepted |
| 12 | No audit log of operator actions | **Low** | Open |

**No vulnerable runtime dependencies.** `pip-audit` reports 15 CVEs, all in `pip` and
`setuptools` — build tooling, not shipped code. PyNaCl, aiohttp, dbus-next, pyudev and
PySide6 are all clean.

---

## High severity

### 1. Web GUI is HTTP-only; the password crosses the network in cleartext

`server/web/app.py` serves plain HTTP with no TLS option anywhere in the codebase. The
login POST carries the password in the clear, and the session cookie is equally readable.

Anyone able to observe traffic between the operator's browser and the server — another
device on the LAN, a compromised access point, anyone on the same WiFi — recovers the
password in full. Because of finding 3, that password is also the players' password.

**Reproduce:** `tcpdump -i any -A 'tcp port 8080'` on the server, then log in from a
browser. The password appears in plaintext in the POST body.

**Fix:** support HTTPS with a self-signed certificate generated on first run, and default
to it. Redirect HTTP to HTTPS, or refuse to serve the login form over HTTP unless the
operator explicitly opts in with `--insecure-http` for localhost-only use.

### 2. Web login has no rate limiting

`handle_login` calls `await asyncio.sleep(1.0)` on failure and nothing else. That delay is
per-request, not per-client: an attacker opening 100 concurrent connections gets 100
guesses per second, and aiohttp will happily serve them.

Contrast with the UDP handshake, which *is* properly rate limited
(`server/sessions.py::_RateLimiter`, 5 attempts per 60 s then a 300 s lockout). The web
path bypasses that entirely.

**Reproduce:** loop 200 parallel POSTs to `/api/login` with different passwords and observe
that none are refused or delayed beyond the fixed 1 s.

**Fix:** put the existing `_RateLimiter` in front of `handle_login`, keyed on
`request.remote`. Reuse rather than reimplement — it already handles windows and lockout.

---

## Medium severity

### 3. One password for two trust levels

`WebState.check_password` compares against `config.password`, the same secret
`SessionManager` uses for clients. A player who legitimately knows the password to connect
a controller also has full operator access: approve/deny clients, reassign adapters, put
adapters into pairing mode.

**Fix:** a separate `admin_password` for the web GUI, defaulting to the shared password
only if unset (so existing setups keep working) with a startup warning.

### 4. No HTTP security headers

No `X-Frame-Options`, `Content-Security-Policy`, `X-Content-Type-Options`, or
`Referrer-Policy` on any response. The GUI can be framed by a hostile page, and an
operator with a live session could be tricked into clicking Approve or Deny.

**Fix:** an aiohttp middleware adding `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and a CSP of
`default-src 'self'` — the UI already uses no external resources, so a strict policy costs
nothing.

### 5. Auth middleware fails open

```python
if request.path in PUBLIC_PATHS or not request.path.startswith("/api"):
    return await handler(request)
```

Anything not under `/api` is unauthenticated **by default**. `/ws` is only safe because it
re-checks the token itself; a future endpoint added outside `/api` would be public with no
visible mistake at the point of addition.

**Fix:** invert to an allow-list — everything requires auth unless explicitly listed in
`PUBLIC_PATHS`. Fail closed.

### 6. Broker: no rate limiting, no relay bandwidth cap

`rendezvous/broker.py` caps message size (`MAX_MESSAGE`) and room count (`MAX_ROOMS`), but
once a relay pair is established it forwards without limit, and there is no per-source
rate limit on signalling.

The relay is not an open relay — `_share_a_room` requires both peers registered in the same
room, and `tests/test_broker.py` covers that. But a single attacker can register two peers
in one room and use the broker as a bandwidth amplifier at its operator's expense.

**Fix:** per-source-address token bucket on signalling; per-relay-pair byte and packet-rate
caps with the pair torn down on breach.

### 7. Password visible in the process list

`--password` on the command line is readable by any local user via `ps`. `RBGC_PASSWORD`
and the systemd password-file are documented and preferred, but the flag invites the
insecure path.

**Fix:** keep the flag but print a warning when it is used, and scrub `sys.argv[]` after
parsing where the platform allows.

---

## Low severity

### 8. Web GUI binds `0.0.0.0` by default
`web_host` defaults to all interfaces. Combined with findings 1–3, the admin interface is
reachable from the whole network out of the box. **Fix:** default to `127.0.0.1` and make
exposure explicit; document an SSH tunnel as the recommended remote-access path.

### 9. `_pending_hello` cleared wholesale
At 64 entries the dict is emptied entirely, discarding legitimate in-flight handshakes. An
attacker can force this cheaply with spoofed HELLOs. Impact is bounded — clients retry —
but it is a free denial of service. **Fix:** evict oldest-first instead of clearing.

### 10. Handshake rate limiting keyed on IP alone
An attacker with many source addresses is unaffected, while users behind CGNAT share one
bucket and can lock each other out. Inherent to IP-based limiting; worth documenting rather
than over-engineering.

### 11. Discovery beacon answers unauthenticated probes — **accepted risk**
`server/discovery.py` replies to any LAN broadcast with server name, port, capacity and
in-use count. No secret is exposed and the port is already discoverable by scanning. The
usability gain (no typing IP addresses) justifies it. Disable with `--no-discovery`.

### 12. No audit log
Approve, deny, adapter enable/disable and pairing-mode changes are logged at INFO but not
to a durable, separate audit trail. **Fix:** an append-only log of operator actions with
timestamp and source address.

---

## Verified correct

These were examined closely and found sound. Recorded so future changes have a baseline.

**Cryptography** (`common/crypto.py`)
- Argon2id via libsodium with `INTERACTIVE` limits, run **once at startup**, not per
  handshake — so a handshake flood cannot be turned into CPU exhaustion.
- Session keys mix fresh 32-byte randoms from **both** peers, so a captured transcript
  cannot decrypt any other session.
- ChaCha20-Poly1305 with a 64-bit counter and **distinct per-direction nonce prefixes**.
  The two directions share a key but can never collide on `(key, nonce)` — the one failure
  that would be catastrophic. Covered by `test_directions_use_disjoint_nonce_space`.
- The counter travels in the clear but is authenticated as associated data, so it cannot be
  manipulated to force nonce reuse.
- Auth proof is bound to client id and protocol version, blocking both proof replay across
  clients and silent downgrade. Verified with `hmac.compare_digest` (constant time).
- Counter exhaustion raises rather than wrapping.

**Replay and session integrity**
- 64-packet sliding window (`protocol.ReplayWindow`), wrap-safe, with out-of-order
  tolerance — necessary because UDP reorders routinely.
- **NAT rebinding cannot be abused to hijack a session.** A session only moves to a new
  address if the datagram both decrypts *and* passes the replay window, so a captured
  packet replayed from a spoofed address is rejected. Covered by
  `test_replayed_packet_cannot_move_a_session`.

**Input validation**
- Every network-facing parser (`decode_input_into`, `decode_control`, `decode_heartbeat`,
  broker JSON, discovery) validates length and type, raises `ValueError` on malformed
  input, and every caller treats that as "drop the packet". Anyone can send a datagram, so
  malformed input is an expected condition rather than an error path.
- Control messages are size-capped below the minimum path MTU.

**Web UI injection**
- Every client-supplied string rendered by the GUI (`username`, `client_name`,
  `device_name`, `address`) passes through `escapeHtml`, which escapes `& < > " '` — correct
  for both element and quoted-attribute contexts.
- `client_id` is hex by construction. No `innerHTML` sink receives unescaped input.

**Command execution**
- All shell-outs (`hciconfig`, `bluetoothctl`) use list arguments, never `shell=True`.
- `bd_addr` values from the web API are looked up in the known-adapter dict before use, so
  an arbitrary string never reaches a command line.

**No outbound data**
- No telemetry, analytics, update check, or crash reporting exists anywhere in the
  codebase. The only network operations are `getaddrinfo` and the project's own sockets.
  Verified statically and by packet capture — see the egress section of the audit report.

**Broker confidentiality**
- The broker never receives the password and cannot decrypt relayed traffic: packets are
  AEAD-sealed end to end before they reach it. It learns only which addresses wish to
  communicate, and the room code.

---

## What was fixed, and what remains

**Fixed** (each with a regression test in `tests/test_security.py`):

- **2** — the UDP handshake's `_RateLimiter` now guards `/api/login`, keyed on source
  address. Verified against *parallel* attempts, which is what defeated the old sleep.
- **3** — separate `admin_password` (CLI `--admin-password`, env `RBGC_ADMIN_PASSWORD`).
  Falls back to the client password when unset so existing setups keep working, with a
  startup warning. Neither password is ever persisted.
- **4** — security-headers middleware on every response including errors. CSP is strict
  (`default-src 'self'`, `frame-ancestors 'none'`), which costs nothing since the UI loads
  no external resources.
- **5** — middleware inverted to an allow-list. A new endpoint is now protected by default;
  the regression test adds a route outside `/api` and asserts it returns 401.
- **8** — `web_host` defaults to `127.0.0.1`.
- **9** — oldest-first eviction instead of clearing the table.
- **1 (High)** — **TLS is now on by default.** A self-signed P-256 certificate is generated
  on first run covering localhost, the hostname and every detected LAN address; the SHA-256
  fingerprint is printed at startup so the operator can verify it once. TLS 1.2 floor,
  forward-secrecy ciphers only, compression disabled. The session cookie gains `Secure` and
  HSTS is sent — both only under TLS, since either on a plain-HTTP origin would lock the
  operator out. Operator-supplied certificates are honoured and never regenerated.
  Disable with `--no-tls` if you genuinely want plaintext on loopback.

  **Residual risk:** self-signed means a browser warning, and it stops *passive*
  interception but not an active attacker who substitutes their own certificate — unless
  the operator checks the printed fingerprint the first time. That check is the whole
  reason the fingerprint is logged.

**Mitigated but not eliminated:**

- **7** — `--password` now logs a warning naming `ps` as the exposure. The flag still
  exists because removing it would break existing scripts.

**Still open:**

- **6 (Medium)** — broker rate limiting and relay caps. Deferred because the broker is not
  currently deployed anywhere public; it matters as soon as one is.
- **10, 12 (Low)** — IP-keyed rate limiting is inherent; audit logging is unimplemented.

### 13. Nonce counter increment was not atomic across threads

`SessionCrypto.encrypt()` read and incremented `send_counter` across several bytecodes, so
the GIL did **not** make it atomic. The web/asyncio thread already called `send_control()`
while the datapath thread sent acks; adding rumble put a third thread (Bluetooth) on the
same path. Two threads could read the same counter and emit the **same nonce**, which under
ChaCha20-Poly1305 leaks the XOR of both plaintexts and enables forgery.

Found while designing rumble feedback rather than by exploitation, and never observed in
the wild — but it was reachable in normal operation, not a theoretical race.

**Fixed:** a lock around counter reservation and nonce assembly, held only for those steps
and not across the AEAD call itself. Uncontended cost is tens of nanoseconds.

## Recommendations beyond the code

- **The Pi has no firewall.** `nft` ruleset empty, `iptables-save` not installed. UDP 47800,
  UDP 47801 and TCP 8080 are exposed to the whole LAN. Not a code defect, but the single
  biggest environmental risk. A minimal nftables ruleset allowing only SSH, the game port
  and the web port from trusted subnets would help considerably.
- **Update `pip` and `setuptools`** on both machines to clear the 15 tooling CVEs.
- **Prefer the systemd password file** over `--password`; it is already the documented path
  in `packaging/rbgc-server.service`.
