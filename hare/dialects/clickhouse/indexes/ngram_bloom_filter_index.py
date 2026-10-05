from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.indexes.constants import CLICKHOUSE_NGRAM_SIZE_RANGE
from hare.dialects.clickhouse.indexes.token_bloom_filter_index import TokenBloomFilterIndex


class NgramBloomFilterIndex(TokenBloomFilterIndex):
    """``TYPE ngrambf_v1(ngram_size, filter_size, hash_functions, seed)`` - a Bloom filter of the
    n-grams of each granule's texts: skips the granules without the n-grams of the text a
    ``__contains``, ``__startswith`` or an equality looks for - a text shorter than an n-gram skips
    none.

    Args:
        args: The arguments of ``TokenBloomFilterIndex``.
        ngram_size: The characters of an n-gram.
        kwargs: The arguments of ``TokenBloomFilterIndex`` - ``filter_size`` (the bytes of each
            granule's filter), ``hash_functions`` (the hash functions of the filter), ``seed`` (the
            seed of the hash functions) and those of every index.

    Raises:
        ConfigurationError: An argument isn't an int in its range.
    """

    INDEX_TYPE = "ngrambf_v1"
    INTEGER_STORAGE_PARAMETERS = ("granularity", "ngram_size", "filter_size", "hash_functions", "seed")

    #: The n-gram size left unset.
    DEFAULT_NGRAM_SIZE = 3

    def __init__(self, *args: Any, ngram_size: int = DEFAULT_NGRAM_SIZE, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ngram_size = self.get_validated_integer("ngram_size", ngram_size, CLICKHOUSE_NGRAM_SIZE_RANGE)

    def get_type_arguments(self) -> tuple[Any, ...]:
        return (self.ngram_size, *super().get_type_arguments())

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.ngram_size != self.DEFAULT_NGRAM_SIZE:
            kwargs["ngram_size"] = self.ngram_size
        return path, args, kwargs
