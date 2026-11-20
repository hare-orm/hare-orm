from __future__ import annotations

import pickle
from contextlib import nullcontext
from enum import IntEnum
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Hare, Model, fields
from hare.contrib.test import assert_query_count
from hare.core.hare_context import HareContext
from hare.ddl.indexes.index import Index
from hare.ddl.schema_objects.view import View
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.exceptions import ConfigurationError, ValidationError
from hare.instrumentation.capture.change_sink import ChangeSink
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.observers.observers import Observers
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.migrations.state.state import State
from hare.models.enums import ModelOption
from hare.query.execution.query_execution import QueryExecution
from hare.query.query_connection import QueryConnection
from hare.query.queryset.model_query import ModelQuery
from hare.query.queryset.queryset import QuerySet
from hare.transactions.transactions import Transactions
from tests.model_setup.binding.projection_models import ProjectionRecord
from tests.utils.fake_client import FakeClient
from tests.utils.statement_plan_checks import StatementPlanChecks

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


class BindingSetup:
    @staticmethod
    def config(*models, app="library"):
        module = ModuleType("binding_contract_models")
        module.__models__ = models
        return {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {app: {"models": [module]}},
        }


@pytest_asyncio.fixture(
    params=[(False, False), (False, True), (True, False), (True, True)],
    ids=["memory-scalar", "memory-composite", "file-scalar", "file-composite"],
)
async def independent_context_clients(request, tmp_path):
    file_database, composite = request.param

    class ContextRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        title = fields.TextField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    first_config = BindingSetup.config(ContextRecord)
    second_config = BindingSetup.config(ContextRecord)
    first_url = f"sqlite+aiosqlite://{tmp_path / 'first.sqlite3'}" if file_database else "sqlite+aiosqlite://:memory:"
    second_url = (
        f"sqlite+aiosqlite://{tmp_path / 'second.sqlite3'}" if file_database else "sqlite+aiosqlite://:memory:"
    )
    first_config["connections"] = {"default": first_url, "secondary": first_url}
    first_config["apps"]["library"]["default_connection"] = "default"
    second_config["connections"] = {"default": second_url}
    async with HareContext() as first:
        await first.init(first_config)
        await first.generate_schemas()
        row = await ContextRecord.objects.create(number=1, title="first")
        source = ContextRecord.objects.all()
        async with HareContext() as second:
            await second.init(second_config)
            await second.generate_schemas()
            await ContextRecord.objects.create(number=1, title="second")
            yield first, second, ContextRecord, source, row, file_database


@pytest.mark.parametrize("client_type", ["shared", "transaction", "independent"])
async def test_queries_and_instances_refuse_another_context_client_before_sql(
    independent_context_clients, client_type
):
    first, second, model, source, row, _file_database = independent_context_clients
    foreign_main = second.get_connection()
    independent = foreign_main.create_independent_client() if client_type == "independent" else None
    try:
        async with (
            foreign_main._in_transaction()
            if client_type == "transaction"
            else nullcontext(independent or foreign_main) as foreign
        ):
            # Publish valid read and write plans before attempting a client with the same alias.
            for _ in range(2):
                assert (await source.get(number=1)).title == "first"
                await source.filter(number=1).update(title="first")
            actions = (
                lambda: source.using(foreign),
                lambda: QuerySet(model).using(foreign).get(number=1),
                lambda: QuerySet(model).using(foreign).count(),
                lambda: QuerySet(model).using(foreign).update(title="changed"),
                lambda: QuerySet(model).using(foreign).delete(),
                lambda: QuerySet(model).using(foreign).create(number=2, title="changed"),
                lambda: row.save(using=foreign),
                lambda: row.refresh_from_db(using=foreign),
                lambda: row.delete(using=foreign),
            )
            with QueryExecution(first):
                async with assert_query_count(0, using=foreign):
                    for action in actions:
                        with pytest.raises(ConfigurationError, match="another HareContext"):
                            result = action()
                            if result is not None:
                                await result
            assert (await source.get(number=1)).title == "first"
    finally:
        if independent is not None:
            await independent.close()
    assert (await second.apps.get_queryset(model).get(number=1)).title == "second"


