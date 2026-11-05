"""Typed JSON columns retain Pydantic conversion during model hydration."""

from __future__ import annotations

import pytest

from tests.native_acceleration.test_field_codecs import assert_reads_match
from tests.shared_models.testmodels_pydantic import JSONFieldsDeclaredType

pytestmark = pytest.mark.database_independent


def test_declared_type_reads_match():
    assert_reads_match(JSONFieldsDeclaredType, "item", [None, '{"name": "x", "value": 1}', '{"name": 5}', "[]"])
