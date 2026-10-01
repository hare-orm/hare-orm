from hare.dialects.postgresql.indexes.postgresql_index import PostgresqlIndex


class BloomIndex(PostgresqlIndex):
    INDEX_TYPE = "BLOOM"
    SUPPORTS_INCLUDE = False


class BrinIndex(PostgresqlIndex):
    INDEX_TYPE = "BRIN"
    SUPPORTS_INCLUDE = False


class GinIndex(PostgresqlIndex):
    INDEX_TYPE = "GIN"
    SUPPORTS_INCLUDE = False


class GistIndex(PostgresqlIndex):
    INDEX_TYPE = "GIST"


class HashIndex(PostgresqlIndex):
    INDEX_TYPE = "HASH"
    SUPPORTS_INCLUDE = False


class SpGistIndex(PostgresqlIndex):
    INDEX_TYPE = "SPGIST"
