"""The native jsonb containment and key tests answer as the Python ones on every pair of a grid of
JSON values - numbers compared as Python compares the int or float json.loads() makes."""

import json

import pytest

from hare.dialects.sqlite.functions.json.sqlite_json_containment import SqliteJsonContainment
from hare.dialects.sqlite.functions.json.sqlite_json_keys import SqliteJsonKeys
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

VALUES = [
    None, True, False, 0, 1, 1.0, -0.0, 2**53, 2**53 + 1, 9007199254740993.0, 10**30, 0.1, "a", "b", "", "1",
    [], [1], [1, 2], [1.0], ["a"], ["a", "b"], [[1]], [{"a": 1}], {}, {"a": 1}, {"a": 1.0}, {"a": [1, 2]},
    {"a": {"b": None}}, {"b": "a"}, {"a": True}, {"a": "x", "b": [True, None]},
]  # fmt: skip


def get_documents() -> list:
    documents = [json.dumps(value) for value in VALUES]
    return [*documents, 5, 2.5, 10**20, "1e400", '{"a": 1, "a": 2}', "NaN", '[1, "\\ud800"]']


@pytest.fixture
def native():
    return SqliteNativeFunctions.module.JsonContainment(
        SqliteJsonContainment.contains, SqliteJsonKeys.has_key, SqliteJsonKeys.has_keys
    )


def call_both(native_call, python_call, *arguments):
    try:
        expected = python_call(*arguments)
    except Exception as error:  # noqa: BLE001 - the error is what is compared.
        with pytest.raises(type(error)):
            native_call(*arguments)
        return
    assert native_call(*arguments) == expected, arguments


def test_contains(native) -> None:
    documents = get_documents()
    for container in documents:
        for wanted in documents:
            call_both(native.contains, SqliteJsonContainment.contains, container, wanted)
    assert native.contains(None, "1") is None


def test_has_key_and_keys(native) -> None:
    keys = ["a", "b", "1", "", "x"]
    for document in get_documents():
        for key in keys:
            call_both(native.has_key, SqliteJsonKeys.has_key, document, key)
        for mode in ("all", "any"):
            for key_list in (["a"], ["a", "b"], [], ["x", "a"]):
                call_both(native.has_keys, SqliteJsonKeys.has_keys, document, json.dumps(key_list), mode)
    call_both(native.has_keys, SqliteJsonKeys.has_keys, '{"1": 1}', "[1]", "all")
    assert native.has_key(None, "a") is None
    assert native.has_key("{}", None) is None
