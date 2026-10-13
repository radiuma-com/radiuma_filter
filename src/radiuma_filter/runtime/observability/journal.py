"""Canonical SQLite/WAL diagnostic journal."""
from __future__ import annotations

import json, sqlite3, threading, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ...api.identity import ApplicationSessionId, CorrelationId, ExecutionId, JournalPosition
from ...api.logs import ExceptionInfo, LogChannel, LogLevel, LogRecord
from .storage import _DiagnosticSanitizer, _Frame, _LogWriter

_READ_WINDOW_BYTES = 512 * 1024

_READ_WINDOW_SECONDS = 0.02

_MAX_DIAGNOSTIC_FRAME_BYTES = 1024 * 1024

class ExecutionLogSource:
    def __init__(self, journal: "LogJournal", execution_id: ExecutionId) -> None:
        self.journal = journal; self._execution_id = ExecutionId.parse(execution_id)
    def high_watermark(self) -> JournalPosition: return self.journal.high_watermark()
    def iter_records(self, *, after_position: JournalPosition, through_position: JournalPosition,
                     level: LogLevel | None = None, channel: LogChannel | None = None,
                     text: str | None = None) -> Iterator[LogRecord]:
        return self.journal.iter_records(
            execution_id=self._execution_id, after_position=after_position, through_position=through_position,
            level=level, channel=channel, text=text,
        )
    def wait_for_new(self, after_position: JournalPosition, timeout: float | None = None) -> bool:
        return self.journal.wait_for_new(after_position, timeout, execution_id=self._execution_id)

