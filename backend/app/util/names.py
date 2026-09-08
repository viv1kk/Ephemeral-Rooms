"""Friendly default display names (spec section 4). Cosmetic only."""

from __future__ import annotations

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
    client without the server having to broadcast a palette."""
    digest = sum(ord(c) * (i + 1) for i, c in enumerate(user_id))
    hue = digest % 360
    return f"hsl({hue}, 68%, 62%)"
