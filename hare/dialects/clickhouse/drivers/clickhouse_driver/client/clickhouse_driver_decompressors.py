from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from clickhouse_driver.streams import compressed

from hare.core.caching.cache import Cache
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_DECOMPRESSOR_CACHE_SIZE


class ClickhouseDriverDecompressors:
    """The decompressor class of a compressed block clickhouse-driver reads, kept by its compression
    method - the library imports the module of the method for every block."""

    #: The library's own lookup - kept to tell it was replaced, and asked once per method.
    library_lookups: ClassVar[list[Callable[[int], Any]]] = []
    #: Compression method byte -> the decompressor class.
    CLASSES_BY_METHOD: ClassVar[Cache[Any]] = Cache(
        CLICKHOUSE_DRIVER_DECOMPRESSOR_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    @classmethod
    def install(cls) -> None:
        """Replaces the library's lookup - once."""
        if cls.library_lookups:
            return
        cls.library_lookups.append(compressed.get_decompressor_cls)
        compressed.get_decompressor_cls = cls.get_decompressor_class

    @staticmethod
    def get_decompressor_class(method_byte: int) -> Any:
        """The decompressor class of a compression method.

        Args:
            method_byte: The method's byte, which starts a compressed block.

        Returns:
            The class.

        Raises:
            UnknownCompressionMethod: The library knows no such method.
        """
        decompressor_class = ClickhouseDriverDecompressors.CLASSES_BY_METHOD.get((method_byte,))
        if decompressor_class is None:
            decompressor_class = ClickhouseDriverDecompressors.library_lookups[0](method_byte)
            ClickhouseDriverDecompressors.CLASSES_BY_METHOD[(method_byte,)] = decompressor_class
        return decompressor_class
