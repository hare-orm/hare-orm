"""Client-local codec trees preserve the library registry and decode independent wire fixtures."""

import pytest
from clickhouse_connect.datatypes import dynamic, registry
from clickhouse_connect.driver.asyncclient import BytesSource
from clickhouse_connect.driver.ctypes import RespBuffCls
from clickhouse_connect.driver.exceptions import StreamFailureError
from clickhouse_connect.driver.query import QueryContext

from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_transform import (
    ClickhouseConnectTransform,
)

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    "name",
    ["Dynamic", "Array(Dynamic)", "Map(String, Dynamic)", "Tuple(Dynamic, String)", "JSON(value Dynamic)"],
)
def test_codec_trees_are_local_to_each_client(name):
    original = registry.get_from_name(name)
    original_insert_name = original.insert_name
    original_decoder = dynamic.decode_shared_variant_value
    first, second = ClickhouseConnectTransform(), ClickhouseConnectTransform()
    first_type, second_type = first.types.get_type(name), second.types.get_type(name)
    assert first_type is not second_type
    assert first_type is not original
    assert first.types.get_type(name) is first_type
    assert registry.get_from_name(name) is original
    assert original.insert_name == original_insert_name
    assert dynamic.decode_shared_variant_value is original_decoder
    assert registry.get_from_name("Dynamic").insert_name == "String"


def test_dynamic_shared_value_and_null_from_native_wire():
    # One column, two rows, name 'value', type Dynamic. Structure v2 has no declared variants;
    # discriminator 0 selects SharedVariant, 255 selects NULL. The blob is UInt8(7).
    body = b"\x01\x02\x05value\x07Dynamic" + b"\x02\x00\x00\x00\x00\x00\x00\x00"
    body += b"\x00" + b"\x00" * 8 + b"\x00\xff\x02\x01\x07"
    result = ClickhouseConnectTransform().parse_response(RespBuffCls(BytesSource(body)), QueryContext())
    assert result.column_names == ("value",)
    assert result.result_rows == [(7,), (None,)]


def test_a_truncated_block_is_not_normal_stream_completion():
    source = RespBuffCls(BytesSource(b"\x01\x01\x05value\x05UInt8"))
    with pytest.raises(StreamFailureError):
        ClickhouseConnectTransform().parse_response(source, QueryContext())
