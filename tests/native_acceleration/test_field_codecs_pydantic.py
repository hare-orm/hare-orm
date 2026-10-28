"""A JSONField with a declared pydantic type reads through ``rust.native.rows.FieldCodec`` as the field's own
Python method reads, and writes through the field: pydantic validates what it writes."""

from __future__ import annotations

import pytest

from tests.native_acceleration.test_field_codecs import CONNECTIONS, assert_builtin_codecs, assert_reads_match
from tests.shared_models.testmodels_pydantic import JSONFieldsDeclaredType

REQUIRES = ("pydantic", "native")

pytestmark = pytest.mark.database_independent


def test_declared_type_reads_match():
    assert_reads_match(JSONFieldsDeclaredType, "item", [None, '{"name": "x", "value": 1}', '{"name": 5}', "[]"])


def test_declared_type_reads_through_its_codec_and_writes_through_the_field():
    assert_builtin_codecs(JSONFieldsDeclaredType, "item", ("json", "call", {}), CONNECTIONS[:2])
