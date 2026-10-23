from __future__ import annotations

from hare.dialects.postgresql.indexes.constants import BLOOM_EXTENSION, BTREE_GIN_EXTENSION
from hare.dialects.postgresql.indexes.postgresql_index import PostgresqlIndex
from hare.dialects.postgresql.schema.constants import BTREE_GIST_EXTENSION


class BloomIndex(PostgresqlIndex):
    INDEX_TYPE = "BLOOM"
    REQUIRED_EXTENSION = BLOOM_EXTENSION
    SUPPORTS_INCLUDE = False


class BrinIndex(PostgresqlIndex):
    INDEX_TYPE = "BRIN"
    SUPPORTS_INCLUDE = False


class GinIndex(PostgresqlIndex):
    INDEX_TYPE = "GIN"
    SCALAR_KEY_EXTENSION = BTREE_GIN_EXTENSION
    SUPPORTS_INCLUDE = False


class GistIndex(PostgresqlIndex):
    INDEX_TYPE = "GIST"
    SCALAR_KEY_EXTENSION = BTREE_GIST_EXTENSION


class HashIndex(PostgresqlIndex):
    INDEX_TYPE = "HASH"
    SUPPORTS_INCLUDE = False


class SpGistIndex(PostgresqlIndex):
    INDEX_TYPE = "SPGIST"