class LogJournal:
    def __init__(self, path: Path, *, session_id: ApplicationSessionId) -> None:
        self._path = Path(path); self._path.parent.mkdir(parents=True, exist_ok=True)
        self._session_id = ApplicationSessionId.parse(session_id)
        self._condition = threading.Condition(); self._closed = False
        self._close_error: BaseException | None = None
        self._fts_available = False
        self._initialize()
        db_max = self._db_high_water()
        spool_root = self._path.parent / "spool"
        self._next_position = db_max
        self._high_water = db_max

        # _LogWriter completes durable spool recovery synchronously before its
        # live writer thread starts. The allocator can therefore begin above one
        # stable canonical high-watermark without racing recovery.
        self.writer = _LogWriter(
            self._path,
            spool_root,
            str(self._session_id),
            committed=self._on_committed,
        )
        recovered_high = self.writer.recovery_high_watermark
        self._next_position = max(self._next_position, recovered_high)
        self._high_water = max(self._high_water, recovered_high)

    def view(self, execution_id: ExecutionId) -> ExecutionLogSource:
        self._ensure_open(); return ExecutionLogSource(self, execution_id)

    def append(self, *, level: str | LogLevel, message: str, channel: LogChannel = LogChannel.APPLICATION,
               source: str = "", execution_id: ExecutionId | str | None = None,
               correlation_id: CorrelationId | str | None = None,
               parent_correlation_id: CorrelationId | str | None = None, operation: str | None = None,
               code: str | None = None, filter_name_snapshot: str | None = None,
               filter_path_snapshot: str | None = None, exception: ExceptionInfo | None = None,
               fields: Mapping[str, Any] | None = None, event_time: datetime | None = None) -> JournalPosition:
        self._ensure_open()
        safe_fields = _DiagnosticSanitizer.fields(fields)
        safe_message = _DiagnosticSanitizer.message(message)
        event = event_time or datetime.now(timezone.utc)
        if event.tzinfo is None: raise ValueError("event_time must be timezone-aware")
        exc = exception
        exception_message = None if exc is None else _DiagnosticSanitizer.message(exc.message)
        exception_stack = None if exc is None or exc.stack is None else _DiagnosticSanitizer.message(exc.stack)
        with self._condition:
            self._next_position += 1; position = self._next_position
        payload = {
            "event_time": event.isoformat(), "level": self._normalize_level(level).value,
            "channel": (channel if isinstance(channel, LogChannel) else LogChannel(channel)).value,
            "source": str(source or "").strip(), "message": safe_message,
            "application_session_id": str(self._session_id),
            "execution_id": None if execution_id is None else str(ExecutionId.parse(execution_id)),
            "correlation_id": None if correlation_id is None else str(CorrelationId.parse(correlation_id)),
            "parent_correlation_id": None if parent_correlation_id is None else str(CorrelationId.parse(parent_correlation_id)),
            "operation": None if operation is None else str(operation), "code": None if code is None else str(code),
            "filter_name_snapshot": filter_name_snapshot, "filter_path_snapshot": filter_path_snapshot,
            "exception_type": None if exc is None else str(exc.type_name), "exception_message": exception_message,
            "exception_stack": exception_stack, "fields": safe_fields,
        }
        frame = _Frame.build(position, payload)
        if frame.byte_size > _MAX_DIAGNOSTIC_FRAME_BYTES:
            raise ValueError(
                f"Diagnostic record exceeds the {_MAX_DIAGNOSTIC_FRAME_BYTES}-byte safety budget"
            )
        self.writer.submit(frame)
        return JournalPosition(position)

    def high_watermark(self) -> JournalPosition:
        self._ensure_open()
        with self._condition: return JournalPosition(self._high_water)

    def wait_for_new(self, after_position: JournalPosition, timeout: float | None, *, execution_id: ExecutionId | None = None) -> bool:
        self._ensure_open()
        after = int(after_position.value)
        if timeout is not None and float(timeout) < 0:
            raise ValueError("timeout cannot be negative")
        deadline = None if timeout is None else time.monotonic() + float(timeout)

        # The journal condition is intentionally global because commits from every
        # execution share one durable writer.  A scoped reader therefore has to
        # re-check its own execution after every wake-up instead of treating an
        # unrelated commit as success.
        while True:
            if execution_id is None:
                if self.high_watermark().value > after:
                    return True
            elif self._has_after(execution_id, after):
                return True

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            else:
                remaining = None

            with self._condition:
                self._ensure_open()
                self._condition.wait(timeout=remaining)

    def iter_records(self, *, after_position: JournalPosition, through_position: JournalPosition,
                     execution_id: ExecutionId | None = None, level: LogLevel | None = None,
                     channel: LogChannel | None = None, text: str | None = None) -> Iterator[LogRecord]:
        self._ensure_open()
        clauses = ["l.position > ?", "l.position <= ?"]
        params: list[Any] = [after_position.value, through_position.value]
        if execution_id is not None: clauses.append("l.execution_id = ?"); params.append(str(execution_id))
        if level is not None: clauses.append("l.level = ?"); params.append(level.value)
        if channel is not None: clauses.append("l.channel = ?"); params.append(channel.value)
        join = ""
        if text:
            if self._fts_available:
                join = " JOIN logs_fts f ON f.rowid = l.position "
                clauses.append("f.logs_fts MATCH ?"); params.append(str(text))
            else:
                clauses.append("instr(l.message, ?) > 0"); params.append(str(text))
        sql_base = "SELECT " + self._SELECT_COLUMNS + " FROM logs l" + join + " WHERE " + " AND ".join(clauses)
        return self._iter_windows(sql_base, tuple(params), through_position.value)

    def iter_all_records(self, *, execution_id: ExecutionId | str | None = None,
                         level: LogLevel | None = None, channel: LogChannel | None = None,
                         text: str | None = None) -> Iterator[LogRecord]:
        high = self.high_watermark()
        return self.iter_records(
            after_position=JournalPosition(0), through_position=high,
            execution_id=None if execution_id is None else ExecutionId.parse(execution_id),
            level=level, channel=channel, text=text,
        )

    def flush(self) -> None: self._ensure_open(); self.writer.flush()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                if self._close_error is not None:
                    raise self._close_error
                return
        error: BaseException | None = None
        try:
            self.writer.close()
        except BaseException as exc:
            error = exc
        with self._condition:
            self._closed = True
            self._close_error = error
            self._condition.notify_all()
        if error is not None:
            raise error

    def _initialize(self) -> None:
        with sqlite3.connect(self._path) as c:
            c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL")
            c.execute("""CREATE TABLE IF NOT EXISTS logs (
                position INTEGER PRIMARY KEY,
                event_time TEXT NOT NULL, persisted_at TEXT NOT NULL,
                level TEXT NOT NULL, channel TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
                application_session_id TEXT NOT NULL, execution_id TEXT,
                correlation_id TEXT, parent_correlation_id TEXT, operation TEXT, code TEXT,
                filter_name_snapshot TEXT, filter_path_snapshot TEXT,
                exception_type TEXT, exception_message TEXT, exception_stack TEXT,
                fields_json TEXT NOT NULL DEFAULT '{}', frame_digest TEXT NOT NULL, schema_version INTEGER NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_logs_execution_position ON logs(execution_id, position)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_logs_correlation_position ON logs(correlation_id, position)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_logs_level_position ON logs(level, position)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_logs_channel_position ON logs(channel, position)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_logs_event_time ON logs(event_time)")
            try:
                existed = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logs_fts'"
                ).fetchone() is not None
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(message, exception_message, content='logs', content_rowid='position')")
                c.execute("""CREATE TRIGGER IF NOT EXISTS logs_ai AFTER INSERT ON logs BEGIN
                    INSERT INTO logs_fts(rowid,message,exception_message) VALUES(new.position,new.message,coalesce(new.exception_message,'')); END""")
                c.execute("""CREATE TRIGGER IF NOT EXISTS logs_ad AFTER DELETE ON logs BEGIN
                    INSERT INTO logs_fts(logs_fts,rowid,message,exception_message) VALUES('delete',old.position,old.message,coalesce(old.exception_message,'')); END""")
                if not existed:
                    c.execute("INSERT INTO logs_fts(logs_fts) VALUES('rebuild')")
                self._fts_available = True
            except sqlite3.OperationalError:
                # FTS5 may be absent in unusual SQLite builds; text search uses a
                # compatibility fallback while preserving the same public query API.
                self._fts_available = False

    def _iter_windows(self, sql_base: str, params: tuple[Any, ...], through: int) -> Iterator[LogRecord]:
        anchor = 0
        # Original query already contains lower bound. We repeatedly add a stronger
        # anchor and close each read transaction after a byte/time slice.
        while anchor < through:
            connection = self._read_connection(); cursor = None; yielded = False
            try:
                sql = sql_base + " AND l.position > ? ORDER BY l.position"
                cursor = connection.execute(sql, (*params, anchor))
                bytes_read = 0; started = time.monotonic()
                for row in cursor:
                    record = self._row_to_record(row); anchor = record.position.value; yielded = True
                    bytes_read += len(record.message.encode("utf-8")) + len(json.dumps(dict(record.fields), ensure_ascii=False).encode("utf-8")) + 256
                    yield record
                    if bytes_read >= _READ_WINDOW_BYTES or time.monotonic() - started >= _READ_WINDOW_SECONDS:
                        break
            finally:
                if cursor is not None: cursor.close()
                connection.close()
            if not yielded: break

    _SELECT_COLUMNS = """l.position,l.event_time,l.persisted_at,l.level,l.channel,l.source,l.message,l.application_session_id,
    l.execution_id,l.correlation_id,l.parent_correlation_id,l.operation,l.code,l.filter_name_snapshot,
    l.filter_path_snapshot,l.exception_type,l.exception_message,l.exception_stack,l.fields_json"""

    @staticmethod
    def _normalize_level(value: str | LogLevel) -> LogLevel:
        if isinstance(value, LogLevel):
            return value
        text = str(value or "info").strip().casefold()
        if text == "warn":
            text = "warning"
        try:
            return LogLevel(text)
        except ValueError:
            return LogLevel.INFO

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> LogRecord:
        fields_raw = row[18]
        try:
            fields = json.loads(str(fields_raw or "{}"))
        except Exception:
            fields = {}
        exception = None
        if row[15]:
            exception = ExceptionInfo(
                str(row[15]),
                str(row[16] or ""),
                None if row[17] is None else str(row[17]),
            )
        return LogRecord(
            position=JournalPosition(int(row[0])),
            event_time=datetime.fromisoformat(str(row[1])),
            persisted_at=datetime.fromisoformat(str(row[2])),
            level=LogLevel(str(row[3])),
            channel=LogChannel(str(row[4])),
            source=str(row[5] or ""),
            message=str(row[6]),
            application_session_id=ApplicationSessionId.parse(row[7]),
            execution_id=None if row[8] is None else ExecutionId.parse(row[8]),
            correlation_id=None if row[9] is None else CorrelationId.parse(row[9]),
            parent_correlation_id=(
                None if row[10] is None else CorrelationId.parse(row[10])
            ),
            operation=row[11],
            code=row[12],
            filter_name_snapshot=row[13],
            filter_path_snapshot=row[14],
            exception=exception,
            fields=fields if isinstance(fields, dict) else {},
        )

    def _db_high_water(self) -> int:
        with sqlite3.connect(self._path) as c:
            row = c.execute("SELECT COALESCE(MAX(position),0) FROM logs").fetchone()
            return int(row[0] if row else 0)

    def _has_after(self, execution_id: ExecutionId, after: int) -> bool:
        with self._read_connection() as c:
            row = c.execute("SELECT 1 FROM logs WHERE execution_id=? AND position>? ORDER BY position", (str(execution_id), after)).fetchone()
        return row is not None

    def _on_committed(self, highest: int) -> None:
        with self._condition:
            self._high_water = max(self._high_water, int(highest)); self._condition.notify_all()
    def _read_connection(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=30.0); c.execute("PRAGMA query_only=ON"); return c
    def _ensure_open(self) -> None:
        if self._closed: raise RuntimeError("Diagnostic journal has been closed")