async def test_aliases_and_transaction_clients_of_the_same_context_remain_valid(independent_context_clients):
    first, _second, model, source, row, file_database = independent_context_clients
    primary = first.get_connection()
    assert (await source.using(primary).get(number=1)).title == "first"
    assert (await source.using("default").get(number=1)).title == "first"
    assert source.using("secondary").get_connection().connection_handler is first.connections
    independent = primary.create_independent_client()
    try:
        assert source.using(independent).get_connection() is independent
        if file_database:
            assert (await source.using("secondary").get(number=1)).title == "first"
            assert (await source.using(independent).get(number=1)).title == "first"
    finally:
        await independent.close()
    with QueryExecution(first):
        async with Transactions.atomic() as transaction:
            assert (await source.using(transaction).get(number=1)).title == "first"
            row.title = "saved"
            await row.save(using=transaction)
    assert (await first.apps.get_queryset(model).get(number=1)).title == "saved"


@pytest.mark.parametrize("input_factory", [list, iter])
async def test_template_filters_capture_membership_values_once(input_factory):
    class Book(Model):
        title = fields.TextField()

    values = [1, 2]
    template = QuerySet(Book).filter(id__in=input_factory(values)).order_by("id")
    values.append(3)
    async with HareContext() as context:
        await context.init(BindingSetup.config(Book))
        await context.generate_schemas()
        await Book.objects.bulk_create([Book(id=number, title=str(number)) for number in (1, 2, 3)])
        assert [book.id for book in await template] == [1, 2]
        assert [book.id for book in await template] == [1, 2]


async def test_template_terminal_entries_bind_before_interpreting_model_fields():
    class Book(Model):
        title = fields.TextField()

    template = QuerySet(Book)
    async with HareContext() as context:
        await context.init(BindingSetup.config(Book), connect=False)
        assert await template.in_bulk([]) == {}
        raw = template.raw("SELECT id, title FROM book")
        assert raw._binding is context.apps.get_binding(Book)
        assert raw._context is context
        assert template._binding is None


async def test_meta_inputs_and_state_options_do_not_mutate_prepared_graphs():
    index = Index(fields=("title",), name="by_title")
    declared_table_options = PostgresqlTableOptions(storage_parameters={"fillfactor": 70})

    class Book(Model):
        title = fields.TextField()

        class Meta:
            table = "original_books"
            indexes = (index,)
            table_options = (declared_table_options,)

    config = BindingSetup.config(Book)
    async with HareContext() as first:
        await first.init(config, connect=False)
        binding = first.apps.get_binding(Book)
        Book.Meta.table = "changed_books"
        declared_table_options.storage_parameters["fillfactor"] = 95
        state = State.from_models([Book], environment=MigrationEnvironment.from_apps(first.apps))
        state.models[("library", "Book")].options[ModelOption.INDEXES][0].name = "state_index"
        assert binding.indexes[0].name == "by_title"
        assert binding.table_options[0].storage_parameters["fillfactor"] == 70
        async with HareContext() as second:
            await second.init(config, connect=False)
            assert second.apps.get_binding(Book).db_table == "original_books"
            assert second.apps.get_binding(Book).indexes[0].name == "by_title"
            assert second.apps.get_binding(Book).table_options[0].storage_parameters["fillfactor"] == 70


async def test_live_registration_regenerates_existing_enum_constraints():
    class Rating(IntEnum):
        LOW = 1
        HIGH = 2

    class Book(Model):
        rating = fields.IntEnumField(Rating)

    class Note(Model):
        text = fields.TextField()

    async with HareContext() as context:
        await context.init(BindingSetup.config(Book), connect=False)
        before = {constraint.name for constraint in context.apps.get_binding(Book).constraints}
        assert before
        context.register_live_models([Note], "library")
        after = {constraint.name for constraint in context.apps.get_binding(Book).constraints}
        assert after == before


