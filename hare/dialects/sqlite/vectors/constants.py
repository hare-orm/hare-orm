from __future__ import annotations

#: What to install for VectorField's distances on SQLite.
SQLITE_VECTOR_EXTENSION_PACKAGE = "hare-orm[sqlite-vec]"

#: The most dimensions a sqlite-vec vector may have.
SQLITE_VECTOR_MAX_DIMENSIONS = 8192

#: The ``array`` type code of a vector's stored form - sqlite-vec's float32 vector, each element a
#: float32 in the machine's byte order.
SQLITE_VECTOR_ARRAY_TYPECODE = "f"

#: sqlite-vec's error for two vectors of different lengths, which the inner product UDF raises too.
SQLITE_VECTOR_DIMENSION_MISMATCH_MESSAGE = (
    "Vector dimension mismatch. First vector has {first} dimensions, while the second has {second} dimensions."
)
