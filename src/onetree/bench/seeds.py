# SeedSequence below is adapted from numpy 2.3.1, numpy/random/bit_generator.pyx, which carries
# this notice:
#
# SeedSequence is derived from Melissa E. O'Neill's C++11 `std::seed_seq`
# implementation, as it has a lot of nice properties that we want.
#
# https://gist.github.com/imneme/540829265469e673d045
# https://www.pcg-random.org/posts/developing-a-seed_seq-alternative.html
#
# The MIT License (MIT)
#
# Copyright (c) 2015 Melissa E. O'Neill
# Copyright (c) 2019 NumPy Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""The hermetic leg's SeedSequence keying (``docs/PREREGISTRATION.md``, decision rules).

``SeedSequence`` must give the words numpy 2.3.1's ``numpy.random.SeedSequence`` gives for a
non-negative integer entropy and a spawn key of non-negative integers, using the standard
library alone: numpy is not a runtime dependency. The tests' reference vectors were generated
by numpy itself, never by this module.

Every corpus choice is an integer-only draw from those words under the spawn key
``(writer, task, purpose)``, and no draw uses ``random``. ``DERIVATION`` names the whole rule in
every new receipt. Historical SHA-derived receipts use a separate compatibility module;
their corpus must never be reinterpreted under this derivation.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator, Sequence

POOL_SIZE = 4
_INIT_A = 0x43B0D7E5
_MULT_A = 0x931E8875
_INIT_B = 0x8B51F9DD
_MULT_B = 0x58F38DED
_MIX_MULT_L = 0xCA01F9DD
_MIX_MULT_R = 0x4973F715
_XSHIFT = 16
_MASK32 = 0xFFFFFFFF
_WORDS = 1 << 32

POOL, PATH, TOKEN, THINK = 0, 1, 2, 3
"""Frozen purpose codes, the last part of every corpus spawn key ``(writer, task, purpose)``."""

DERIVATION = (
    "numpy 2.3.1 SeedSequence(entropy=seed, spawn_key=(writer, task, purpose)) uint32 words;"
    " purposes pool=0 path=1 token=2 think=3;"
    " the hot draw for path slot j is pool word j * d < n * 2**32 for conflict n/d;"
    " a draw of k outcomes is word % k for the first word below 2**32 - 2**32 % k,"
    " k = candidates for paths in slot order, jitter_ms + 1 for think, no think draw at 0;"
    " slot j's token is token word 2j | (token word 2j+1 & 0xffff) << 32 as 12 hex digits"
)


def _uint32_words(value: int) -> list[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("seed entropy and spawn key parts must be integers")
    if value < 0:
        raise ValueError("seed entropy and spawn key parts must be non-negative")
    words = [value & _MASK32]
    value >>= 32
    while value:
        words.append(value & _MASK32)
        value >>= 32
    return words


def _pool(entropy: Sequence[int]) -> tuple[int, ...]:
    hash_const = _INIT_A

    def hashmix(value: int) -> int:
        nonlocal hash_const
        value ^= hash_const
        hash_const = (hash_const * _MULT_A) & _MASK32
        value = (value * hash_const) & _MASK32
        return value ^ (value >> _XSHIFT)

    def mix(x: int, y: int) -> int:
        result = (_MIX_MULT_L * x - _MIX_MULT_R * y) & _MASK32
        return result ^ (result >> _XSHIFT)

    pool = [hashmix(entropy[i] if i < len(entropy) else 0) for i in range(POOL_SIZE)]
    for source in range(POOL_SIZE):
        for target in range(POOL_SIZE):
            if source != target:
                pool[target] = mix(pool[target], hashmix(pool[source]))
    for source in range(POOL_SIZE, len(entropy)):
        for target in range(POOL_SIZE):
            pool[target] = mix(pool[target], hashmix(entropy[source]))
    return tuple(pool)


class SeedSequence:
    __slots__ = ("_entropy", "_pool", "_spawn_key")

    def __init__(self, entropy: int, spawn_key: Iterable[int] = ()) -> None:
        key = tuple(spawn_key)
        run = _uint32_words(entropy)
        spawned = [word for part in key for word in _uint32_words(part)]
        if spawned and len(run) < POOL_SIZE:
            # numpy pads only when a spawn key is present (gh-16539), so an unspawned small
            # entropy keeps its original stream.
            run += [0] * (POOL_SIZE - len(run))
        self._entropy = entropy
        self._spawn_key = key
        self._pool = _pool(run + spawned)

    @property
    def entropy(self) -> int:
        return self._entropy

    @property
    def spawn_key(self) -> tuple[int, ...]:
        return self._spawn_key

    def words(self) -> Iterator[int]:
        """The unbounded uint32 stream whose first ``n`` words are ``generate_state(n)``."""

        hash_const = _INIT_B
        for word in itertools.cycle(self._pool):
            value = word ^ hash_const
            hash_const = (hash_const * _MULT_B) & _MASK32
            value = (value * hash_const) & _MASK32
            yield value ^ (value >> _XSHIFT)

    def generate_state(self, n_words: int) -> list[int]:
        return list(itertools.islice(self.words(), n_words))

    def __repr__(self) -> str:
        return f"SeedSequence({self._entropy}, spawn_key={self._spawn_key})"


def _below(words: Iterator[int], outcomes: int) -> int:
    if not 0 < outcomes <= _WORDS:
        raise ValueError("a draw needs between 1 and 2**32 outcomes")
    limit = _WORDS - _WORDS % outcomes
    while True:
        word = next(words)
        if word < limit:
            return word % outcomes


class TaskDraws:
    """The corpus draws of one writer's task, each purpose from its own spawn key."""

    __slots__ = ("_nodes", "_paths")

    def __init__(self, seed: int, writer: int, task: int) -> None:
        self._nodes = tuple(
            SeedSequence(seed, (writer, task, purpose)) for purpose in (POOL, PATH, TOKEN, THINK)
        )
        self._paths = self._nodes[PATH].words()

    def hot(self, slot: int, conflict: float) -> bool:
        numerator, denominator = conflict.as_integer_ratio()
        word = self._nodes[POOL].generate_state(slot + 1)[slot]
        return word * denominator < numerator << 32

    def choice(self, candidates: Sequence[str]) -> str:
        return candidates[_below(self._paths, len(candidates))]

    def token(self, slot: int) -> str:
        low, high = self._nodes[TOKEN].generate_state(2 * slot + 2)[2 * slot :]
        return f"{low | ((high & 0xFFFF) << 32):012x}"

    def think_ms(self, jitter_ms: int) -> int:
        return _below(self._nodes[THINK].words(), jitter_ms + 1) if jitter_ms else 0


__all__ = ["DERIVATION", "PATH", "POOL", "POOL_SIZE", "THINK", "TOKEN", "SeedSequence", "TaskDraws"]
