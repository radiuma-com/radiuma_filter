"""Radiuma Filter public API.

The public Composite hierarchy is ``FilterComponent`` -> ``Filter`` leaves and
``FilterPipeline`` composites. Every component owns the same
``execute(inputs=...)`` operation; execution infrastructure remains private.
"""
from .api.errors import (
    BackendUnavailableError,
    DuplicateComponentError,
    FilteringError,
    InvalidExecutionError,
    InvalidPipelineError,
    ResultStateError,
    UnsupportedFilterError,
    UnsupportedInputError,
)
from .api.execution import (
    DevicePreference,
    ErrorPolicy,
    EventType,
    ExecutionEvent,
    ExecutionObserver,
    ExecutionOptions,
    ExecutionResult,
)
from .filters import (
    CannyFilter,
    Filter,
    FilterComponent,
    FilterPipeline,
    GaborFilter,
    GaussianFilter,
    LawsFilter,
    LoGFilter,
    MeanFilter,
    MedianFilter,
    NLMFilter,
    SobelFilter,
    WaveletFilter,
)

__all__ = [
    "BackendUnavailableError",
    "CannyFilter",
    "DevicePreference",
    "DuplicateComponentError",
    "ErrorPolicy",
    "EventType",
    "ExecutionEvent",
    "ExecutionObserver",
    "ExecutionOptions",
    "ExecutionResult",
    "Filter",
    "FilterComponent",
    "FilterPipeline",
        "FilteringError",
    "GaborFilter",
    "GaussianFilter",
    "InvalidExecutionError",
    "InvalidPipelineError",
    "LawsFilter",
    "LoGFilter",
    "MeanFilter",
    "MedianFilter",
    "NLMFilter",
    "ResultStateError",
    "SobelFilter",
    "UnsupportedFilterError",
    "UnsupportedInputError",
    "WaveletFilter",
]

# Stable advanced submodule imports without physical shell packages.
import sys as _sys
from .api import errors as errors, execution as execution, extensions as extensions, identity as identity, logs as logs
for _name, _module in {
    "errors": errors,
    "execution": execution,
    "extensions": extensions,
    "identity": identity,
    "logs": logs,
}.items():
    _sys.modules.setdefault(f"{__name__}.{_name}", _module)
del _name, _module, _sys
