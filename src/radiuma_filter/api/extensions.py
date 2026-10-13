"""Stable extension contracts for custom input adapters and filter backends.

Normal end users do not need this module. It is the supported extension surface
for packages that add new input formats or alternative CPU/GPU filter backends.
"""
from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from os import PathLike
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .execution import DevicePreference
from ..filters.core import Filter
from .identity import CorrelationId, ExecutionId


class InputSource:
    """Normalized source marker used between the resolver and input adapters."""

    __slots__ = ()


    @staticmethod
    def _normalize_path(value: str | PathLike[str]) -> Path:
        return Path(value).expanduser()


@dataclass(frozen=True, slots=True)
class MemorySource(InputSource):
    image: Any
    key: str


@dataclass(frozen=True, slots=True)
class PathSource(InputSource):
    path: Path

    def __init__(self, path: str | PathLike[str]) -> None:
        object.__setattr__(self, "path", self._normalize_path(path))


@dataclass(frozen=True, slots=True)
class DicomSeriesSource(InputSource):
    directory: Path
    series_id: str

    def __init__(self, directory: str | PathLike[str], series_id: str) -> None:
        object.__setattr__(self, "directory", self._normalize_path(directory))
        object.__setattr__(self, "series_id", str(series_id))


@dataclass(frozen=True, slots=True)
class LogicalInput:
    index: int
    key: str
    source: InputSource

    def __post_init__(self) -> None:
        index = int(self.index)
        if index < 0:
            raise ValueError("LogicalInput.index cannot be negative")
        object.__setattr__(self, "index", index)
        key = str(self.key).strip()
        if not key:
            raise ValueError("LogicalInput.key is required")
        object.__setattr__(self, "key", key)
        if not isinstance(self.source, InputSource):
            raise TypeError("LogicalInput.source must be InputSource")


@dataclass(frozen=True, slots=True)
class LoadedImage:
    """Metadata-preserving image payload used by input adapters and backends."""

    data: Any
    spacing: tuple[float, ...] | None = None
    origin: tuple[float, ...] | None = None
    direction: tuple[float, ...] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source: str | None = None
    native: Any = None

    def __post_init__(self) -> None:
        array = np.asarray(self.data)
        if array.ndim < 2:
            raise ValueError("loaded image data must be at least 2D")
        object.__setattr__(self, "data", array)

        spacing = self.spacing
        if spacing is None:
            spacing = (1.0,) * array.ndim
        spacing = tuple(float(value) for value in spacing)
        if len(spacing) != array.ndim:
            raise ValueError("spacing dimensionality must match image data")
        if any(value <= 0.0 or not np.isfinite(value) for value in spacing):
            raise ValueError("spacing values must be finite and positive")
        object.__setattr__(self, "spacing", spacing)

        if self.origin is not None:
            object.__setattr__(self, "origin", tuple(float(value) for value in self.origin))
        if self.direction is not None:
            object.__setattr__(self, "direction", tuple(float(value) for value in self.direction))
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(copy.deepcopy(dict(self.metadata))),
        )
        object.__setattr__(self, "source", None if self.source is None else str(self.source))

    def with_data(self, data: Any) -> "LoadedImage":
        array = np.asarray(data)
        if array.ndim == len(self.spacing):
            return replace(self, data=array)
        return replace(
            self,
            data=array,
            spacing=(1.0,) * array.ndim,
            origin=None,
            direction=None,
        )


@dataclass(frozen=True, slots=True)
class FilterContext:
    execution_id: ExecutionId
    correlation_id: CorrelationId
    root_index: int
    input_index: int
    step_index: int
    device: DevicePreference

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", ExecutionId.parse(self.execution_id))
        object.__setattr__(self, "correlation_id", CorrelationId.parse(self.correlation_id))
        object.__setattr__(self, "root_index", int(self.root_index))
        object.__setattr__(self, "input_index", int(self.input_index))
        object.__setattr__(self, "step_index", int(self.step_index))
        if not isinstance(self.device, DevicePreference):
            object.__setattr__(self, "device", DevicePreference(self.device))


@runtime_checkable
class InputAdapter(Protocol):
    """Extension contract for discovering and loading normalized inputs."""

    def supports(self, source: InputSource) -> bool: ...
    def discover(self, source: InputSource) -> Iterator[tuple[str, InputSource]]: ...
    def load(self, item: LogicalInput) -> Any: ...


@runtime_checkable
class FilterBackend(Protocol):
    """Extension contract for executing a filter definition on a device."""

    def supports(self, filter: Filter, *, device: DevicePreference) -> bool: ...
    def execute(self, filter: Filter, *, image: Any, context: FilterContext) -> Any: ...


__all__ = [
    "DicomSeriesSource",
    "FilterBackend",
    "FilterContext",
    "InputAdapter",
    "InputSource",
    "LoadedImage",
    "LogicalInput",
    "MemorySource",
    "PathSource",
]
