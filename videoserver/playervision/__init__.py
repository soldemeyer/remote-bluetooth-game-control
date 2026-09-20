"""Optional player identification for the video server.

Learns which on-screen entity belongs to each player, follows it, and reports
where it is so clients can draw that player's name above their character.
Game-independent by construction: nothing here holds a character list, a
per-game profile, or a model trained on a particular title.

**Off is the original path.** With ``player_id_enabled`` false, or with the
capture machine's own ``playervision_allowed`` false, nothing in this package
is imported past this module's own name: no frame is sampled, no worker
starts, no model loads, and ``status()`` grows no key. Two switches and two
questions -- the Bluetooth server asks for labels, the capture machine says
whether it is willing to run them -- because "put a model on your GPU" is the
decision of whoever owns the GPU.

What lives where, and why the boundary is drawn there:

  * ``types``      the vocabulary. Stdlib only.
  * ``identity``   who a track belongs to. Arithmetic and bookkeeping, no
                   models, no PyAV -- so the part most likely to be subtly
                   wrong is testable with tuples.
  * ``tracking``   detections to tracks across frames.
  * ``worker``     the driver: a frame and some evidence in, published rows
                   out. Pure, so both runners drive the same one.
  * ``service``    what the video server actually holds. Samples frames and
                   hands results back.
  * ``backends``   the only place a model is ever mentioned.

This package knows the layout and is *told* the viewport-to-player map. It
imports no router, no session and no controller code, which is what keeps
``videoserver/layout.py`` free of them too -- the same three-layer separation
``server/screen_state.py`` documents for split-screen, of which this is the
second half.
"""

from __future__ import annotations

__all__ = ["types"]
