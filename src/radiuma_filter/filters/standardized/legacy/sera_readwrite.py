"""Compatibility symbols required by the unchanged legacy Sera_Filter module.

The unified runtime does not call the legacy folder wrappers. ``readimage``
reuses the canonical path input provider so file decoding exists in one place.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


def readimage(path: str | Path):
    from ....api.extensions import LogicalInput, PathSource
    from ....runtime.io.inputs import PathInputAdapter

    source = Path(path)
    normalized = PathSource(source)
    loaded = PathInputAdapter().load(
        LogicalInput(index=0, key=source.name, source=normalized)
    )
    native = loaded.native
    header = {
        "spacing": np.asarray(loaded.spacing, dtype=float),
        "origin": native.GetOrigin() if native is not None else loaded.origin,
        "direction": native.GetDirection() if native is not None else loaded.direction,
        "sitk_image": native,
    }
    name = source.name.casefold()
    if name.endswith((".dcm", ".dicom")):
        kind = "Dicom"
    elif name.endswith(".nrrd"):
        kind = "Nrrd"
    else:
        kind = "Nifti"
    return [loaded.data, header, kind, source.name]


def convert_modalities(
    data: Any,
    header: Any,
    source_type: str,
    destination_type: str,
    destination_folder: str | Path,
    name: str,
    createfolder: str = "False",
):
    import SimpleITK as sitk

    destination = Path(destination_folder)
    destination.mkdir(parents=True, exist_ok=True)
    array = np.asarray(data)
    image = sitk.GetImageFromArray(array)
    if isinstance(header, dict):
        spacing = header.get("spacing")
        if spacing is not None and len(spacing) == array.ndim:
            image.SetSpacing(tuple(float(value) for value in reversed(tuple(spacing))))
        origin = header.get("origin")
        if origin is not None and len(origin) == array.ndim:
            image.SetOrigin(tuple(float(value) for value in origin))
        direction = header.get("direction")
        if direction is not None:
            try:
                image.SetDirection(tuple(float(value) for value in direction))
            except Exception:
                pass

    output_name = str(name)
    if not Path(output_name).suffix:
        output_name += ".nii.gz"
    output_path = destination / output_name
    sitk.WriteImage(image, str(output_path))
    return str(output_path)


# Sera_Filter imports this historical symbol but does not call it.
similar = np.array_equal
