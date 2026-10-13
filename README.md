# Radiuma Filter

The package namespace is `radiuma_filter`.

The implementation uses a `src/` layout and responsibility-based hierarchy:

- `api/` contains stable public contracts.
- `filters/core/` contains the Composite abstractions.
- `filters/native/` contains one native filter per module plus shared native behavior.
- `filters/standardized/` contains standardized wrappers and their private preserved legacy implementation.
- `runtime/execution/` owns orchestration.
- `runtime/io/` owns input discovery/loading.
- `runtime/extensions/` owns extension/backend resolution.
- `runtime/observability/` owns observers, progress, diagnostics, and durable log storage.

Install the project (for example, editable during development) and import from the new namespace:


```python
from radiuma_filter import GaussianFilter, FilterPipeline
```

Only the `radiuma_filter` package namespace is provided.
