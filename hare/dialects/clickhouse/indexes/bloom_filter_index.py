from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.indexes.clickhouse_index import ClickhouseIndex
from hare.dialects.clickhouse.indexes.constants import CLICKHOUSE_BLOOM_FILTER_FALSE_POSITIVE
from hare.exceptions import ConfigurationError


class BloomFilterIndex(ClickhouseIndex):
    """``TYPE bloom_filter(false_positive)`` - a Bloom filter of each granule's values: skips the
    granules that surely hold none of the values an equality, ``__in`` or an array's ``__contains``
    looks for.

    Args:
        false_positive: The share of granules read in vain - above 0 and below 1; a smaller one
            takes a larger filter.

    Raises:
        ConfigurationError: ``false_positive`` isn't a number above 0 and below 1.
    """

    INDEX_TYPE = "bloom_filter"
    FLOAT_STORAGE_PARAMETERS = ("false_positive",)

    def __init__(
        self, *args: Any, false_positive: float = CLICKHOUSE_BLOOM_FILTER_FALSE_POSITIVE, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            isinstance(false_positive, bool)
            or not isinstance(false_positive, int | float)
            or not 0 < false_positive < 1
        ):
            raise ConfigurationError(
                f"BloomFilterIndex(false_positive=...) takes a number above 0 and below 1, got {false_positive!r}"
            )
        self.false_positive = float(false_positive)

    def get_type_arguments(self) -> tuple[Any, ...]:
        return (self.false_positive,)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.false_positive != CLICKHOUSE_BLOOM_FILTER_FALSE_POSITIVE:
            kwargs["false_positive"] = self.false_positive
        return path, args, kwargs
