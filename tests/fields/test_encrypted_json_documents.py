"""EncryptedJSONField stores every value of a JSON document encrypted - any root, any depth, every
type of value - and with ``encrypt_keys=True`` every dict key too; an AlterField switching
``encrypt_keys`` rewrites the stored keys."""

import json
from typing import Any

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError, DecryptionError, FieldError
from hare.fields import EncryptedJSONField, IntField
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.migrations.migration import Migration
from hare.migrations.operations import AlterField, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import F
from tests.fields.models_encrypted import EncryptedDocumentRecord, EncryptedWebhookSettings

SECRET_KEY = "test-field-encryption-secret"
NEW_SECRET_KEY = "test-field-encryption-secret-2"

#: Documents of every shape: each root type, nesting of lists and dicts, every type of leaf.
DOCUMENTS = {
    "string": "text",
    "empty_string": "",
    "unicode_string": "привет 🌍",
    "integer": 12,
    "negative_float": -1.5,
    "long_integer": 10**30,
    "true": True,
    "false": False,
    "empty_list": [],
    "empty_dict": {},
    "list_of_strings": ["a", "b", "a"],
    "list_of_dicts": [{"name": "a", "count": 1}, {"name": "b", "flags": [True, None]}],
    "dict_of_lists": {"tags": ["x", "y"], "scores": [1, 2.5, -3], "empty": []},
    "mixed_leaves": {"text": "t", "number": 7, "float": 0.1, "yes": True, "no": False, "nothing": None},
    "deep": {"a": [{"b": {"c": [[{"d": ["deep", 1, None, {"e": False}]}]]}}]},
    "unicode_keys": {"ключ": "значение", "emoji 🌍": ["ü"]},
}


@pytest.fixture(autouse=True)
def field_encryption_key():
    FieldEncryption.configure(SECRET_KEY)
    yield
    FieldEncryption.reset()


async def fetch_raw_document(record_id: int, column: str) -> Any:
    connection = Connections.get("models")
    literals = connection.dialect.literals
    quoted_table = literals.quote_identifier("encrypted_document_record")
    rows = await connection.execute_dicts(
        f"SELECT {literals.quote_identifier(column)} AS stored FROM {quoted_table} WHERE id = {record_id}"
    )
    stored = rows[0]["stored"]
    return json.loads(stored) if isinstance(stored, (str, bytes)) else stored


def collect_leaves_and_keys(document: Any, leaves: list[Any], keys: list[str]) -> None:
    if isinstance(document, dict):
        for key, item in document.items():
            keys.append(key)
            collect_leaves_and_keys(item, leaves, keys)
    elif isinstance(document, list):
        for item in document:
            collect_leaves_and_keys(item, leaves, keys)
    else:
        leaves.append(document)


def decrypt_stored_document(document: Any, decrypts_keys: bool) -> Any:
    """A stored document with every leaf - and, when asked, every key - decrypted, built
    independently of the field's own reader."""
    if isinstance(document, dict):
        return {
            (FieldEncryption.decrypt(key, "test") if decrypts_keys else key): decrypt_stored_document(
                item, decrypts_keys
            )
            for key, item in document.items()
        }
    if isinstance(document, list):
        return [decrypt_stored_document(item, decrypts_keys) for item in document]
    assert isinstance(document, str)
    return json.loads(FieldEncryption.decrypt(document, "test"))


def get_plain_leaves_and_keys(document: Any) -> tuple[list[Any], list[str]]:
    leaves: list[Any] = []
    keys: list[str] = []
    collect_leaves_and_keys(document, leaves, keys)
    return leaves, keys


@pytest.mark.asyncio
@pytest.mark.parametrize("document_name", DOCUMENTS)
@pytest.mark.parametrize("column", ["plain_keys", "encrypted_keys"])
async def test_every_value_of_every_document_is_stored_encrypted(db_encrypted_fields, document_name, column):
    document = DOCUMENTS[document_name]
    record = await EncryptedDocumentRecord.objects.create(id=1, **{column: document})
    fetched = await EncryptedDocumentRecord.objects.get(id=record.id)
    assert getattr(fetched, column) == document
    assert type(getattr(fetched, column)) is type(document)

    stored = await fetch_raw_document(record.id, column)
    plain_leaves, plain_keys = get_plain_leaves_and_keys(document)
    stored_leaves, stored_keys = get_plain_leaves_and_keys(stored)
    # Every leaf is a token of the leaf's JSON text, in the document's own place (the database
    # may keep the keys of a dict in another order).
    assert len(stored_leaves) == len(plain_leaves)
    assert all(isinstance(stored_leaf, str) for stored_leaf in stored_leaves)
    encrypts_keys = column == "encrypted_keys"
    assert decrypt_stored_document(stored, encrypts_keys) == document
    if encrypts_keys:
        assert not set(stored_keys) & set(plain_keys)
    else:
        assert sorted(stored_keys) == sorted(plain_keys)


