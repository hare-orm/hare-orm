import json

import pytest

from hare import Model, fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.models.write.returned_values import ReturnedValues

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("key", [{"a": [1, {"b": 2}]}, [1, {"a": [2]}]])
def test_returned_rows_match_container_keys_and_keep_the_first_instance(key):
    class Book(Model):
        key = fields.JSONField(source_field="book_key")

    first = Book(key=key)
    repeated = Book(key=key)
    second = Book(key={"other": [3]})
    rows = [
        {"book_key": json.dumps(second.key)},
        {"book_key": json.dumps(key)},
        {"book_key": json.dumps(key)},
        {"book_key": json.dumps({"absent": 4})},
    ]
    matched = list(
        ReturnedValues.match_rows(
            Book, DialectRegistry.get_dialect("sqlite").types, [first, repeated, second], rows, ["key"]
        )
    )
    assert len(matched) == 2
    assert matched[0][0] is second
    assert matched[1][0] is first
    assert [row for _, row in matched] == rows[:2]
