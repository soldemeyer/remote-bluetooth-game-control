"""Handing the newest frame to another process, without ever waiting.

Stdlib only, and both halves live here so the whole protocol is testable in
one process before a subprocess is involved.

A SLOT, NOT A QUEUE
--------------------
One fixed buffer holding one frame. A writer always overwrites; a reader
always takes the newest and never sees the same frame twice. That is the same
discipline as ``VideoCapture._pending``, ``L2CAPSink`` and the video
receiver's latest-wins slot, and it is here for the reason it is there: a
worker that falls behind must drop frames, not accumulate a backlog. Identity
does not change in the frames it skipped, and latency that grows without bound
is the failure this project keeps having to find.

The writer is the video server's control thread, which also sends the status
message. It cannot be allowed to block on a busy worker, so nothing here
takes a lock or waits on anything.

THE SEQLOCK, AND WHAT IT HONESTLY GUARANTEES
----------------------------------------------
``seq`` is odd while a write is in progress and even when the buffer is
settled. A reader takes ``seq``, copies, takes ``seq`` again, and keeps the
frame only if both reads were equal and even.

**That detects a torn read; it does not prevent one.** Python offers no memory
barrier and this is genuine shared memory between processes, so on paper a
reader could see a payload byte from one frame beside a header from another
without the sequence changing. In practice the frame is memcpy'd between two
integer stores on the same thread, and the consequence of the rare miss is one
skipped or one visually smeared sample at 6 Hz -- which costs a tracker one
frame of a box being slightly wrong. It is not worth a lock that could block
the writer, and it is worth writing down rather than implying a guarantee
that is not there.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

__all__ = ["FrameSlot", "MAX_PAYLOAD", "MAX_SAMPLE_WIDTH", "SLOT_BYTES"]

#: ``magic, seq, width, height, stride, channels, capture_ts``.
#:
#: Fixed size and fixed offsets: both ends compute where the payload starts
#: from this, so a field added without both ends agreeing would be read as
#: pixel data rather than as a field.
_HEADER = struct.Struct("<IIHHIBxxxQ")
HEADER_BYTES = _HEADER.size

#: Anything without this at offset zero is not our slot -- a stale segment
#: with the same name, or one that was never written.
MAGIC = 0x52424756      # "RBGV"

#: The widest sample any backend may ask for.
#:
#: 1280 because YOLO exports at 1280 are real and somebody will use one. The
#: cap lives here rather than on the backend because the slot is what cannot
#: accommodate a surprise: it is sized once and cannot be resized under a
#: reader.
MAX_SAMPLE_WIDTH = 1280

#: A sample's ``stride`` is **not** ``width * channels`` -- swscale pads rows,
#: and ``write`` checks ``stride * height``. A slot sized from the unpadded
#: product would refuse a frame exactly at the cap, which is the worst place
#: to discover the arithmetic was optimistic.
_STRIDE_SLACK = 64

#: The largest frame the slot can carry. Derived, so raising the width cannot
#: leave this behind -- the failure if it did is silent: ``write`` returns
#: False, ``oversized`` ticks, and every other counter reads healthy.
#:
#: **Allocated only in ``ProcessRunner.start``**, so only when the feature is
#: on *and* the backend is isolated. Off still constructs nothing; this is not
#: five megabytes a switched-off video server carries.
MAX_PAYLOAD = (MAX_SAMPLE_WIDTH * 3 + _STRIDE_SLACK) * MAX_SAMPLE_WIDTH

SLOT_BYTES = HEADER_BYTES + MAX_PAYLOAD


class FrameSlot:
    """One shared frame. Create in the parent, attach in the child."""

    def __init__(self, name: str | None = None, *, create: bool = False) -> None:
        from multiprocessing import shared_memory

        if create:
            self._shm = shared_memory.SharedMemory(create=True, size=SLOT_BYTES)
            self._view = self._shm.buf
            # Stamped before anything can read it, so an attach that races
            # creation sees either nothing or a valid empty slot.
            _HEADER.pack_into(self._view, 0, MAGIC, 0, 0, 0, 0, 0, 0)
        else:
            self._shm = shared_memory.SharedMemory(name=name)
            self._view = self._shm.buf

        self.name = self._shm.name
        self._created = create
        self._last_seq = 0
        self.writes = 0
        self.reads = 0
        self.torn = 0
        self.oversized = 0

    # -- the writing half --------------------------------------------------

    def write(self, data, width: int, height: int, stride: int,
              channels: int, capture_ts: int) -> bool:
        """Publish a frame. Never blocks, never waits, overwrites whatever
        was there.

        ``False`` when the frame does not fit, which is a sizing mistake
        rather than a transient -- counted and reported, because a slot that
        silently accepted nothing would look exactly like a worker that found
        nothing.
        """
        needed = stride * height
        if needed <= 0 or needed > MAX_PAYLOAD:
            self.oversized += 1
            return False

        view = self._view
        seq = self._last_seq + 1            # odd: a write is in progress
        struct.pack_into("<I", view, 4, seq)

        view[HEADER_BYTES:HEADER_BYTES + needed] = data[:needed]
        _HEADER.pack_into(
            view, 0, MAGIC, seq, width, height, stride, channels, capture_ts
        )

        seq += 1                            # even: settled
        struct.pack_into("<I", view, 4, seq)
        self._last_seq = seq
        self.writes += 1
        return True

    # -- the reading half --------------------------------------------------

    def read(self) -> tuple[bytes, int, int, int, int, int] | None:
        """The newest frame, or ``None`` if there is no new one.

        ``None`` is the ordinary answer: the reader polls faster than frames
        arrive, so most calls find nothing and cost two integer reads.
        """
        view = self._view
        first = struct.unpack_from("<I", view, 4)[0]
        if first == 0 or first & 1:
            # Never written, or a write is in progress. Either way, not now.
            return None
        if first == self._last_seq:
            return None                     # already had this one

        magic, seq, width, height, stride, channels, capture_ts = _HEADER.unpack_from(
            view, 0
        )
        if magic != MAGIC:
            return None

        needed = stride * height
        if needed <= 0 or needed > MAX_PAYLOAD:
            return None
        payload = bytes(view[HEADER_BYTES:HEADER_BYTES + needed])

        if struct.unpack_from("<I", view, 4)[0] != first:
            # Overwritten while we copied. Skip it -- the next one is along in
            # a few milliseconds and is fresher anyway.
            self.torn += 1
            return None

        self._last_seq = first
        self.reads += 1
        return payload, width, height, stride, channels, capture_ts

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Detach. The creator also unlinks.

        Both halves must close or the POSIX resource tracker complains at
        exit, and on Windows the segment stays mapped until every handle goes.
        """
        try:
            self._view.release()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._shm.close()
        except Exception:  # noqa: BLE001
            pass
        if self._created:
            try:
                self._shm.unlink()
            except Exception:  # noqa: BLE001 -- already gone is fine
                pass

    def snapshot(self) -> dict[str, int]:
        return {
            "writes": self.writes,
            "reads": self.reads,
            "torn": self.torn,
            "oversized": self.oversized,
        }
