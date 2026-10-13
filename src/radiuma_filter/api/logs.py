"""Structured diagnostic models and lazy log readers."""
from __future__ import annotations

import copy
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, TypeAlias

from .identity import ApplicationSessionId, CorrelationId, ExecutionId, JournalPosition

SafeLogValue: TypeAlias = None | bool | int | float | str | tuple["SafeLogValue", ...] | Mapping[str, "SafeLogValue"]

class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

class LogChannel(str, Enum):
    APPLICATION = "application"
    EXECUTION = "execution"
    SYSTEM = "system"
    BACKEND = "backend"

class LogStart(str, Enum):
    BEGINNING = "beginning"
    NOW = "now"

@dataclass(frozen=True, slots=True)
class ExceptionInfo:
    type_name: str
    message: str
    stack: str | None = None

    @classmethod
    def from_exception(cls, exc: BaseException, *, stack: str | None = None) -> "ExceptionInfo":
        return cls(
            type_name=type(exc).__name__,
            message=str(exc),
            stack=stack,
        )

@dataclass(frozen=True, slots=True)
class LogRecord:
    position: JournalPosition
    event_time: datetime
    persisted_at: datetime
    level: LogLevel
    channel: LogChannel
    source: str
    message: str
    application_session_id: ApplicationSessionId
    execution_id: ExecutionId | None = None
    correlation_id: CorrelationId | None = None
    parent_correlation_id: CorrelationId | None = None
    operation: str | None = None
    code: str | None = None
    filter_name_snapshot: str | None = None
    filter_path_snapshot: str | None = None
    exception: ExceptionInfo | None = None
    fields: Mapping[str, SafeLogValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.position, JournalPosition):
            object.__setattr__(self, "position", JournalPosition(int(self.position)))
        for attr in ("event_time", "persisted_at"):
            value = getattr(self, attr)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"LogRecord.{attr} must be timezone-aware")
        if not isinstance(self.level, LogLevel):
            object.__setattr__(self, "level", LogLevel(self.level))
        if not isinstance(self.channel, LogChannel):
            object.__setattr__(self, "channel", LogChannel(self.channel))
        object.__setattr__(self, "application_session_id", ApplicationSessionId.parse(self.application_session_id))
        if self.execution_id is not None:
            object.__setattr__(self, "execution_id", ExecutionId.parse(self.execution_id))
        if self.correlation_id is not None:
            object.__setattr__(self, "correlation_id", CorrelationId.parse(self.correlation_id))
        if self.parent_correlation_id is not None:
            object.__setattr__(self, "parent_correlation_id", CorrelationId.parse(self.parent_correlation_id))
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "message", str(self.message))
        object.__setattr__(self, "fields", MappingProxyType(copy.deepcopy(dict(self.fields))))

    @property
    def sequence(self) -> int:
        return self.position.value

    @property
    def timestamp(self) -> datetime:
        return self.event_time

class LogSource(Protocol):
    def high_watermark(self) -> JournalPosition: ...

    def iter_records(
        self,
        *,
        after_position: JournalPosition,
        through_position: JournalPosition,
        level: LogLevel | None = None,
        channel: LogChannel | None = None,
        text: str | None = None,
    ) -> Iterator[LogRecord]: ...

    def wait_for_new(
        self,
        after_position: JournalPosition,
        timeout: float | None = None,
    ) -> bool: ...

class LogReader:
    def __init__(self) -> None:
        raise TypeError("LogReader instances are created by log views")

    @classmethod
    def _create(cls, source: LogSource, start: LogStart) -> "LogReader":
        self = cls.__new__(cls)
        self._source = source
        self._lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._cursor = (
            source.high_watermark()
            if start is LogStart.NOW
            else JournalPosition(0)
        )
        return self

    @property
    def position(self) -> JournalPosition:
        with self._lock:
            return self._cursor

    def read(self) -> Iterator[LogRecord]:
        def generate() -> Iterator[LogRecord]:
            with self._read_lock:
                with self._lock:
                    start = self._cursor
                high = self._source.high_watermark()
                if high.value <= start.value:
                    return
                for record in self._source.iter_records(
                    after_position=start,
                    through_position=high,
                ):
                    with self._lock:
                        if record.position.value > self._cursor.value:
                            self._cursor = record.position
                    yield record

        return generate()

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None and float(timeout) < 0:
            raise ValueError("timeout cannot be negative")
        with self._lock:
            cursor = self._cursor
        return self._source.wait_for_new(
            cursor,
            None if timeout is None else float(timeout),
        )

class _LogsView:
    @staticmethod
    def _enum_or_none(value: Any, enum_type):
        return None if value is None else value if isinstance(value, enum_type) else enum_type(value)

    def __init__(self) -> None:
        raise TypeError(f"{type(self).__name__} instances are provided by the filtering runtime")

    @classmethod
    def _create(cls, source):
        self = cls.__new__(cls)
        self._source = source
        return self

    def _resolved_source(self):
        return self._source() if callable(self._source) else self._source

    def __iter__(self) -> Iterator[LogRecord]:
        return self.query()

class ExecutionLogs(_LogsView):
    def query(
        self,
        *,
        level: LogLevel | None = None,
        channel: LogChannel | None = None,
        text: str | None = None,
    ) -> Iterator[LogRecord]:
        source = self._resolved_source()
        high = source.high_watermark()
        if high.value <= 0:
            return iter(())
        return source.iter_records(
            after_position=JournalPosition(0),
            through_position=high,
            level=self._enum_or_none(level, LogLevel),
            channel=self._enum_or_none(channel, LogChannel),
            text=None if text is None else str(text),
        )

    def reader(self, *, start: LogStart = LogStart.BEGINNING) -> LogReader:
        resolved = start if isinstance(start, LogStart) else LogStart(start)
        return LogReader._create(self._resolved_source(), resolved)

    def tail(self) -> LogReader:
        return LogReader._create(self._resolved_source(), LogStart.NOW)

__all__ = [
"ExceptionInfo", "ExecutionLogs", "LogChannel", "LogLevel",
    "LogReader", "LogRecord", "LogStart", "SafeLogValue",
]
