"""A UDP path you can make worse on purpose.

Phases 8, 9 and 23 all need the same missing capability: **deliberately
degrading the network**. Congestion control cannot be measured on a path that
never congests, loss recovery cannot be measured on a path that never loses,
and "does latency recover or accumulate" cannot be answered without something
to recover from.

It sits between a client and a server and re-originates every datagram, the
same shape as the ``UdpForwarder`` the broker tests already use to stand in for
frp::

    client ---> impair.py ---> server
           <---           <---

Both directions are impaired independently, because they are not symmetric: an
uplink carrying 8 Mbps of video and a downlink carrying 30-byte acks fail in
completely different ways.

**It is a measurement instrument, so it must not lie.** Every impairment is
counted and reported, and the counters are what a test asserts on -- "I asked
for 2% loss" is a request, "it dropped 214 of 10,432" is the measurement.

Usage:
    python -m tools.impair --listen 47899 --target 127.0.0.1:47800 --loss 2
    python -m tools.impair --listen 47816 --target 127.0.0.1:47810 \\
                           --jitter 20 --rate 6000
"""

from __future__ import annotations

import argparse
import heapq
import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, field

from common.timing import now_ns

log = logging.getLogger(__name__)

#: Long enough that the release thread is not a busy loop, short enough that a
#: sub-millisecond release schedule is still honoured.
_TICK_S = 0.0005


@dataclass(slots=True)
class Impairment:
    """What to do to a path. Every field is independent of the others."""

    #: Fraction of datagrams to drop outright, 0.0-1.0.
    loss: float = 0.0
    #: Fixed one-way delay added to every datagram, milliseconds.
    delay_ms: float = 0.0
    #: Extra delay drawn uniformly from [0, jitter_ms). Reordering follows from
    #: this naturally, exactly as it does on a real path.
    jitter_ms: float = 0.0
    #: Fraction of datagrams to deliver twice.
    duplicate: float = 0.0
    #: Bandwidth ceiling in kilobits per second. 0 means unlimited. Enforced
    #: with a token bucket, so a burst is buffered rather than shredded -- a
    #: rate limiter that simply drops is a loss generator wearing a hat, and
    #: the queueing is the thing congestion control has to notice.
    rate_kbps: float = 0.0
    #: Bytes the token bucket may hold. Datagrams beyond it are dropped, which
    #: is what a real queue does when it is full.
    burst_bytes: int = 64 * 1024

    def describe(self) -> str:
        parts = []
        if self.loss:
            parts.append(f"loss {self.loss * 100:.1f}%")
        if self.delay_ms:
            parts.append(f"delay {self.delay_ms:.0f} ms")
        if self.jitter_ms:
            parts.append(f"jitter {self.jitter_ms:.0f} ms")
        if self.duplicate:
            parts.append(f"dup {self.duplicate * 100:.1f}%")
        if self.rate_kbps:
            parts.append(f"cap {self.rate_kbps:.0f} kbps")
        return ", ".join(parts) or "clean"


@dataclass(slots=True)
class Counters:
    forwarded: int = 0
    dropped_loss: int = 0
    dropped_queue: int = 0
    duplicated: int = 0
    bytes_forwarded: int = 0
    delay_applied_ms: float = 0.0

    def snapshot(self) -> dict[str, float | int]:
        offered = self.forwarded + self.dropped_loss + self.dropped_queue
        return {
            "offered": offered,
            "forwarded": self.forwarded,
            "dropped_loss": self.dropped_loss,
            "dropped_queue": self.dropped_queue,
            "duplicated": self.duplicated,
            "bytes_forwarded": self.bytes_forwarded,
            "loss_pct": (
                (self.dropped_loss + self.dropped_queue) / offered * 100 if offered else 0.0
            ),
            "mean_delay_ms": (
                self.delay_applied_ms / self.forwarded if self.forwarded else 0.0
            ),
        }


