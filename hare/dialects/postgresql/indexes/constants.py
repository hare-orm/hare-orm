from __future__ import annotations

#: pgvector's accepted range of an IVFFlat index's `lists` storage parameter.
IVFFLAT_LISTS_RANGE = (1, 32768)

#: pgvector's accepted range of an HNSW index's `m` storage parameter.
HNSW_M_RANGE = (2, 100)

#: pgvector's accepted range of an HNSW index's `ef_construction` storage parameter - which must
#: also be at least twice `m`.
HNSW_EF_CONSTRUCTION_RANGE = (4, 1000)
