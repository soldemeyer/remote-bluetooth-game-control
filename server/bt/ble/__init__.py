"""Bluetooth Low Energy peripheral: HID over GATT.

A **second transport**, parallel to the Classic stack in ``server/bt/``, not a
layer on top of it. The two share the profile layer -- the report descriptor
and the bytes of each report are identical -- and nothing else. Classic carries
them over L2CAP PSM 17/19 with an SDP record; BLE carries them as GATT
notifications on HID Service 0x1812.

It exists because measurement forced it: the Analogue 3D's controller
advertises ``BR/EDR Not Supported`` and HID ``0x1812``, so that console cannot
see a Classic device at all, however perfect its advertisement. See "The
Analogue 3D is BLE" in CLAUDE.md.

Nothing here is imported at server start-up unless BLE is enabled, so a machine
without the server extras is unaffected. ``hogp`` is stdlib-only and safe to
import anywhere; everything else needs dbus-next.
"""
