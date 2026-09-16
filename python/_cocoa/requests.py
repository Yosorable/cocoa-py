"""Private request helpers shared by the public system modules."""

import json
import math
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, overload

from . import _system


def seconds(value: float | None, name: str = "timeout", *, maximum: float = 3600,
            allow_none: bool = False) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum} seconds")
    return value


def _check(state: dict[str, Any]) -> None:
    if error := state["error"]:
        exception = {
            "permission": PermissionError, "value": ValueError,
            "os": OSError, "file_not_found": FileNotFoundError,
            "not_implemented": NotImplementedError, "runtime": RuntimeError,
        }.get(error["kind"], RuntimeError)
        raise exception(error["message"])


@dataclass(frozen=True)
class StreamStats:
    """A snapshot of a stream's bounded queue, in numbers of samples."""

    capacity: int
    buffered: int
    dropped: int


class Request[T = Any]:
    """An explicit native operation with polling, waiting, and cancellation.

    Closing cancels pending work. A system sharing service that has already
    started may finish in its own application after the request is closed.
    """

    def __init__(self, operation: str, options: Mapping[str, object],
                 convert: Callable[[Any], T] = lambda value: value, *,
                 _buffers: Mapping[str, object] | None = None) -> None:
        self._handle = None
        self._convert = convert
        self._state: dict[str, Any] = {"done": False, "closed": False}
        arguments = (operation, json.dumps(options, allow_nan=False))
        self._handle = _system.start(*arguments) if _buffers is None else _system.start(*arguments, _buffers)
        try:
            self._snapshot(0, consume=False)
        except BaseException:
            self.close()
            raise

    def _snapshot(self, timeout: float, *, consume: bool = True) -> dict[str, Any]:
        handle = self._handle
        if handle is None:
            raise ValueError("This request is closed")
        self._state = json.loads(_system.poll(handle, timeout, consume))
        _check(self._state)
        return self._state

    @property
    def done(self) -> bool:
        """Whether the operation finished, including a native failure."""
        if self._handle is not None:
            self._state = json.loads(_system.poll(self._handle, 0, False))
        return self._state["done"]

    @property
    def closed(self) -> bool:
        return self._handle is None

    def wait(self, timeout: float | None = 60) -> T:
        """Return the result, or raise TimeoutError without closing the request."""
        timeout = seconds(timeout, allow_none=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        # Returning to Python between bounded waits allows injected interrupts.
        while True:
            remaining = 0.05 if deadline is None else max(0, min(0.05, deadline - time.monotonic()))
            state = self._snapshot(remaining, consume=False)
            if state["done"]:
                return self._convert(state["result"])
            if state["closed"]:
                raise ValueError("This request was closed")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("The system request did not finish before the timeout")

    def close(self) -> None:
        """Cancel pending work and release native resources. Safe to repeat."""
        if self._handle is not None:
            _system.close(self._handle)
            self._handle = None
            self._state["closed"] = True

    def __enter__(self) -> Self:
        if self.closed:
            raise ValueError("This request is closed")
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()


class Stream[T = Any](Request[T]):
    """A bounded native sample queue; old samples are dropped on overflow."""

    def read(self, timeout: float | None = 1) -> T | None:
        """Return one sample, or None when the timeout expires."""
        timeout = seconds(timeout, allow_none=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = 0.05 if deadline is None else max(0, min(0.05, deadline - time.monotonic()))
            state = self._snapshot(remaining)
            if state["sample"] is not None:
                return self._convert(state["sample"])
            if state["closed"]:
                raise ValueError("This stream was closed")
            if state["done"] or (deadline is not None and time.monotonic() >= deadline):
                return None

    @property
    def stats(self) -> StreamStats:
        """Queue capacity, buffered sample count, and dropped sample count."""
        state = self._snapshot(0, consume=False)
        return StreamStats(capacity=state["capacity"], buffered=state["buffered"],
                           dropped=state["dropped"])

    def __iter__(self) -> Iterator[T]:
        while not self.closed:
            if (sample := self.read()) is not None:
                yield sample


@overload
def call[T](operation: str, options: Mapping[str, object] | None = None, *,
            timeout: float | None = 60, convert: Callable[[Any], T]) -> T: ...


@overload
def call(operation: str, options: Mapping[str, object] | None = None, *,
         timeout: float | None = 60) -> Any: ...


def call(operation: str, options: Mapping[str, object] | None = None, *,
         timeout: float | None = 60,
         convert: Callable[[Any], Any] = lambda value: value) -> Any:
    seconds(timeout, allow_none=True)
    with Request(operation, options or {}, convert) as request:
        return request.wait(timeout)
