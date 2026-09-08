"""Friendly default display names (spec section 4). Cosmetic only."""

from __future__ import annotations

import colorsys
import secrets

_ADJECTIVES = (
    "Blue", "Quiet", "Red", "Swift", "Amber", "Calm", "Bold", "Green",
    "Bright", "Silver", "Gentle", "Rapid", "Violet", "Clever", "Golden",
    "Steady", "Teal", "Lucky", "Brave", "Crimson",
)

_ANIMALS = (
    "Fox", "Panda", "Wolf", "Otter", "Heron", "Lynx", "Falcon", "Badger",
    "Marten", "Ibis", "Tapir", "Osprey", "Gecko", "Bison", "Puffin",
    "Marmot", "Sable", "Crane", "Stoat", "Raven",
)


def random_display_name() -> str:
    return f"{secrets.choice(_ADJECTIVES)} {secrets.choice(_ANIMALS)}"


def color_for(user_id: str) -> str:
    """A stable colour derived from the user's UUID (spec section 6).

    Hue is taken from the id so the same user is the same colour on every
    client without the server having to broadcast a palette.

    Returned as hex, not `hsl(...)`, and that matters. y-codemirror.next paints
    a remote *selection* by appending an alpha suffix to this string
    (`color + '33'`). "hsl(274, 68%, 62%)33" is not valid CSS, so the browser
    silently drops the declaration and the selection highlight renders
    transparent while every element is still present in the DOM. "#rrggbb33"
    is valid eight-digit hex."""
    digest = sum(ord(c) * (i + 1) for i, c in enumerate(user_id))
    hue = digest % 360
    # Same perceptual target as the previous hsl(hue, 68%, 62%).
    red, green, blue = colorsys.hls_to_rgb(hue / 360, 0.62, 0.68)
    return "#{:02x}{:02x}{:02x}".format(
        round(red * 255), round(green * 255), round(blue * 255)
    )
