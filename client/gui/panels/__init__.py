"""The client's control panels, as widgets that own what they build.

`MainWindow` used to construct every widget in these three groups itself,
which is how one class came to hold widget construction, session state, config
persistence and network callbacks at once. A panel owns its own widgets and
exposes them by name; the window keeps the behaviour and reaches for them
through the panel.

**Construction only.** No panel decides anything: handlers stay on the window
and are passed in, so moving a group here changes where a widget is *made* and
nothing about what it does.
"""

from client.gui.panels.controllers import (
    COL_CONFIG,
    COL_CONFIGURE,
    COL_COUNT,
    COL_GAMEPAD,
    COL_NAME,
    COL_RUMBLE,
    COL_SLOT,
    COL_STATUS,
    COL_TYPE,
    COL_USE,
    ControllersPanel,
)
from client.gui.panels.connection import ConnectionPanel
from client.gui.panels.latency import LatencyPanel

__all__ = [
    "COL_CONFIG", "COL_CONFIGURE", "COL_COUNT", "COL_GAMEPAD", "COL_NAME",
    "COL_RUMBLE", "COL_SLOT", "COL_STATUS", "COL_TYPE", "COL_USE",
    "ConnectionPanel", "ControllersPanel", "LatencyPanel",
]
