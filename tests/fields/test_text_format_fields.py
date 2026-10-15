"""EmailField, URLField, SlugField and PhoneField: what they accept and write on every write path,
filter values converted the same way, their declaration checks, their migrations and their
pydantic schema."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from hare import Connections, fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.text.phone_field import PhoneField
from hare.fields.field import Field
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.migration import Migration
from hare.migrations.operations import AlterField, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from tests.fields.models_text_formats import Contact
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.fixture
def without_phonenumbers(monkeypatch):
    monkeypatch.setattr(PhoneField, "phonenumbers", None)
    monkeypatch.setattr(PhoneField, "phonenumbers_looked_up", True)


def write(field: Field[Any], value: Any) -> Any:
    field.model_field_name = field.model_field_name or "value"
    return field.to_db_value(value, Contact)


# ============================================================================
# Declaration and conversion of a value, without a database
# ============================================================================


@pytest.mark.parametrize(
    ("lowercase", "value", "written"),
    [
        (False, "Ann.Lee@Example.COM", "Ann.Lee@example.com"),
        (False, "ann@example.com", "ann@example.com"),
        (True, "Ann.Lee@Example.COM", "ann.lee@example.com"),
        (False, '"quoted.name"@Example.org', '"quoted.name"@example.org'),
        (False, "user@[192.168.0.1]", "user@[192.168.0.1]"),
    ],
)
def test_email_is_written_with_its_domain_lowercase(lowercase, value, written):
    assert write(fields.EmailField(lowercase=lowercase), value) == written


@pytest.mark.parametrize("value", ["", "ann", "ann@", "@example.com", "a..b@example.com", "ann@example", "a@b@c.com"])
def test_an_invalid_email_is_refused(value):
    with pytest.raises(ValidationError, match="^value: Invalid email address$"):
        write(fields.EmailField(), value)


def test_email_defaults_and_declaration_checks():
    field = fields.EmailField()
    assert field.max_length == 254
    assert field.SQL_TYPE == "VARCHAR(254)"
    with pytest.raises(ValidationError, match="Length of"):
        write(fields.EmailField(max_length=12), "ann@example.com")
    with pytest.raises(ConfigurationError, match="lowercase must be a bool"):
        fields.EmailField(lowercase="yes")  # type: ignore[call-overload]


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "HTTPS://Example.com:8443/a/b?c=d#e",
        "http://localhost:8000/",
        "https://[::1]/",
        "http://10.0.0.1",
    ],
)
def test_a_url_is_written_as_it_is(value):
    assert write(fields.URLField(), value) == value


def test_url_schemes():
    with pytest.raises(ValidationError, match="Invalid scheme: ftp is not allowed"):
        write(fields.URLField(), "ftp://example.com")
    assert write(fields.URLField(schemes=["ftp"]), "FTP://example.com") == "FTP://example.com"
    with pytest.raises(ValidationError, match="Invalid URL"):
        write(fields.URLField(), "http://exa mple.com")
    assert fields.URLField().max_length == 2048


@pytest.mark.parametrize("schemes", ["http", [], ["HTTP"], ["1http"], [""], [5], None])
def test_url_schemes_are_checked_on_declaration(schemes):
    with pytest.raises(ConfigurationError, match="URLField: "):
        fields.URLField(schemes=schemes)


def test_slug():
    assert write(fields.SlugField(), "my-first_post2") == "my-first_post2"
    with pytest.raises(ValidationError, match="Invalid slug"):
        write(fields.SlugField(), "привет-мир")
    assert write(fields.SlugField(allow_unicode=True), "привет-мир") == "привет-мир"
    with pytest.raises(ValidationError, match="Invalid slug"):
        write(fields.SlugField(allow_unicode=True), "привет мир")
    assert fields.SlugField().max_length == 50
    with pytest.raises(ConfigurationError, match="allow_unicode must be a bool"):
        fields.SlugField(allow_unicode=1)  # type: ignore[call-overload]


def test_slug_is_indexed_by_default_but_not_as_a_migration_replays_it():
    assert fields.SlugField().index is True
    assert fields.SlugField(db_index=False).index is False
    assert fields.SlugField().deconstruct()[2] == {"db_index": True}
    assert "db_index" not in fields.SlugField(db_index=False).deconstruct()[2]
    with Field.replaying_migration_scope():
        assert fields.SlugField().index is False
        assert fields.SlugField(db_index=True).index is True


@pytest.mark.parametrize(
    ("region", "value", "written"),
    [
        (None, "+16502530000", "+16502530000"),
        (None, "+1 (650) 253-0000", "+16502530000"),
        (None, "+44 20 7946 0958", "+442079460958"),
        ("US", "(650) 253-0000", "+16502530000"),
        ("US", "+44 20 7946 0958", "+442079460958"),
        ("GB", "020 7946 0958", "+442079460958"),
    ],
)
def test_phone_with_phonenumbers_is_written_in_e164_form(region, value, written):
    assert write(fields.PhoneField(region=region), value) == written


@pytest.mark.parametrize(
    ("region", "value"), [(None, "(650) 253-0000"), ("US", "+1 000"), ("US", "phone"), (None, "")]
)
def test_a_number_phonenumbers_refuses_is_refused(region, value):
    with pytest.raises(ValidationError, match="^value: Invalid phone number$"):
        write(fields.PhoneField(region=region), value)


@pytest.mark.parametrize("region", ["us", "XX", 5, ""])
def test_phone_region_is_checked_on_declaration(region):
    with pytest.raises(ConfigurationError, match="region must be an ISO 3166-1 region code"):
        fields.PhoneField(region=region)


@pytest.mark.usefixtures("without_phonenumbers")
def test_phone_without_phonenumbers_accepts_only_e164():
    field = fields.PhoneField()
    assert write(field, "+16502530000") == "+16502530000"
    with pytest.raises(ValidationError, match="^value: Length of"):
        write(field, "+1234567890123456")
    for value in ("+1 650 253 0000", "16502530000", "+0123", "+123456789012a"):
        with pytest.raises(ValidationError, match="^value: Invalid phone number$"):
            write(field, value)
    with pytest.raises(ConfigurationError, match="region= needs the 'phonenumbers' package"):
        fields.PhoneField(region="US")
    assert field.constraints == {"max_length": 16, "pattern": r"^\+[1-9][0-9]{1,14}$"}


def test_deconstruct_writes_the_arguments_given():
    assert fields.EmailField().deconstruct()[2] == {}
    assert fields.EmailField(lowercase=True, max_length=100).deconstruct()[2] == {"max_length": 100, "lowercase": True}
    assert fields.URLField().deconstruct()[2] == {}
    assert fields.URLField(schemes=["ftp"]).deconstruct()[2] == {"schemes": ("ftp",)}
    assert fields.SlugField(allow_unicode=True, db_index=False).deconstruct()[2] == {"allow_unicode": True}
    assert fields.PhoneField(region="US", null=True).deconstruct()[2] == {"region": "US", "null": True}


def test_constraints():
    assert fields.EmailField().constraints == {"max_length": 254, "format": "email"}
    assert fields.URLField().constraints == {"max_length": 2048, "format": "uri"}
    assert fields.SlugField().constraints == {"max_length": 50, "pattern": "^[-a-zA-Z0-9_]+$"}
    assert fields.SlugField(allow_unicode=True).constraints == {"max_length": 50, "pattern": r"^[-\w]+$"}
    assert fields.PhoneField().constraints == {"max_length": 16}


# ============================================================================
# Every write path and filter on the database
# ============================================================================


@pytest.mark.asyncio
async def test_every_write_path_normalizes(db_text_formats):
    contact = await Contact.objects.create(
        id=1, email="Ann@Example.COM", login_email="Ann@Example.COM", us_phone="(650) 253-0000", slug="ann"
    )
    await contact.refresh_from_db()
    assert (contact.email, contact.login_email, contact.us_phone) == (
        "Ann@example.com",
        "ann@example.com",
        "+16502530000",
    )

    await Contact.objects.bulk_create(
        [
            Contact(id=2, email="Bob@Example.COM", phone="+44 20 7946 0958"),
            Contact(id=3, email="Cid@EXAMPLE.org", website="https://Example.org/x"),
        ]
    )
    contact.email = "Ann@NEW.example"
    contact.us_phone = "650-253-0001"
    await contact.save()
    await Contact.objects.filter(id=2).update(login_email="BOB@Example.COM")
    others = await Contact.objects.filter(id__in=[2, 3]).order_by("id")
    for other in others:
        other.email = other.email.upper()
    await Contact.objects.bulk_update(others, fields=["email"])
    rows = (
        await Contact.objects.all()
        .order_by("id")
        .values_list("id", "email", "login_email", "phone", "us_phone", "website")
    )
    assert rows == [
        (1, "Ann@new.example", "ann@example.com", None, "+16502530001", None),
        (2, "BOB@example.com", "bob@example.com", "+442079460958", None, None),
        (3, "CID@example.org", None, None, None, "https://Example.org/x"),
    ]


@pytest.mark.asyncio
async def test_filter_values_are_converted_like_written_ones(db_text_formats):
    await Contact.objects.create(id=1, email="ann@example.com", us_phone="+16502530000", phone="+442079460958")
    await Contact.objects.create(id=2, email="bob@example.com")
    assert await Contact.objects.filter(email="ann@EXAMPLE.COM").values_list("id", flat=True) == [1]
    assert await Contact.objects.filter(email__in=["ann@Example.com", "BOB@example.com"]).count() == 1
    assert await Contact.objects.filter(email__in=["ann@Example.com", "bob@EXAMPLE.com"]).count() == 2
    assert await Contact.objects.filter(us_phone="(650) 253-0000").values_list("id", flat=True) == [1]
    assert await Contact.objects.filter(phone__in=["+44 20 7946 0958"]).values_list("id", flat=True) == [1]
    assert await Contact.objects.filter(email__icontains="EXAMPLE").count() == 2
    assert await Contact.objects.filter(email__not="ann@EXAMPLE.com").values_list("id", flat=True) == [2]
    with pytest.raises(ValidationError, match="Invalid email address"):
        await Contact.objects.filter(email="not-an-email").count()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"email": "not-an-email"}, "email: Invalid email address"),
        ({"email": "a@example.com", "website": "ftp://example.com"}, "website: Invalid scheme: ftp is not allowed"),
        ({"email": "a@example.com", "mirror": "http://example.com"}, "mirror: Invalid scheme: http is not allowed"),
        ({"email": "a@example.com", "slug": "a b"}, "slug: Invalid slug"),
        ({"email": "a@example.com", "short_slug": "ab"}, "short_slug: Length of 'ab' 2 < 3"),
        ({"email": "a@example.com", "phone": "650 253 0000"}, "phone: Invalid phone number"),
    ],
)
async def test_an_invalid_value_is_refused_on_every_write_path(db_text_formats, values, message):
    with pytest.raises(ValidationError, match=message):
        await Contact.objects.create(id=10, **values)
    with pytest.raises(ValidationError, match=message):
        await Contact.objects.bulk_create([Contact(id=11, **values)])
    await Contact.objects.create(id=12, email="ok@example.com")
    with pytest.raises(ValidationError, match=message):
        await Contact.objects.filter(id=12).update(**values)
    assert await Contact.objects.filter(id__in=[10, 11]).count() == 0


@pytest.mark.asyncio
async def test_bulk_create_with_copy_normalizes_and_checks(db_text_formats):
    if not DatabaseUnderTest.get_dialect().features.supports_copy:
        pytest.skip("The database has no COPY bulk-load protocol")
    await Contact.objects.bulk_create(
        [Contact(id=1, email="Ann@EXAMPLE.com", us_phone="(650) 253-0000", slug="a-b")], use_copy=True
    )
    assert await Contact.objects.values_list("email", "us_phone").get(id=1) == ("Ann@example.com", "+16502530000")
    with pytest.raises(ValidationError, match="email: Invalid email address"):
        await Contact.objects.bulk_create([Contact(id=2, email="ann")], use_copy=True)
    with pytest.raises(ValidationError, match="slug: Invalid slug"):
        await Contact.objects.bulk_create([Contact(id=3, email="a@b.com", slug="a b")], use_copy=True)


@pytest.mark.asyncio
async def test_slug_columns_are_indexed(db_text_formats):
    connection = Connections.get("models")
    sql = connection.dialect.schema_editor_class(connection).table_creation.get_create_schema_sql()
    slug_index_lines = [line for line in sql.splitlines() if "INDEX" in line and "text_format_contact" in line]
    quote_identifier = connection.dialect.literals.quote_identifier
    indexed_columns = {
        name
        for name in ("slug", "unicode_slug", "short_slug")
        if any(f"({quote_identifier(name)})" in line for line in slug_index_lines)
    }
    assert indexed_columns == {"slug", "short_slug"}, sql


@pytest.mark.asyncio
async def test_pydantic_schema(db_text_formats):
    schema = pydantic_model_creator(Contact).model_json_schema()["properties"]
    assert schema["email"]["format"] == "email"
    assert schema["website"].get("format") == "uri", schema["website"]
    assert "^[-a-zA-Z0-9_]+$" in str(schema["slug"])
    assert "maxLength" in str(schema["phone"])


# ============================================================================
# Migrations
# ============================================================================


def make_model(name: str, app_label: str, **model_fields: Field[Any]) -> type[Model]:
    meta = type("Meta", (), {"app": app_label, "table": name.lower()})
    return type(name, (Model,), {**model_fields, "Meta": meta})


@pytest.mark.asyncio
async def test_autodetector_alters_a_char_field_into_a_format_field(tmp_path: Path, monkeypatch):
    app_label = "text_format_app"
    migrations_dir = tmp_path / app_label / "migrations"
    migrations_dir.mkdir(parents=True)
    (tmp_path / app_label / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import fields, migrations",
                "from hare.migrations import operations as ops",
                "",
                "class Migration(migrations.Migration):",
                "    initial = True",
                "    operations = [",
                "        ops.CreateModel(",
                "            name='Profile',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "                ('email', fields.CharField(max_length=254)),",
                "                ('slug', fields.CharField(max_length=50)),",
                "                ('phone', fields.CharField(max_length=16, null=True)),",
                "            ],",
                "            options={'table': 'profile'},",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    apps = StateApps()
    profile = make_model(
        "Profile",
        app_label,
        id=fields.IntField(primary_key=True),
        email=fields.EmailField(),
        slug=fields.SlugField(),
        phone=fields.PhoneField(null=True, region="US"),
    )
    apps.register_model(app_label, profile)
    autodetector = MigrationAutodetector(
        apps,
        {app_label: {"models": [], "default_connection": "default", "migrations": f"{app_label}.migrations"}},
        now=lambda: dt.datetime(2024, 1, 1, 12, 0),
    )
    changes = await autodetector.changes()
    assert len(changes) == 1
    altered = {operation.name: operation.field for operation in changes[0].operations}
    assert set(altered) == {"email", "slug", "phone"}
    assert isinstance(altered["email"], fields.EmailField)
    source = changes[0].as_string()
    assert "fields.EmailField()" in source
    assert "fields.SlugField(db_index=True)" in source
    assert (
        "fields.PhoneField(region='US', null=True)" in source or 'fields.PhoneField(region="US", null=True)' in source
    )


@pytest.mark.asyncio
async def test_alter_field_into_a_format_field_on_the_database(db_text_formats):
    connection = Connections.get("models")
    literals = connection.dialect.literals
    table_name = "text_format_alter"
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    create_model = CreateModel(
        name="AlterProfile",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("email", fields.CharField(max_length=254)),
            ("slug", fields.CharField(max_length=50)),
        ],
        options={"table": table_name},
    )
    try:
        await create_model.run("models", state, dry_run=False, state_editor=editor)
        await connection.execute(
            f"INSERT INTO {literals.quote_identifier(table_name)} (id, email, slug) VALUES (1, 'a@example.com', 'a-b')"
        )
        migration = Migration(name="0002_formats", app_label="models")
        migration.operations = [
            AlterField(model_name="AlterProfile", name="email", field=fields.EmailField()),
            AlterField(model_name="AlterProfile", name="slug", field=fields.SlugField()),
        ]
        previous_state = state.clone()
        state = await migration.apply(state, dry_run=False, schema_editor=editor)
        altered_model = state.apps.get_model("models", "AlterProfile")
        assert isinstance(altered_model._meta.fields_map["email"], fields.EmailField)
        rows = await connection.execute_dicts(f"SELECT id, email, slug FROM {literals.quote_identifier(table_name)}")
        assert rows == [{"id": 1, "email": "a@example.com", "slug": "a-b"}]
        await migration.unapply(previous_state, dry_run=False, schema_editor=editor)
    finally:
        await connection.execute_script(f"DROP TABLE IF EXISTS {literals.quote_identifier(table_name)}")