async def test_collection_bulk_and_membership_reject_foreign_application_instances():
    class Tag(Model):
        label = fields.TextField()

    class Book(Model):
        title = fields.TextField()
        tags = fields.ManyToManyField(Tag, related_name="books")

    config = BindingSetup.config(Tag, Book)
    async with HareContext() as first:
        await first.init(config)
        await first.generate_schemas()
        owner = await Book.objects.create(id=1, title="first")
        source = Book.objects.all()
        async with HareContext() as second:
            await second.init(config)
            await second.generate_schemas()
            foreign_tag = await Tag.objects.create(id=1, label="second")
            foreign_book = await Book.objects.create(id=1, title="second")
            with pytest.raises(ValidationError, match="different model graphs"):
                await owner.tags.add(foreign_tag)
            with pytest.raises(ValidationError, match="different model graphs"):
                await source.bulk_update([foreign_book], ["title"])
            with pytest.raises(ValidationError, match="different model graphs"):
                await source.bulk_create([Book(id=2, title="foreign")])
            with pytest.raises(ValidationError, match="different model graphs"):
                await source.contains(foreign_book)
            assert (await source.get(id=1)).title == "first"
            assert (await Book.objects.get(id=1)).title == "second"


async def test_static_generic_relation_default_survives_a_new_graph():
    class Parent(Model):
        title = fields.TextField()

    async with HareContext() as context:
        await context.init(BindingSetup.config(Parent))
        await context.generate_schemas()
        parent = await Parent.objects.create(title="default")

        class Comment(Model):
            target = fields.GenericForeignKeyField({"parent": Parent}, default=parent)

        context.register_live_models([Comment], "library")
        comment = Comment()
        assert comment.parent_id == parent.pk
        assert comment._binding.graph is context.apps.binding_graph
        assert parent._binding.graph is not comment._binding.graph


async def test_schema_generation_uses_the_explicit_context_under_another_current_one():
    class Book(Model):
        title = fields.TextField()

    class Tag(Model):
        label = fields.TextField()

    async with HareContext() as first:
        await first.init(BindingSetup.config(Book))
        source = Book.objects.all()
        async with HareContext() as second:
            await second.init(BindingSetup.config(Tag))
            await first.generate_schemas()
            await source.create(title="first")
            assert await source.count() == 1
            assert HareContext.get_current() is second


async def test_compatible_projection_protocol_survives_instance_serialization():
    apps = Hare.bind_models(BindingSetup.config(ProjectionRecord))
    obj = apps.construct(ProjectionRecord, id=1, score=7)
    loaded = pickle.loads(pickle.dumps(obj))
    assert loaded.id == 1 and loaded.score == 7
    assert loaded._context is None
    assert loaded._binding.graph is not obj._binding.graph
    assert loaded._binding.table_options[0].projections[0].get_read_columns() == ("score",)


async def test_global_model_labels_do_not_require_every_independent_application():
    class Book(Model):
        title = fields.TextField()

    class Tag(Model):
        label = fields.TextField()

    def observe(event):
        pass

    Observers.observe(RowsChanged, observe, models=["first.Book"])
    try:
        async with HareContext() as first:
            await first.init(BindingSetup.config(Book, app="first"), connect=False)
            async with HareContext() as second:
                await second.init(BindingSetup.config(Tag, app="second"), connect=False)
    finally:
        Observers.unobserve(RowsChanged, observe)


async def test_events_of_a_retained_instance_keep_the_original_model_label():
    class Book(Model):
        title = fields.TextField()

    events = []
    async with HareContext() as context:
        await context.init(BindingSetup.config(Book, app="first"))
        await context.generate_schemas()
        book = await Book.objects.create(title="before")
        Observers.observe(RowsChanged, events.append, models=["first.Book"])
        try:
            context.unregister_live_models([Book])
            context.register_live_models([Book], "second")
            book.title = "after"
            await book.save()
            assert len(events) == 1
            assert events[0].binding.app == "first"
        finally:
            Observers.unobserve(RowsChanged, events.append)


async def test_failed_publication_cannot_execute_an_escaped_draft_query():
    class Book(Model):
        title = fields.TextField()

    class RejectingSink(ChangeSink):
        escaped = None

        def link_models(self, apps):
            type(self).escaped = apps.get_queryset(Book)
            raise ConfigurationError("reject publication")

        def get_field_names(self, binding):
            return ()

        async def write(self, connection, binding, changes):
            pytest.fail("A failed preparation cannot write captured changes")

    class Note(Model):
        text = fields.TextField()

        class Meta:
            change_capture = RejectingSink()

    async with HareContext() as context:
        await context.init(BindingSetup.config(Book), connect=False)
        graph = context.apps.binding_graph
        try:
            with pytest.raises(ConfigurationError, match="reject publication"):
                context.register_live_models([Note], "library")
            assert context.apps.binding_graph is graph
            with pytest.raises(ConfigurationError, match="unpublished"):
                QueryExecution.get_bound(RejectingSink.escaped)
        finally:
            RejectingSink.escaped = None


