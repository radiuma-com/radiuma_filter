"""Extension discovery and runtime resolution."""
from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

from ...api.errors import BackendUnavailableError, UnsupportedFilterError, UnsupportedInputError
from ...api.execution import DevicePreference
from ...api.extensions import FilterBackend, FilterContext, InputAdapter, InputSource
from ...filters import Filter
from ..io.inputs import MemoryInputAdapter, PathInputAdapter

_LOG = logging.getLogger(__name__)
_INPUT_GROUP = "radiuma_filter.input_adapters"
_FILTER_GROUP = "radiuma_filter.filter_backends"

class BuiltinFilterBackend:
    """Fallback CPU backend for filters carrying a built-in ``_apply`` hook."""

    @staticmethod
    def is_standardized(filter: Filter) -> bool:
        return bool(type(filter)._preserved_standardized)

    def supports(
        self,
        filter: Filter,
        *,
        device: DevicePreference,
    ) -> bool:
        return (
            type(filter)._has_builtin_implementation()
            and device in {DevicePreference.AUTO, DevicePreference.CPU}
        )

    def execute(
        self,
        filter: Filter,
        *,
        image: Any,
        context: FilterContext,
    ) -> Any:
        if context.device not in {DevicePreference.AUTO, DevicePreference.CPU}:
            raise TypeError(
                f"BuiltinFilterBackend cannot execute on {context.device.value}"
            )
        if not type(filter)._has_builtin_implementation():
            raise TypeError(
                f"BuiltinFilterBackend cannot execute {type(filter).__name__}"
            )
        return filter._apply(image)

class ExtensionRegistry:
    """Runtime registry for input adapters and filter backends."""

    @staticmethod
    def _materialize(value: Any) -> tuple[Any, ...]:
        if isinstance(value, type):
            value = value()
        elif callable(value) and not any(
            hasattr(value, attr) for attr in ("supports", "discover", "execute")
        ):
            value = value()
        if value is None:
            return ()
        if isinstance(value, (tuple, list)):
            return tuple(value)
        return (value,)

    @classmethod
    def _discover_extensions(cls) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        input_adapters: list[Any] = []
        filter_backends: list[Any] = []
        for group, target in (
            (_INPUT_GROUP, input_adapters),
            (_FILTER_GROUP, filter_backends),
        ):
            for entry in metadata.entry_points(group=group):
                try:
                    target.extend(cls._materialize(entry.load()))
                except Exception:
                    _LOG.exception(
                        "Failed to load filter extension %s:%s",
                        group,
                        entry.name,
                    )
        return tuple(input_adapters), tuple(filter_backends)

    def __init__(self) -> None:
        external_adapters, external_backends = self._discover_extensions()
        self._builtin_backend = BuiltinFilterBackend()
        self._input_adapters: tuple[InputAdapter, ...] = (
            MemoryInputAdapter(),
            *external_adapters,
            PathInputAdapter(),
        )
        self._filter_backends: tuple[FilterBackend, ...] = (
            *external_backends,
            self._builtin_backend,
        )

    def resolve_input_adapter(self, source: InputSource) -> InputAdapter:
        for adapter in self._input_adapters:
            try:
                if adapter.supports(source):
                    return adapter
            except Exception:
                continue
        raise UnsupportedInputError(
            f"no installed input adapter supports {type(source).__name__}"
        )

    def resolve_filter_backend(
        self,
        filter: Filter,
        *,
        device: DevicePreference,
    ) -> FilterBackend:
        # Standardized filters are deliberately pinned to the preserved
        # implementation. A different backend must never silently replace it.
        if self._builtin_backend.is_standardized(filter):
            if self._builtin_backend.supports(filter, device=device):
                return self._builtin_backend
            raise BackendUnavailableError(
                f"{type(filter).__name__} is standardized and has no preserved "
                f"implementation for requested device {device.value!r}"
            )

        supported_on_other_device = False
        for backend in self._filter_backends:
            try:
                if backend.supports(filter, device=device):
                    return backend
                if (
                    device is not DevicePreference.AUTO
                    and backend.supports(filter, device=DevicePreference.AUTO)
                ):
                    supported_on_other_device = True
            except Exception:
                continue

        if supported_on_other_device:
            raise BackendUnavailableError(
                f"{type(filter).__name__} has no backend for requested "
                f"device {device.value!r}"
            )
        raise UnsupportedFilterError(
            f"no installed filter backend supports {type(filter).__name__}"
        )
