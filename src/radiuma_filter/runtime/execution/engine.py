"""Execution planning and lazy bounded orchestration."""
from __future__ import annotations

import atexit, logging, os, threading, time, traceback
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ...api.errors import DuplicateComponentError, InvalidExecutionError, InvalidPipelineError
from ...api.execution import ErrorPolicy, ErrorStage, EventType, ExecutionError, ExecutionEvent, ExecutionObserver, ExecutionOptions, ExecutionResult, _OutputRecord
from ...api.extensions import FilterContext, LoadedImage, LogicalInput
from ...filters import Filter, FilterComponent, FilterPipeline
from ...api.identity import ApplicationSessionId, CorrelationId, ExecutionId
from ...api.logs import ExceptionInfo, ExecutionLogs, LogChannel, LogLevel
from ..extensions.registry import ExtensionRegistry
from ..io.inputs import InputResolver
from ..observability.journal import LogJournal
from ..observability.dispatcher import ObserverDispatcher, TerminalProgress

@dataclass(frozen=True, slots=True)
class PlannedStep:
    index: int
    filter: Filter

@dataclass(frozen=True, slots=True)
class RootPlan:
    root_index: int
    root: FilterComponent
    steps: tuple[PlannedStep, ...]

@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    roots: tuple[RootPlan, ...]
    options: ExecutionOptions

class ExecutionPlanner:
    """Plan one executable definition into independent roots and sequential steps."""

    def plan(
        self,
        executable: FilterComponent,
        *,
        options: ExecutionOptions,
    ) -> ExecutionPlan:
        if not isinstance(executable, FilterComponent):
            raise TypeError("executable must be FilterComponent")

        roots = self._independent_roots(executable, ancestry=())
        if not roots:
            raise InvalidExecutionError("at least one filter is required")

        seen: set[int] = set()
        plans: list[RootPlan] = []
        for root_index, root in enumerate(roots):
            marker = id(root)
            if marker in seen:
                raise DuplicateComponentError(
                    "the same FilterComponent instance cannot be an independent root twice"
                )
            seen.add(marker)

            leaves = self._sequence_steps(root, ancestry=())
            plans.append(
                RootPlan(
                    root_index=root_index,
                    root=root,
                    steps=tuple(
                        PlannedStep(index=index, filter=leaf)
                        for index, leaf in enumerate(leaves)
                    ),
                )
            )

        return ExecutionPlan(roots=tuple(plans), options=options)

    def _independent_roots(
        self,
        executable: FilterComponent,
        *,
        ancestry: tuple[int, ...],
    ) -> tuple[FilterComponent, ...]:
        if not isinstance(executable, FilterPipeline) or executable._is_sequence():
            return (executable,)

        marker = id(executable)
        if marker in ancestry:
            raise InvalidPipelineError("recursive FilterPipeline cycle detected")

        roots: list[FilterComponent] = []
        next_ancestry = (*ancestry, marker)
        for child in executable.children:
            roots.extend(self._independent_roots(child, ancestry=next_ancestry))
        return tuple(roots)

    def _sequence_steps(
        self,
        executable: FilterComponent,
        *,
        ancestry: tuple[int, ...],
    ) -> tuple[Filter, ...]:
        if isinstance(executable, Filter):
            return (executable,)
        if not isinstance(executable, FilterPipeline):
            raise TypeError("pipeline child must be FilterComponent")

        if executable._is_independent():
            raise InvalidPipelineError(
                "an independent FilterPipeline cannot be used as one step inside "
                "a sequential FilterPipeline"
            )

        marker = id(executable)
        if marker in ancestry:
            raise InvalidPipelineError("recursive FilterPipeline cycle detected")

        steps: list[Filter] = []
        next_ancestry = (*ancestry, marker)
        for child in executable.children:
            steps.extend(self._sequence_steps(child, ancestry=next_ancestry))
        return tuple(steps)

_LOG = logging.getLogger(__name__)

class _FilterStepFailure(Exception):
    __slots__ = ("filter", "original", "step_index")

    def __init__(
        self,
        filter: Filter,
        original: BaseException,
        *,
        step_index: int,
    ) -> None:
        super().__init__(str(original))
        self.filter = filter
        self.original = original
        self.step_index = int(step_index)

