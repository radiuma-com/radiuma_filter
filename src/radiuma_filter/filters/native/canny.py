"""CannyFilter definition."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import BoundaryMode, _NativeFilter

@dataclass(frozen=True, slots=True, eq=False)
class CannyFilter(_NativeFilter):
    """2D Canny edge detector producing a boolean image.

    The runtime automatically applies it slice-by-slice when a 3D volume is
    supplied, then reconstructs one 3D output. Users do not manage this policy.
    """

    _supported_dimensions: ClassVar[tuple[int, ...]] = (2,)
    _auto_slice_axis: ClassVar[int | None] = 0

    sigma: float = 1.0
    low_threshold: float | None = None
    high_threshold: float | None = None
    use_quantiles: bool = False
    boundary_mode: BoundaryMode = "reflect"
    constant_value: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sigma",
            self._finite_float("sigma", self.sigma, minimum=0.0),
        )
        if self.sigma <= 0:
            raise ValueError("sigma must be > 0")
        low = (
            None
            if self.low_threshold is None
            else self._finite_float("low_threshold", self.low_threshold)
        )
        high = (
            None
            if self.high_threshold is None
            else self._finite_float("high_threshold", self.high_threshold)
        )
        use_quantiles = bool(self.use_quantiles)
        if use_quantiles:
            for label, value in (("low_threshold", low), ("high_threshold", high)):
                if value is not None and not 0.0 <= value <= 1.0:
                    raise ValueError(
                        f"{label} must be within [0, 1] when use_quantiles=True"
                    )
        if low is not None and high is not None and low > high:
            raise ValueError("low_threshold cannot exceed high_threshold")
        object.__setattr__(self, "low_threshold", low)
        object.__setattr__(self, "high_threshold", high)
        object.__setattr__(self, "use_quantiles", use_quantiles)
        object.__setattr__(
            self, "boundary_mode", self._boundary_mode(self.boundary_mode)
        )
        object.__setattr__(
            self,
            "constant_value",
            self._finite_float("constant_value", self.constant_value),
        )

    def _apply(self, image: Any) -> np.ndarray:
        from skimage.feature import canny

        source = self._image_array(image)
        result = canny(
            source.astype(np.float64, copy=False),
            sigma=self.sigma,
            low_threshold=self.low_threshold,
            high_threshold=self.high_threshold,
            use_quantiles=self.use_quantiles,
            mode=self.boundary_mode,
            cval=self.constant_value,
        )
        return np.asarray(result, dtype=bool)

__all__ = ["CannyFilter"]
