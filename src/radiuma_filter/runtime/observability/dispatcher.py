"""Observer delivery and optional terminal progress presentation."""
from __future__ import annotations

import logging, math, shutil, sys, threading, time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from ...api.execution import EventType, ExecutionEvent, ExecutionObserver
from ...api.identity import ExecutionId

_LOG = logging.getLogger(__name__)

_SOFT_LIMIT = 4096

_DROPPABLE = {EventType.FILTER_STARTED, EventType.FILTER_COMPLETED}

class ObserverDispatcher:
    """Single-worker FIFO dispatcher owned by the private filtering runtime."""

    def __init__(
        self,
        *,
        observers: Callable[[ExecutionEvent], tuple[ExecutionObserver, ...]],
    ) -> None:
        self._observers = observers
        self._condition = threading.Condition(threading.RLock())
        self._pending: deque[tuple[ExecutionEvent, tuple[ExecutionObserver, ...]]] = deque()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="radiuma-filtering-observer",
            daemon=True,
        )
        self._thread.start()

    def emit(self, event: ExecutionEvent) -> None:
        with self._condition:
            if self._closed:
                return
            if len(self._pending) >= _SOFT_LIMIT:
                if event.type in _DROPPABLE:
                    return
                for index, (existing, _) in enumerate(self._pending):
                    if existing.type in _DROPPABLE:
                        del self._pending[index]
                        break
            self._pending.append((event, self._observers(event)))
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if not self._pending and self._closed:
                    return
                event, observers = self._pending.popleft()

            for observer in observers:
                try:
                    observer.on_event(event)
                except Exception:
                    _LOG.exception(
                        "Execution observer failed while handling %s",
                        event.type.value,
                    )

@dataclass(slots=True)
class _ProgressState:
    phase: str = "Preparing"
    discovered_inputs: int = 0
    total_inputs: int | None = None
    completed_inputs: int = 0
    completed_work: int = 0
    total_work: int | None = None
    current_input: str | None = None
    errors: int = 0
    status: str | None = None

@dataclass(slots=True)
class _FilterActivity:
    key: str
    filter_name: str
    detail: str
    started_at: float
    updated_at: float

