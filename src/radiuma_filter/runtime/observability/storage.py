"""Durable diagnostic storage, redaction, spool recovery, and async writing."""
from __future__ import annotations

import hashlib, json, math, os, re, sqlite3, struct, threading, time, uuid, zlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ...api.identity import ApplicationSessionId, CorrelationId, ExecutionId, JournalPosition
from ...api.logs import SafeLogValue

_REDACTED = "<redacted>"

_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "authorization", "api_key", "apikey", "private_key",
    "patientname", "patient_name", "patientid", "patient_id", "birthdate", "birth_date",
    "accessionnumber", "accession_number",
})

_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|authorization|api[_-]?key|private[_-]?key|patient[_-]?name|patientname|patient[_-]?id|patientid|birth[_-]?date|birthdate|accession[_-]?number|accessionnumber)\s*[:=]\s*([^,;\r\n]+)"
)

_IDS = (ApplicationSessionId, CorrelationId, ExecutionId, JournalPosition)

class _DiagnosticSanitizer:
    """Redaction and safe-value normalization for diagnostic payloads."""

    @classmethod
    def message(cls, message: str) -> str:
        return _PATTERN.sub(lambda match: f"{match.group(1)}={_REDACTED}", str(message))

    @classmethod
    def fields(cls, fields: Mapping[str, Any] | None) -> dict[str, SafeLogValue]:
        return {
            str(key): cls._value(str(key), value)
            for key, value in dict(fields or {}).items()
        }

    @classmethod
    def _value(cls, key: str, value: Any) -> SafeLogValue:
        if key.strip().casefold() in _SENSITIVE_KEYS:
            return _REDACTED
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, str):
            return cls.message(value)
        if isinstance(value, _IDS):
            return str(value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise TypeError("naive datetime is not a SafeLogValue")
            return value.isoformat()
        if isinstance(value, Mapping):
            return {str(k): cls._value(str(k), v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return tuple(cls._value(key, item) for item in value)
        raise TypeError(
            f"Unsupported diagnostic field type for '{key}': {type(value).__name__}"
        )


_MAGIC = b"RSP1"

_HEADER = struct.Struct(">QI")  # JournalPosition, payload bytes

_CRC = struct.Struct(">I")

@dataclass(frozen=True, slots=True)
class _SpoolFrame:
    position: int
    payload: dict[str, Any]
    encoded_bytes: int

class _SpoolCorruption(RuntimeError):
    pass

class _DurableSpool:
    def __init__(self, root: Path, session_id: str, *, segment_target_bytes: int = 8 * 1024 * 1024,
                 segment_max_age: float = 30.0, sync_target_bytes: int = 1024 * 1024,
                 sync_max_latency: float = 0.05) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._session_id = str(session_id)
        self._segment_target_bytes = int(segment_target_bytes)
        self._segment_max_age = float(segment_max_age)
        self._sync_target_bytes = int(sync_target_bytes)
        self._sync_max_latency = float(sync_max_latency)
        self._lock = threading.RLock()
        self._active_path: Path | None = None
        self._active_created = 0.0
        self._offsets: dict[Path, int] = {}
        self._unsynced_bytes = 0
        self._last_sync = time.monotonic()
        self._quarantine = self._root / "quarantine"
    def append(self, position: int, payload: dict[str, Any]) -> int:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        frame = _HEADER.pack(int(position), len(body)) + body + _CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)
        with self._lock:
            path = self._ensure_active_locked()
            self._rotate_if_needed_locked(len(frame))
            path = self._ensure_active_locked()
            with path.open("ab", buffering=0) as handle:
                handle.write(frame)
                self._unsynced_bytes += len(frame)
                now = time.monotonic()
                if self._unsynced_bytes >= self._sync_target_bytes or now - self._last_sync >= self._sync_max_latency:
                    handle.flush(); os.fsync(handle.fileno())
                    self._unsynced_bytes = 0; self._last_sync = now
            return len(frame)

    def sync(self) -> None:
        with self._lock:
            if self._active_path is None or not self._active_path.exists():
                return
            with self._active_path.open("ab", buffering=0) as handle:
                handle.flush(); os.fsync(handle.fileno())
            self._unsynced_bytes = 0; self._last_sync = time.monotonic()

    def read_frames(self, byte_budget: int) -> list[tuple[_SpoolFrame, Path, int]]:
        """Read an ordered provisional byte-bounded batch without acknowledgement.

        Durable offsets are advanced only by ``acknowledge`` after canonical commit,
        so a writer crash can safely replay the same frames.
        """
        budget = max(1, int(byte_budget))
        result_items: list[tuple[_SpoolFrame, Path, int]] = []
        consumed = 0
        with self._lock:
            provisional = dict(self._offsets)
            for path in self._ordered_segments_locked():
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                offset = provisional.get(path, len(_MAGIC))
                while offset < size and consumed < budget:
                    try:
                        item = self._read_one_locked(path, offset, size)
                    except _SpoolCorruption:
                        self._quarantine_locked(path)
                        self._offsets.pop(path, None)
                        break
                    if item is None:
                        if path != self._active_path:
                            if path.name.startswith("active-"):
                                self._truncate_tail_locked(path, offset)
                            else:
                                self._quarantine_locked(path)
                                self._offsets.pop(path, None)
                        break
                    frame, end = item
                    result_items.append((frame, path, end))
                    consumed += frame.encoded_bytes
                    offset = end
                    provisional[path] = end
                    if consumed >= budget:
                        break
        return result_items

    def acknowledge(self, path: Path, end_offset: int) -> None:
        with self._lock:
            if not path.exists(): return
            self._offsets[path] = int(end_offset)
            if path != self._active_path and end_offset >= path.stat().st_size:
                self._safe_delete_locked(path); self._offsets.pop(path, None)

    def reclaim_active_if_empty(self) -> None:
        with self._lock:
            path = self._active_path
            if path is None or not path.exists(): return
            offset = self._offsets.get(path, len(_MAGIC))
            if offset < path.stat().st_size: return
            # All frames are canonical. Truncating this transport copy is safe.
            with path.open("wb", buffering=0) as handle:
                handle.write(_MAGIC); handle.flush(); os.fsync(handle.fileno())
            self._offsets[path] = len(_MAGIC)
            self._active_created = time.monotonic()

    def pending_bytes(self) -> int:
        with self._lock:
            total = 0
            for path in self._ordered_segments_locked():
                try:
                    total += max(0, path.stat().st_size - self._offsets.get(path, len(_MAGIC)))
                except OSError:
                    pass
            return total

    def max_position(self) -> int:
        """Recovery scan for the largest valid frame position without mutating files."""
        highest = 0
        with self._lock:
            for path in self._ordered_segments_locked():
                try:
                    offset = len(_MAGIC); size = path.stat().st_size
                    while offset < size:
                        result = self._read_one_locked(path, offset, size)
                        if result is None: break
                        frame, offset = result
                        highest = max(highest, frame.position)
                except Exception:
                    continue
        return highest

    def close(self) -> None:
        self.sync()

    def _ensure_active_locked(self) -> Path:
        if self._active_path is None or not self._active_path.exists():
            name = f"active-{self._session_id}-{uuid.uuid4().hex}.rsp"
            self._active_path = self._root / name
            with self._active_path.open("wb", buffering=0) as handle:
                handle.write(_MAGIC); handle.flush(); os.fsync(handle.fileno())
            self._active_created = time.monotonic()
            self._offsets[self._active_path] = len(_MAGIC)
        return self._active_path

    def _rotate_if_needed_locked(self, incoming: int) -> None:
        path = self._active_path
        if path is None: return
        age = time.monotonic() - self._active_created
        if path.stat().st_size + incoming < self._segment_target_bytes and age < self._segment_max_age:
            return
        self.sync()
        sealed = self._root / path.name.replace("active-", "sealed-", 1)
        os.replace(path, sealed)
        self._offsets[sealed] = self._offsets.pop(path, len(_MAGIC))
        self._active_path = None

    def _ordered_segments_locked(self) -> list[Path]:
        paths = list(self._root.glob("sealed-*.rsp"))
        if self._active_path is not None and self._active_path.exists(): paths.append(self._active_path)
        else:
            # Recover abandoned active segments from previous sessions as sealed input.
            for path in self._root.glob("active-*.rsp"):
                if path != self._active_path: paths.append(path)
        def key(path: Path) -> tuple[int, str]:
            try:
                result = self._read_one_locked(path, len(_MAGIC), path.stat().st_size)
                return ((result[0].position if result else 2**63-1), path.name)
            except Exception:
                return (2**63-1, path.name)
        return sorted(dict.fromkeys(paths), key=key)

    def _read_one_locked(self, path: Path, start: int, size: int):
        with path.open("rb") as handle:
            magic = handle.read(len(_MAGIC))
            if magic != _MAGIC: raise _SpoolCorruption(f"Invalid spool header: {path}")
            if start < len(_MAGIC): start = len(_MAGIC)
            if size - start < _HEADER.size: return None
            handle.seek(start)
            raw = handle.read(_HEADER.size)
            position, length = _HEADER.unpack(raw)
            if length < 0 or length > 64 * 1024 * 1024:
                raise _SpoolCorruption(f"Implausible spool frame length in {path}")
            required = _HEADER.size + length + _CRC.size
            if size - start < required: return None
            body = handle.read(length)
            expected = _CRC.unpack(handle.read(_CRC.size))[0]
            if (zlib.crc32(body) & 0xFFFFFFFF) != expected:
                raise _SpoolCorruption(f"Spool frame checksum mismatch in {path}")
            try: payload = json.loads(body.decode("utf-8"))
            except Exception as exc: raise _SpoolCorruption(f"Invalid spool payload in {path}") from exc
            if not isinstance(payload, dict): raise _SpoolCorruption("Spool payload must be a mapping")
            return _SpoolFrame(int(position), payload, required), start + required

    def _truncate_tail_locked(self, path: Path, valid_end: int) -> None:
        """Repair an incomplete crash tail while preserving every valid frame."""
        try:
            with path.open("r+b", buffering=0) as handle:
                handle.truncate(max(len(_MAGIC), int(valid_end)))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise _SpoolCorruption(f"Unable to repair incomplete spool tail: {path}") from exc

    def _quarantine_locked(self, path: Path) -> None:
        self._quarantine.mkdir(parents=True, exist_ok=True)
        target = self._quarantine / f"{path.name}.{uuid.uuid4().hex}.bad"
        try: os.replace(path, target)
        except OSError: pass
        if path == self._active_path: self._active_path = None

    @staticmethod
    def _safe_delete_locked(path: Path) -> None:
        try: path.unlink(missing_ok=True)
        except OSError: pass

@dataclass(frozen=True, slots=True)
class _Frame:
    position: int
    payload: dict[str, Any]
    encoded: bytes
    digest: str
    recovered: bool = False

    @property
    def byte_size(self) -> int:
        return len(self.encoded)

    @classmethod
    def build(cls, position: int, payload: dict[str, Any]) -> "_Frame":
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return cls(
            int(position),
            payload,
            encoded,
            hashlib.sha256(encoded).hexdigest(),
            False,
        )

class _LogWriter:
    def __init__(self, path: Path, spool_root: Path, session_id: str, *,
                 committed: Callable[[int], None],
                 memory_budget_bytes: int = 4 * 1024 * 1024,
                 batch_target_bytes: int = 512 * 1024, batch_max_latency: float = 0.02) -> None:
        self._path = Path(path)
        self._committed = committed
        self._memory_budget = int(memory_budget_bytes)
        self._batch_target = int(batch_target_bytes)
        self._batch_latency = float(batch_max_latency)
        self.spool = _DurableSpool(spool_root, session_id)
        self._cv = threading.Condition()
        self._memory: deque[_Frame] = deque()
        self._memory_bytes = 0
        self._accepting = True
        self._closed = False
        self._inflight_spool = 0
        self._error: BaseException | None = None
        self._close_error: BaseException | None = None
        self._working = False

        # Recovery must finish before the live writer thread starts. Otherwise a
        # recovered frame can be committed and removed from the spool between the
        # journal's DB high-water scan and its spool high-water scan, allowing the
        # live allocator to reuse the same JournalPosition.
        self._recovery_high_watermark = self._recover_spool()
        self.spooling = self.spool.pending_bytes() > 0

        self._thread = threading.Thread(
            target=self._run,
            name="radiuma-filtering-diagnostic-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def recovery_high_watermark(self) -> int:
        return self._recovery_high_watermark

    def _recover_spool(self) -> int:
        highest = self._database_high_watermark()
        if self.spool.pending_bytes() <= 0:
            return max(highest, self.spool.max_position())

        connection = sqlite3.connect(self._path, timeout=30.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")

            while self.spool.pending_bytes() > 0:
                items = self.spool.read_frames(self._batch_target)
                if not items:
                    break

                batch: list[_Frame] = []
                acknowledgements: dict[Path, int] = {}
                for raw, path, end_offset in items:
                    payload = dict(raw.payload)
                    digest = str(payload.pop("_frame_digest", ""))
                    encoded = json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                    batch.append(
                        _Frame(
                            raw.position,
                            payload,
                            encoded,
                            digest or hashlib.sha256(encoded).hexdigest(),
                            True,
                        )
                    )
                    acknowledgements[path] = max(
                        acknowledgements.get(path, 0),
                        end_offset,
                    )

                committed_high = self._commit_batch(connection, batch)
                highest = max(highest, committed_high)
                for path, end_offset in acknowledgements.items():
                    self.spool.acknowledge(path, end_offset)
                if committed_high:
                    self._committed(committed_high)

            self.spool.reclaim_active_if_empty()
            return max(highest, self._database_high_watermark(connection))
        finally:
            connection.close()

    def _database_high_watermark(self, connection: sqlite3.Connection | None = None) -> int:
        owns_connection = connection is None
        current = connection or sqlite3.connect(self._path, timeout=30.0)
        try:
            row = current.execute(
                "SELECT COALESCE(MAX(position), 0) FROM logs"
            ).fetchone()
            return int(row[0] if row else 0)
        finally:
            if owns_connection:
                current.close()

    def submit(self, frame: _Frame) -> None:
        if not isinstance(frame, _Frame): raise TypeError("frame must be _Frame")
        use_spool = False
        with self._cv:
            self._raise_unavailable_locked()
            if not self._accepting: raise RuntimeError("Diagnostic writer is closing")
            if self.spooling or self._memory_bytes + frame.byte_size > self._memory_budget:
                self.spooling = True; self._inflight_spool += 1; use_spool = True
            else:
                self._memory.append(frame); self._memory_bytes += frame.byte_size; self._cv.notify_all()
        if use_spool:
            try:
                self.spool.append(frame.position, {**frame.payload, "_frame_digest": frame.digest})
            finally:
                with self._cv:
                    self._inflight_spool -= 1; self._cv.notify_all()

    def flush(self) -> None:
        self.spool.sync()
        with self._cv:
            self._cv.wait_for(lambda: self._error is not None or (
                not self._memory and self._memory_bytes == 0 and self.spool.pending_bytes() == 0
                and self._inflight_spool == 0 and not self._working
            ))
            if self._error is not None: raise RuntimeError(f"Diagnostic writer failed: {self._error}") from self._error

    def close(self) -> None:
        with self._cv:
            if self._closed:
                if self._close_error is not None:
                    raise self._close_error
                return
            self._accepting = False; self._cv.notify_all()
        error: BaseException | None = None
        try:
            self.flush()
        except BaseException as exc:
            error = exc
        with self._cv:
            self._closed = True; self._cv.notify_all()
        self._thread.join(timeout=10.0)
        try:
            self.spool.close()
        except BaseException as exc:
            if error is None:
                error = exc
        if self._thread.is_alive() and error is None:
            error = RuntimeError("Diagnostic writer did not stop cleanly")
        with self._cv:
            self._close_error = error
        if error is not None:
            raise error

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path, timeout=30.0)
            connection.execute("PRAGMA journal_mode=WAL"); connection.execute("PRAGMA synchronous=NORMAL")
            while True:
                batch, spool_acks = self._next_batch()
                if not batch:
                    with self._cv:
                        if self._closed and not self._memory and self.spool.pending_bytes() == 0: break
                        if not self._accepting and not self._memory and self.spool.pending_bytes() == 0 and self._inflight_spool == 0:
                            self._closed = True; self._cv.notify_all(); break
                    continue
                with self._cv: self._working = True
                highest = self._commit_batch(connection, batch)
                for path, end in spool_acks:
                    self.spool.acknowledge(path, end)
                if highest:
                    self._committed(highest)
                with self._cv:
                    self._working = False
                    if self.spooling and not self._memory and self._inflight_spool == 0 and self.spool.pending_bytes() == 0:
                        self.spool.reclaim_active_if_empty(); self.spooling = False
                    self._cv.notify_all()
        except BaseException as exc:
            self._error = exc
            # Preserve any in-memory accepted frames by moving them to spool.
            with self._cv:
                remaining = list(self._memory); self._memory.clear(); self._memory_bytes = 0; self._working = False
            for frame in remaining:
                try: self.spool.append(frame.position, {**frame.payload, "_frame_digest": frame.digest})
                except Exception: pass
            try: self.spool.sync()
            except Exception: pass
            with self._cv: self._cv.notify_all()
        finally:
            if connection is not None: connection.close()

    def _next_batch(self) -> tuple[list[_Frame], list[tuple[Path, int]]]:
        """Build one byte/latency-bounded batch without penalizing spool recovery.

        The latency deadline is only a coalescing delay when no work is available.
        If durable spool data already exists, it is read immediately in a
        provisional byte-bounded batch.  Durable spool offsets are advanced only
        after the canonical transaction commits.
        """
        deadline = time.monotonic() + self._batch_latency
        batch: list[_Frame] = []
        ack_by_path: dict[Path, int] = {}
        total = 0
        while total < self._batch_target:
            frame: _Frame | None = None
            spool_pending = self.spooling or self.spool.pending_bytes() > 0
            with self._cv:
                if self._memory:
                    frame = self._memory.popleft()
                    self._memory_bytes -= frame.byte_size
                    self._working = True
                elif not batch and not spool_pending:
                    remaining_wait = max(0.0, deadline - time.monotonic())
                    if remaining_wait > 0.0 and self._accepting:
                        self._cv.wait(timeout=remaining_wait)
                        # Re-evaluate both memory and spool after the coalescing wait.
                        continue
                    return [], []
            if frame is not None:
                batch.append(frame)
                total += frame.byte_size
                if time.monotonic() >= deadline:
                    break
                continue

            if spool_pending:
                remaining = max(1, self._batch_target - total)
                spool_items = self.spool.read_frames(remaining)
                if spool_items:
                    with self._cv:
                        self._working = True
                    # read_frames already enforces the byte budget.  Consume the
                    # full provisional batch even if the coalescing deadline has
                    # elapsed; otherwise pressure recovery degenerates to one
                    # frame per latency interval.
                    for raw, path, end_offset in spool_items:
                        payload = dict(raw.payload)
                        digest = str(payload.pop("_frame_digest", ""))
                        encoded = json.dumps(
                            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8")
                        recovered = _Frame(
                            raw.position,
                            payload,
                            encoded,
                            digest or hashlib.sha256(encoded).hexdigest(),
                            True,
                        )
                        batch.append(recovered)
                        total += recovered.byte_size
                        previous = ack_by_path.get(path, 0)
                        if end_offset > previous:
                            ack_by_path[path] = end_offset
                    break
            if batch or time.monotonic() >= deadline:
                break
        return batch, list(ack_by_path.items())

    def _commit_batch(self, connection: sqlite3.Connection, batch: list[_Frame]) -> int:
        persisted = datetime.now(timezone.utc).isoformat()
        highest = 0

        with connection:
            for frame in batch:
                p = frame.payload
                position = frame.position
                existing = connection.execute(
                    "SELECT frame_digest FROM logs WHERE position=?",
                    (position,),
                ).fetchone()

                if existing is not None:
                    if str(existing[0]) == frame.digest:
                        highest = max(highest, position)
                        continue

                    if not frame.recovered:
                        raise RuntimeError(
                            f"Diagnostic integrity conflict at JournalPosition {position}"
                        )

                    # A previous startup race could leave a durable spool frame with
                    # the same position as a different already-committed record.
                    # On recovery, preserve both records by relocating only the
                    # stale spool frame to the next canonical position.
                    row = connection.execute(
                        "SELECT COALESCE(MAX(position), 0) FROM logs"
                    ).fetchone()
                    position = int(row[0] if row else 0) + 1

                connection.execute(
                    """INSERT INTO logs(position,event_time,persisted_at,level,channel,source,message,application_session_id,
                    execution_id,correlation_id,parent_correlation_id,operation,code,filter_name_snapshot,
                    filter_path_snapshot,exception_type,exception_message,exception_stack,fields_json,frame_digest,schema_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (position,p["event_time"],persisted,p["level"],p["channel"],p["source"],p["message"],
                     p["application_session_id"],p.get("execution_id"),p.get("correlation_id"),
                     p.get("parent_correlation_id"),p.get("operation"),p.get("code"),p.get("filter_name_snapshot"),
                     p.get("filter_path_snapshot"),p.get("exception_type"),p.get("exception_message"),p.get("exception_stack"),
                     json.dumps(p.get("fields",{}),ensure_ascii=False,separators=(",",":")),frame.digest,2)
                )
                highest = max(highest, position)

        return highest

    def _raise_unavailable_locked(self) -> None:
        if self._error is not None: raise RuntimeError(f"Diagnostic writer failed: {self._error}") from self._error
        if self._closed: raise RuntimeError("Diagnostic writer is closed")
