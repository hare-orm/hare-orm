"""The clickhouse-driver driver spends no work per statement on what it doesn't use: the ProfileEvents
block the server sends after each statement is read past unread, the OS user and the host name of
the client description are read once, the decompressor of a block is not imported again, and the
statement's settings are written as the bytes kept from the first statement with the same ones."""

import io
import socket
from types import SimpleNamespace

import pytest

pytest.importorskip("clickhouse_driver")

from clickhouse_driver import connection, defines  # noqa: E402
from clickhouse_driver.block import BlockInfo  # noqa: E402
from clickhouse_driver.bufferedreader import BufferedSocketReader  # noqa: E402
from clickhouse_driver.clientinfo import ClientInfo  # noqa: E402
from clickhouse_driver.protocol import CompressionMethodByte  # noqa: E402
from clickhouse_driver.reader import read_binary_str  # noqa: E402
from clickhouse_driver.streams import compressed  # noqa: E402
from clickhouse_driver.varint import write_varint  # noqa: E402
from clickhouse_driver.writer import (  # noqa: E402
    write_binary_int8,
    write_binary_int64,
    write_binary_str,
    write_binary_uint8,
    write_binary_uint32,
    write_binary_uint64,
)

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client_info import (  # noqa: E402
    ClickhouseDriverClientInfo,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_decompressors import (  # noqa: E402
    ClickhouseDriverDecompressors,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (  # noqa: E402
    ClickhouseDriverLibraryClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_connection import (  # noqa: E402
    ClickhouseDriverLibraryConnection,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_service_block_stream import (  # noqa: E402
    ClickhouseDriverServiceBlockStream,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_settings import (  # noqa: E402
    ClickhouseDriverSettings,
)
from hare.dialects.clickhouse.drivers.constants import (  # noqa: E402
    CLICKHOUSE_OPTIONAL_SESSION_SETTINGS,
    CLICKHOUSE_SESSION_SETTINGS,
)

PROFILE_EVENTS_COLUMNS = (
    ("host_name", "String"),
    ("current_time", "DateTime"),
    ("thread_id", "UInt64"),
    ("type", "Enum8('increment' = 1, 'gauge' = 2)"),
    ("name", "String"),
    ("value", "Int64"),
)


def write_block(buffer, columns, row_count, write_values):
    BlockInfo().write(buffer)
    write_varint(len(columns), buffer)
    write_varint(row_count, buffer)
    for name, column_type in columns:
        write_binary_str(name, buffer)
        write_binary_str(column_type, buffer)
        # No custom serialization.
        write_binary_uint8(0, buffer)
        write_values(name, buffer)


def write_profile_events_values(name, buffer):
    for row in range(2):
        if name in {"host_name", "name"}:
            write_binary_str(f"{name}-{row}", buffer)
        elif name == "current_time":
            write_binary_uint32(1_700_000_000 + row, buffer)
        elif name == "thread_id":
            write_binary_uint64(row, buffer)
        elif name == "type":
            write_binary_int8(1, buffer)
        else:
            write_binary_int64(-row, buffer)


def read_through_socket(data):
    """A stream reading ``data`` the way a connection reads the server's bytes."""
    sending, receiving = socket.socketpair()
    sending.sendall(data)
    sending.close()
    context = SimpleNamespace(
        server_info=SimpleNamespace(used_revision=defines.CLIENT_REVISION, get_timezone=lambda: "UTC"),
        client_settings={"use_numpy": False, "strings_as_bytes": False, "strings_encoding": "utf-8"},
        settings={},
    )
    reader = BufferedSocketReader(receiving, 1024)
    return ClickhouseDriverServiceBlockStream(reader, context), reader, receiving


def test_the_profile_events_block_is_read_past_without_its_rows():
    buffer = io.BytesIO()
    write_block(buffer, PROFILE_EVENTS_COLUMNS, 2, write_profile_events_values)
    write_binary_str("after the block", buffer)
    stream, reader, receiving = read_through_socket(buffer.getvalue())
    try:
        block = stream.read()
        assert block.num_rows == 0
        assert [name for name, _type in block.columns_with_types] == [name for name, _type in PROFILE_EVENTS_COLUMNS]
        # The stream stands right after the block.
        assert read_binary_str(reader) == "after the block"
    finally:
        receiving.close()


def test_a_log_block_is_read_whole():
    columns = (("event_time", "DateTime"), ("text", "String"))

    def write_log_values(name, buffer):
        for row in range(2):
            if name == "event_time":
                write_binary_uint32(1_700_000_000 + row, buffer)
            else:
                write_binary_str(f"line {row}", buffer)

    buffer = io.BytesIO()
    write_block(buffer, columns, 2, write_log_values)
    stream, _reader, receiving = read_through_socket(buffer.getvalue())
    try:
        block = stream.read()
        assert block.num_rows == 2
        assert [row[1] for row in block.get_rows()] == ["line 0", "line 1"]
    finally:
        receiving.close()


def test_a_connection_the_client_hands_out_is_hare_s():
    library_client = ClickhouseDriverLibraryClient(host="db.local")
    assert type(library_client.connection) is ClickhouseDriverLibraryConnection
    library_client.connection = library_client.get_connection()
    assert type(library_client.connection) is ClickhouseDriverLibraryConnection


def test_the_client_description_is_the_library_s():
    ClickhouseDriverClientInfo.install()
    context = SimpleNamespace(
        client_settings={"opentelemetry_traceparent": None, "opentelemetry_tracestate": "", "quota_key": "quota"}
    )
    description = ClientInfo("hare", context, 54468)
    library_description = ClientInfo.__new__(ClientInfo)
    ClickhouseDriverClientInfo.library_initializers[0](library_description, "hare", context, 54468)
    # Every attribute the same, but the trace context object and the moment the statement starts.
    varying = {"client_trace_context", "initial_query_start_time_microseconds"}
    assert {name: value for name, value in vars(description).items() if name not in varying} == {
        name: value for name, value in vars(library_description).items() if name not in varying
    }
    assert description.client_trace_context.tracestate == library_description.client_trace_context.tracestate
    assert (
        abs(
            description.initial_query_start_time_microseconds
            - library_description.initial_query_start_time_microseconds
        )
        < 10_000_000
    )


def test_a_decompressor_class_is_looked_up_once_per_method():
    ClickhouseDriverDecompressors.install()
    library_class = ClickhouseDriverDecompressors.library_lookups[0](CompressionMethodByte.LZ4)
    assert compressed.get_decompressor_cls(CompressionMethodByte.LZ4) is library_class
    assert ClickhouseDriverDecompressors.CLASSES_BY_METHOD.get((CompressionMethodByte.LZ4,)) is library_class
    assert compressed.get_decompressor_cls(CompressionMethodByte.LZ4) is library_class


@pytest.mark.parametrize(
    "settings",
    [
        {**CLICKHOUSE_SESSION_SETTINGS, **CLICKHOUSE_OPTIONAL_SESSION_SETTINGS},
        # Equal keys, written differently.
        {"join_use_nulls": 1},
        {"join_use_nulls": True},
        {"max_block_size": 1.0},
        {"unhashable": [1, 2]},
        {},
        None,
    ],
)
@pytest.mark.parametrize("settings_as_strings", [True, False])
@pytest.mark.parametrize("flags", [0, 1, 2])
def test_statement_settings_are_written_as_the_library_writes_them(settings, settings_as_strings, flags):
    ClickhouseDriverSettings.install()
    library_writer = ClickhouseDriverSettings.library_writers[0]
    assert connection.write_settings is ClickhouseDriverSettings.write
    expected = io.BytesIO()
    library_writer(settings, expected, settings_as_strings, flags)
    # The second time from the kept bytes.
    for _ in range(2):
        written = io.BytesIO()
        connection.write_settings(settings, written, settings_as_strings, flags)
        assert written.getvalue() == expected.getvalue()
