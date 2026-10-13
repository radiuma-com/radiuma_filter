"""GaussianFilter definition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import BoundaryMode, _NativeFilter

@dataclass(frozen=True, slots=True, eq=False)
class GaussianFilter(_NativeFilter):
    """Separable Gaussian smoothing for 2D or 3D numeric images."""

    _supported_dimensions: ClassVar[tuple[int, ...]] = (2, 3)

    sigma: float = 1.0
    truncate: float = 4.0
    boundary_mode: BoundaryMode = "reflect"
    constant_value: float = 0.0
    preserve_scalar_type: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sigma",
            self._finite_float("sigma", self.sigma, minimum=0.05, maximum=50.0),
        )
        object.__setattr__(
            self,
            "truncate",
            self._finite_float(
                "truncate", self.truncate, minimum=1.0, maximum=8.0
            ),
        )
        object.__setattr__(
            self, "boundary_mode", self._boundary_mode(self.boundary_mode)
        )
        object.__setattr__(
            self,
            "constant_value",
            self._finite_float("constant_value", self.constant_value),
        )
        object.__setattr__(
            self, "preserve_scalar_type", bool(self.preserve_scalar_type)
        )

    def _apply(self, image: Any) -> np.ndarray:
        source = self._image_array(image)
        original_dtype = source.dtype
        filtered = source.astype(np.float64, copy=False)
        kernel = self._kernel()
        for axis in range(filtered.ndim):
            filtered = self._convolve_axis(filtered, kernel, axis)
        return self._finalize_output(
            filtered,
            original_dtype,
            preserve_scalar_type=self.preserve_scalar_type,
        )

    def _kernel(self) -> np.ndarray:
        radius = max(1, int(self.truncate * self.sigma + 0.5))
        coordinates = np.arange(-radius, radius + 1, dtype=np.float64)
        kernel = np.exp(-0.5 * (coordinates / self.sigma) ** 2)
        total = float(kernel.sum())
        if total <= 0.0:
            raise ValueError("GaussianFilter produced an invalid kernel")
        return kernel / total

    def _convolve_axis(
        self,
        array: np.ndarray,
        kernel: np.ndarray,
        axis: int,
    ) -> np.ndarray:
        radius = len(kernel) // 2
        pad_width = [(0, 0)] * array.ndim
        pad_width[axis] = (radius, radius)
        if self.boundary_mode == "nearest":
            padded = np.pad(array, pad_width, mode="edge")
        elif self.boundary_mode == "constant":
            padded = np.pad(
                array,
                pad_width,
                mode="constant",
                constant_values=self.constant_value,
            )
        else:
            padded = np.pad(array, pad_width, mode="reflect")
        return np.apply_along_axis(
            lambda values: np.convolve(values, kernel, mode="valid"),
            axis,
            padded,
        )

__all__ = ["GaussianFilter"]
