"""What the adapters claim to *be*, as opposed to what they can *do*.

A profile decides the HID report descriptor -- the shape of the data a console
receives. An identity decides how we introduce ourselves before any of that
happens: the advertised name, and the vendor and product ids in the DeviceID
SDP record. The two are independent, and conflating them would be a mistake:
changing identity must never change the report layout underneath it.

Why this exists
---------------

Some consoles do not present a list of nearby devices to choose from. You press
their pairing button and they scan, then connect to whatever they recognise as
a controller. The Analogue 3D is one of these. A controller it does not
recognise is not rejected with a message -- it is simply never connected to,
which is indistinguishable from being out of range or switched off.

Two filters, at two different moments, and it matters which is which:

  * **During inquiry**, before any connection exists, a console can only see
    the class of device, the name (in the extended inquiry response) and any
    advertised service UUIDs. Our class is already a gamepad, so the **name** is
    the discriminator here.
  * **After connecting**, it can read our SDP records, including DeviceID --
    which is where the **vendor and product ids** come in.

So an identity sets both, because either can be the thing standing in the way.

Accuracy, and how much to trust these
-------------------------------------

The vendor ids below are the real, well-known USB vendor ids for those
companies. The product ids are for one representative model each and are
**best-effort**: a vendor ships many, and a console that checks the product id
as well as the vendor may want a specific one. If a console still refuses, the
product id is the first thing to vary -- which is exactly why this is a table
and not a hardcoded pair.

Impersonating a vendor is a real trade, and worth stating plainly: a host that
applies vendor-specific quirks may then expect behaviour our HID layer does not
implement. The generic identity avoids that entirely, and is still the default.
"""

from __future__ import annotations

from dataclasses import dataclass

#: DeviceID vendor id sources, from the Bluetooth DeviceID specification.
VENDOR_SOURCE_BLUETOOTH_SIG = 0x0001
VENDOR_SOURCE_USB = 0x0002


@dataclass(frozen=True, slots=True)
class ControllerIdentity:
    """One way of introducing ourselves to a console."""

    key: str
    display_name: str

    #: Advertised Bluetooth name. Adapters append their number to this, so four
    #: dongles are distinguishable to a host that shows names at all.
    device_name: str

    vendor_id: int
    product_id: int

    #: Almost always USB: controller vendors register a USB vendor id and reuse
    #: it over Bluetooth. A host that reads the source and expects SIG ids will
    #: not match a USB one, so this is part of the identity rather than fixed.
    vendor_source: int = VENDOR_SOURCE_USB

    #: Device release number, as a BCD-ish uint16. Rarely checked.
    version: int = 0x0100

    #: Shown under the name in the web GUI, so the operator can tell which one
    #: to reach for without leaving the page.
    note: str = ""


#: Ordered for the dropdown: the safe default first, then the one most likely to
#: satisfy a console that filters, then the rest.
IDENTITIES: tuple[ControllerIdentity, ...] = (
    ControllerIdentity(
        key="generic",
        display_name="Generic Bluetooth gamepad",
        device_name="RBGC Gamepad",
        # Linux Foundation's vendor id with a generic product id. Deliberately
        # not impersonating anyone: nothing applies quirks to this, so it is
        # the identity least likely to make a host expect something we do not
        # do. It is also the one a filtering console will ignore.
        vendor_id=0x1D6B,
        product_id=0x0246,
        note="Honest and quirk-free. Works with PCs, phones and generic receivers.",
    ),
    ControllerIdentity(
        key="8bitdo",
        display_name="8BitDo controller",
        device_name="8BitDo 64 gamepad",
        vendor_id=0x2DC8,
        product_id=0x3106,
        note=(
            "Try this first with a console that only supports specific pads -- "
            "the Analogue 3D's official controller is an 8BitDo."
        ),
    ),
    ControllerIdentity(
        key="xbox",
        display_name="Xbox Wireless Controller",
        device_name="Xbox Wireless Controller",
        vendor_id=0x045E,
        product_id=0x0B13,
        note="Widely accepted by PCs and anything supporting Xbox pads.",
    ),
    ControllerIdentity(
        key="ps5",
        display_name="PlayStation DualSense",
        # What a DualSense actually advertises. Unhelpfully generic, and
        # correct -- a console matching on the name wants exactly this.
        device_name="Wireless Controller",
        vendor_id=0x054C,
        product_id=0x0CE6,
        note="Advertises as 'Wireless Controller', which is what a real one does.",
    ),
    ControllerIdentity(
        key="ps4",
        display_name="PlayStation DualShock 4",
        device_name="Wireless Controller",
        vendor_id=0x054C,
        product_id=0x09CC,
        note="Older PlayStation pad. Some hosts accept this where DualSense fails.",
    ),
    ControllerIdentity(
        key="switch_pro",
        display_name="Nintendo Switch Pro Controller",
        device_name="Pro Controller",
        vendor_id=0x057E,
        product_id=0x2009,
        note=(
            "Identity only. For a real Switch, also set the adapter's profile "
            "to Switch Pro so the report format matches what it expects."
        ),
    ),
    ControllerIdentity(
        key="razer",
        display_name="Razer gamepad",
        device_name="Razer Raiju",
        vendor_id=0x1532,
        product_id=0x1000,
        note="Razer's vendor id with a representative product id.",
    ),
    ControllerIdentity(
        key="gamesir",
        display_name="GameSir controller",
        device_name="GameSir Controller",
        # Less well attested than the others above; if a console refuses this
        # one, the product id is the first thing worth changing.
        vendor_id=0x3537,
        product_id=0x1001,
        note="Vendor id is less well attested than the others -- try 8BitDo first.",
    ),
)

DEFAULT_IDENTITY = "generic"

_BY_KEY = {identity.key: identity for identity in IDENTITIES}


def get_identity(key: str) -> ControllerIdentity:
    """Look one up, falling back to the generic identity.

    Never raises: an unknown key in a config file (an older build, a typo, a
    preset removed later) must leave the adapters working as a plain gamepad
    rather than refusing to bring Bluetooth up at all.
    """
    return _BY_KEY.get(key or DEFAULT_IDENTITY, _BY_KEY[DEFAULT_IDENTITY])


def identity_choices() -> list[dict[str, str]]:
    """The list the web GUI renders, in display order."""
    return [
        {
            "key": identity.key,
            "name": identity.display_name,
            "device_name": identity.device_name,
            "vendor": f"{identity.vendor_id:04X}:{identity.product_id:04X}",
            "note": identity.note,
        }
        for identity in IDENTITIES
    ]