class TerminalProgress:
    """Terminal presentation for one complete filter/group ``execute`` call.

    The progress bar always represents the *whole execution*. Its numerator is
    completed filter invocations across all logical inputs and its denominator
    is the total number of filter invocations planned for that execution.

    Current input/filter information is secondary status only and never changes
    the meaning of the bar. The renderer uses carriage returns and spaces only,
    avoiding ANSI escape sequences so Windows consoles that do not enable ANSI
    processing cannot print control codes such as ``ESC[2K`` literally.
    """

    _BAR_WIDTH = 24

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None or seconds < 0 or not math.isfinite(seconds):
            return "--:--"
        total = int(round(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _clean(value: object, *, maximum: int = 40) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= maximum:
            return text
        return text[: max(1, maximum - 1)] + "…"

    def __init__(
        self,
        execution_id: ExecutionId,
        *,
        enabled: bool,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.execution_id = ExecutionId.parse(execution_id)
        self._stream = stream or sys.stderr
        self._lock = threading.RLock()
        self._state = _ProgressState()
        self._started_at: float | None = None
        self._work_started_at: float | None = None
        self._activities: dict[int, _FilterActivity] = {}
        self._next_activity_id = 0
        self._last_width = 0
        self._last_line: str | None = None
        self._suspended = False
        self._closed = False
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def start_discovery(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._started_at is None:
                self._started_at = time.monotonic()
            self._state.phase = "Discovering"
            self._ensure_heartbeat_locked()
            self._render_locked()

    def discovered(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state.discovered_inputs += 1
            self._state.current_input = self._clean(key)
            self._render_locked()

    def begin_execution(self, *, total_inputs: int, total_work: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            if self._started_at is None:
                self._started_at = now
            self._work_started_at = now
            self._state.phase = "Filtering"
            self._state.total_inputs = int(total_inputs)
            self._state.total_work = int(total_work)
            self._state.completed_inputs = 0
            self._state.completed_work = 0
            self._activities.clear()
            self._ensure_heartbeat_locked()
            self._render_locked()

    def loading(self, *, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state.phase = "Loading"
            self._state.current_input = self._clean(key)
            self._render_locked()

    def work_completed(self, count: int = 1) -> None:
        if not self.enabled or count <= 0:
            return
        with self._lock:
            self._advance_locked(count)

    def filter_started(self, *, key: str, filter_name: str) -> int | None:
        if not self.enabled:
            return None
        with self._lock:
            now = time.monotonic()
            self._state.phase = "Filtering"
            self._state.current_input = self._clean(key)
            self._next_activity_id += 1
            activity_id = self._next_activity_id
            clean_name = self._clean(filter_name)
            self._activities[activity_id] = _FilterActivity(
                key=self._clean(key),
                filter_name=clean_name,
                detail=clean_name,
                started_at=now,
                updated_at=now,
            )
            self._render_locked()
            return activity_id

    def filter_detail(self, activity_id: int | None, detail: str) -> None:
        if not self.enabled or activity_id is None:
            return
        with self._lock:
            activity = self._activities.get(activity_id)
            if activity is None:
                return
            activity.detail = self._clean(detail)
            activity.updated_at = time.monotonic()
            self._render_locked()

    def filter_finished(
        self,
        activity_id: int | None,
        *,
        failed: bool = False,
    ) -> float | None:
        if not self.enabled:
            return None
        with self._lock:
            now = time.monotonic()
            activity = (
                self._activities.pop(activity_id, None)
                if activity_id is not None
                else None
            )
            duration = (
                None
                if activity is None
                else max(0.0, now - activity.started_at)
            )
            if failed:
                self._state.errors += 1
            self._advance_locked()
            return duration

    def input_completed(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state.completed_inputs += 1
            self._render_locked()

    def load_failed(self, *, skipped_filter_steps: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state.errors += 1
            self._state.completed_inputs += 1
            self._advance_locked(max(0, int(skipped_filter_steps)))

    def complete(self, *, errors: int) -> None:
        error_count = max(0, int(errors))
        self._finish(
            phase="Completed",
            status=(
                "completed"
                if not error_count
                else f"completed with {error_count} error(s)"
            ),
            errors=error_count,
            complete_work=True,
        )

    def fail(self, exc: BaseException) -> None:
        self._finish(
            phase="Failed",
            status=f"failed: {self._clean(type(exc).__name__, maximum=24)}",
            errors=1,
        )

    def cancel(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._closed:
                return
            if self._started_at is None:
                self._closed = True
                self._stop.set()
                return
        self._finish(phase="Cancelled", status="cancelled")

    def _finish(
        self,
        *,
        phase: str,
        status: str,
        errors: int = 0,
        complete_work: bool = False,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._closed:
                return
            self._state.phase = phase
            self._state.status = status
            self._state.errors = max(self._state.errors, int(errors))
            if complete_work and self._state.total_work is not None:
                self._state.completed_work = max(
                    self._state.completed_work,
                    self._state.total_work,
                )
            self._activities.clear()
            self._render_locked(final=True)
        self.close()

    def _advance_locked(self, count: int = 1) -> None:
        self._state.completed_work += int(count)
        self._render_locked()

    def suspend(self) -> None:
        """Clear and pause the live line while caller code handles a result."""
        if not self.enabled:
            return
        with self._lock:
            if self._closed or self._suspended:
                return
            self._suspended = True
            self._clear_line_locked()

    def resume(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._closed or not self._suspended:
                return
            self._suspended = False
            self._render_locked()

    def close(self) -> None:
        if not self.enabled:
            return
        heartbeat: threading.Thread | None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            heartbeat = self._heartbeat
            self._heartbeat = None
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=1.0)

    def _ensure_heartbeat_locked(self) -> None:
        if self._heartbeat is not None or self._closed:
            return
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"filter-progress-{str(self.execution_id)[:8]}",
            daemon=True,
        )
        self._heartbeat = thread
        thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(0.25):
            with self._lock:
                if self._closed:
                    return
                if not self._suspended:
                    self._render_locked()

    def _is_tty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def _terminal_columns(self) -> int:
        try:
            columns = shutil.get_terminal_size(fallback=(120, 24)).columns
        except Exception:
            columns = 120
        return max(50, int(columns))

    def _clear_line_locked(self) -> None:
        if self._last_width <= 0 or not self._is_tty():
            self._last_width = 0
            self._last_line = None
            return
        try:
            # Portable line clearing: no ANSI dependency. This works in classic
            # Windows console hosts as well as ANSI-capable terminals.
            self._stream.write("\r" + (" " * self._last_width) + "\r")
            self._stream.flush()
            self._last_width = 0
            self._last_line = None
        except Exception:
            self.enabled = False
            self._stop.set()

    def _fit_line(self, line: str) -> str:
        maximum = max(30, self._terminal_columns() - 1)
        if len(line) <= maximum:
            return line
        if maximum <= 3:
            return line[:maximum]
        return line[: maximum - 3] + "..."

    def _render_locked(self, *, final: bool = False) -> None:
        if (self._closed and not final) or (self._suspended and not final):
            return

        # Redirected output should be log-friendly rather than emitting a stream
        # of carriage-return frames. Only the terminal gets live refreshes.
        is_tty = self._is_tty()
        if not is_tty and not final:
            return

        now = time.monotonic()
        started = self._started_at or now
        overall_elapsed = max(0.0, now - started)
        state = self._state

        if state.total_work is None:
            line = (
                f"Overall execution | {state.phase}"
                f" | inputs discovered {state.discovered_inputs}"
                f" | elapsed {self._format_duration(overall_elapsed)}"
            )
        else:
            total = max(0, state.total_work)
            completed = min(max(0, state.completed_work), total) if total else 0
            ratio = (completed / total) if total else 1.0
            percent = ratio * 100.0

            work_elapsed = (
                None
                if self._work_started_at is None
                else max(0.0, now - self._work_started_at)
            )
            remaining = None
            if work_elapsed is not None and work_elapsed > 0 and completed > 0:
                rate = completed / work_elapsed
                if rate > 0:
                    remaining = max(0, total - completed) / rate

            input_text = ""
            if state.total_inputs is not None:
                input_text = (
                    f" | inputs {state.completed_inputs}/{state.total_inputs}"
                )

            status_text = ""
            if final:
                status_text = f" | {state.phase}"
            elif self._activities:
                activity = max(
                    self._activities.values(),
                    key=lambda item: item.updated_at,
                )
                current_elapsed = max(0.0, now - activity.started_at)
                status_text = (
                    f" || current {activity.detail}"
                    f" on {activity.key}"
                    f" | current elapsed {self._format_duration(current_elapsed)}"
                )
                if len(self._activities) > 1:
                    status_text += f" | active {len(self._activities)}"

            error_text = f" | errors {state.errors}" if state.errors else ""
            remaining_text = (
                ""
                if final or remaining is None
                else f" | est. remaining {self._format_duration(remaining)}"
            )

            # The bar has exactly one meaning: overall execution completion by
            # filter invocations across all inputs. Everything after it is
            # secondary status.
            fixed_tail = (
                f" {percent:5.1f}%"
                f" | filter-runs {completed}/{total}"
                f"{input_text}"
                f" | elapsed {self._format_duration(overall_elapsed)}"
                f"{remaining_text}"
                f"{status_text}"
                f"{error_text}"
            )
            prefix = "Overall execution "
            columns = self._terminal_columns()
            available_bar = columns - len(prefix) - len(fixed_tail) - 2
            bar_width = max(8, min(self._BAR_WIDTH, available_bar))
            filled = int(round(bar_width * ratio))
            bar = "#" * filled + "-" * (bar_width - filled)
            line = f"{prefix}[{bar}]" + fixed_tail

        line = self._fit_line(line)
        if not final and line == self._last_line:
            return

        try:
            if is_tty:
                padding = " " * max(0, self._last_width - len(line))
                self._stream.write("\r" + line + padding)
                if final:
                    self._stream.write("\n")
                    self._last_width = 0
                    self._last_line = None
                else:
                    self._last_width = len(line)
                    self._last_line = line
            else:
                self._stream.write(line + "\n")
                self._last_width = 0
                self._last_line = None
            self._stream.flush()
        except Exception:
            self.enabled = False
            self._stop.set()
