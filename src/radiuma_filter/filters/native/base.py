"""Shared validation and array helpers for native filters."""
from __future__ import annotations

import math
from typing import Any, Literal, cast
import numpy as np

from ..core import Filter

BoundaryMode = Literal["reflect", "nearest", "constant"]

class _NativeFilter(Filter):
    """Shared implementation support for native numerical filter leaves."""

    __slots__ = ()

    def _image_array(self, image: Any) -> np.ndarray:
        from ...api.extensions import LoadedImage

        source = image.data if isinstance(image, LoadedImage) else image
        try:
            array = np.asarray(source)
        except Exception as exc:
            raise TypeError(
                f"{type(self).__name__} cannot convert input image to an array: {exc}"
            ) from exc

        dimensions = type(self)._supported_dimensions
        if dimensions is not None and array.ndim not in dimensions:
            allowed = " or ".join(str(value) for value in dimensions)
            raise ValueError(
                f"{type(self).__name__} supports {allowed} dimensional images; "
                f"received {array.ndim}"
            )
        if array.size == 0:
            raise ValueError(f"{type(self).__name__} cannot process an empty image")
        if not (
            np.issubdtype(array.dtype, np.number)
            or np.issubdtype(array.dtype, np.bool_)
        ):
            raise TypeError(
                f"{type(self).__name__} requires numeric image data; "
                f"received {array.dtype}"
            )
        return array

    @staticmethod
    def _finalize_output(
        filtered: Any,
        original_dtype: np.dtype[Any],
        *,
        preserve_scalar_type: bool,
    ) -> np.ndarray:
        array = np.asarray(filtered)
        if not preserve_scalar_type:
            return array.astype(np.float64, copy=False)
        if np.issubdtype(original_dtype, np.bool_):
            return array >= 0.5
        if np.issubdtype(original_dtype, np.integer):
            limits = np.iinfo(original_dtype)
            array = np.clip(np.rint(array), limits.min, limits.max)
        return array.astype(original_dtype, copy=False)

    @staticmethod
    def _finite_float(
        name: str,
        value: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        resolved = float(value)
        if not math.isfinite(resolved):
            raise ValueError(f"{name} must be finite")
        if minimum is not None and resolved < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        if maximum is not None and resolved > maximum:
            raise ValueError(f"{name} must be <= {maximum}")
        return resolved

    @staticmethod
    def _positive_int(name: str, value: int, *, minimum: int = 1) -> int:
        if isinstance(value, bool):
            raise TypeError(f"{name} must be an integer")
        resolved = int(value)
        if resolved != value or resolved < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
        return resolved

    @staticmethod
    def _boundary_mode(value: str) -> BoundaryMode:
        resolved = str(value).strip().casefold()
        if resolved not in {"reflect", "nearest", "constant"}:
            raise ValueError(
                "boundary_mode must be 'reflect', 'nearest', or 'constant'"
            )
        return cast(BoundaryMode, resolved)

__all__ = ["BoundaryMode"]
