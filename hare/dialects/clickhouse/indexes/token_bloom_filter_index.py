from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.indexes.clickhouse_index import ClickhouseIndex
from hare.dialects.clickhouse.indexes.constants import (
    CLICKHOUSE_FILTER_SEED_RANGE,
    CLICKHOUSE_FILTER_SIZE_RANGE,
    CLICKHOUSE_HASH_FUNCTIONS_RANGE,
)


class TokenBloomFilterIndex(ClickhouseIndex):
    """``TYPE tokenbf_v1(filter_size, hash_functions, seed)`` - a Bloom filter of the words of each
    granule's texts (runs of letters and digits): skips the granules without a word an equality or a
    whole-word search looks for.

    Args:
        filter_size: The bytes of each granule's filter.
        hash_functions: The hash functions of the filter.
        seed: The seed of the hash functions.

    Raises:
        ConfigurationError: An argument isn't an int in its range.
    """

    INDEX_TYPE = "tokenbf_v1"
    INTEGER_STORAGE_PARAMETERS: tuple[str, ...] = ("granularity", "filter_size", "hash_functions", "seed")

    #: The arguments left unset.
    DEFAULT_FILTER_SIZE = 256
    DEFAULT_HASH_FUNCTIONS = 2

    def __init__(
        self,
        *args: Any,
        filter_size: int = DEFAULT_FILTER_SIZE,
        hash_functions: int = DEFAULT_HASH_FUNCTIONS,
        seed: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.filter_size = self.get_validated_integer("filter_size", filter_size, CLICKHOUSE_FILTER_SIZE_RANGE)
        self.hash_functions = self.get_validated_integer(
            "hash_functions", hash_functions, CLICKHOUSE_HASH_FUNCTIONS_RANGE
        )
        self.seed = self.get_validated_integer("seed", seed, CLICKHOUSE_FILTER_SEED_RANGE)

    def get_type_arguments(self) -> tuple[Any, ...]:
        return (self.filter_size, self.hash_functions, self.seed)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.filter_size != self.DEFAULT_FILTER_SIZE:
            kwargs["filter_size"] = self.filter_size
        if self.hash_functions != self.DEFAULT_HASH_FUNCTIONS:
            kwargs["hash_functions"] = self.hash_functions
        if self.seed:
            kwargs["seed"] = self.seed
        return path, args, kwargs