class _TokenBucket:
    """Bandwidth cap that queues rather than shreds. Not thread-safe by itself."""

    __slots__ = ("_rate_bytes", "_capacity", "_tokens", "_last_ns")

    def __init__(self, rate_kbps: float, burst_bytes: int) -> None:
        self._rate_bytes = rate_kbps * 1000 / 8
        self._capacity = float(burst_bytes)
        self._tokens = float(burst_bytes)
        self._last_ns = now_ns()

    def take(self, size: int) -> float | None:
        """Seconds to wait before this datagram may go, or None to drop it."""
        now = now_ns()
        elapsed = (now - self._last_ns) / 1e9
        self._last_ns = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_bytes)

        if self._tokens >= size:
            self._tokens -= size
            return 0.0

        deficit = size - self._tokens
        wait = deficit / self._rate_bytes
        # A datagram that would have to wait longer than the bucket could ever
        # hold is what an overfull queue looks like: dropped, not delayed
        # forever.
        if deficit > self._capacity:
            return None
        self._tokens -= size          # goes negative; repaid by the wait
        return wait


class _Direction:
    """One way of the path: its impairment, its bucket, its counters."""

    def __init__(self, name: str, impairment: Impairment, rng: random.Random) -> None:
        self.name = name
        self.impairment = impairment
        self.counters = Counters()
        self._rng = rng
        #: Rebuilt when the rate changes, rather than fixed at construction.
        #:
        #: `loss` and `jitter` are read per datagram, so mutating them mid-run
        #: works; `rate_kbps` was baked into a bucket built once, so changing it
        #: silently did nothing. A scenario that turned congestion on halfway
        #: through therefore measured a completely clean path and reported it as
        #: a pass -- an instrument that ignores a setting is worse than one that
        #: refuses it.
        self._bucket: _TokenBucket | None = None
        self._bucket_rate: float = -1.0

    def _current_bucket(self) -> "_TokenBucket | None":
        """The bucket for the rate as it is *now*, rebuilding if it changed."""
        rate = self.impairment.rate_kbps
        if rate != self._bucket_rate:
            self._bucket_rate = rate
            self._bucket = (
                _TokenBucket(rate, self.impairment.burst_bytes) if rate else None
            )
        return self._bucket

    def schedule(self, size: int) -> tuple[bool, float, bool]:
        """Decide this datagram's fate.

        Returns ``(deliver, delay_seconds, duplicate)``.
        """
        imp = self.impairment
        if imp.loss and self._rng.random() < imp.loss:
            self.counters.dropped_loss += 1
            return False, 0.0, False

        wait = 0.0
        bucket = self._current_bucket()
        if bucket is not None:
            queued = bucket.take(size)
            if queued is None:
                self.counters.dropped_queue += 1
                return False, 0.0, False
            wait = queued

        delay = imp.delay_ms / 1000.0
        if imp.jitter_ms:
            delay += self._rng.random() * imp.jitter_ms / 1000.0

        duplicate = bool(imp.duplicate) and self._rng.random() < imp.duplicate
        total = wait + delay
        self.counters.forwarded += 1
        self.counters.bytes_forwarded += size
        self.counters.delay_applied_ms += total * 1000.0
        return True, total, duplicate


