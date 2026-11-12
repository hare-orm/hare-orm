"""Required tenant context applies to explicit clients and connections remembered by instances."""

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.exceptions import ConfigurationError, QueryError
from hare.migrations import Migration, MigrationRunner, State
from hare.models.enums import ModelOption
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from hare.query.expressions import Subquery
from hare.query.functions import Count
from tests.contracts.tenant_connection_models import SchemaEdition, SchemaNote
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.tenant_connection_assertions import TenantConnectionAssertions


@pytest_asyncio.fixture(params=(False, True), ids=("missing-template", "missing-scope"))
async def tenant_connection(request):
    url = DatabaseUnderTest.get_url()
    if request.param:
        if DatabaseUnderTest.get_dialect().name != "postgresql":
            pytest.skip("schema-per-tenant connections require PostgreSQL")
        url += ("&" if "?" in url else "?") + "tenant_schema_template=tenant_%7Btenant%7D"
    async with hare_test_context(
        ["tests.contracts.tenant_connection_models"], db_url=url, _generate_schemas=False
    ) as context:
        connection = context.get_connection()
        before = State(default_connections={"models": connection.connection_alias})
        public = State.from_models(
            [SchemaNote, SchemaEdition], default_connections={"models": connection.connection_alias}
        )
        # An ordinary table of the same name must never be mistaken for a tenant's table.
        # Historical declarations create the public fixture without changing the live models.
        for model_state in public.models.values():
            model_state.options.pop(ModelOption.TENANT_SCHEMA, None)
        after = await MigrationRunner(connection).apply(
            Migration("shared_tables", "models", operations=before.get_operations(public, "models").operations),
            before,
        )
        shared_note = after.apps.get_model("models.SchemaNote")
        shared_edition = after.apps.get_model("models.SchemaEdition")
        note = await shared_note.objects.create(id=1, text="shared")
        edition = await shared_edition.objects.create(number=1, revision=2, text="shared", note_id=1)
        await note.selected_editions.add(edition)
        yield connection, QueryError if request.param else ConfigurationError, request.param, shared_note


@pytest.fixture(params=(SchemaNote, SchemaEdition))
def tenant_model(request):
    return request.param


@pytest.mark.asyncio
async def test_reads_require_a_tenant_even_with_an_explicit_client(tenant_connection, tenant_model):
    connection, error, _, _ = tenant_connection
    for using in (None, connection.connection_alias, connection):
        query = tenant_model.objects.using(using)
        for operation in (
            lambda: query.all(),
            lambda: query.values("text"),
            lambda: query.values_list("text", flat=True),
            lambda: query.count(),
            lambda: query.exists(),
            lambda: query.first(),
            lambda: query.aggregate(total=Count("text")),
            lambda: TenantConnectionAssertions.iterate(query),
        ):
            await TenantConnectionAssertions.refused(operation, error)


@pytest.mark.asyncio
async def test_instance_writes_check_explicit_and_remembered_clients(tenant_connection, tenant_model):
    connection, error, _, _ = tenant_connection
    values = {"id": 1} if tenant_model is SchemaNote else {"number": 1, "revision": 2, "note_id": 1}
    saved = tenant_model.construct(
        _saved_in_db=True, _connection_alias=connection.connection_alias, text="shared", **values
    )
    for using in (None, connection.connection_alias, connection):
        for operation in (
            lambda: saved.save(using=using, update_fields=["text"]),
            lambda: saved.refresh_from_db(using=using),
            lambda: saved.delete_preview(using=using),
            lambda: saved.delete(using=using),
            lambda: saved.hard_delete(using=using),
        ):
            await TenantConnectionAssertions.refused(operation, error)


@pytest.mark.asyncio
async def test_queryset_writes_check_the_explicit_client(tenant_connection, tenant_model):
    connection, error, _, _ = tenant_connection
    values = {"id": 2} if tenant_model is SchemaNote else {"number": 1, "revision": 3, "note_id": 1}
    for using in (None, connection.connection_alias, connection):
        query = tenant_model.objects.using(using)
        for operation in (
            lambda: query.create(text="new", **values),
            lambda: query.bulk_create([tenant_model(text="new", **values)]),
            lambda: query.update(text="changed"),
            lambda: query.delete(),
            lambda: query.hard_delete(),
        ):
            await TenantConnectionAssertions.refused(operation, error)


@pytest.mark.asyncio
async def test_relation_writes_require_the_declaring_models_tenant(tenant_connection):
    connection, error, _, _ = tenant_connection
    note = SchemaNote.construct(_saved_in_db=True, _connection_alias=connection.connection_alias, id=1, text="shared")
    edition = SchemaEdition.construct(
        _saved_in_db=True,
        _connection_alias=connection.connection_alias,
        number=1,
        revision=2,
        text="shared",
        note_id=1,
    )
    for using in (None, connection.connection_alias, connection):
        for instance, relation_name, obj in ((note, "selected_editions", edition), (edition, "readers", note)):
            for operation in (
                lambda: getattr(instance, relation_name).add(obj, using=using),
                lambda: getattr(instance, relation_name).remove(obj, using=using),
                lambda: getattr(instance, relation_name).clear(using=using),
                lambda: getattr(instance, relation_name).set([obj], using=using),
            ):
                await TenantConnectionAssertions.refused(operation, error)
        await TenantConnectionAssertions.refused(lambda: note.editions.add(edition, using=using), error)


@pytest.mark.asyncio
async def test_a_selected_tenant_client_keeps_working(tenant_connection):
    connection, _, has_template, _ = tenant_connection
    if not has_template:
        pytest.skip("this control needs a configured tenant schema")
    await TenantSchemas.create("alpha", connection.connection_alias)
    with Tenancy.scope("alpha"):
        client = SchemaNote.get_connection()
        note = await SchemaNote.objects.using(client).create(id=1, text="tenant")
        edition = await SchemaEdition.objects.using(client).create(number=1, revision=2, text="tenant", note=note)
        await note.selected_editions.add(edition, using=client)
        assert await note.selected_editions.values_list("text", flat=True) == ["tenant"]
        note.text = "updated"
        await note.save(using=client)
        await note.refresh_from_db(using=client)
        assert note.text == "updated"
    # The explicitly selected client still names one tenant outside its ambient scope.
    assert await SchemaNote.objects.using(client).values_list("text", flat=True) == ["updated"]
    note.text = "pinned"
    await note.save(using=client)
    await note.refresh_from_db(using=client)
    assert note.text == "pinned"
    await note.selected_editions.remove(edition, using=client)
    await note.selected_editions.add(edition, using=client)
    await note.selected_editions.set([], using=client)
    assert await note.selected_editions.using(client).count() == 0
    await note.selected_editions.set([edition], using=client)
    assert await note.selected_editions.using(client).values_list("text", flat=True) == ["tenant"]


@pytest.mark.asyncio
async def test_embedded_tenant_queries_check_the_outer_client(tenant_connection, tenant_model):
    connection, error, _, shared_note = tenant_connection
    for using in (None, connection.connection_alias, connection):
        outer = shared_note.objects.using(using)
        source = tenant_model.objects.values("text")
        for operation in (
            lambda: outer.filter(text__in=source),
            lambda: outer.filter(text__in=source).count(),
            lambda: outer.annotate(tenant_text=Subquery(source[:1])),
            lambda: outer.values("text").union(source.using(connection)),
        ):
            await TenantConnectionAssertions.refused(operation, error)
