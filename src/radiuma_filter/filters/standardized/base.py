"""Shared bridge for standardized filters backed by the preserved implementation."""
from __future__ import annotations

from typing import Any, ClassVar
import numpy as np

from ..core import Filter

class _StandardizedFilter(Filter):
    """Shared bridge behavior for standardized filter leaves."""

    __slots__ = ()
    _preserved_standardized: ClassVar[bool] = True
    _supported_dimensions: ClassVar[tuple[int, ...]] = (3,)

    class _SilentLegacyLogging:
        @staticmethod
        def log(*args: Any, **kwargs: Any) -> None:
            return None

    @classmethod
    def _legacy_module(cls):
        from .legacy import sera_filter

        # Keep the validated legacy source byte-preserved while suppressing its
        # private root-logger debug chatter from the library's public terminal UI.
        sera_filter.logging = cls._SilentLegacyLogging
        return sera_filter

    def _volume_payload(self, image: Any):
        from ...api.extensions import LoadedImage

        payload = image if isinstance(image, LoadedImage) else LoadedImage(data=image)
        if payload.data.ndim != 3:
            raise ValueError(
                f"{type(self).__name__} uses the preserved standardized "
                f"implementation, which expects a 3D voxel grid; received "
                f"{payload.data.ndim}D"
            )
        return payload

    @staticmethod
    def _legacy_image(payload: Any) -> list[Any]:
        return [
            payload.data,
            {
                "spacing": np.asarray(payload.spacing, dtype=float),
                "origin": payload.origin,
                "direction": payload.direction,
                "metadata": dict(payload.metadata),
            },
            "Unified",
            payload.source or "memory",
        ]