@pytest.mark.asyncio
async def test_the_same_document_is_stored_differently_every_time(db_encrypted_fields):
    document = {"key": ["value", 1]}
    await EncryptedDocumentRecord.objects.create(id=1, plain_keys=document, encrypted_keys=document)
    await EncryptedDocumentRecord.objects.create(id=2, plain_keys=document, encrypted_keys=document)
    for column in ("plain_keys", "encrypted_keys"):
        assert await fetch_raw_document(1, column) != await fetch_raw_document(2, column)


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["plain_keys", "encrypted_keys"])
async def test_every_write_path_encrypts_the_whole_document(db_encrypted_fields, column):
    first = await EncryptedDocumentRecord.objects.create(id=1, **{column: {"a": 1}})
    await EncryptedDocumentRecord.objects.bulk_create(
        [EncryptedDocumentRecord(id=2, **{column: [{"b": [2]}]}), EncryptedDocumentRecord(id=3, **{column: "c"})]
    )
    await EncryptedDocumentRecord.objects.filter(id=1).update(**{column: {"updated": [True, {"x": None}]}})
    third = await EncryptedDocumentRecord.objects.get(id=3)
    setattr(third, column, {"saved": 3.5})
    await third.save()
    second = await EncryptedDocumentRecord.objects.get(id=2)
    setattr(second, column, ["bulk", {"updated": 1}])
    await EncryptedDocumentRecord.objects.bulk_update([second], fields=[column])

    assert [getattr(row, column) for row in await EncryptedDocumentRecord.objects.order_by("id")] == [
        {"updated": [True, {"x": None}]},
        ["bulk", {"updated": 1}],
        {"saved": 3.5},
    ]
    for record_id in (1, 2, 3):
        stored_leaves, _stored_keys = get_plain_leaves_and_keys(await fetch_raw_document(record_id, column))
        assert all(isinstance(leaf, str) and leaf.startswith("gAAAAA") for leaf in stored_leaves)
    assert first.id == 1


@pytest.mark.asyncio
async def test_key_lookups_need_plaintext_keys(db_encrypted_fields):
    await EncryptedDocumentRecord.objects.create(id=1, plain_keys={"a": 1, "b": 2}, encrypted_keys={"a": 1})
    await EncryptedDocumentRecord.objects.create(id=2, plain_keys={"c": 3})
    assert [row.id for row in await EncryptedDocumentRecord.objects.filter(plain_keys__has_key="a")] == [1]
    assert [row.id for row in await EncryptedDocumentRecord.objects.filter(plain_keys__has_keys=["a", "b"])] == [1]
    assert [row.id for row in await EncryptedDocumentRecord.objects.filter(plain_keys__has_any_keys=["c", "z"])] == [2]
    assert [row.id for row in await EncryptedDocumentRecord.objects.filter(encrypted_keys__isnull=True)] == [2]
    assert [row.id for row in await EncryptedDocumentRecord.objects.filter(encrypted_keys__not_isnull=True)] == [1]
    for lookup in ("has_key", "has_keys", "has_any_keys"):
        value = "a" if lookup == "has_key" else ["a"]
        with pytest.raises(FieldError, match="keys included"):
            await EncryptedDocumentRecord.objects.filter(**{f"encrypted_keys__{lookup}": value})
    with pytest.raises(FieldError):
        await EncryptedDocumentRecord.objects.filter(plain_keys={"a": 1})


@pytest.mark.asyncio
async def test_a_declared_type_of_any_root_is_restored(db_encrypted_fields):
    settings = [EncryptedWebhookSettings(url="https://a.example", retries=1), {"url": "https://b.example"}]
    record = await EncryptedDocumentRecord.objects.create(id=1, settings_list=settings)
    fetched = await EncryptedDocumentRecord.objects.get(id=record.id)
    assert fetched.settings_list == [
        EncryptedWebhookSettings(url="https://a.example", retries=1),
        EncryptedWebhookSettings(url="https://b.example", retries=3),
    ]
    stored = await fetch_raw_document(record.id, "settings_list")
    assert all(set(item) and all(key.startswith("gAAAAA") for key in item) for item in stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["plain_keys", "encrypted_keys"])
