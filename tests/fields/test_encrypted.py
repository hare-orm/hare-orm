import datetime
import json
import pathlib
import subprocess  # nosec
import sys
import uuid

import pytest

from hare.contrib.pydantic import pydantic_model_creator, pydantic_queryset_creator
from hare.core.connections.connections import Connections
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError, DecryptionError, FieldError, ValidationError
from hare.fields import CharField, EncryptedJSONField, EncryptedTextField, GeneratedField, IntField, TextField
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.query.expressions import Case, F, OuterReference, Subquery, Value, When, Window
from hare.query.functions import Coalesce, Count, Max, Upper
from hare.query.functions.window import FirstValue, Lag, RowNumber, Sum as WindowSum
from tests.fields.models_encrypted import (
    EncryptedOwner,
    EncryptedRecord,
    EncryptedRecordAttachment,
    EncryptedTypedRecord,
    EncryptedWebhookSettings,
    SensitiveGeneratedRecord,
)

SECRET_KEY = "test-field-encryption-secret"
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Pinned: a generated schema's name (and every $ref built from it) must stay stable.
ROOT_PUBLIC_RECORD_SCHEMA_NAME = "tests.fields.models_encrypted.EncryptedRecord:jhq63swidditdeg3"


@pytest.fixture(autouse=True)
def field_encryption_key():
    FieldEncryption.configure(SECRET_KEY)
    yield
    FieldEncryption.reset()


