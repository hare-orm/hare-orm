from __future__ import annotations

#: The index granules an index's granule covers - one by default, an entry of the index for each
#: granule of the table.
CLICKHOUSE_INDEX_GRANULARITY_RANGE = (1, 1_000_000)
#: The distinct values a ``set`` index keeps of each of its granules - 0 for all of them.
CLICKHOUSE_SET_INDEX_MAX_ROWS_RANGE = (0, 1_000_000_000)
#: The share of false positives of a ``bloom_filter`` index - ClickHouse's default.
CLICKHOUSE_BLOOM_FILTER_FALSE_POSITIVE = 0.025
#: The characters of an n-gram, the bytes of a Bloom filter, its hash functions and its seed.
CLICKHOUSE_NGRAM_SIZE_RANGE = (1, 8)
CLICKHOUSE_FILTER_SIZE_RANGE = (1, 1 << 30)
CLICKHOUSE_HASH_FUNCTIONS_RANGE = (1, 32)
CLICKHOUSE_FILTER_SEED_RANGE = (0, 2**32 - 1)