async def test_a_wrong_key_or_a_plain_stored_value_raises_decryption_error(db_encrypted_fields, column):
    await EncryptedDocumentRecord.objects.create(id=1, **{column: {"a": ["b", 1]}})
    FieldEncryption.configure(NEW_SECRET_KEY)
    with pytest.raises(DecryptionError):
        await EncryptedDocumentRecord.objects.get(id=1)
    FieldEncryption.configure(SECRET_KEY)
    field = EncryptedDocumentRecord._meta.fields_map[column]
    with pytest.raises(DecryptionError, match="plain int"):
        field.from_db_value(json.dumps([FieldEncryption.encrypt("1"), 2]))
    with pytest.raises(DecryptionError, match="no JSON value"):
        field.from_db_value(json.dumps([FieldEncryption.encrypt("not json")]))


@pytest.mark.asyncio
async def test_reencrypt_fields_rewrites_every_leaf_and_key_with_the_new_secret(db_encrypted_fields):
    document = {"outer": [{"inner": "value"}, 5, None]}
    await EncryptedDocumentRecord.objects.create(id=1, plain_keys=document, encrypted_keys=document)
    FieldEncryption.configure(NEW_SECRET_KEY, previous_keys=[SECRET_KEY])
    assert await FieldEncryption.reencrypt(EncryptedDocumentRecord) == 1
    FieldEncryption.configure(NEW_SECRET_KEY)
    fetched = await EncryptedDocumentRecord.objects.get(id=1)
    assert fetched.plain_keys == document
    assert fetched.encrypted_keys == document


@pytest.mark.asyncio
async def test_ciphertext_is_copied_only_between_fields_storing_it_the_same_way(db_encrypted_fields):
    await EncryptedDocumentRecord.objects.create(id=1, plain_keys={"a": [1]}, encrypted_keys={"b": "c"})
    with pytest.raises(FieldError, match="same encrypt_keys"):
        await EncryptedDocumentRecord.objects.filter(id=1).update(encrypted_keys=F("plain_keys"))
    with pytest.raises(FieldError, match="same encrypt_keys"):
        await EncryptedDocumentRecord.objects.filter(id=1).update(plain_keys=F("encrypted_keys"))
    await EncryptedDocumentRecord.objects.filter(id=1).update(settings_list=F("encrypted_keys"))
    stored = await fetch_raw_document(1, "settings_list")
    assert decrypt_stored_document(stored, decrypts_keys=True) == {"b": "c"}


def test_encrypt_keys_takes_a_bool():
    invalid_values: tuple[Any, ...] = ("yes", 1, None)
    for value in invalid_values:
        with pytest.raises(ConfigurationError, match="encrypt_keys"):
            EncryptedJSONField(encrypt_keys=value)