@pytest.mark.parametrize("operation", ["save", "force_create", "force_update", "refresh", "delete"])
async def test_failed_publication_cannot_execute_an_escaped_instance(independent_context_clients, operation):
    first, second, model, source, retained, _file_database = independent_context_clients
    escaped = []
    defaults = []

    async def get_title():
        defaults.append(True)
        return "unpublished"

    class RejectingSink(ChangeSink):
        def link_models(self, apps):
            obj = apps.new(model, number=1, title="unpublished")
            obj._await_when_save = {"title": get_title}
            obj._saved_in_db = operation in ("refresh", "delete")
            escaped.append(obj)
            raise ConfigurationError("reject publication")

        def get_field_names(self, binding):
            return ()

        async def write(self, connection, binding, changes):
            pytest.fail("An unpublished graph cannot write changes")

    class Note(Model):
        text = fields.TextField()

        class Meta:
            change_capture = RejectingSink()

    graph = first.apps.binding_graph
    with pytest.raises(ConfigurationError, match="reject publication"):
        first.register_live_models([Note], "library")
    assert first.apps.binding_graph is graph
    assert not escaped[0]._binding.graph.sealed
    with QueryExecution(first):
        async with assert_query_count(0, using=first.get_connection()):
            with pytest.raises(ConfigurationError, match="unpublished"):
                if operation == "refresh":
                    await escaped[0].refresh_from_db()
                elif operation == "delete":
                    await escaped[0].delete()
                else:
                    await escaped[0].save(**({operation: True} if operation != "save" else {}))
    assert not defaults
    assert HareContext.get_current() is second
    retained.title = "retained"
    await retained.save(update_fields=["title"])
    assert await source.values_list("title", flat=True) == ["retained"]


@pytest.mark.parametrize("file_database", [False, True], ids=["memory", "file"])
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_registry_rebuild_uses_its_context_under_another_application(tmp_path, file_database, composite):
    class AbstractBook(Model):
        title = fields.TextField(source_field="wire_title")

        class Meta:
            abstract = True

    class Book(AbstractBook):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Chapter(Model):
        book = fields.ForeignKeyField(Book, related_name="chapters")
        title = fields.TextField()

    class Spare(Model):
        text = fields.TextField()

    declared = dict(vars(Book))
    first, second = HareContext(), HareContext()

    def get_table_name(model):
        return ("first_" if HareContext.require_current() is first else "second_") + model.__name__.lower()

    def get_config(name):
        url = (
            f"sqlite+aiosqlite://{tmp_path / (name + '.sqlite3')}" if file_database else "sqlite+aiosqlite://:memory:"
        )
        return {"connections": {"default": url}, "apps": {}}

    async with first:
        await first.init(get_config("first"), table_name_generator=get_table_name)
        first.register_live_models([Book, Chapter, Spare], "library")
        await first.generate_schemas()
        first_book = await Book.objects.create(number=1, title="first")
        await Chapter.objects.create(book=first_book, title="first chapter")
        first_books = Book.objects.filter(number=1)
        async with second:
            await second.init(get_config("second"), table_name_generator=get_table_name)
            second.register_live_models([Book, Chapter], "library")
            await second.generate_schemas()
            second_book = await Book.objects.create(number=1, title="second")
            await Chapter.objects.create(book=second_book, title="second chapter")
            second_books = Book.objects.filter(number=1)
            for query, label in ((first_books, "first"), (second_books, "second")):
                assert await query.values_list("title", flat=True) == [label]
                values, hits = await StatementPlanChecks.count_plan_hits(query.values_list("title", flat=True))
                assert (values, hits) == ([label], 1)

            first.unregister_live_models([Spare])
            for context, retained, name in ((first, first_books, "first"), (second, second_books, "second")):
                binding = context.apps.get_binding(Book)
                assert binding.db_table == f"{name}_book"
                assert binding.fields_db_projection["title"] == "wire_title"
                assert await context.get_connection().execute_dicts(f"SELECT wire_title FROM {name}_book") == [
                    {"wire_title": name}
                ]
                for query in (retained, context.apps.get_queryset(Book).filter(number=1)):
                    assert await query.values_list("title", flat=True) == [name]
                    (obj,) = await query.prefetch_related("chapters")
                    assert [chapter.title for chapter in obj.chapters] == [f"{name} chapter"]
            assert dict(vars(Book)) == declared
            assert HareContext.get_current() is second


