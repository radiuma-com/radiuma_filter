"""Direct client examples for Radiuma Filter."""

from pathlib import Path

from radiuma_filter import (
    CannyFilter,
    ErrorPolicy,
    ExecutionOptions,
    FilterPipeline,
    GaborFilter,
    GaussianFilter,
    LawsFilter,
    LoGFilter,
    MeanFilter,
    MedianFilter,
    NLMFilter,
    SobelFilter,
    WaveletFilter,
)


SINGLE_IMAGE = Path(
    r"PATH_TO_IMAGE"
)

IMAGE_FOLDER = Path(
    r"PATH_TO_FOLDER"
)


gaussian = GaussianFilter(sigma=1.5)
median = MedianFilter(radius=2)
sobel = SobelFilter()

nlm = NLMFilter(
    patch_size=3,
    patch_distance=5,
    h=0.1,
)

canny = CannyFilter(
    low_threshold=0.10,
    high_threshold=0.20,
    use_quantiles=True,
)

mean = MeanFilter(
    by_slice=False,
    BoundaryCondition="reflect",
    FilterSize=3,
)

log = LoGFilter(
    by_slice=False,
    BoundaryCondition="reflect",
    img_average=False,
    riesz_steered=False,
    sigma=1.0,
    sigma_cutoff=4.0,
    riesz=None,
)

laws = LawsFilter(
    by_slice=False,
    BoundaryCondition="reflect",
    Kernel="L5E5S5",
    pooling_method="mean",
    cal_energy=True,
    rotation_inver=True,
    delta=7,
)

gabor = GaborFilter(
    by_slice=True,
    BoundaryCondition="reflect",
    pooling_method="mean",
    response="modulus",
    rotation_inver=False,
    gamma=1.0,
    lambdaa=4.0,
    sigma=1.0,
    step=0.0,
    theta_initial=0.0,
    SigmaTruncate=4.0,
)

wavelet = WaveletFilter(
    by_slice=False,
    mode="reflect",
    filter_config="HHH",
    pooling_method="mean",
    wavelet_family="haar",
    riesz_steered=False,
    rot_invariance=False,
    decomposition_level=1,
    filter_size=3,
    riesz=None,
    wavelet_type="haar",
)


all_filters = (
    gaussian,
    median,
    sobel,
    nlm,
    canny,
    mean,
    log,
    laws,
    gabor,
    wavelet,
)


independent_filters = FilterPipeline.independent(
    *all_filters,
    name="all-filters",
)

sequential_pipeline = FilterPipeline.sequence(
    gaussian,
    median,
    canny,
    name="denoise-and-edge",
)


# CASE 1: one filter x one image
result = gaussian.execute(
    inputs=SINGLE_IMAGE,
)

for output in result.output():
    value = output[gaussian]
    print("CASE 1:", output.key, value.shape, value.dtype)


# CASE 2: one filter x multiple images
result = gaussian.execute(
    inputs=IMAGE_FOLDER,
    options=ExecutionOptions(
        max_in_flight=4,
        error_policy=ErrorPolicy.COLLECT,
    ),
)

for output in result.output():
    if output.errors:
        print("CASE 2 FAILED:", output.key, output.errors)
        continue

    value = output[gaussian]
    print("CASE 2:", output.key, value.shape, value.dtype)


# CASE 3: multiple independent filters x one image
result = independent_filters.execute(
    inputs=SINGLE_IMAGE,
    options=ExecutionOptions(
        error_policy=ErrorPolicy.COLLECT,
    ),
)

for output in result.output():
    print("CASE 3:", output.key)

    for filter_object in all_filters:
        failures = [
            error
            for error in output.errors
            if error.root_component is filter_object
        ]

        if failures:
            print(
                "  FAILED:",
                type(filter_object).__name__,
                failures,
            )
            continue

        value = output[filter_object]
        print(
            " ",
            type(filter_object).__name__,
            value.shape,
            value.dtype,
        )


# CASE 4: multiple independent filters x multiple images
result = independent_filters.execute(
    inputs=IMAGE_FOLDER,
    options=ExecutionOptions(
        max_in_flight=4,
        error_policy=ErrorPolicy.COLLECT,
    ),
)

for output in result.output():
    print("CASE 4:", output.key)

    for filter_object in all_filters:
        failures = [
            error
            for error in output.errors
            if error.root_component is filter_object
        ]

        if failures:
            print(
                "  FAILED:",
                type(filter_object).__name__,
                failures,
            )
            continue

        value = output[filter_object]
        print(
            " ",
            type(filter_object).__name__,
            value.shape,
            value.dtype,
        )


# CASE 5: sequential pipeline x one image
result = sequential_pipeline.execute(
    inputs=SINGLE_IMAGE,
)

for output in result.output():
    value = output[sequential_pipeline]
    print("CASE 5:", output.key, value.shape, value.dtype)


# CASE 6: sequential pipeline x multiple images
result = sequential_pipeline.execute(
    inputs=IMAGE_FOLDER,
    options=ExecutionOptions(
        max_in_flight=4,
        error_policy=ErrorPolicy.COLLECT,
    ),
)

for output in result.output():
    if output.errors:
        print("CASE 6 FAILED:", output.key, output.errors)
        continue

    value = output[sequential_pipeline]
    print("CASE 6:", output.key, value.shape, value.dtype)
