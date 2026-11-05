from __future__ import annotations

from collections.abc import Generator
from typing import Any

from aiohttp import ClientPayloadError
from clickhouse_connect.driver.exceptions import (
    OperationalError,
    StreamCompleteException,
    StreamFailureError,
    _error_code_from_message,
    error_name_from_body,
)
from clickhouse_connect.driver.transform import extract_error_message, extract_exception_with_tag, format_stream_error


class ClickhouseConnectResponse:
    """One Native response, with a client-local factory at the column decoding boundary.

    The HTTP client supplies the source and controls its lease. Errors use the library's
    tagged-error parser, so a server exception at the end of a response is not mistaken for
    normal stream completion. No HTTP request or retry logic is duplicated here.
    """

    def __init__(self, source: Any, context: Any, types: Any) -> None:
        self.source = source
        self.context = context
        self.types = types
        self.names: list[str] = []
        self.column_types: list[Any] = []
        self.blocks_read = 0

    def get_source_error(self, *, tagged_only: bool = False) -> str | None:
        """Read a server error without exposing text the query context asks to redact."""
        message = self.source.last_message
        if not message:
            return None
        tag = getattr(self.source, "exception_tag", None)
        if tag and (error := extract_exception_with_tag(message, tag)):
            return str(error)
        if tagged_only or (self.context.show_clickhouse_errors is not True and b"Code: " not in message[-1024:]):
            return None
        return extract_error_message(message)

    def server_error(self, message: str) -> StreamFailureError:
        """Preserve the library's error code, visibility policy and optional server name."""
        shown = self.context.show_clickhouse_errors
        return StreamFailureError(
            format_stream_error(message, shown),
            code=_error_code_from_message(message),
            name=error_name_from_body(message) if shown else None,
        )

    def read_block(self) -> list[Any] | None:
        """Read a block or normal EOF; close the source on every decoding failure."""
        try:
            try:
                if self.context.block_info:
                    self.source.read_bytes(8)
                column_count = self.source.read_leb128()
            except StreamCompleteException:
                if message := self.get_source_error(tagged_only=True):
                    raise self.server_error(message) from None
                return None
            row_count = self.source.read_leb128()
            columns = []
            for index in range(column_count):
                name = self.source.read_leb128_str()
                type_name = self.source.read_leb128_str()
                if not self.blocks_read:
                    renamer = self.context.column_renamer
                    self.names.append(renamer(name) if renamer is not None else name)
                    self.column_types.append(self.types.get_type(type_name))
                self.context.start_column(name)
                columns.append(
                    self.column_types[index].read_column(self.source, row_count, self.context) if row_count else ()
                )
            self.blocks_read += 1
            return columns
        except Exception as error:
            self.source.close()
            if isinstance(error, StreamFailureError):
                raise
            if isinstance(error, (StreamCompleteException, OperationalError, ClientPayloadError)):
                if message := self.get_source_error():
                    raise self.server_error(message) from error
                raise StreamFailureError("Stream ended before the Native block was complete") from error
            raise

    def blocks(self, first: list[Any]) -> Generator[list[Any]]:
        """Yield decoded blocks lazily, retaining the same codec tree for the response."""
        yield first
        while (block := self.read_block()) is not None:
            yield block
