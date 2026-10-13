"""SobelFilter definition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import BoundaryMode, _NativeFilter

@dataclass(frozen=True, slots=True, eq=False)
class SobelFilter(_NativeFilter):
    """Sobel derivative or gradient magnitude for 2D/3D images."""

    _supported_dimensions: ClassVar[tuple[int, ...]] = (2, 3)

    axis: int | None = None
    boundary_mode: BoundaryMode = "reflect"
    constant_value: float = 0.0
    preserve_scalar_type: bool = False

    def __post_init__(self) -> None:
        if self.axis is not None:
            if isinstance(self.axis, bool):
                raise TypeError("axis must be an integer or None")
            object.__setattr__(self, "axis", int(self.axis))
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
        from scipy import ndimage as ndi

        source = self._image_array(image)
        original_dtype = source.dtype
        work = source.astype(np.float64, copy=False)
        if self.axis is None:
            components = [
                ndi.sobel(
                    work,
                    axis=axis,
                    mode=self.boundary_mode,
                    cval=self.constant_value,
                )
                for axis in range(work.ndim)
            ]
            filtered = np.sqrt(
                sum(component * component for component in components)
            )
        else:
            axis = self.axis
            if not -work.ndim <= axis < work.ndim:
                raise ValueError(
                    f"SobelFilter axis {axis} is invalid for {work.ndim}D input"
                )
            filtered = ndi.sobel(
                work,
                axis=axis,
                mode=self.boundary_mode,
                cval=self.constant_value,
            )
        return self._finalize_output(
            filtered,
            original_dtype,
            preserve_scalar_type=self.preserve_scalar_type,
        )

__all__ = ["SobelFilter"]
