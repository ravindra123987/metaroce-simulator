"""Discrete-event loop. Time is an integer number of nanoseconds."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable


class Sim:
    def __init__(self) -> None:
        self.now = 0
        self._q: list = []
        self._seq = itertools.count()

    def at(self, t: int, fn: Callable, *args) -> list:
        """Schedule fn(*args) at absolute time t. Returns a handle for cancel()."""
        if t < self.now:
            raise ValueError(f"cannot schedule in the past ({t} < {self.now})")
        ev = [t, next(self._seq), fn, args]
        heapq.heappush(self._q, ev)
        return ev

    def after(self, delay: int, fn: Callable, *args) -> list:
        return self.at(self.now + int(delay), fn, *args)

    @staticmethod
    def cancel(ev: list) -> None:
        ev[2] = None

    def run(self, until: int | None = None) -> None:
        q = self._q
        while q:
            t, _, fn, args = q[0]
            if until is not None and t > until:
                self.now = until
                return
            heapq.heappop(q)
            self.now = t
            if fn is not None:
                fn(*args)
        if until is not None:
            self.now = max(self.now, until)
