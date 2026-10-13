"""NLMFilter definition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import BoundaryMode, _NativeFilter

@dataclass(frozen=True, slots=True, eq=False)
class NLMFilter(_NativeFilter):
    """Non-local-means denoising for 2D/3D scalar images."""

    _supported_dimensions: ClassVar[tuple[int, ...]] = (2, 3)

    patch_size: int = 7
    patch_distance: int = 11
    h: float = 0.1
    sigma: float = 0.0
    fast_mode: bool = True
    preserve_scalar_type: bool = False

    def __post_init__(self) -> None:
        patch_size = self._positive_int("patch_size", self.patch_size)
        if patch_size % 2 == 0:
            raise ValueError("patch_size must be odd")
        object.__setattr__(self, "patch_size", patch_size)
        object.__setattr__(
            self,
            "patch_distance",
            self._positive_int("patch_distance", self.patch_distance),
        )
        object.__setattr__(self, "h", self._finite_float("h", self.h, minimum=0.0))
        if self.h <= 0:
            raise ValueError("h must be > 0")
        object.__setattr__(
            self,
            "sigma",
            self._finite_float("sigma", self.sigma, minimum=0.0),
        )
        object.__setattr__(self, "fast_mode", bool(self.fast_mode))
        object.__setattr__(
            self, "preserve_scalar_type", bool(self.preserve_scalar_type)
        )

    def _apply(self, image: Any) -> np.ndarray:
        from skimage.restoration import denoise_nl_means

        source = self._image_array(image)
        original_dtype = source.dtype
        filtered = np.asarray(
            denoise_nl_means(
                source,
                patch_size=self.patch_size,
                patch_distance=self.patch_distance,
                h=self.h,
                fast_mode=self.fast_mode,
                sigma=self.sigma,
                preserve_range=True,
                channel_axis=None,
            ),
            dtype=np.float64,
        )
        return self._finalize_output(
            filtered,
            original_dtype,
            preserve_scalar_type=self.preserve_scalar_type,
        )

__all__ = ["NLMFilter"]