def test_encrypt_keys_is_written_into_migrations():
    assert "encrypt_keys" not in EncryptedJSONField().deconstruct()[2]
    assert EncryptedJSONField(encrypt_keys=True).deconstruct()[2]["encrypt_keys"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("row_count", [0, 3, 1201])
async def test_alter_field_switching_encrypt_keys_rewrites_the_stored_keys(db_encrypted_fields, row_count):
    """The rows are rewritten in batches by the primary key - 1201 rows take three of them; the
    leaves stay the same tokens, the keys are encrypted forward and decrypted back."""
    connection = Connections.get("models")
    literals = connection.dialect.literals
    parameters = connection.dialect.parameters
    table_name = "encrypted_keys_switch"
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    create_model = CreateModel(
        name="KeysSwitch",
        fields=[("id", IntField(primary_key=True)), ("document", EncryptedJSONField(null=True))],
        options={"table": table_name},
    )
    plain_field = EncryptedJSONField(null=True)
    try:
        await create_model.run("models", state, dry_run=False, state_editor=editor)
        documents = {number: {"n": number, "items": [{"k": str(number)}]} for number in range(1, row_count + 1)}
        if documents:
            await connection.execute_many(
                f"INSERT INTO {literals.quote_identifier(table_name)} (id, document) VALUES "
                f"({parameters.get_placeholder(1)}, {parameters.get_placeholder(2)})",
                [
                    [number, plain_field.to_db_value(document, EncryptedDocumentRecord)]
                    for number, document in documents.items()
                ],
            )
        await connection.execute(f"INSERT INTO {literals.quote_identifier(table_name)} (id) VALUES ({row_count + 1})")
        before = await connection.execute_dicts(f"SELECT id, document FROM {literals.quote_identifier(table_name)}")

        migration = Migration(name="0002_encrypt_keys", app_label="models")
        migration.operations = [
            AlterField(
                model_name="KeysSwitch", name="document", field=EncryptedJSONField(null=True, encrypt_keys=True)
            )
        ]
        previous_state = state.clone()
        state = await migration.apply(state, dry_run=False, schema_editor=editor)

        encrypted_keys_field = EncryptedJSONField(null=True, encrypt_keys=True)
        rows = await connection.execute_dicts(f"SELECT id, document FROM {literals.quote_identifier(table_name)}")
        assert len(rows) == row_count + 1
        before_by_id = {row["id"]: row["document"] for row in before}
        for row in rows:
            if row["id"] > row_count:
                assert row["document"] is None
                continue
            assert encrypted_keys_field.from_db_value(row["document"]) == documents[row["id"]]
            stored = json.loads(row["document"]) if isinstance(row["document"], (str, bytes)) else row["document"]
            original = before_by_id[row["id"]]
            original = json.loads(original) if isinstance(original, (str, bytes)) else original
            assert "n" not in stored
            # The leaf tokens are the very same.
            assert sorted(get_plain_leaves_and_keys(stored)[0]) == sorted(get_plain_leaves_and_keys(original)[0])

        await migration.unapply(previous_state, dry_run=False, schema_editor=editor)
        rows = await connection.execute_dicts(f"SELECT id, document FROM {literals.quote_identifier(table_name)}")
        for row in rows:
            if row["id"] <= row_count:
                assert plain_field.from_db_value(row["document"]) == documents[row["id"]]
    finally:
        await connection.execute_script(f"DROP TABLE IF EXISTS {literals.quote_identifier(table_name)}")


@pytest.mark.asyncio
async def test_sqlmigrate_tells_the_keys_are_rewritten_in_python(db_encrypted_fields):
    connection = Connections.get("models")
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="KeysSwitch",
        fields=[("id", IntField(primary_key=True)), ("document", EncryptedJSONField(null=True))],
        options={"table": "encrypted_keys_switch_sql"},
    ).run("models", state, dry_run=False, state_editor=editor)
    migration = Migration(name="0002_encrypt_keys", app_label="models")
    migration.operations = [
        AlterField(model_name="KeysSwitch", name="document", field=EncryptedJSONField(null=True, encrypt_keys=True))
    ]
    await migration.apply(state, dry_run=False, schema_editor=editor)
    assert any("rewritten in Python" in sql for sql in editor.collected_sql)


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
async def test_alter_field_without_a_key_configured_leaves_the_rows_as_they_were(db_encrypted_fields):
    connection = Connections.get("models")
    literals = connection.dialect.literals
    table_name = "encrypted_keys_switch_no_key"
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="KeysSwitch",
        fields=[("id", IntField(primary_key=True)), ("document", EncryptedJSONField(null=True))],
        options={"table": table_name},
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        stored = EncryptedJSONField(null=True).to_db_value({"a": 1}, EncryptedDocumentRecord)
        await connection.execute(
            f"INSERT INTO {literals.quote_identifier(table_name)} (id, document) VALUES "
            f"(1, {connection.dialect.parameters.get_placeholder(1)})",
            [stored],
        )
        FieldEncryption.reset()
        migration = Migration(name="0002_encrypt_keys", app_label="models")
        migration.operations = [
            AlterField(
                model_name="KeysSwitch", name="document", field=EncryptedJSONField(null=True, encrypt_keys=True)
            )
        ]
        with pytest.raises(ConfigurationError, match="encryption key"):
            async with connection._in_transaction() as transaction_connection:
                transaction_editor = connection.dialect.schema_editor_class(
                    transaction_connection, atomic=True, collect_sql=False
                )
                await migration.apply(state.clone(), dry_run=False, schema_editor=transaction_editor)
        rows = await connection.execute_dicts(f"SELECT document FROM {literals.quote_identifier(table_name)}")
        assert stored is not None
        assert rows[0]["document"] == stored or json.loads(rows[0]["document"]) == json.loads(stored)
    finally:
        await connection.execute_script(f"DROP TABLE IF EXISTS {literals.quote_identifier(table_name)}")
