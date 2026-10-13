"""MeanFilter standardized wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import _StandardizedFilter

@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class MeanFilter(_StandardizedFilter):
    """Parameters for the preserved standardized mean implementation."""

    by_slice: bool
    BoundaryCondition: str
    FilterSize: int

    def _apply(self, image: Any) -> np.ndarray:
        module = self._legacy_module()
        payload = self._volume_payload(image)
        return np.asarray(
            module.mean_filter(
                payload.data,
                self.by_slice,
                self.BoundaryCondition,
                self.FilterSize,
            )
        )

__all__ = ["MeanFilter"]
