"""LawsFilter standardized wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import _StandardizedFilter

@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class LawsFilter(_StandardizedFilter):
    """Parameters for the preserved standardized Laws implementation."""

    by_slice: bool
    BoundaryCondition: str
    Kernel: str
    pooling_method: str
    cal_energy: bool
    rotation_inver: bool
    delta: int

    def _apply(self, image: Any) -> np.ndarray:
        module = self._legacy_module()
        payload = self._volume_payload(image)
        return np.asarray(
            module.Laws_filter(
                self._legacy_image(payload),
                self.by_slice,
                self.BoundaryCondition,
                self.Kernel,
                self.pooling_method,
                self.cal_energy,
                self.rotation_inver,
                self.delta,
            )
        )

__all__ = ["LawsFilter"]
