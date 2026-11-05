import json

import pytest

from hare import Model, fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.models.write.returned_values import ReturnedValues

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("key", [{"a": [1, {"b": 2}]}, [1, {"a": [2]}]])
@pytest.mark.parametrize(
    "match,expected_indices",
    [(ReturnedValues.match_rows, [2, 0]), (ReturnedValues.match_rows_by_stored_values, [2, 0, 1])],
    ids=["first_instance", "each_unmatched_instance"],
)
def test_returned_rows_match_container_keys_in_input_order(key, match, expected_indices):
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
    instances = [first, repeated, second]
    matched = list(match(Book, DialectRegistry.get_dialect("sqlite").types, instances, rows, ["key"]))
    assert len(matched) == len(expected_indices)
    assert all(obj is instances[index] for (obj, _row), index in zip(matched, expected_indices, strict=True))
    assert [row for _, row in matched] == rows[: len(expected_indices)]
