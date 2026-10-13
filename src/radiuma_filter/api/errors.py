"""Public exception hierarchy for the filtering library."""

class FilteringError(Exception):
    """Base class for library-defined failures."""

class InvalidExecutionError(FilteringError):
    pass

class DuplicateComponentError(InvalidExecutionError):
    pass

class InvalidPipelineError(FilteringError):
    pass

class ResultStateError(FilteringError):
    pass

class UnsupportedInputError(FilteringError):
    pass

class UnsupportedFilterError(FilteringError):
    pass

class BackendUnavailableError(FilteringError):
    pass

__all__ = [
    "BackendUnavailableError",
    "DuplicateComponentError",
    "FilteringError",
    "InvalidExecutionError",
    "InvalidPipelineError",
    "ResultStateError",
    "UnsupportedFilterError",
    "UnsupportedInputError",
]
