"""Core filtering component hierarchy."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, TYPE_CHECKING

from ...api.errors import InvalidPipelineError

if TYPE_CHECKING:
    from ...api.execution import ExecutionObserver, ExecutionOptions, ExecutionResult


class FilterComponent:
    """Common Component in the filtering Composite hierarchy.

    ``Filter`` is the leaf abstraction and ``FilterPipeline`` is the composite.
    Both expose the same ``execute(inputs=...)`` operation. Runtime concerns such
    as discovery, scheduling, progress, diagnostics, observers, and backend
    selection remain delegated to private infrastructure.
    """

    __slots__ = ()
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def execute(
        self,
        *,
        inputs: object | Iterable[object],
        options: "ExecutionOptions | None" = None,
        observers: "ExecutionObserver | Iterable[ExecutionObserver] | None" = None,
    ) -> "ExecutionResult":
        """Execute this component over one or more logical inputs."""
        from ...runtime.execution.engine import _RuntimeManager

        return _RuntimeManager.instance().execute(
            self,
            inputs=inputs,
            options=options,
            observers=observers,
        )

    def _execution_name(self) -> str:
        """Stable runtime-facing label without exposing orchestration publicly."""
        return type(self).__name__


class Filter(FilterComponent):
    """Leaf filter definition.

    A leaf owns filter parameters, validation, dimensional capability metadata,
    and optionally a built-in numerical implementation. It does not own runtime
    orchestration.
    """

    __slots__ = ()

    _preserved_standardized: ClassVar[bool] = False
    _supported_dimensions: ClassVar[tuple[int, ...] | None] = None
    _auto_slice_axis: ClassVar[int | None] = None

    def _apply(self, image: Any) -> Any:
        """Protected built-in CPU operation used only for primitive leaves."""
        raise NotImplementedError

    @classmethod
    def _has_builtin_implementation(cls) -> bool:
        return cls._apply is not Filter._apply

    @classmethod
    def _dimension_mode(cls, input_dimension: int) -> str:
        dimensions = cls._supported_dimensions
        if dimensions is None or input_dimension in dimensions:
            return "direct"
        if input_dimension == 3 and cls._auto_slice_axis is not None and 2 in dimensions:
            return "slice_2d"
        return "unsupported"

    @classmethod
    def _dimension_description(cls) -> str:
        dimensions = cls._supported_dimensions
        if dimensions is None:
            return "backend-defined"
        return "/".join(f"{value}D" for value in dimensions)


@dataclass(frozen=True, slots=True, eq=False, init=False)
class FilterPipeline(FilterComponent):
    """Immutable composition of filtering components.

    ``FilterPipeline(a, b, c)`` and ``FilterPipeline.sequence(a, b, c)`` are
    sequential composition::

        input -> a -> b -> c -> output

    ``FilterPipeline.independent(a, b, c)`` creates independent roots over the
    same logical input(s). The composition policy remains private to the
    pipeline; users express intent through these construction forms rather than
    through a public topology enum.
    """

    _SEQUENCE: ClassVar[str] = "sequence"
    _INDEPENDENT: ClassVar[str] = "independent"

    _components: tuple[FilterComponent, ...]
    _mode: str
    _name: str | None

    def __init__(
        self,
        *components: FilterComponent,
        name: str | None = None,
    ) -> None:
        self._initialize(components, name=name, mode=self._SEQUENCE)

    @classmethod
    def sequence(
        cls,
        *components: FilterComponent,
        name: str | None = None,
    ) -> "FilterPipeline":
        """Create a sequential pipeline."""
        return cls(*components, name=name)

    @classmethod
    def independent(
        cls,
        *components: FilterComponent,
        name: str | None = None,
    ) -> "FilterPipeline":
        """Create independent roots over the same input set."""
        pipeline = cls.__new__(cls)
        pipeline._initialize(components, name=name, mode=cls._INDEPENDENT)
        return pipeline

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def children(self) -> tuple[FilterComponent, ...]:
        """Immutable child components in declaration order."""
        return self._components

    def _initialize(
        self,
        components: tuple[FilterComponent, ...],
        *,
        name: str | None,
        mode: str,
    ) -> None:
        if not components:
            raise InvalidPipelineError("FilterPipeline requires at least one component")
        for index, component in enumerate(components):
            if not isinstance(component, FilterComponent):
                raise TypeError(f"components[{index}] must be FilterComponent")
        if mode not in (self._SEQUENCE, self._INDEPENDENT):
            raise InvalidPipelineError(f"unsupported pipeline mode: {mode!r}")

        clean_name = None if name is None else str(name).strip()
        if name is not None and not clean_name:
            raise InvalidPipelineError("FilterPipeline name cannot be blank")

        object.__setattr__(self, "_components", tuple(components))
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_name", clean_name)

    def _is_sequence(self) -> bool:
        return self._mode == self._SEQUENCE

    def _is_independent(self) -> bool:
        return self._mode == self._INDEPENDENT

    def _execution_name(self) -> str:
        return self._name or type(self).__name__

    def __repr__(self) -> str:
        label = f", name={self._name!r}" if self._name is not None else ""
        return (
            f"FilterPipeline({len(self._components)} components, "
            f"mode={self._mode!r}{label})"
        )


__all__ = ["Filter", "FilterComponent", "FilterPipeline"]
