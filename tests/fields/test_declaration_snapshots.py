from copy import copy, deepcopy
from enum import StrEnum
from pickle import dumps, loads

import pytest

from hare import Model, fields
from hare.migrations import Migration, State
from hare.migrations.operations import CreateModel, RunPython, SeparateDatabaseAndState
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import F
from tests.fields.field_declaration.subclass_fields import EnumField

pytestmark = pytest.mark.database_independent


class Label(StrEnum):
    BOOK = "book"


class SlottedEnumField(EnumField):
    __slots__ = ("labels", "__note")


@pytest.mark.parametrize("operation", ["copy", "deepcopy", "snapshot", "pickle"])
def test_field_copy_preserves_inherited_and_private_slots(operation):
    source = SlottedEnumField(Label)
    source.labels = ["original"]
    source._SlottedEnumField__note = "private"
    source.cache_buckets[1] = object()
    if operation == "pickle":
        copied = loads(dumps(source))
    elif operation == "snapshot":
        copied = source.snapshot()
    else:
        copied = {"copy": copy, "deepcopy": deepcopy}[operation](source)
    assert copied.enum_type is Label
    assert copied._SlottedEnumField__note == "private"
    assert copied.to_python("book") is Label.BOOK
    assert copied.labels == ["original"]
    assert not copied.cache_buckets
    assert copied.validators is not source.validators
    copied.labels.append("changed")
    assert source.labels == (["original", "changed"] if operation == "copy" else ["original"])


def test_slotted_field_state_survives_model_publication_and_foreign_key_copy():
    declaration = EnumField(Label, unique=True)

    class Author(Model):
        label = declaration

    class Book(Model):
        author = fields.ForeignKeyField("library.Author", to_field="label")

    apps = StateApps()
    apps.register_model("library", Author)
    apps.register_model("library", Book)
    apps.init_relations()
    published = Author._meta.fields_map["label"]
    shadow = Book._meta.fields_map["author_id"]
    assert published is not declaration
    assert published.enum_type is shadow.enum_type is Label
    assert shadow.to_python("book") is Label.BOOK
    assert published.snapshot().enum_type is Label


class DefaultFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return []

    def __deepcopy__(self, memo):
        raise AssertionError("A callback must not be copied")


def test_nested_field_snapshot_copies_literals_and_preserves_callbacks():
    factory = DefaultFactory()
    element = fields.JSONField(default=factory)
    source = fields.ArrayField(element, default=[{"labels": []}])
    copied = source.snapshot()
    assert copied is not source
    assert copied.base_field is not element
    assert copied.base_field.default is factory
    assert factory.calls == 0
    copied.default[0]["labels"].append("changed")
    copied.base_field.null = True
    assert source.default == [{"labels": []}]
    assert element.null is False


def test_generated_field_snapshot_does_not_keep_its_output_model_binding():
    class Book(Model):
        price = fields.IntField()
        total = fields.GeneratedField(F("price") + 1, output_field=fields.IntField())

    original = Book._meta.fields_map["total"]
    copied = original.snapshot()
    assert copied.model is None
    assert copied.model_field_name == ""
    assert copied.output_field.model is None
    assert copied.output_field is not original.output_field
    assert original.output_field.model is Book


def test_historical_relation_declaration_rebinds_without_live_model_references():
    class Author(Model):
        name = fields.TextField(unique=True)

    class Book(Model):
        author = fields.ForeignKeyField("library.Author", to_field="name", source_field="author_name")

    apps = StateApps()
    apps.register_model("library", Author)
    apps.register_model("library", Book)
    apps.init_relations()
    state = State.from_models([Author, Book], app_label="library")
    relation = state.models[("library", "Book")].fields["author"]
    assert relation.model is relation.related_model is relation.to_field_instance is None
    assert relation.to_field_instances == relation.source_fields == ()
    assert relation.source_field == "author_name"
    historical = state.apps.get_model("library.Book")._meta.fields_map["author"]
    assert historical.related_model is state.apps.get_model("library.Author")
    assert historical.related_model is not Author
    assert relation.related_model is None
    assert historical.db_column_names == ("author_name",)


def test_operation_snapshot_preserves_field_factory_and_nested_python_callback():
    factory = DefaultFactory()
    field = fields.JSONField(default=factory)
    migration = Migration(
        "initial",
        "library",
        operations=[
            CreateModel("Book", [("id", fields.IntField(primary_key=True)), ("payload", field)]),
            SeparateDatabaseAndState(database_operations=[RunPython(factory, factory)]),
        ],
    )
    prepared = migration.prepare(State())
    assert prepared.after.models[("library", "Book")].fields["payload"].default is factory
    assert prepared.operations[1].database_operations.operations[0].operation.code is factory
    assert factory.calls == 0
    assert field.model is None


def test_one_declaration_can_be_published_under_different_model_field_names():
    factory = DefaultFactory()
    declaration = fields.ArrayField(fields.JSONField(default=factory))

    class Book(Model):
        labels = declaration

    class Author(Model):
        tags = declaration

    book_field = Book._meta.fields_map["labels"]
    author_field = Author._meta.fields_map["tags"]
    assert declaration.model is None
    assert declaration.model_field_name == ""
    assert book_field is not author_field
    assert book_field.base_field is not author_field.base_field
    assert book_field.model is Book
    assert author_field.model is Author
    assert book_field.base_field.default is author_field.base_field.default is factory
    assert factory.calls == 0


def test_publication_snapshot_and_conversion_buckets_have_separate_lifetimes():
    from copy import copy

    from hare.core.caching.cache import Cache

    class Book(Model):
        payload = fields.JSONField(default={"labels": []})

    field = Book._meta.fields_map["payload"]
    binding = field.get_binding()
    cache = Cache()
    cache.get_bucket(field)["conversion"] = object()
    field.default["labels"].append("runtime")
    declaration = field.snapshot()
    assert binding.model is Book and binding.name == "payload"
    assert binding.column == "payload"
    assert declaration.model is None
    assert declaration.default == {"labels": []}
    assert not declaration.cache_buckets
    assert binding.cache_buckets
    copied = copy(field)
    assert copied.get_binding() is not binding
    assert copied.model is Book
    assert not copied.cache_buckets
    copied.null = True
    assert copied.snapshot().null is True
    assert field.snapshot().null is False


def test_relation_snapshot_keeps_implicit_column_declaration_after_linking():
    class Author(Model):
        name = fields.TextField()

    class Book(Model):
        author = fields.ForeignKeyField("library.Author")

    apps = StateApps()
    apps.register_model("library", Author)
    apps.register_model("library", Book)
    apps.init_relations()
    field = Book._meta.fields_map["author"]
    assert field.source_field == "author_id"
    declaration = field.snapshot()
    assert declaration.source_field is None
    assert declaration.model is declaration.related_model is None
    assert declaration.source_fields == declaration.db_column_names == ()


def test_abstract_inheritance_uses_the_same_factory_without_copying_its_resources():
    factory = DefaultFactory()

    class Base(Model):
        payload = fields.JSONField(default=factory)

        class Meta:
            abstract = True

    class Book(Base):
        pass

    class Author(Base):
        pass

    assert Book._meta.fields_map["payload"].default is factory
    assert Author._meta.fields_map["payload"].default is factory
    assert Book._meta.fields_map["payload"] is not Author._meta.fields_map["payload"]
    assert factory.calls == 0