class ImpairedRelay:
    """A degradable UDP path between a client and one server.

    One client at a time, which is what every measurement here needs. A second
    client would silently share the first one's return path, so the address is
    latched and a stranger is ignored rather than quietly corrupting the run.
    """

    def __init__(
        self,
        target: tuple[str, int],
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        to_server: Impairment | None = None,
        to_client: Impairment | None = None,
        seed: int | None = None,
    ) -> None:
        self._target = target
        self._rng = random.Random(seed)
        self.to_server = _Direction("to-server", to_server or Impairment(), self._rng)
        self.to_client = _Direction("to-client", to_client or Impairment(), self._rng)

        self._front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._front.bind((listen_host, listen_port))
        self._front.settimeout(0.05)
        self._back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._back.bind((listen_host, 0))
        self._back.settimeout(0.05)

        #: Latched at construction, not read from the socket on demand. A
        #: measurement instrument has to keep reporting after it is switched
        #: off -- the counters are usually read once the run has finished, and
        #: `getsockname` on a closed socket raises.
        self._address: tuple[str, int] = self._front.getsockname()

        self._client: tuple[str, int] | None = None
        self._stop = threading.Event()
        #: (release_ns, sequence, socket, payload, address). The sequence keeps
        #: heapq from comparing bytes when two datagrams share a release time.
        self._queue: list[tuple[int, int, socket.socket, bytes, tuple[str, int]]] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        return self._address[1]

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for target in (self._pump_front, self._pump_back, self._release):
            thread = threading.Thread(target=target, name=f"impair-{target.__name__}",
                                      daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info(
            "Impairing %s:%d -> %s:%d  [to server: %s] [to client: %s]",
            *self.address, *self._target,
            self.to_server.impairment.describe(),
            self.to_client.impairment.describe(),
        )

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        self._front.close()
        self._back.close()

    # -- the three threads -------------------------------------------------

    def _pump_front(self) -> None:
        while not self._stop.is_set():
            try:
                data, source = self._front.recvfrom(65535)
            except (TimeoutError, socket.timeout, OSError):
                continue
            if self._client is None:
                self._client = source
                log.info("Client %s:%d attached", *source)
            elif source != self._client:
                continue        # a stranger; see the class docstring
            self._offer(self.to_server, self._back, data, self._target)

    def _pump_back(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._back.recvfrom(65535)
            except (TimeoutError, socket.timeout, OSError):
                continue
            client = self._client
            if client is None:
                continue
            self._offer(self.to_client, self._front, data, client)

    def _offer(self, direction, sock, data: bytes, address) -> None:
        deliver, delay_s, duplicate = direction.schedule(len(data))
        if not deliver:
            return
        if delay_s <= 0:
            self._send(sock, data, address)
        else:
            self._enqueue(now_ns() + int(delay_s * 1e9), sock, data, address)
        if duplicate:
            direction.counters.duplicated += 1
            self._enqueue(now_ns() + int(delay_s * 1e9), sock, data, address)

    def _enqueue(self, release_ns: int, sock, data: bytes, address) -> None:
        with self._lock:
            self._seq += 1
            heapq.heappush(self._queue, (release_ns, self._seq, sock, data, address))

    def _release(self) -> None:
        while not self._stop.is_set():
            now = now_ns()
            ready = []
            with self._lock:
                while self._queue and self._queue[0][0] <= now:
                    ready.append(heapq.heappop(self._queue))
            for _release_ns, _seq, sock, data, address in ready:
                self._send(sock, data, address)
            if not ready:
                time.sleep(_TICK_S)

    @staticmethod
    def _send(sock, data: bytes, address) -> None:
        try:
            sock.sendto(data, address)
        except OSError:
            pass        # the far end went away; the run is over either way

    def snapshot(self) -> dict[str, object]:
        return {
            "listen": f"{self.address[0]}:{self.address[1]}",
            "target": f"{self._target[0]}:{self._target[1]}",
            "to_server": self.to_server.counters.snapshot(),
            "to_client": self.to_client.counters.snapshot(),
        }


def _impairment_from(args, prefix: str = "") -> Impairment:
    def get(name, default=0.0):
        return getattr(args, f"{prefix}{name}", None) or default

    return Impairment(
        loss=get("loss") / 100.0,
        delay_ms=get("delay"),
        jitter_ms=get("jitter"),
        duplicate=get("duplicate") / 100.0,
        rate_kbps=get("rate"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="impair",
        description="Sit between a client and a server and degrade the path.",
    )
    parser.add_argument("--listen", type=int, default=0, help="local UDP port")
    parser.add_argument("--target", required=True, help="host:port to forward to")
    parser.add_argument("--loss", type=float, default=0.0, help="percent")
    parser.add_argument("--delay", type=float, default=0.0, help="ms, each way")
    parser.add_argument("--jitter", type=float, default=0.0, help="ms, uniform")
    parser.add_argument("--duplicate", type=float, default=0.0, help="percent")
    parser.add_argument("--rate", type=float, default=0.0, help="kbps ceiling")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    host, _, port = args.target.partition(":")
    impairment = _impairment_from(args)
    relay = ImpairedRelay(
        (host, int(port)),
        listen_port=args.listen,
        to_server=impairment,
        to_client=impairment,
        seed=args.seed,
    )
    relay.start()
    print(f"  impairing  {relay.address[0]}:{relay.address[1]}  ->  {host}:{port}")
    print(f"  each way:  {impairment.describe()}")
    print("  point the client at the address above.\n")

    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        snap = relay.snapshot()
        for name in ("to_server", "to_client"):
            counts = snap[name]
            print(
                f"  {name:9} offered {counts['offered']:7d}  "
                f"forwarded {counts['forwarded']:7d}  "
                f"loss {counts['loss_pct']:5.2f}%  "
                f"mean delay {counts['mean_delay_ms']:6.2f} ms"
            )
        relay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
