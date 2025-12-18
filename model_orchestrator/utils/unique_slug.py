from __future__ import annotations

import secrets
import string
from typing import Iterable, Set, Optional

from coolname import generate_slug


_ALPH = string.ascii_lowercase + string.digits


def _suffix(k: int = 5) -> str:
    return "".join(secrets.choice(_ALPH) for _ in range(k))


def generate_unique_coolname(existing_slugs: Iterable[str], *, max_tries: int = 30) -> str:
    """
    Returns a coolname slug that is not in `existing_slugs`.

    - Tries `max_tries` times with plain coolname generation.
    - Then falls back to appending a random suffix to guarantee uniqueness.
    """
    existing: Set[str] = set(existing_slugs)

    for _ in range(max_tries):
        slug = generate_slug(2)  # e.g. "silent-otter"
        if slug not in existing:
            return slug

    # Fallback: force uniqueness with suffix
    while True:
        slug = f"{generate_slug(2)}-{_suffix()}"
        if slug not in existing:
            return slug