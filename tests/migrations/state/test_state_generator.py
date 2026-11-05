from __future__ import annotations

from typing import Any, cast

import pytest

from hare import fields
from hare.core.hare_context import HareContext
from hare.ddl.constraints import UniqueConstraint
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.migrations.operations import CreateModel
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model

pytestmark = pytest.mark.database_independent


def test_model_state_skips_fk_reference_fields() -> None:
    class Author(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            app = "blog"

    class Post(Model):
        id = fields.IntField(primary_key=True)
        author: ForeignKeyFieldInstance[Any] = fields.ForeignKeyField("blog.Author", related_name="posts")

        class Meta:
            app = "blog"

    state = ModelState.make_from_model("blog", Post)
    assert "author" in state.fields
    assert "author_id" not in state.fields


def test_model_state_keeps_effective_relation_schema_without_live_bindings() -> None:
    class Author(Model):
        pass

    class Book(Model):
        authors = fields.ManyToManyField(
            "blog.Author", through="blog.Credit", related_name="books", on_delete=fields.SET_NULL
        )
        reviewers = fields.ManyToManyField("blog.Author", related_name="reviewed_books")

    class Credit(Model):
        book = fields.ForeignKeyField("blog.Book", related_name="credits", null=True)
        author = fields.ForeignKeyField("blog.Author", related_name="credits", null=True, source_field="writer_key")
        position = fields.IntField()

        class Meta:
            constraints = (UniqueConstraint(fields=("book", "position")),)

    apps = StateApps()
    for model in (Author, Book, Credit):
        apps.register_model("blog", model)
    apps.init_relations()
    state = State.from_models((Author, Book, Credit))
    for snapshot in (state, state.clone()):
        credit = snapshot.models[("blog", "Credit")]
        book_field = cast(ForeignKeyFieldInstance, credit.fields["book"])
        assert book_field.on_delete is fields.SET_NULL
        assert book_field.index is False
        assert book_field.to_field == "id"
        assert book_field.source_field == "book_id"
        assert book_field.related_model is None
        assert credit.fields["author"].source_field == "writer_key"
        book = snapshot.models[("blog", "Book")]
        assert book.fields["authors"].deconstruct()[2]["through"] == "blog.Credit"
        assert book.fields["reviewers"].deconstruct()[2]["through"] == "book_author"
        rendered = snapshot.apps.get_model("blog.Credit")
        assert rendered._meta.fields_map["book"].on_delete is fields.SET_NULL
        assert rendered._meta.fields_map["book"].index is False


def test_model_state_preserves_tenant_soft_delete_and_optimistic_lock_fields() -> None:
    """Without these three in ModelState.make_from_model()'s own options dict, a model rendered
    through this state (e.g. apps.get_model() inside a RunPython migration) silently loses its
    Meta.tenant_field/soft_delete_field/optimistic_lock_field - the default manager then applies no
    tenant/soft-delete ambient filtering at all, and no optimistic-lock version check, regardless
    of what the real model declares."""

    class Order(Model):
        org_id = fields.IntField()
        deleted_at = fields.DatetimeField(null=True)
        version = fields.IntField(default=1)

        class Meta:
            app = "blog"
            tenant_field = "org_id"
            soft_delete_field = "deleted_at"
            optimistic_lock_field = "version"

    state = ModelState.make_from_model("blog", Order)
    rendered = state.render()
    assert rendered._meta.tenant_field == "org_id"
    assert rendered._meta.soft_delete_field == "deleted_at"
    assert rendered._meta.optimistic_lock_field == "version"


def test_field_signature_ignores_implicit_db_column() -> None:
    field = fields.CharField(max_length=100)
    field.model_field_name = ""
    field.source_field = None
    from hare.migrations.autodetection.state_signatures import StateSignatures

    signature = StateSignatures.get_field_signature(field)
    assert "db_column" not in signature


def test_state_apps_builds_relations_before_querysets() -> None:
    with HareContext() as ctx:
        ctx.connections._init_config(
            {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": ":memory:"},
                }
            }
        )
        state = State(default_connections={"blog": "default"})
        CreateModel(
            name="Author",
            fields=[("id", fields.IntField(primary_key=True))],
        ).state_forward("blog", state)
        CreateModel(
            name="Post",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("author", fields.ForeignKeyField("blog.Author", related_name="posts")),
            ],
        ).state_forward("blog", state)

        post_model = state.apps.get_model("blog.Post")
        author_field = cast(ForeignKeyFieldInstance, post_model._meta.fields_map["author"])
        assert author_field.to_field_instance is not None