async def fetch_raw_record_row() -> dict:
    connection = Connections.get("models")
    rows = await connection.execute_dicts("SELECT secret, config, public_note FROM encrypted_record")
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_round_trip_create_get(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(
        secret="s3cr3t",
        config={"url": "https://hook.example/abc", "retries": 3, "enabled": True, "empty": "", "nested": [1, "a"]},
    )
    assert created.secret == "s3cr3t"
    assert created.config["url"] == "https://hook.example/abc"

    fetched = await EncryptedRecord.objects.get(id=created.id)
    assert fetched.secret == "s3cr3t"
    assert fetched.config == {
        "url": "https://hook.example/abc",
        "retries": 3,
        "enabled": True,
        "empty": "",
        "nested": [1, "a"],
    }
    assert fetched.to_dict()["secret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_round_trip_update_values_and_bulk(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(secret="first", config={"token": "one"})
    created.secret = "second"
    created.config = {"token": "two"}
    await created.save()
    await created.refresh_from_db()
    assert (created.secret, created.config) == ("second", {"token": "two"})

    await EncryptedRecord.objects.filter(id=created.id).update(secret="third")
    assert await EncryptedRecord.objects.filter(id=created.id).values_list("secret", flat=True) == ["third"]
    assert await EncryptedRecord.objects.filter(id=created.id).values("config") == [{"config": {"token": "two"}}]

    await EncryptedRecord.objects.bulk_create(
        [EncryptedRecord(secret=f"bulk-{index}", config={"k": "v"}) for index in range(3)]
    )
    secrets = sorted(await EncryptedRecord.objects.filter(title="").values_list("secret", flat=True))
    assert secrets == ["bulk-0", "bulk-1", "bulk-2", "third"]


@pytest.mark.asyncio
async def test_null_and_empty_values(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(secret="", config=None)
    fetched = await EncryptedRecord.objects.get(id=created.id)
    assert fetched.secret == ""
    assert fetched.config is None
    raw_row = await fetch_raw_record_row()
    assert raw_row["secret"] not in ("", None)
    assert raw_row["config"] is None


@pytest.mark.asyncio
async def test_ciphertext_stored_in_database(db_encrypted_fields):
    await EncryptedRecord.objects.create(
        secret="plain-secret", config={"url": "plain-url", "port": 8080}, public_note="note"
    )
    raw_row = await fetch_raw_record_row()

    assert "plain-secret" not in raw_row["secret"]
    assert FieldEncryption.decrypt(raw_row["secret"], "secret") == "plain-secret"

    raw_config = raw_row["config"] if isinstance(raw_row["config"], dict) else json.loads(raw_row["config"])
    assert set(raw_config) == {"url", "port"}
    assert raw_config["port"] != 8080
    assert raw_config["url"] != "plain-url"
    # Each value is a token of its JSON text - a number as a number.
    assert FieldEncryption.decrypt(raw_config["url"], "config") == '"plain-url"'
    assert FieldEncryption.decrypt(raw_config["port"], "config") == "8080"
    # sensitive=False only changes the flag, not the encryption.
    assert raw_row["public_note"] != "note"


@pytest.mark.asyncio
async def test_same_plaintext_encrypts_differently(db_encrypted_fields):
    field = EncryptedRecord._meta.fields_map["secret"]
    assert field.to_db_value("same", EncryptedRecord) != field.to_db_value("same", EncryptedRecord)


@pytest.mark.asyncio
async def test_wrong_key_raises_decryption_error(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(secret="s3cr3t")
    FieldEncryption.configure("some-other-key")
    with pytest.raises(DecryptionError, match=r"EncryptedRecord\.secret.*different key"):
        await EncryptedRecord.objects.get(id=created.id)


@pytest.mark.asyncio
async def test_wrong_key_raises_decryption_error_for_json(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(config={"url": "x"})
    FieldEncryption.configure("some-other-key")
    with pytest.raises(DecryptionError, match=r"EncryptedRecord\.config"):
        await EncryptedRecord.objects.get(id=created.id)


@pytest.mark.asyncio
async def test_key_not_configured_raises_configuration_error(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(secret="s3cr3t")
    FieldEncryption.reset()
    # Assigning plaintext needs no key - nothing is encrypted/decrypted until save/read.
    unsaved = EncryptedRecord(secret="plain")
    assert unsaved.secret == "plain"
    with pytest.raises(ConfigurationError, match="FieldEncryption.configure"):
        await EncryptedRecord.objects.get(id=created.id)
    with pytest.raises(ConfigurationError, match="FieldEncryption.configure"):
        await EncryptedRecord.objects.create(secret="another")


def test_configure_rejects_empty_or_non_string_key():
    for bad_key in ("", None, 123):
        with pytest.raises(ConfigurationError):
            FieldEncryption.configure(bad_key)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_assigned_value_is_not_decrypted(db_encrypted_fields):
    record = EncryptedRecord(secret="plain", config={"url": "plain"})
    assert record.secret == "plain"
    assert record.config == {"url": "plain"}
    assert EncryptedRecord(secret=123).secret == "123"


@pytest.mark.asyncio
async def test_json_field_takes_any_json_value(db_encrypted_fields):
    field = EncryptedRecord._meta.fields_map["config"]
    assert field.from_db_value(field.to_db_value(["a", "b"], EncryptedRecord)) == ["a", "b"]
    # A str is a value, as on JSONField - not JSON text.
    assert field.from_db_value(field.to_db_value("{broken", EncryptedRecord)) == "{broken"
    assert field.from_db_value(field.to_db_value('{"url": "x"}', EncryptedRecord)) == '{"url": "x"}'
    with pytest.raises(ValidationError, match="not valid JSON"):
        field.from_db_value("{broken")


@pytest.mark.asyncio
async def test_filtering_by_value_raises_field_error(db_encrypted_fields):
    await EncryptedRecord.objects.create(secret="s3cr3t", config={"url": "x"})
    for lookup in ({"secret": "s3cr3t"}, {"secret__in": ["s3cr3t"]}, {"secret__contains": "s3"}, {"secret__not": "a"}):
        with pytest.raises(FieldError, match=r"EncryptedRecord\.secret is encrypted.*__isnull"):
            await EncryptedRecord.objects.filter(**lookup).first()
    for lookup in ({"config": {"url": "x"}}, {"config__contains": {"url": "x"}}):
        with pytest.raises(FieldError, match=r"EncryptedRecord\.config is encrypted.*__has_key"):
            await EncryptedRecord.objects.filter(**lookup).first()
    with pytest.raises(FieldError):
        await EncryptedRecord.objects.get_or_create(secret="s3cr3t")


@pytest.mark.asyncio
async def test_null_and_key_lookups_still_work(db_encrypted_fields):
    with_secret = await EncryptedRecord.objects.create(secret="s3cr3t", config={"url": "x"})
    await EncryptedRecord.objects.create(title="empty")
    assert [record.id for record in await EncryptedRecord.objects.filter(secret__isnull=False)] == [with_secret.id]
    assert await EncryptedRecord.objects.filter(secret__not_isnull=True).count() == 1
    assert await EncryptedRecord.objects.filter(config__isnull=True).count() == 1
    assert await EncryptedRecord.objects.exclude(secret__isnull=True).count() == 1


@pytest.mark.asyncio
async def test_json_has_key_lookup(db_encrypted_fields):
    record = await EncryptedRecord.objects.create(config={"url": "x"})
    await EncryptedRecord.objects.create(config={"other": "y"})
    assert [found.id for found in await EncryptedRecord.objects.filter(config__has_key="url")] == [record.id]


@pytest.mark.asyncio
async def test_sensitive_fields(db_encrypted_fields):
    assert EncryptedRecord._meta.sensitive_fields == frozenset({"secret", "config", "api_token", "owner", "owner_id"})
    assert EncryptedOwner._meta.sensitive_fields == frozenset()


def test_sensitive_defaults():
    assert EncryptedTextField().sensitive is True
    assert EncryptedJSONField().sensitive is True
    assert EncryptedTextField(sensitive=False).sensitive is False
    assert CharField(max_length=10).sensitive is False
    assert TextField(sensitive=True).sensitive is True
    with pytest.raises(ConfigurationError, match="sensitive must be a bool"):
        CharField(max_length=10, sensitive="yes")


@pytest.mark.asyncio
async def test_fields_tell_whether_they_are_sensitive(db_encrypted_fields):
    fields_map = EncryptedRecord._meta.fields_map
    assert fields_map["secret"].sensitive is True
    assert fields_map["config"].sensitive is True
    assert fields_map["api_token"].sensitive is True
    assert fields_map["public_note"].sensitive is False
    assert fields_map["title"].sensitive is False
    assert fields_map[next(iter(EncryptedRecord._meta.foreign_key_fields))].sensitive is True


@pytest.mark.asyncio
async def test_sensitive_is_not_part_of_migrations(db_encrypted_fields):
    for field_name in ("secret", "api_token", "owner"):
        _path, _args, kwargs = EncryptedRecord._meta.fields_map[field_name].deconstruct()
        assert "sensitive" not in kwargs
    assert StateSignatures.get_field_signature(CharField(max_length=10)) == StateSignatures.get_field_signature(
        CharField(max_length=10, sensitive=True)
    )
    assert StateSignatures.get_field_signature(EncryptedTextField()) == StateSignatures.get_field_signature(
        EncryptedTextField(sensitive=False)
    )


@pytest.mark.asyncio
async def test_pydantic_exclude_sensitive(db_encrypted_fields):
    full_schema = pydantic_model_creator(EncryptedRecord)
    public_schema = pydantic_model_creator(EncryptedRecord, exclude_sensitive=True)
    assert {"secret", "config", "api_token"} <= set(full_schema.model_fields)
    assert set(public_schema.model_fields) == {"id", "title", "public_note"}
    assert public_schema is not full_schema
    # Cached variants never mix, in either call order.
    assert pydantic_model_creator(EncryptedRecord) is full_schema
    assert pydantic_model_creator(EncryptedRecord, exclude_sensitive=True) is public_schema
    assert pydantic_model_creator(EncryptedOwner, exclude_sensitive=True).model_fields.keys() == (
        pydantic_model_creator(EncryptedOwner).model_fields.keys()
    )


@pytest.mark.asyncio
async def test_pydantic_exclude_sensitive_nested(db_encrypted_fields):
    owner_schema = pydantic_model_creator(EncryptedOwner, exclude_sensitive=True, name="EncryptedOwnerPublic")
    nested_record_schema = owner_schema.model_fields["records"].annotation.__args__[0]
    assert "secret" not in nested_record_schema.model_fields
    assert "title" in nested_record_schema.model_fields
    owner_full_schema = pydantic_model_creator(EncryptedOwner, name="EncryptedOwnerFull")
    assert "secret" in owner_full_schema.model_fields["records"].annotation.__args__[0].model_fields


@pytest.mark.asyncio
async def test_pydantic_from_orm_gives_plaintext(db_encrypted_fields):
    created = await EncryptedRecord.objects.create(secret="s3cr3t", config={"url": "x"})
    schema = pydantic_model_creator(EncryptedRecord, exclude=("owner",))
    dumped = (await schema.from_hare_orm(await EncryptedRecord.objects.get(id=created.id))).model_dump()
    assert dumped["secret"] == "s3cr3t"
    assert dumped["config"] == {"url": "x"}


@pytest.mark.slow
def test_import_without_cryptography_installed():
    script = """
import sys
sys.modules["cryptography"] = None
from hare.fields import EncryptedTextField
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.exceptions import ConfigurationError
field = EncryptedTextField()
for action in (lambda: field.to_db_value("x", None), lambda: FieldEncryption.configure("key")):
    try:
        action()
    except ConfigurationError as exc:
        assert "hare-orm[encryption]" in str(exc), exc
    else:
        raise AssertionError("expected ConfigurationError")
print("ok")
"""
    completed = subprocess.run(  # nosec
        [sys.executable, "-c", script], cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_bare_f_annotation_decrypts(db_encrypted_fields):
    record = await EncryptedRecord.objects.create(
        secret="s3cr3t", config={"url": "https://hook.example", "retries": 3}
    )
    await EncryptedRecordAttachment.objects.create(record=record)
    expected_config = {"url": "https://hook.example", "retries": 3}

    annotated = EncryptedRecord.objects.annotate(copied_secret=F("secret"), copied_config=F("config"))
    assert await annotated.values_list("copied_secret", flat=True) == ["s3cr3t"]
    assert await annotated.values("copied_secret", "copied_config") == [
        {"copied_secret": "s3cr3t", "copied_config": expected_config}
    ]
    instance = await annotated.first()
    assert (instance.copied_secret, instance.copied_config) == ("s3cr3t", expected_config)
    union_instances = await annotated.union(
        EncryptedRecord.objects.annotate(copied_secret=F("secret"), copied_config=F("config")), all=True
    )
    assert [union_instance.copied_secret for union_instance in union_instances] == ["s3cr3t", "s3cr3t"]
    assert await EncryptedRecord.objects.annotate(copied=F("secret")).annotate(again=F("copied")).values_list(
        "again", flat=True
    ) == ["s3cr3t"]

    through_relation = EncryptedRecordAttachment.objects.annotate(
        record_secret=F("record__secret"), record_config=F("record__config")
    )
    assert await through_relation.values("record_secret", "record_config") == [
        {"record_secret": "s3cr3t", "record_config": expected_config}
    ]
    attachment = await through_relation.select_related("record").first()
    assert (attachment.record_secret, attachment.record.secret) == ("s3cr3t", "s3cr3t")
    assert await EncryptedRecord.objects.annotate(
        attachment_record_secret=Subquery(
            EncryptedRecordAttachment.objects.filter(record_id=OuterReference("pk"))
            .annotate(copied=F("record__secret"))
            .values("copied")
        )
    ).values_list("attachment_record_secret", flat=True) == ["s3cr3t"]


@pytest.mark.asyncio
async def test_f_json_key_path_over_encrypted_json_raises(db_encrypted_fields):
    await EncryptedRecord.objects.create(config={"url": "https://hook.example"})
    with pytest.raises(FieldError, match=r"EncryptedRecord\.config is encrypted"):
        await EncryptedRecord.objects.annotate(url=F("config__url")).values_list("url", flat=True)


@pytest.mark.asyncio
async def test_pydantic_root_schema_not_replaced_by_leaf_submodel(db_encrypted_fields):
    first = pydantic_model_creator(EncryptedRecord, exclude_sensitive=True)
    owner_schema = pydantic_model_creator(EncryptedOwner, exclude_sensitive=True)
    nested_record_schema = owner_schema.model_fields["records"].annotation.__args__[0]
    assert nested_record_schema.__name__.endswith(":leaf")
    second = pydantic_model_creator(EncryptedRecord, exclude_sensitive=True)
    assert second is first
    assert pydantic_model_creator(EncryptedOwner, exclude_sensitive=True) is owner_schema
    assert pydantic_queryset_creator(EncryptedRecord, exclude_sensitive=True) is pydantic_queryset_creator(
        EncryptedRecord, exclude_sensitive=True
    )
    assert first.__name__ == ROOT_PUBLIC_RECORD_SCHEMA_NAME
    assert nested_record_schema.__name__ == f"{ROOT_PUBLIC_RECORD_SCHEMA_NAME}:leaf"


async def assert_secret_row_unchanged(record_id: int, secret: str) -> None:
    raw_row = await fetch_raw_record_row()
    assert FieldEncryption.decrypt(raw_row["secret"], "secret") == secret
    assert (await EncryptedRecord.objects.get(id=record_id)).secret == secret


@pytest.mark.asyncio
async def test_update_to_expression_rejected(db_encrypted_fields):
    record = await EncryptedRecord.objects.create(title="a", secret="s3cr3t", config={"url": "x"})
    queryset = EncryptedRecord.objects.filter(id=record.id)
    for update_kwargs in (
        {"secret": F("title")},
        {"secret": Upper("title")},
        {"secret": Case(When(title="a", then=Value("x")), default=Value("y"))},
        {"secret": F("config")},
        {"config": F("title")},
        {"config": F("secret")},
    ):
        with pytest.raises(FieldError, match=r"is encrypted - it can only be updated"):
            await queryset.update(**update_kwargs)
    with pytest.raises(FieldError, match=r"EncryptedRecord\.secret is encrypted.*updating EncryptedRecord\.title"):
        await queryset.update(title=F("secret"))
    await assert_secret_row_unchanged(record.id, "s3cr3t")


@pytest.mark.asyncio
async def test_update_to_value_literal_is_encrypted_like_a_plain_value(db_encrypted_fields):
    record = await EncryptedRecord.objects.create(title="a", secret="s3cr3t")
    await EncryptedRecord.objects.filter(id=record.id).update(secret=Value("replaced"))
    await assert_secret_row_unchanged(record.id, "replaced")


@pytest.mark.asyncio
async def test_update_copies_ciphertext_between_encrypted_fields(db_encrypted_fields):
    record = await EncryptedRecord.objects.create(secret="s3cr3t")
    await EncryptedRecord.objects.filter(id=record.id).update(public_note=F("secret"))
    assert (await EncryptedRecord.objects.get(id=record.id)).public_note == "s3cr3t"
    record.secret = F("public_note")
    record.public_note = "other"
    await record.save()
    fetched = await EncryptedRecord.objects.get(id=record.id)
    assert (fetched.secret, fetched.public_note) == ("s3cr3t", "other")


@pytest.mark.asyncio
async def test_save_and_update_or_create_to_expression_rejected(db_encrypted_fields):
    record = await EncryptedRecord.objects.create(title="a", secret="s3cr3t")
    record.secret = Upper("title")
    with pytest.raises(FieldError, match=r"is encrypted - it can only be updated"):
        await record.save()
    fetched = await EncryptedRecord.objects.get(id=record.id)
    fetched.title = F("secret")
    with pytest.raises(FieldError, match=r"EncryptedRecord\.secret is encrypted"):
        await fetched.save(update_fields=["title"])
    with pytest.raises(FieldError, match=r"is encrypted - it can only be updated"):
        await EncryptedRecord.objects.update_or_create(defaults={"secret": Value("leak")}, id=record.id)
    await assert_secret_row_unchanged(record.id, "s3cr3t")


@pytest.mark.asyncio
async def test_encrypted_field_inside_expression_rejected(db_encrypted_fields):
    await EncryptedRecord.objects.create(title="a", secret="s3cr3t", config={"url": "x"})
    for annotation in (
        Coalesce("secret", Value("n/a")),
        Coalesce("secret", "plain"),
        Coalesce("title", F("secret")),
        Upper("secret"),
        Max("secret"),
        Count("secret", distinct=True),
        Coalesce("config", Value("{}")),
        Case(When(title="a", then=F("secret")), default=Value("n/a")),
        Case(When(title="a", then=Value("n/a")), default=F("secret")),
        F("secret") + Value("x"),
        Window(WindowSum("secret")),
        Window(Lag("secret", default="n/a"), order_by=["id"]),
        Window(RowNumber(), order_by=["secret"]),
        Window(RowNumber(), partition_by=["secret"]),
    ):
        with pytest.raises(FieldError, match=r"EncryptedRecord\.(secret|config) is encrypted with non-deterministic"):
            await EncryptedRecord.objects.annotate(value=annotation).values("value")
    with pytest.raises(FieldError, match="non-deterministic"):
        await EncryptedRecord.objects.all().aggregate(value=Max("secret"))


@pytest.mark.asyncio
async def test_encrypted_field_expressions_that_still_work(db_encrypted_fields):
    first = await EncryptedRecord.objects.create(title="a", secret="one")
    await EncryptedRecord.objects.create(title="b")
    assert await EncryptedRecord.objects.all().aggregate(total=Count("secret")) == {"total": 1}
    rows = (
        await EncryptedRecord.objects.annotate(
            copied=F("secret"),
            first_secret=Window(FirstValue("secret"), order_by=["id"]),
            previous_secret=Window(Lag("secret"), order_by=["id"]),
        )
        .order_by("id")
        .values("copied", "first_secret", "previous_secret")
    )
    assert rows == [
        {"copied": "one", "first_secret": "one", "previous_secret": None},
        {"copied": None, "first_secret": "one", "previous_secret": "one"},
    ]
    secret_counts = (
        await EncryptedRecord.objects.filter(id=first.id)
        .annotate(secret_count=Count("secret"))
        .values_list("secret_count", flat=True)
    )
    assert secret_counts == [1]


@pytest.mark.asyncio
async def test_ordering_grouping_distinct_by_encrypted_field_rejected(db_encrypted_fields):
    owner = await EncryptedOwner.objects.create(name="owner")
    for __ in range(2):
        await EncryptedRecord.objects.create(title="a", secret="same", config={"url": "x"}, owner=owner)
    rejected_queries = (
        (EncryptedRecord.objects.all().order_by("secret"), "ORDER BY"),
        (EncryptedRecord.objects.all().order_by("-config"), "ORDER BY"),
        (EncryptedRecord.objects.all().order_by(F("secret").desc()), "ORDER BY"),
        (EncryptedRecord.objects.annotate(copied=F("secret")).order_by("copied"), "ORDER BY"),
        (EncryptedOwner.objects.all().order_by("records__secret"), "ORDER BY"),
        (EncryptedRecord.objects.all().order_by("secret").values("title"), "ORDER BY"),
        (EncryptedRecord.objects.all().distinct().values("secret"), "DISTINCT"),
        (EncryptedRecord.objects.all().distinct().values("title", "secret"), "DISTINCT"),
        (EncryptedRecord.objects.all().distinct().values_list("secret", flat=True), "DISTINCT"),
        (EncryptedOwner.objects.all().distinct().values("records__secret"), "DISTINCT"),
        (EncryptedRecord.objects.annotate(total=Count("id")).group_by("secret").values("secret", "total"), "GROUP BY"),
        (
            EncryptedRecord.objects.annotate(total=Count("id")).group_by("config").values_list("total", flat=True),
            "GROUP BY",
        ),
    )
    for queryset, usage in rejected_queries:
        with pytest.raises(FieldError, match=rf"is encrypted with non-deterministic.*{usage} can't use it"):
            await queryset
    # Instances are grouped by their primary key only - count() emits no GROUP BY over the field.
    assert await EncryptedRecord.objects.all().group_by("secret").count() == 2


@pytest.mark.asyncio
async def test_distinct_on_encrypted_field_rejected(db_encrypted_fields):
    if not Connections.get("models").dialect.features.supports_distinct_on:
        pytest.skip("The database has no DISTINCT ON")
    await EncryptedRecord.objects.create(secret="same")
    with pytest.raises(FieldError, match=r"distinct\(\*fields\) can't use it"):
        await EncryptedRecord.objects.all().distinct("secret")


@pytest.mark.asyncio
async def test_distinct_that_still_works_with_encrypted_fields(db_encrypted_fields):
    for __ in range(2):
        await EncryptedRecord.objects.create(title="a", secret="same")
    assert len(await EncryptedRecord.objects.all().distinct()) == 2
    assert len(await EncryptedRecord.objects.all().distinct().values("id", "secret")) == 2
    assert len(await EncryptedRecord.objects.all().distinct().values_list("pk", "secret")) == 2
    assert await EncryptedRecord.objects.all().distinct().values("title") == [{"title": "a"}]
    assert await EncryptedRecord.objects.all().distinct().count() == 2


@pytest.mark.asyncio
async def test_generated_field_inherits_sensitive(db_encrypted_fields):
    assert SensitiveGeneratedRecord._meta.sensitive_fields == frozenset({"api_token", "api_token_upper"})
    assert GeneratedField(RawSQLTerm("1"), output_field=CharField(max_length=10, sensitive=True)).sensitive is True
    assert GeneratedField(RawSQLTerm("1"), output_field=IntField()).sensitive is False
    assert GeneratedField(RawSQLTerm("1"), output_field=IntField(), sensitive=True).sensitive is True
    public_schema = pydantic_model_creator(SensitiveGeneratedRecord, exclude_sensitive=True)
    assert set(public_schema.model_fields) == {"id", "api_token_length"}
    created = await SensitiveGeneratedRecord.objects.create(api_token="abc")
    await created.refresh_from_db()
    assert (created.api_token_upper, created.api_token_length) == ("ABC", 3)
    _path, _args, kwargs = SensitiveGeneratedRecord._meta.fields_map["api_token_upper"].deconstruct()
    assert "sensitive" not in kwargs


def test_encrypted_fields_reject_db_default():
    for field_class, db_default in ((EncryptedTextField, "x"), (EncryptedJSONField, {"url": "x"})):
        with pytest.raises(ConfigurationError, match=r"can't take db_default=.*default="):
            field_class(db_default=db_default)
    assert EncryptedTextField(default="x").default == "x"
    assert EncryptedJSONField(default=dict).default is dict


@pytest.mark.asyncio
async def test_json_values_serialized_to_strings_are_encrypted(db_encrypted_fields):
    request_id = uuid.UUID(int=7)
    created = await EncryptedRecord.objects.create(
        config={
            "expires": datetime.datetime(2030, 1, 1, 12, 0),
            "day": datetime.date(2030, 1, 2),
            "request_id": request_id,
            "retries": 3,
        }
    )
    raw_row = await fetch_raw_record_row()
    raw_config = raw_row["config"] if isinstance(raw_row["config"], dict) else json.loads(raw_row["config"])
    assert FieldEncryption.decrypt(raw_config["retries"], "config") == "3"
    for key, plaintext in (("expires", "2030-01-01T12:00:00"), ("day", "2030-01-02"), ("request_id", str(request_id))):
        assert plaintext not in raw_config[key]
        assert FieldEncryption.decrypt(raw_config[key], "config") == f'"{plaintext}"'
    fetched = await EncryptedRecord.objects.get(id=created.id)
    assert fetched.config == {
        "expires": "2030-01-01T12:00:00",
        "day": "2030-01-02",
        "request_id": str(request_id),
        "retries": 3,
    }
    assert [record.id for record in await EncryptedRecord.objects.all()] == [created.id]


@pytest.mark.asyncio
async def test_json_not_serializable_error_hides_value(db_encrypted_fields):
    with pytest.raises(ValidationError, match="not JSON serializable") as error_info:
        await EncryptedRecord.objects.create(config={"token": "LEAKME", "other": object()})
    assert "LEAKME" not in str(error_info.value)


@pytest.mark.asyncio
async def test_json_field_type_round_trip(db_encrypted_fields):
    from_dict = await EncryptedTypedRecord.objects.create(settings={"url": "https://secret.example"})
    from_model = await EncryptedTypedRecord.objects.create(
        settings=EncryptedWebhookSettings(url="https://other.example")
    )
    assert from_dict.settings == EncryptedWebhookSettings(url="https://secret.example")
    fetched = await EncryptedTypedRecord.objects.get(id=from_model.id)
    assert fetched.settings == EncryptedWebhookSettings(url="https://other.example", retries=3)
    await EncryptedTypedRecord.objects.filter(id=from_dict.id).update(
        settings={"url": "https://updated.example", "retries": 5}
    )
    assert await EncryptedTypedRecord.objects.filter(id=from_dict.id).values_list("settings", flat=True) == [
        EncryptedWebhookSettings(url="https://updated.example", retries=5)
    ]
    rows = await Connections.get("models").execute_dicts("SELECT settings FROM encrypted_typed_record")
    for row in rows:
        raw_settings = row["settings"] if isinstance(row["settings"], dict) else json.loads(row["settings"])
        assert "example" not in raw_settings["url"]
    with pytest.raises(ValidationError):
        await EncryptedTypedRecord.objects.create(settings={"retries": 1})


@pytest.mark.asyncio
async def test_validation_errors_hide_sensitive_values(db_encrypted_fields):
    with pytest.raises(ValidationError, match=r"api_key: Length of <hidden> 19 < 32") as error_info:
        await EncryptedTypedRecord.objects.create(api_key="sk_live_SHORTSECRET")
    assert "SHORTSECRET" not in str(error_info.value)
    with pytest.raises(ValidationError, match=r"pin: Value <hidden> does not match regex") as error_info:
        await EncryptedTypedRecord.objects.create(api_key="x" * 40, pin="12345-SECRETPIN")
    assert "SECRETPIN" not in str(error_info.value)
    with pytest.raises(ValidationError) as error_info:
        await EncryptedRecord.objects.create(api_token="TOKEN-" + "z" * 200)
    assert "TOKEN-" not in str(error_info.value)
    assert "<hidden>" in str(error_info.value)


@pytest.mark.asyncio
async def test_comparing_encrypted_field_with_expression_raises(db_encrypted_fields):
    owner = await EncryptedOwner.objects.create(name="S")
    record = await EncryptedRecord.objects.create(title="S", secret="S", owner=owner)
    await EncryptedRecordAttachment.objects.create(record=record)
    # A comparison with an encrypted field fails when the filter is built.
    for build_queryset in (
        lambda: EncryptedRecord.objects.filter(secret=F("title")),
        lambda: EncryptedRecord.objects.filter(title=F("secret")),
        lambda: EncryptedRecord.objects.filter(secret__gt=F("title")),
        lambda: EncryptedRecord.objects.filter(public_note=F("secret")),
        lambda: EncryptedRecord.objects.filter(config=F("title")),
        lambda: EncryptedOwner.objects.filter(name=F("records__secret")),
        lambda: EncryptedOwner.objects.filter(records__secret=F("name")),
        lambda: EncryptedRecordAttachment.objects.filter(record__secret=F("record__title")),
        lambda: EncryptedRecord.objects.filter(
            secret=Subquery(EncryptedRecord.objects.all().limit(1).values("secret"))
        ),
        lambda: EncryptedRecord.objects.filter(secret__in=Subquery(EncryptedRecord.objects.all().values("title"))),
        lambda: EncryptedRecord.objects.filter(title__in=Subquery(EncryptedRecord.objects.all().values("secret"))),
        lambda: EncryptedRecord.objects.filter(
            title__in=EncryptedRecord.objects.all().values_list("secret", flat=True)
        ),
    ):
        with pytest.raises(FieldError, match=r"EncryptedRecord\.(secret|config|public_note) is encrypted"):
            await build_queryset()
    assert await EncryptedRecord.objects.filter(title=F("title"), secret__isnull=False).count() == 1
