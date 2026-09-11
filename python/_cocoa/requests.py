"""Private request helpers shared by the public system modules."""

import json
import math
import time

from . import _system


def seconds(value, name="timeout", *, maximum=3600, allow_none=False):
    if value is None and allow_none:
        return None
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum} seconds")
    return value


def _check(state):
    if error := state["error"]:
        exception = {
            "permission": PermissionError, "value": ValueError,
            "os": OSError, "file_not_found": FileNotFoundError,
            "not_implemented": NotImplementedError, "runtime": RuntimeError,
        }.get(error["kind"], RuntimeError)
        raise exception(error["message"])


class Request:
    """An explicit native operation with polling, waiting, and cancellation.

    Closing cancels pending work. A system sharing service that has already
    started may finish in its own application after the request is closed.
    """

    def __init__(self, operation, options, convert=lambda value: value):
        self._handle = None
        self._convert = convert
        self._state = {"done": False, "closed": False}
        self._handle = _system.start(operation, json.dumps(options, allow_nan=False))
        try:
            self._snapshot(0, consume=False)
        except BaseException:
            self.close()
            raise

    def _snapshot(self, timeout, *, consume=True):
        handle = self._handle
        if handle is None:
            raise ValueError("This request is closed")
        self._state = json.loads(_system.poll(handle, timeout, consume))
        _check(self._state)
        return self._state

    @property
    def done(self):
        """Whether the operation finished, including a native failure."""
        if self._handle is not None:
            self._state = json.loads(_system.poll(self._handle, 0, False))
        return self._state["done"]

    @property
    def closed(self):
        return self._handle is None

    def wait(self, timeout=60):
        """Return the result, or raise TimeoutError without closing the request."""
        timeout = seconds(timeout, allow_none=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = 0.05 if deadline is None else max(0, min(0.05, deadline - time.monotonic()))
            state = self._snapshot(remaining, consume=False)
            if state["done"]:
                return self._convert(state["result"])
            if state["closed"]:
                raise ValueError("This request was closed")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("The system request did not finish before the timeout")

    def close(self):
        """Cancel pending work and release native resources. Safe to repeat."""
        if self._handle is not None:
            _system.close(self._handle)
            self._handle = None
            self._state["closed"] = True

    def __enter__(self):
        if self.closed:
            raise ValueError("This request is closed")
        return self

    def __exit__(self, *exc):
        self.close()


class Stream(Request):
    """A bounded native sample queue; old samples are dropped on overflow."""

    def read(self, timeout=1):
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
    def stats(self):
        """Queue capacity, buffered sample count, and dropped sample count."""
        state = self._snapshot(0, consume=False)
        return {key: state[key] for key in ("capacity", "buffered", "dropped")}

    def __iter__(self):
        while not self.closed:
            if (sample := self.read()) is not None:
                yield sample


def call(operation, options=None, *, timeout=60, convert=lambda value: value):
    seconds(timeout, allow_none=True)
    with Request(operation, options or {}, convert) as request:
        return request.wait(timeout)
