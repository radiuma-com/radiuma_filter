"""Input normalization, discovery, and built-in loading."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Set
from os import PathLike
from pathlib import Path
from typing import Any, TYPE_CHECKING

import numpy as np
from skimage import io as skio

from ...api.errors import UnsupportedInputError
from ...api.extensions import DicomSeriesSource, InputSource, LoadedImage, LogicalInput, MemorySource, PathSource

if TYPE_CHECKING:
    from ..extensions.registry import ExtensionRegistry

class MemoryInputAdapter:
    def supports(self, source: InputSource) -> bool:
        return isinstance(source, MemorySource)

    def discover(self, source: InputSource) -> Iterator[tuple[str, InputSource]]:
        if not isinstance(source, MemorySource):
            return iter(())
        key = source.key
        return iter(((key, source),))

    def load(self, item: LogicalInput) -> LoadedImage:
        if not isinstance(item.source, MemorySource):
            raise TypeError("MemoryInputAdapter can load only MemorySource")
        array = np.asarray(item.source.image)
        return LoadedImage(data=array, source=item.key)

class PathInputAdapter:
    """Deterministic reader for common scalar and medical image inputs.

    File-versus-directory semantics are private implementation details. The
    public caller always supplies the path through the same ``inputs=`` argument.
    """

    _NUMPY = {".npy"}
    _RASTER = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
    _MEDICAL = {".nii", ".nrrd", ".mha", ".mhd", ".dcm", ".dicom"}

    def supports(self, source: InputSource) -> bool:
        return isinstance(source, (PathSource, DicomSeriesSource))

    def discover(self, source: InputSource) -> Iterator[tuple[str, InputSource]]:
        if isinstance(source, DicomSeriesSource):
            key = f"{source.directory.name}:{source.series_id}"
            return iter(((key, source),))
        if not isinstance(source, PathSource):
            return iter(())

        path = source.path
        if not path.exists():
            raise FileNotFoundError(path)

        if path.is_file():
            # Defer format recognition to load(). This also permits medical
            # files without a conventional extension.
            return iter(((path.name, source),))

        if not path.is_dir():
            raise UnsupportedInputError(f"input path is neither a file nor a directory: {path}")

        discovered: list[tuple[str, InputSource]] = []
        consumed_dicom: set[Path] = set()

        # Ask GDCM about every directory rather than relying on .dcm suffixes;
        # real DICOM datasets commonly contain extensionless slice files.
        try:
            sitk = self._simpleitk()
        except UnsupportedInputError:
            sitk = None
        if sitk is not None:
            directories = [path] + sorted(
                (candidate for candidate in path.rglob("*") if candidate.is_dir()),
                key=lambda value: value.relative_to(path).as_posix().casefold(),
            )
            for directory in directories:
                # Avoid invoking GDCM for directories that contain only formats
                # already recognized as non-DICOM (for example a NIfTI-only
                # folder). This preserves extensionless/arbitrary-extension DICOM
                # discovery while avoiding GDCM's noisy "No Series were found"
                # warning for ordinary medical-image folders.
                direct_files = [
                    candidate
                    for candidate in directory.iterdir()
                    if candidate.is_file()
                ]
                needs_dicom_probe = any(
                    candidate.suffix.casefold() in {".dcm", ".dicom"}
                    or self._kind(candidate) is None
                    for candidate in direct_files
                )
                if not needs_dicom_probe:
                    continue

                series_ids = sitk.ImageSeriesReader.GetGDCMSeriesIDs(str(directory)) or []
                for series_id in sorted(str(value) for value in series_ids):
                    filenames = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(directory), series_id)
                    consumed_dicom.update(Path(name).resolve() for name in filenames)
                    relative = directory.relative_to(path).as_posix() if directory != path else "."
                    key = f"{relative}/series-{series_id}"
                    discovered.append((key, DicomSeriesSource(directory, series_id)))

        for candidate in sorted(
            (value for value in path.rglob("*") if value.is_file()),
            key=lambda value: value.relative_to(path).as_posix().casefold(),
        ):
            if candidate.resolve() in consumed_dicom:
                continue
            if self._kind(candidate) is None:
                continue
            key = candidate.relative_to(path).as_posix()
            discovered.append((key, PathSource(candidate)))

        discovered.sort(key=lambda pair: pair[0].casefold())
        if not discovered:
            raise UnsupportedInputError(f"directory contains no supported image data: {path}")

        return iter(discovered)

    def load(self, item: LogicalInput) -> LoadedImage:
        source = item.source
        if isinstance(source, DicomSeriesSource):
            return self._load_dicom_series(source, key=item.key)
        if not isinstance(source, PathSource):
            raise TypeError("PathInputAdapter received an unsupported normalized source")

        path = source.path
        kind = self._kind(path)

        if kind == "numpy":
            array = np.asarray(np.load(path, allow_pickle=False))
            return self._validated_payload(array, key=item.key, source=path)

        if kind == "raster":
            array = np.asarray(skio.imread(path))
            if array.ndim == 3 and array.shape[-1] in (3, 4):
                raise ValueError(
                    f"color raster input is not implicitly converted to a scalar image: {path}; "
                    "convert it explicitly to a scalar representation first"
                )
            return self._validated_payload(array, key=item.key, source=path)

        if kind == "medical":
            sitk = self._simpleitk()
            image = sitk.ReadImage(str(path))
            return self._from_sitk(image, key=item.key, source=path)

        # Final medical-reader probe supports extensionless medical files.
        try:
            sitk = self._simpleitk()
            image = sitk.ReadImage(str(path))
        except Exception as exc:
            raise UnsupportedInputError(f"no built-in reader supports {path}") from exc
        return self._from_sitk(image, key=item.key, source=path)

    def _load_dicom_series(self, source: DicomSeriesSource, *, key: str) -> LoadedImage:
        sitk = self._simpleitk()
        filenames = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(source.directory), source.series_id)
        if not filenames:
            raise UnsupportedInputError(
                f"DICOM series {source.series_id!r} contains no readable files in {source.directory}"
            )
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(filenames)
        image = reader.Execute()
        return self._from_sitk(
            image,
            key=key,
            source=source.directory,
            extra_metadata={"dicom_series_id": source.series_id, "dicom_files": tuple(filenames)},
        )

    @staticmethod
    def _simpleitk():
        try:
            import SimpleITK as sitk
        except ImportError as exc:
            raise UnsupportedInputError(
                "medical image input requires SimpleITK; install the package dependencies"
            ) from exc
        return sitk

    @classmethod
    def _from_sitk(
        cls,
        image: Any,
        *,
        key: str,
        source: Path,
        extra_metadata: dict[str, Any] | None = None,
    ) -> LoadedImage:
        sitk = cls._simpleitk()
        array = np.asarray(sitk.GetArrayFromImage(image))
        # SimpleITK arrays are ordered z,y,x (or y,x for 2D), whereas GetSpacing
        # is x,y,z. Reversing aligns spacing with array axes and with the legacy
        # filtering code's voxel-grid convention.
        spacing = tuple(float(value) for value in reversed(image.GetSpacing()))
        origin = tuple(float(value) for value in reversed(image.GetOrigin()))
        metadata: dict[str, Any] = {
            "key": key,
            "sitk_spacing": tuple(float(value) for value in image.GetSpacing()),
            "sitk_origin": tuple(float(value) for value in image.GetOrigin()),
            "sitk_direction": tuple(float(value) for value in image.GetDirection()),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return cls._validated_payload(
            array,
            key=key,
            source=source,
            spacing=spacing,
            origin=origin,
            direction=tuple(float(value) for value in image.GetDirection()),
            metadata=metadata,
            native=image,
        )

    @staticmethod
    def _validated_payload(
        array: np.ndarray,
        *,
        key: str,
        source: Path,
        spacing: tuple[float, ...] | None = None,
        origin: tuple[float, ...] | None = None,
        direction: tuple[float, ...] | None = None,
        metadata: dict[str, Any] | None = None,
        native: Any = None,
    ) -> LoadedImage:
        if array.ndim < 2:
            raise ValueError(f"image must be at least 2D: {source}")
        return LoadedImage(
            data=array,
            spacing=spacing,
            origin=origin,
            direction=direction,
            metadata=metadata or {"key": key},
            source=str(source),
            native=native,
        )

    @classmethod
    def _kind(cls, path: Path) -> str | None:
        name = path.name.casefold()
        if name.endswith(".nii.gz"):
            return "medical"
        suffix = path.suffix.casefold()
        if suffix in cls._NUMPY:
            return "numpy"
        if suffix in cls._RASTER:
            return "raster"
        if suffix in cls._MEDICAL:
            return "medical"
        return None

class InputResolver:
    """Normalize caller-owned values into internal image sources.

    Public callers do not need to know whether an input is an in-memory image,
    a file, a directory, or an adapter-specific source.  That distinction is
    resolved here and in the adapter layer.
    """

    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def discover(self, inputs: object | Iterable[object]) -> Iterator[LogicalInput]:
        def generate() -> Iterator[LogicalInput]:
            global_index = 0
            for source in self._normalize_many(inputs):
                adapter = self._registry.resolve_input_adapter(source)
                for discovered in adapter.discover(source):
                    try:
                        key, normalized_source = discovered
                    except (TypeError, ValueError) as exc:
                        raise TypeError(
                            "input adapter discover() must yield (key, InputSource) pairs"
                        ) from exc
                    if not isinstance(normalized_source, InputSource):
                        raise TypeError(
                            "input adapter discover() source must be InputSource"
                        )
                    yield LogicalInput(
                        index=global_index,
                        key=key,
                        source=normalized_source,
                    )
                    global_index += 1

        return generate()

    def load(self, item: LogicalInput) -> LoadedImage:
        adapter = self._registry.resolve_input_adapter(item.source)
        loaded = adapter.load(item)
        if isinstance(loaded, LoadedImage):
            return loaded
        return LoadedImage(data=loaded, source=item.key)

    def _normalize_many(self, inputs: object | Iterable[object]) -> Iterator[InputSource]:
        if isinstance(inputs, Mapping):
            raise TypeError("inputs cannot be a mapping; pass one image/path or an ordered flat collection")
        if isinstance(inputs, Set):
            raise TypeError("inputs cannot be an unordered set; use a list, tuple, or ordered iterator")

        # Atomic forms must be tested before generic Iterable.  In particular,
        # ndarray and path strings are iterable-ish values but represent one
        # caller input.
        if self._is_atomic(inputs):
            yield self._normalize_one(inputs, ordinal=0)
            return

        try:
            iterator = iter(inputs)  # type: ignore[arg-type]
        except TypeError:
            yield self._normalize_one(inputs, ordinal=0)
            return

        for ordinal, value in enumerate(iterator):
            if not self._is_atomic(value) and isinstance(value, Iterable):
                raise TypeError(
                    "nested input collections are ambiguous; pass a flat collection "
                    "of images/paths, or convert one nested numeric image to numpy.ndarray"
                )
            yield self._normalize_one(value, ordinal=ordinal)

    @staticmethod
    def _is_atomic(value: object) -> bool:
        return isinstance(
            value,
            (
                InputSource,
                np.ndarray,
                str,
                PathLike,
            ),
        ) or hasattr(value, "__array__")

    @staticmethod
    def _normalize_one(value: object, *, ordinal: int) -> InputSource:
        if isinstance(value, InputSource):
            return value
        if isinstance(value, (str, PathLike)):
            return PathSource(value)
        if isinstance(value, np.ndarray) or hasattr(value, "__array__"):
            return MemorySource(value, key=f"memory-{ordinal}")

        try:
            array = np.asarray(value)
        except Exception as exc:
            raise TypeError(
                f"unsupported input type: {type(value).__name__}"
            ) from exc
        if array.ndim < 2:
            raise TypeError(
                f"unsupported input type: {type(value).__name__}; "
                "in-memory images must be at least 2D"
            )
        return MemorySource(value, key=f"memory-{ordinal}")
