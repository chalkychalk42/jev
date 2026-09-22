"""Nested execution observations under the body's original arm.

The worker binds immutable attribution once. Primitives can report their existing
observations without knowing about the coach, allocating ticks or creating decisions.
Outside that binding these helpers do nothing. Native return codes are observations,
not strategic success labels.
"""

from __future__ import annotations

import contextlib
import functools
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from jev.learn.episode import ExecutionEventRow, Recorder
from jev.world.state_v1 import ArmedBy

if TYPE_CHECKING:
    from jev.orch.runtime import Armed


@dataclass(frozen=True)
class _Binding:
    recorder: Recorder
    client_id: str
    arm_id: str | None
    decision_id: str | None
    armed_by: ArmedBy
    step_id: str | None
    situation_key: str


@dataclass(frozen=True)
class _Scope:
    binding: _Binding
    operation_id: str | None = None


_current: ContextVar[_Scope | None] = ContextVar("jev_execution", default=None)


def bind(recorder: Recorder | None, arm: Armed | None, *, client_id: str):
    """Capture attribution now; activate it only when the worker enters the context.

    In particular, later tracker movement or an accepted decision must not relabel an
    already dispatched operation. Context variables isolate independent body threads.
    """
    scope = None if recorder is None or arm is None else _Scope(_Binding(
        recorder, client_id, arm.arm_id or None, arm.decision_id or None,
        arm.by, arm.step_id, arm.situation_key,
    ))
    return _activate(scope)


@contextlib.contextmanager
def _activate(scope: _Scope | None):
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


def _write(scope: _Scope, *, operation_id: str, parent_operation_id: str | None,
           name: str, phase: str, code: str = "", detail: str = "",
           duration_s: float | None = None, data: dict[str, Any] | None = None):
    binding = scope.binding
    binding.recorder.execution(ExecutionEventRow(
        run_id=binding.recorder.run_id, client_id=binding.client_id,
        arm_id=binding.arm_id, decision_id=binding.decision_id,
        armed_by=binding.armed_by, step_id=binding.step_id,
        situation_key=binding.situation_key, event_id=uuid.uuid4().hex,
        operation_id=operation_id, parent_operation_id=parent_operation_id,
        t=time.time(), tick_id=binding.recorder.tick_id, phase=phase,
        operation=name, code=code, detail=detail, duration_s=duration_s,
        data=dict(data or {}),
    ))


class _Span:
    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.code = ""
        self.detail = ""
        self.data: dict[str, Any] = {}

    def finish(self, *, code: str = "", detail: str = "",
               data: dict[str, Any] | None = None) -> None:
        """Supply the native result; the end row is written when the scope exits."""
        if self.enabled:
            self.code, self.detail = code, detail
            self.data.update(data or {})


@contextlib.contextmanager
def operation(name: str, *, data: dict[str, Any] | None = None):
    """Record a bounded operation and nest any observations made inside it."""
    scope = _current.get()
    span = _Span(enabled=scope is not None)
    if scope is None:
        yield span
        return
    operation_id = uuid.uuid4().hex
    started = time.monotonic()
    _write(scope, operation_id=operation_id, parent_operation_id=scope.operation_id,
           name=name, phase="begin", data=data)
    token = _current.set(_Scope(scope.binding, operation_id))
    try:
        try:
            yield span
        except BaseException as exc:
            span.finish(code="exception", detail=f"{type(exc).__name__}: {exc}",
                        data={"exception_type": type(exc).__name__})
            raise
        finally:
            _write(scope, operation_id=operation_id, parent_operation_id=scope.operation_id,
                   name=name, phase="end", code=span.code, detail=span.detail,
                   duration_s=max(0.0, time.monotonic() - started),
                   data={**(data or {}), **span.data})
    finally:
        _current.reset(token)


def event(name: str, *, code: str = "", detail: str = "",
          data: dict[str, Any] | None = None) -> None:
    """Record an observation within the current operation, without allocating a tick."""
    scope = _current.get()
    if scope is not None:
        _write(scope, operation_id=uuid.uuid4().hex,
               parent_operation_id=scope.operation_id, name=name, phase="event",
               code=code, detail=detail, data=data)


def traced(name: str):
    """Record a method's native return value and detail without judging its success."""
    def decorate(method):
        @functools.wraps(method)
        def wrapped(*args, **kwargs):
            if _current.get() is None:
                return method(*args, **kwargs)
            with operation(name) as span:
                result = method(*args, **kwargs)
                code = (str(result.value) if isinstance(result, Enum) else
                        "true" if result is True else "false" if result is False else
                        "none" if result is None else "returned")
                detail = str(getattr(args[0], "detail", "")) if args else ""
                span.finish(code=code, detail=detail)
                return result
        return wrapped
    return decorate