async def test_offline_schema_client_never_borrows_the_current_application():
    class Book(Model):
        title = fields.TextField()

    apps = Hare.bind_models(BindingSetup.config(Book))
    source = apps.get_queryset(Book).values("title")
    client = FakeClient("sqlite")
    baseline = QueryConnection.get_schema_queryset(source, client).sql()
    async with HareContext() as context:
        await context.init(BindingSetup.config(Book, app="other"), connect=False)
        compiled = QueryConnection.get_schema_queryset(source, client)
        assert compiled._context is None
        assert compiled._binding is source._binding
        assert compiled.sql() == baseline
        assert HareContext.get_current() is context
    assert source._connection is None
    assert source._context is None


async def test_date_query_templates_bind_before_reading_their_field_metadata():
    import datetime

    class Event(Model):
        moment = fields.DatetimeField()

    dates = Event.objects.dates("moment", "day")
    datetimes = Event.objects.datetimes("moment", "day")
    assert dates._binding is datetimes._binding is None
    async with HareContext() as context:
        await context.init(BindingSetup.config(Event))
        await context.generate_schemas()
        await Event.objects.create(moment=datetime.datetime(2024, 1, 2, 12, tzinfo=datetime.UTC))
        assert await dates == [datetime.date(2024, 1, 2)]
        assert await datetimes == [datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)]
    assert dates._binding is datetimes._binding is None


@pytest.mark.parametrize("input_factory", [list, iter])
@pytest.mark.parametrize("entry", ["chain", "calls"])
async def test_model_query_captures_filter_inputs_before_repeated_schema_compilation(input_factory, entry):
    class Book(Model):
        title = fields.TextField()

    values = [1, 2]
    if entry == "chain":
        template = ModelQuery().filter(id__in=input_factory(values)).order_by("id").values("id")
    else:
        template = ModelQuery(
            calls=(
                ("filter", (), {"id__in": input_factory(values)}),
                ("order_by", ("id",), {}),
                ("values", ("id",), {}),
            )
        )
    values.append(3)
    async with HareContext() as context:
        await context.init(BindingSetup.config(Book), connect=False)
        expected = Book.objects.filter(id__in=[1, 2]).order_by("id").values("id").sql(parameters_inline=True)
        view = View("selected_books", query=template).for_binding(context.apps.get_binding(Book))
        assert view.get_query_sql(context.get_connection()) == expected
        assert view.get_query_sql(context.get_connection()) == expected
        assert template._binding is None
        assert template.model is None


async def test_model_query_clones_preserve_subclass_state_without_repeating_construction():
    constructed = []
    marker = object()

    class TaggedQuery(ModelQuery):
        def __init__(self, *args, **kwargs):
            constructed.append(True)
            super().__init__(*args, **kwargs)
            self.marker = marker

    class Book(Model):
        title = fields.TextField()

    template = TaggedQuery()
    filtered = template.filter(title="selected")
    declared = filtered.for_model(Book)
    async with HareContext() as context:
        await context.init(BindingSetup.config(Book), connect=False)
        binding = context.apps.get_binding(Book)
        bound = declared.for_binding(binding)
        rebound = bound.for_model(Book)
        assert bound._binding is binding
        assert rebound._binding is None
    for query in (template, filtered, declared, bound, rebound):
        assert isinstance(query, TaggedQuery)
        assert query.marker is marker
    assert template.calls == ()
    assert template is not filtered
    assert declared is not bound
    assert bound is not rebound
    assert constructed == [True]
