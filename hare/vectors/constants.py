from __future__ import annotations

#: The most dimensions a VectorField takes - pgvector's limit; a dialect may take fewer.
VECTOR_MAX_DIMENSIONS = 16000

#: The largest magnitude of a vector element - a float32 (float4), what every dialect stores.
VECTOR_ELEMENT_MAX = 3.4028234663852886e38

#: The Features flag a vector distance and the nearby lookup need.
VECTOR_SEARCH_REQUIRED_FEATURE = "supports_vector_search"
