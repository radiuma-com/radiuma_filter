"""LoGFilter standardized wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import _StandardizedFilter

@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class LoGFilter(_StandardizedFilter):
    """Parameters for the preserved standardized LoG implementation."""

    by_slice: bool
    BoundaryCondition: str
    img_average: bool
    riesz_steered: bool
    sigma: float
    sigma_cutoff: float
    riesz: tuple[int, ...] | list[int] | None

    def _apply(self, image: Any) -> np.ndarray:
        module = self._legacy_module()
        payload = self._volume_payload(image)
        return np.asarray(
            module.log_filter(
                self._legacy_image(payload),
                self.by_slice,
                self.BoundaryCondition,
                self.img_average,
                self.riesz_steered,
                self.sigma,
                self.sigma_cutoff,
                self.riesz,
            )
        )

__all__ = ["LoGFilter"]
