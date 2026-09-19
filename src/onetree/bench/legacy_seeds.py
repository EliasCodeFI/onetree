"""Historical SHA-derived corpus keys, retained solely to verify earlier receipts.

The v2 format and the unpublished policy-v3 format name this derivation. Their corpus
bytes must retain that interpretation; new runs use the accepted SeedSequence contract.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable

DERIVATION = "sha256(run_seed '/' key '/' key ...) -> 64-bit seed for random.Random"


class SeedKey:
    """One node of the spawn tree: a run seed plus the key path that reached this node."""

    __slots__ = ("_path", "_seed")

    def __init__(self, seed: int, path: Iterable[str] = ()) -> None:
        if isinstance(seed, bool) or seed < 0:
            raise ValueError("run seed must be a non-negative integer")
        self._seed = seed
        self._path = tuple(str(part) for part in path)
        for part in self._path:
            if not part or "/" in part:
                raise ValueError("key path parts must be non-empty and contain no '/'")

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def path(self) -> tuple[str, ...]:
        return self._path

    def spawn(self, *keys: str) -> SeedKey:
        """A child key in the historical receipt's canonical string-key domain."""

        return SeedKey(self._seed, (*self._path, *keys))

    def digest(self) -> str:
        return hashlib.sha256("/".join((str(self._seed), *self._path)).encode()).hexdigest()

    def rng(self) -> random.Random:
        """A fresh generator for this node; calling twice yields identical streams."""

        return random.Random(int(self.digest()[:16], 16))  # noqa: S311 - not security

    def __repr__(self) -> str:
        return f"SeedKey({self._seed}, {'/'.join(self._path)!r})"


__all__ = ["DERIVATION", "SeedKey"]
