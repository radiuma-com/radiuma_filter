"""MedianFilter definition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import BoundaryMode, _NativeFilter

@dataclass(frozen=True, slots=True, eq=False)
class MedianFilter(_NativeFilter):
    """Median denoising with an isotropic radius in pixels/voxels."""

    _supported_dimensions: ClassVar[tuple[int, ...]] = (2, 3)

    radius: int
    boundary_mode: BoundaryMode = "reflect"
    constant_value: float = 0.0
    preserve_scalar_type: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "radius", self._positive_int("radius", self.radius))
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
        filtered = ndi.median_filter(
            source,
            size=2 * self.radius + 1,
            mode=self.boundary_mode,
            cval=self.constant_value,
        )
        return self._finalize_output(
            filtered,
            original_dtype,
            preserve_scalar_type=self.preserve_scalar_type,
        )

__all__ = ["MedianFilter"]
