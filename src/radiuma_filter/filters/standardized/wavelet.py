"""WaveletFilter standardized wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
import numpy as np

from .base import _StandardizedFilter

@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class WaveletFilter(_StandardizedFilter):
    """Parameters for the preserved standardized Wavelet implementation."""

    by_slice: bool
    mode: str
    filter_config: str
    pooling_method: str
    wavelet_family: str
    riesz_steered: bool
    rot_invariance: bool
    decomposition_level: int
    filter_size: int | tuple[int, ...] | list[int]
    riesz: tuple[int, ...] | list[int] | None
    wavelet_type: str

    def _apply(self, image: Any) -> np.ndarray:
        module = self._legacy_module()
        payload = self._volume_payload(image)
        return np.asarray(
            module.Wavelet_filter(
                self._legacy_image(payload),
                self.by_slice,
                self.mode,
                self.filter_config,
                self.pooling_method,
                self.wavelet_family,
                self.riesz_steered,
                self.rot_invariance,
                self.decomposition_level,
                self.filter_size,
                self.riesz,
                self.wavelet_type,
            )
        )

__all__ = ["WaveletFilter"]