class _ExecutionSession:
    def __init__(
        self,
        *,
        execution_id: ExecutionId,
        plan: ExecutionPlan,
        inputs: object | Iterable[object],
        registry: ExtensionRegistry,
        resolver: InputResolver,
        journal: LogJournal,
        emit: Callable[[ExecutionEvent], None],
        errors: list[ExecutionError],
    ) -> None:
        self.execution_id = execution_id
        self.plan = plan
        self.inputs = inputs
        self.registry = registry
        self.resolver = resolver
        self.journal = journal
        self.errors = errors
        self._emit_callback = emit
        self._roots = tuple(root.root for root in plan.roots)
        self._errors_lock = threading.RLock()
        self._sequence_lock = threading.Lock()
        self._sequence = 0
        self._journal_failure_lock = threading.Lock()
        self._journal_failure_reported = False
        self._correlation_id = CorrelationId.new()
        self._cancel = threading.Event()
        self._pool_lock = threading.RLock()
        self._pool: ThreadPoolExecutor | None = None
        self._closed = False
        self._filter_steps_per_input = sum(len(root.steps) for root in plan.roots)
        # Overall progress counts actual filter invocations only. Input loading
        # and terminal bookkeeping are shown separately and do not inflate the
        # filter denominator.
        self._work_per_input = self._filter_steps_per_input
        self._progress = TerminalProgress(
            execution_id,
            enabled=plan.options.progress,
        )

    def close(self) -> None:
        with self._pool_lock:
            if self._closed:
                return
            self._closed = True
            self._cancel.set()
            pool = self._pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        self._progress.cancel()

    def stream(self) -> Iterator[_OutputRecord]:
        return iter(()) if self._closed else self._generate()

    def _generate(self) -> Iterator[_OutputRecord]:
        execution_started_at = time.monotonic()
        self._event(EventType.EXECUTION_STARTED)
        self._log(LogLevel.INFO, "Filtering execution started", operation="execute")
        discovery_failure: BaseException | None = None
        source_iter = iter(self.resolver.discover(self.inputs))

        if self.plan.options.progress:
            self._progress.start_discovery()
            discovered: list[LogicalInput] = []
            try:
                for logical in source_iter:
                    discovered.append(logical)
                    self._progress.discovered(logical.key)
            except BaseException as exc:
                discovery_failure = exc
            source_iter = iter(discovered)
            self._progress.begin_execution(
                total_inputs=len(discovered),
                total_work=len(discovered) * self._work_per_input,
            )

        pool = ThreadPoolExecutor(
            max_workers=self.plan.options.max_in_flight,
            thread_name_prefix=f"radiuma-filter-{str(self.execution_id)[:8]}",
        )
        with self._pool_lock:
            if self._closed:
                pool.shutdown(wait=False, cancel_futures=True)
                return
            self._pool = pool

        pending: deque[Future[_OutputRecord]] = deque()
        discovery_done = False

        def fill() -> None:
            nonlocal discovery_done, discovery_failure
            while (
                not discovery_done
                and not self._cancel.is_set()
                and len(pending) < self.plan.options.max_in_flight
            ):
                try:
                    logical = next(source_iter)
                except StopIteration:
                    discovery_done = True
                    return
                except BaseException as exc:
                    discovery_done = True
                    discovery_failure = exc
                    return
                pending.append(pool.submit(self._process_input, logical))

        try:
            fill()
            while pending and not self._cancel.is_set():
                future = pending.popleft()
                try:
                    output = future.result()
                except BaseException:
                    self._cancel.set()
                    for remaining in pending:
                        remaining.cancel()
                    raise
                # Result iteration is lazy. Temporarily remove the live
                # terminal line while caller code handles this record so normal
                # ``print()`` output cannot collide with the progress renderer.
                self._progress.suspend()
                try:
                    yield output
                except GeneratorExit:
                    self._progress.cancel()
                    raise
                else:
                    self._progress.resume()
                fill()

            if self._cancel.is_set():
                return

            if discovery_failure is not None:
                error = ExecutionError(
                    stage=ErrorStage.DISCOVER,
                    exception=discovery_failure,
                )
                self._record_error(error)
                self._event(EventType.PROBLEM, error=discovery_failure)
                self._log(
                    LogLevel.ERROR,
                    "Input discovery failed",
                    operation="discover",
                    exception=discovery_failure,
                )
                if self.plan.options.error_policy is ErrorPolicy.RAISE:
                    raise discovery_failure

            self._event(EventType.EXECUTION_COMPLETED)
            self._log(
                LogLevel.INFO if not self.errors else LogLevel.WARNING,
                "Filtering execution completed",
                operation="execute",
                fields={
                    "error_count": len(self.errors),
                    "duration_seconds": max(0.0, time.monotonic() - execution_started_at),
                },
            )
            self._progress.complete(errors=len(self.errors))
        except GeneratorExit:
            self._cancel.set()
            self._progress.cancel()
            raise
        except BaseException as exc:
            self._progress.fail(exc)
            self._event(EventType.EXECUTION_FAILED, error=exc)
            self._log(
                LogLevel.ERROR,
                "Filtering execution failed",
                operation="execute",
                fields={
                    "duration_seconds": max(0.0, time.monotonic() - execution_started_at),
                },
                exception=exc,
            )
            raise
        finally:
            self._cancel.set()
            with self._pool_lock:
                current = self._pool
                self._pool = None
            if current is not None:
                current.shutdown(wait=True, cancel_futures=True)
            self._progress.close()

    def _process_input(self, logical: LogicalInput) -> _OutputRecord:
        values: list[tuple[Filter, Any]] = []
        output_errors: list[ExecutionError] = []

        if self._cancel.is_set():
            return self._build_output(logical, values, output_errors)

        self._progress.loading(key=logical.key)
        try:
            image = self.resolver.load(logical)
        except BaseException as exc:
            error = ExecutionError(
                stage=ErrorStage.LOAD,
                exception=exc,
                input_index=logical.index,
                input_key=logical.key,
            )
            self._record_error(error)
            output_errors.append(error)
            self._event(EventType.PROBLEM, input_index=logical.index, error=exc)
            self._log(
                LogLevel.ERROR,
                "Input load failed",
                input_index=logical.index,
                operation="load",
                fields={"input_key": logical.key},
                exception=exc,
            )
            if self.plan.options.error_policy is ErrorPolicy.RAISE:
                raise
            self._progress.load_failed(
                skipped_filter_steps=self._filter_steps_per_input,
            )
            return self._build_output(logical, values, output_errors)

        try:
            for root in self.plan.roots:
                if self._cancel.is_set():
                    break
                try:
                    value = self._execute_root(root, logical, image)
                except _FilterStepFailure as failure:
                    error = ExecutionError(
                        stage=ErrorStage.FILTER,
                        exception=failure.original,
                        input_index=logical.index,
                        input_key=logical.key,
                        root_component=root.root,
                        filter=failure.filter,
                    )
                    self._record_error(error)
                    output_errors.append(error)
                    if self.plan.options.error_policy is ErrorPolicy.RAISE:
                        raise failure.original
                    self._progress.work_completed(
                        len(root.steps) - failure.step_index - 1
                    )
                    continue
                values.append((root.root, value))
            return self._build_output(logical, values, output_errors)
        finally:
            self._progress.input_completed()

    def _build_output(
        self,
        logical: LogicalInput,
        values: list[tuple[FilterComponent, Any]],
        errors: list[ExecutionError],
    ) -> _OutputRecord:
        return _OutputRecord(
            index=logical.index,
            key=logical.key,
            roots=self._roots,
            values=tuple(values),
            errors=tuple(errors),
        )

    def _execute_root(
        self,
        root: RootPlan,
        logical: LogicalInput,
        image: Any,
    ) -> Any:
        current = image
        for step in root.steps:
            executable = step.filter
            if self._cancel.is_set():
                return current.data if isinstance(current, LoadedImage) else current

            filter_name = executable._execution_name()
            dimension_mode, input_dimension, slice_count = self._dimension_mode(
                executable,
                current,
            )
            display_name = filter_name
            if dimension_mode == "slice_2d":
                display_name = f"{filter_name} [auto 2D x {slice_count}]"
            elif dimension_mode == "unsupported":
                display_name = f"{filter_name} [{input_dimension}D unsupported]"

            progress_activity = self._progress.filter_started(
                key=logical.key,
                filter_name=display_name,
            )
            filter_started_at = time.monotonic()
            event_fields = {
                "root_component": root.root,
                "filter": executable,
                "input_index": logical.index,
            }
            log_fields = {
                "step_index": step.index,
                "root_index": root.root_index,
            }
            self._event(EventType.FILTER_STARTED, **event_fields)
            self._log(
                LogLevel.DEBUG,
                "Filter started",
                operation="filter",
                fields=log_fields,
                **event_fields,
            )

            try:
                backend = self.registry.resolve_filter_backend(
                    executable,
                    device=self.plan.options.device,
                )
                context = FilterContext(
                    execution_id=self.execution_id,
                    correlation_id=self._correlation_id,
                    root_index=root.root_index,
                    input_index=logical.index,
                    step_index=step.index,
                    device=self.plan.options.device,
                )
                if dimension_mode == "slice_2d":
                    self._log(
                        LogLevel.INFO,
                        "Automatically adapting 3D input to 2D filter slices",
                        operation="filter.dimension",
                        fields={
                            **log_fields,
                            "input_dimension": input_dimension,
                            "supported_dimensions": type(executable)._dimension_description(),
                            "slice_count": slice_count,
                            "adaptation": "3D_to_2D_slices",
                        },
                        **event_fields,
                    )

                produced = self._execute_filter(
                    backend=backend,
                    filter=executable,
                    image=current,
                    context=context,
                    mode=dimension_mode,
                    input_dimension=input_dimension,
                    slice_count=slice_count,
                    filter_name=filter_name,
                    progress_activity=progress_activity,
                )
                current = (
                    current.with_data(produced)
                    if isinstance(current, LoadedImage)
                    and not isinstance(produced, LoadedImage)
                    else produced
                )
            except BaseException as exc:
                filter_duration = max(0.0, time.monotonic() - filter_started_at)
                self._event(EventType.FILTER_FAILED, error=exc, **event_fields)
                self._log(
                    LogLevel.ERROR,
                    "Filter failed",
                    operation="filter",
                    fields={**log_fields, "duration_seconds": filter_duration},
                    exception=exc,
                    **event_fields,
                )
                self._progress.filter_finished(progress_activity, failed=True)
                raise _FilterStepFailure(
                    executable,
                    exc,
                    step_index=step.index,
                ) from exc

            filter_duration = max(0.0, time.monotonic() - filter_started_at)
            self._progress.filter_finished(progress_activity)
            self._event(EventType.FILTER_COMPLETED, **event_fields)
            self._log(
                LogLevel.DEBUG,
                "Filter completed",
                operation="filter",
                fields={**log_fields, "duration_seconds": filter_duration},
                **event_fields,
            )

        return current.data if isinstance(current, LoadedImage) else current

    @staticmethod
    def _dimension_mode(
        filter: Filter,
        image: Any,
    ) -> tuple[str, int | None, int | None]:
        dimensions = type(filter)._supported_dimensions
        if dimensions is None:
            return "direct", None, None

        source = image.data if isinstance(image, LoadedImage) else image
        try:
            array = np.asarray(source)
        except Exception:
            # The concrete backend/filter remains responsible for reporting a
            # conversion problem when dimensionality cannot be inspected.
            return "direct", None, None

        input_dimension = int(array.ndim)
        mode = type(filter)._dimension_mode(input_dimension)
        slice_axis = type(filter)._auto_slice_axis if mode == "slice_2d" else None
        if slice_axis is not None:
            axis = int(slice_axis)
            if axis < 0:
                axis += input_dimension
            if not 0 <= axis < input_dimension:
                return "unsupported", input_dimension, None
            slice_count = int(array.shape[axis])
        else:
            slice_count = None
        return mode, input_dimension, slice_count

    def _execute_filter(
        self,
        *,
        backend: Any,
        filter: Filter,
        image: Any,
        context: FilterContext,
        mode: str,
        input_dimension: int | None,
        slice_count: int | None,
        filter_name: str,
        progress_activity: int | None,
    ) -> Any:
        if mode == "direct":
            return backend.execute(filter, image=image, context=context)

        if mode == "unsupported":
            supported = type(filter)._dimension_description()
            raise ValueError(
                f"{filter_name} supports {supported} input; received "
                f"{input_dimension}D. Dimensional adaptation is handled "
                "automatically when the filter declares a safe adaptation."
            )

        if mode != "slice_2d":
            raise RuntimeError(f"unknown filter dimension mode: {mode!r}")

        source = image.data if isinstance(image, LoadedImage) else np.asarray(image)
        axis = int(type(filter)._auto_slice_axis or 0)
        if axis < 0:
            axis += source.ndim
        total = int(slice_count or source.shape[axis])
        outputs: list[np.ndarray] = []

        for index in range(total):
            self._progress.filter_detail(
                progress_activity,
                f"{filter_name} [auto 2D {index + 1}/{total}]",
            )
            slice_data = np.take(source, index, axis=axis)
            if isinstance(image, LoadedImage):
                slice_spacing = tuple(
                    value for position, value in enumerate(image.spacing)
                    if position != axis
                )
                slice_image: Any = LoadedImage(
                    data=slice_data,
                    spacing=slice_spacing,
                    metadata=image.metadata,
                    source=image.source,
                )
            else:
                slice_image = slice_data

            produced = backend.execute(
                filter,
                image=slice_image,
                context=context,
            )
            produced_data = (
                produced.data if isinstance(produced, LoadedImage) else produced
            )
            array = np.asarray(produced_data)
            if array.ndim != 2:
                raise ValueError(
                    f"{filter_name} automatic slice execution must return a "
                    f"2D image for every slice; slice {index} returned "
                    f"{array.ndim}D"
                )
            outputs.append(array)

        if not outputs:
            raise ValueError(f"{filter_name} cannot process an empty 3D volume")

        try:
            return np.stack(outputs, axis=axis)
        except ValueError as exc:
            raise ValueError(
                f"{filter_name} produced inconsistent 2D slice shapes during "
                "automatic 3D reconstruction"
            ) from exc

    def _record_error(self, error: ExecutionError) -> None:
        with self._errors_lock:
            self.errors.append(error)

    def _event(
        self,
        event_type: EventType,
        *,
        root_component: FilterComponent | None = None,
        filter: Filter | None = None,
        input_index: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        event = ExecutionEvent(
            type=event_type,
            execution_id=self.execution_id,
            timestamp=datetime.now(timezone.utc),
            sequence=sequence,
            correlation_id=self._correlation_id,
            root_component=root_component,
            filter=filter,
            input_index=input_index,
            error=error,
        )
        try:
            self._emit_callback(event)
        except Exception:
            _LOG.exception("Semantic event dispatcher failed")

    def _log(
        self,
        level: LogLevel,
        message: str,
        *,
        root_component: FilterComponent | None = None,
        filter: Filter | None = None,
        input_index: int | None = None,
        operation: str | None = None,
        fields: dict[str, Any] | None = None,
        exception: BaseException | None = None,
    ) -> None:
        payload = dict(fields or {})
        if input_index is not None:
            payload["input_index"] = input_index
        exception_info = None
        if exception is not None:
            stack = "".join(
                traceback.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            )
            exception_info = ExceptionInfo.from_exception(exception, stack=stack)
        try:
            self.journal.append(
                level=level,
                message=message,
                channel=LogChannel.EXECUTION,
                source="filtering.execution",
                execution_id=self.execution_id,
                correlation_id=self._correlation_id,
                operation=operation,
                filter_name_snapshot=(filter or root_component)._execution_name() if (filter or root_component) is not None else None,
                filter_path_snapshot=self._filter_path(root_component, filter),
                exception=exception_info,
                fields=payload,
            )
        except Exception:
            # Diagnostics are secondary to filtering. Report a writer failure once
            # per execution instead of flooding stderr on every later event.
            with self._journal_failure_lock:
                if not self._journal_failure_reported:
                    self._journal_failure_reported = True
                    _LOG.exception("Diagnostic journal write failed")

    @staticmethod
    def _filter_path(
        root_component: FilterComponent | None,
        filter: Filter | None,
    ) -> str | None:
        if root_component is None and filter is None:
            return None
        root_name = None if root_component is None else root_component._execution_name()
        filter_name = None if filter is None else filter._execution_name()
        if root_component is None or filter is root_component or filter is None:
            return root_name or filter_name
        return f"{root_name}/{filter_name}"

class ExecutionEngine:
    def __init__(
        self,
        *,
        journal: LogJournal,
        emit: Callable[[ExecutionEvent], None],
    ) -> None:
        self._registry = ExtensionRegistry()
        self._resolver = InputResolver(self._registry)
        self._planner = ExecutionPlanner()
        self._journal = journal
        self._emit = emit
        self._lock = threading.RLock()
        self._sessions: set[_ExecutionSession] = set()
        self._closed = False

    def execute(
        self,
        execution_id: ExecutionId,
        executable: FilterComponent,
        *,
        inputs: object | Iterable[object],
        options: ExecutionOptions,
        on_close: Callable[[], None] | None = None,
    ) -> ExecutionResult:
        with self._lock:
            if self._closed:
                raise RuntimeError("ExecutionEngine has been closed")

        plan = self._planner.plan(executable, options=options)
        errors: list[ExecutionError] = []
        session = _ExecutionSession(
            execution_id=execution_id,
            plan=plan,
            inputs=inputs,
            registry=self._registry,
            resolver=self._resolver,
            journal=self._journal,
            emit=self._emit,
            errors=errors,
        )
        with self._lock:
            self._sessions.add(session)

        close_lock = threading.Lock()
        closed = False

        def close_session() -> None:
            nonlocal closed
            with close_lock:
                if closed:
                    return
                closed = True
            try:
                session.close()
                with self._lock:
                    self._sessions.discard(session)
            finally:
                if on_close is not None:
                    on_close()

        return ExecutionResult(
            execution_id=execution_id,
            roots=tuple(root.root for root in plan.roots),
            logs=ExecutionLogs._create(self._journal.view(execution_id)),
            stream_factory=session.stream,
            stream_close=close_session,
            errors=errors,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()


class _RuntimeManager:
    """Process-private execution infrastructure behind FilterComponent.execute()."""

    _instance_lock = threading.Lock()
    _instance: "_RuntimeManager | None" = None

    @classmethod
    def instance(cls) -> "_RuntimeManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def shutdown_global(cls) -> None:
        with cls._instance_lock:
            runtime = cls._instance
            cls._instance = None
        if runtime is not None:
            runtime.close()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._observers: dict[str, tuple[ExecutionObserver, ...]] = {}
        configured = os.environ.get("RADIUMA_FILTER_DATA_ROOT", "").strip()
        data_root = (
            os.path.expanduser(configured)
            if configured
            else os.path.join(os.path.expanduser("~"), ".radiuma_filter")
        )
        journal_path = os.path.join(data_root, "diagnostics", "journal.sqlite3")
        self._journal = LogJournal(
            journal_path,
            session_id=ApplicationSessionId.new(),
        )
        self._dispatcher = ObserverDispatcher(observers=self._observer_snapshot)
        self._engine = ExecutionEngine(
            journal=self._journal,
            emit=self._dispatcher.emit,
        )

    def execute(
        self,
        executable: FilterComponent,
        *,
        inputs: object | Iterable[object],
        options: ExecutionOptions | None,
        observers: ExecutionObserver | Iterable[ExecutionObserver] | None,
    ) -> ExecutionResult:
        resolved_options = options or ExecutionOptions()
        if not isinstance(resolved_options, ExecutionOptions):
            raise TypeError("options must be ExecutionOptions")

        execution_id = ExecutionId.new()
        resolved_observers = self._normalize_observers(observers)
        key = str(execution_id)

        with self._lock:
            if self._closed:
                raise RuntimeError("filter runtime has been closed")
            self._observers[key] = resolved_observers

        try:
            return self._engine.execute(
                execution_id,
                executable,
                inputs=inputs,
                options=resolved_options,
                on_close=lambda: self._release_execution(key),
            )
        except BaseException:
            self._release_execution(key)
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._observers.clear()

        errors: list[BaseException] = []
        for close in (self._engine.close, self._dispatcher.close, self._journal.close):
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            _LOG.error("Private filter runtime close failed: %s", errors[0])

    def _release_execution(self, key: str) -> None:
        with self._lock:
            self._observers.pop(key, None)

    def _observer_snapshot(self, event: ExecutionEvent) -> tuple[ExecutionObserver, ...]:
        with self._lock:
            return self._observers.get(str(event.execution_id), ())

    @staticmethod
    def _normalize_observers(
        observers: ExecutionObserver | Iterable[ExecutionObserver] | None,
    ) -> tuple[ExecutionObserver, ...]:
        if observers is None:
            return ()
        if isinstance(observers, ExecutionObserver):
            values = (observers,)
        else:
            try:
                values = tuple(observers)
            except TypeError as exc:
                raise TypeError(
                    "observers must be an ExecutionObserver or iterable of observers"
                ) from exc
        for index, observer in enumerate(values):
            if not isinstance(observer, ExecutionObserver):
                raise TypeError(
                    f"observers[{index}] must implement ExecutionObserver"
                )
        # Identity de-duplication preserves registration order.
        unique: list[ExecutionObserver] = []
        seen: set[int] = set()
        for observer in values:
            marker = id(observer)
            if marker not in seen:
                seen.add(marker)
                unique.append(observer)
        return tuple(unique)


atexit.register(_RuntimeManager.shutdown_global)
