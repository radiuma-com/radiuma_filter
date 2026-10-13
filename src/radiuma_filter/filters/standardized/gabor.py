"""GaborFilter standardized wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import _StandardizedFilter

@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class GaborFilter(_StandardizedFilter):
    """Parameters for the preserved standardized Gabor implementation."""

    by_slice: bool
    BoundaryCondition: str
    pooling_method: str
    response: str
    rotation_inver: bool
    gamma: float
    lambdaa: float
    sigma: float
    step: float
    theta_initial: float
    SigmaTruncate: float | None

    def _apply(self, image: Any) -> np.ndarray:
        module = self._legacy_module()
        payload = self._volume_payload(image)
        return np.asarray(
            module.gabor_filter(
                self._legacy_image(payload),
                self.by_slice,
                self.BoundaryCondition,
                self.pooling_method,
                self.response,
                self.rotation_inver,
                self.gamma,
                self.lambdaa,
                self.sigma,
                self.step,
                self.theta_initial,
                self.SigmaTruncate,
            )
        )

__all__ = ["GaborFilter"]
