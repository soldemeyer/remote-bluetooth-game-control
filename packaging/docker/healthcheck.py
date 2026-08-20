"""Liveness probe for the rendezvous broker.

Sends the one operation the broker answers without any prior state -- `list`,
which a client uses to browse public rooms -- and requires a well-formed reply.

Deliberately a real round trip on the service port rather than a process check:
the failure this has to catch is the socket being bound but unanswered, and a
`pgrep` cannot tell those apart. It reveals nothing either, since `list` only
ever returns rooms that opted in to being public.

Exits 0 when healthy, 1 otherwise, as Docker's HEALTHCHECK expects.
"""

from __future__ import annotations

import json
import os
import socket
import sys

TIMEOUT_S = 3.0


def main() -> int:
    port = int(os.environ.get("RBGC_BROKER_PORT", "47900"))
    # Loopback: the probe runs inside the container, and asking over the
    # published address would test the host's port mapping rather than the
    # broker itself.
    host = os.environ.get("RBGC_HEALTHCHECK_HOST", "127.0.0.1")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(TIMEOUT_S)
            sock.sendto(json.dumps({"op": "list"}).encode(), (host, port))
            data, _ = sock.recvfrom(65535)
    except OSError as exc:
        print(f"broker did not answer on {host}:{port}: {exc}", file=sys.stderr)
        return 1

    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"broker replied with something unparseable: {exc}", file=sys.stderr)
        return 1

    if message.get("op") != "servers":
        print(f"unexpected reply: {message!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
