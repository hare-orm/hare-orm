"""GenericForeignKeyField: an exclusive arc of real foreign keys - its branches, its CHECK, reading
and assigning it, querying, loading and writing through it, on_delete, unique constraints, migrations
and its pydantic schema."""

from __future__ import annotations

import contextlib
from typing import Any, Literal, get_args

import pytest
from pydantic import ValidationError as PydanticValidationError

from hare import Connections
from hare.contrib.pydantic import pydantic_model_creator
from hare.contrib.request_query import FilterField, InvalidRequestQuery, RequestQuery
from hare.exceptions import FieldError, IntegrityError, NoValuesFetched, QueryError, ValidationError
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.models.tenancy.tenancy import Tenancy
from hare.query.enums import Lookup
from hare.query.expressions import Case, Exists, OuterReference, Q, Value, When
from hare.query.functions import Count
from hare.transactions.transactions import Transactions
from tests.fields.models_generic_fk import (
    Activity,
    ArticleVersion,
    Author,
    Bookmark,
    Comment,
    Draft,
    Flag,
    Like,
    Mention,
    Photo,
    Pin,
    Post,
    Reaction,
    Shop,
)


@pytest.mark.asyncio
async def test_the_branches_are_foreign_keys(db_generic_fk):
    meta = Comment._meta
    assert set(meta.generic_foreign_key_fields) == {"target"}
    for branch_name in ("post", "photo", "article_version"):
        branch = meta.fields_map[branch_name]
        assert isinstance(branch, ForeignKeyFieldInstance)
        assert branch.null
    assert meta.fields_map["post"].related_model is Post
    assert meta.fields_map["photo"].related_model is Photo
    assert "target" not in meta.fields_map


@pytest.mark.asyncio
async def test_create_and_read(db_generic_fk):
    post = await Post.objects.create(id=1, title="p")
    comment = await Comment.objects.create(id=1, target=post)
    assert comment.post_id == 1
    assert comment.photo_id is None
    assert await comment.target == post
    connection = Connections.get("models")
    sql = connection.dialect.schema_editor_class(connection).table_creation.get_create_schema_sql()
    assert "target_exclusive_arc" in sql


async def make_targets() -> tuple[Post, Post, Photo, ArticleVersion]:
    first_post = await Post.objects.create(id=1, title="first")
    second_post = await Post.objects.create(id=2, title="second")
    photo = await Photo.objects.create(id=1, url="u")
    version = await ArticleVersion.objects.create(article=7, version=2)
    return first_post, second_post, photo, version


async def make_comments() -> tuple[Post, Post, Photo, ArticleVersion]:
    first_post, second_post, photo, version = await make_targets()
    await Comment.objects.create(id=1, target=first_post)
    await Comment.objects.create(id=2, target=second_post)
    await Comment.objects.create(id=3, target=photo)
    await Comment.objects.create(id=4, target=version)
    return first_post, second_post, photo, version


def savepoint(connection: Any) -> Any:
    """A savepoint keeping a failing statement from aborting the test's transaction - nothing on a
    database without transactions."""
    if connection.features.supports_transactions:
        return Transactions.atomic("models")
    return contextlib.nullcontext()


async def ids(queryset: Any) -> list[int]:
    return sorted(await queryset.values_list("id", flat=True))


@pytest.mark.asyncio
async def test_filters(db_generic_fk):
    first_post, second_post, photo, version = await make_comments()
    objects = Comment.objects
    assert await ids(objects.filter(target=first_post)) == [1]
    assert await ids(objects.filter(target=photo)) == [3]
    assert await ids(objects.filter(target=version)) == [4]
    assert await ids(objects.filter(target__in=[second_post, photo, version])) == [2, 3, 4]
    assert await ids(objects.filter(target__in=[first_post, second_post])) == [1, 2]
    assert await ids(objects.filter(target__in=[])) == []
    assert await ids(objects.filter(target__not=first_post)) == [2, 3, 4]
    assert await ids(objects.filter(target__not_in=[first_post, photo])) == [2, 4]
    assert await ids(objects.filter(target__type="post")) == [1, 2]
    assert await ids(objects.filter(target__type="article_version")) == [4]
    assert await ids(objects.filter(target__type__in=["photo", "article_version"])) == [3, 4]
    assert await ids(objects.filter(target__type__not="post")) == [3, 4]
    assert await ids(objects.filter(target__type="nothing")) == []
    assert await ids(objects.filter(target__isnull=False)) == [1, 2, 3, 4]
    assert await ids(objects.filter(target__isnull=True)) == []
    assert await ids(objects.exclude(target__type="post")) == [3, 4]
    assert await ids(objects.filter(Q(target=photo) | Q(target__type="article_version"))) == [3, 4]
    assert await ids(objects.filter(~Q(target__type="post"), id__gt=3)) == [4]
    # The same key with another type runs on a plan of its own.
    for _ in range(2):
        assert await ids(objects.filter(target__type="photo")) == [3]
        assert await ids(objects.filter(target__type="post")) == [1, 2]
    assert (await objects.get(target=photo)).id == 3
    assert (await objects.get(target__type="photo")).id == 3
    assert (await objects.get(target__type="article_version")).id == 4
    assert await objects.get(target__type="nothing", does_not_exist_exception=None) is None
    with pytest.raises(ValidationError, match="Expected one of the model types"):
        await objects.filter(target=await Author.objects.create(id=1, name="a")).count()
    with pytest.raises(FieldError):
        await objects.filter(target__gt=first_post).count()


@pytest.mark.asyncio
async def test_filters_through_relations_and_inside_expressions(db_generic_fk):
    first_post, _second_post, photo, _version = await make_comments()
    assert await ids(Post.objects.filter(comments__target__type="post")) == [1, 2]
    annotated = Comment.objects.annotate(
        is_photo=Case(When(target__type="photo", then=Value(1)), default=Value(0))
    ).order_by("id")
    assert [row.is_photo for row in await annotated] == [0, 0, 1, 0]
    counted = (
        await Post.objects.annotate(first_comments=Count("comments", _filter=Q(comments__target=first_post)))
        .order_by("id")
        .values_list("first_comments", flat=True)
    )
    assert counted == [1, 0]
    assert await ids(
        Comment.objects.filter(Exists(Comment.objects.filter(target=photo, id=OuterReference("id"))))
    ) == [3]


@pytest.mark.asyncio
async def test_the_check_keeps_exactly_one_branch(db_generic_fk):
    first_post, _second_post, photo, _version = await make_targets()
    connection = Connections.get("models")
    literals = connection.dialect.literals
    parameters = connection.dialect.parameters
    table = literals.quote_identifier("generic_comment")
    with pytest.raises(IntegrityError):
        async with savepoint(connection):
            await connection.execute(
                f"INSERT INTO {table} (id, text, post_id, photo_id) VALUES "
                f"(9, '', {parameters.get_placeholder(1)}, {parameters.get_placeholder(2)})",
                [first_post.id, photo.id],
            )
    with pytest.raises(IntegrityError):
        async with savepoint(connection):
            await connection.execute(f"INSERT INTO {table} (id, text) VALUES (10, '')")
    with pytest.raises(IntegrityError):
        async with savepoint(connection):
            await connection.execute(f"INSERT INTO {table} (id, text, article_version_article) VALUES (11, '', 7)")
    # A null=True field takes no target at all.
    reaction = await Reaction.objects.create(id=1)
    assert await reaction.subject is None


@pytest.mark.asyncio
async def test_assigning_sets_one_branch_and_clears_the_others(db_generic_fk):
    first_post, _second_post, photo, version = await make_targets()
    comment = Comment(id=1, target=first_post)
    assert (comment.post_id, comment.photo_id) == (1, None)
    comment.target = photo
    assert (comment.post_id, comment.photo_id) == (None, 1)
    assert await comment.target == photo
    comment.target = version
    assert (comment.photo_id, comment.article_version_article, comment.article_version_version) == (None, 7, 2)
    await comment.save()
    stored = await Comment.objects.get(id=1)
    assert await stored.target == version
    with pytest.raises(ValidationError, match="Expected one of the model types"):
        comment.target = Author(id=5, name="x")
    with pytest.raises(QueryError, match="non nullable"):
        comment.target = None
    with pytest.raises(QueryError, match="both given"):
        Comment(id=2, target=first_post, photo=photo)


@pytest.mark.asyncio
async def test_select_related_and_prefetch_related(db_generic_fk):
    first_post, _second_post, photo, version = await make_comments()
    comments = await Comment.objects.select_related("target").order_by("id")
    assert [comment.target for comment in comments] == [first_post, _second_post, photo, version]
    comments = await Comment.objects.prefetch_related("target").order_by("id")
    assert [comment.target for comment in comments] == [first_post, _second_post, photo, version]
    likes = await Like.objects.select_related("author")
    assert likes == []
    with pytest.raises(FieldError, match="can't go on past"):
        await Comment.objects.select_related("target__title")
    # The backward relation of each target.
    assert sorted(comment.id for comment in await first_post.comments) == [1]
    assert sorted(comment.id for comment in await photo.comments) == [3]


@pytest.mark.asyncio
async def test_the_type_is_read_and_ordered(db_generic_fk):
    await make_comments()
    await Comment.objects.create(id=5, target=await Photo.objects.create(id=2, url="v"))
    rows = await Comment.objects.order_by("id").values("id", "target__type")
    assert [row["target__type"] for row in rows] == ["post", "post", "photo", "article_version", "photo"]
    assert await Comment.objects.order_by("target__type", "id").values_list("id", flat=True) == [4, 3, 5, 1, 2]
    assert await Comment.objects.order_by("-target__type", "id").values_list("id", flat=True) == [1, 2, 3, 5, 4]
    assert await Comment.objects.filter(target__type="photo").values_list("id", "target__type") == [
        (3, "photo"),
        (5, "photo"),
    ]
    assert await Reaction.objects.values_list("subject__type", flat=True) == []
    await Reaction.objects.create(id=1)
    assert await Reaction.objects.values_list("subject__type", flat=True) == [None]


@pytest.mark.asyncio
async def test_writes(db_generic_fk):
    first_post, second_post, photo, version = await make_targets()
    await Comment.objects.bulk_create(
        [Comment(id=1, target=first_post), Comment(id=2, target=photo), Comment(id=3, target=version)]
    )
    assert await ids(Comment.objects.filter(target__type="post")) == [1]
    await Comment.objects.filter(id=2).update(target=second_post)
    assert await ids(Comment.objects.filter(target=second_post)) == [2]
    assert await Comment.objects.filter(id=2).values_list("photo_id", flat=True) == [None]
    comments = await Comment.objects.order_by("id")
    comments[0].target = photo
    comments[2].target = first_post
    await Comment.objects.bulk_update(comments, fields=["target"])
    assert await Comment.objects.order_by("id").values_list("id", "target__type") == [
        (1, "photo"),
        (2, "post"),
        (3, "post"),
    ]
    comment = await Comment.objects.get(id=1)
    comment.target = version
    comment.text = "kept"
    await comment.save(update_fields=["target"])
    stored = await Comment.objects.get(id=1)
    assert (await stored.target, stored.text) == (version, "")
    with pytest.raises(QueryError, match="non nullable"):
        await Comment.objects.filter(id=1).update(target=None)
    created, is_new = await Comment.objects.get_or_create(target=photo, defaults={"id": 9})
    assert is_new and created.photo_id == photo.id
    found, is_new = await Comment.objects.get_or_create(target=photo, defaults={"id": 10})
    assert not is_new and found.id == 9


@pytest.mark.asyncio
async def test_on_delete(db_generic_fk):
    first_post, second_post, photo, version = await make_comments()
    await Reaction.objects.create(id=1, subject=first_post)
    await Reaction.objects.create(id=2, subject=photo)
    await Pin.objects.create(id=1, pinned=second_post)
    await Flag.objects.create(id=1, flagged=photo)
    default_post = first_post
    await Bookmark.objects.create(id=1, item=second_post)
    await Bookmark.objects.create(id=2)
    assert await (await Bookmark.objects.get(id=2)).item == default_post

    # CASCADE deletes the comments of the deleted target only.
    await version.delete()
    assert await ids(Comment.objects.all()) == [1, 2, 3]
    # SET_NULL clears the branch, leaving the row with no target.
    with pytest.raises(IntegrityError):
        async with savepoint(Connections.get("models")):
            await Photo.objects.filter(id=photo.id).delete()
    await Flag.objects.all().delete()
    await photo.delete()
    assert await Reaction.objects.order_by("id").values_list("id", "subject__type") == [(1, "reacted_post"), (2, None)]
    assert await ids(Comment.objects.all()) == [1, 2]
    # PROTECT refuses while a pin points at the post.
    with pytest.raises(IntegrityError):
        async with savepoint(Connections.get("models")):
            await second_post.delete()
    await Pin.objects.all().delete()
    # SET_DEFAULT moves the bookmark to the default post, clearing its other branches.
    await second_post.delete()
    bookmark = await Bookmark.objects.get(id=1)
    assert (bookmark.bookmarked_post_id, bookmark.bookmarked_photo_id) == (default_post.id, None)


@pytest.mark.asyncio
async def test_two_generic_relations_sharing_a_target(db_generic_fk):
    author = await Author.objects.create(id=1, name="a")
    post = await Post.objects.create(id=1, title="p")
    await Activity.objects.create(id=1, subject=post, actor=author)
    await Activity.objects.create(id=2, subject=author)
    assert await ids(Activity.objects.filter(actor=author)) == [1]
    assert await ids(Activity.objects.filter(subject=author)) == [2]
    assert await ids(author.subject_activities) == [2]
    assert await ids(author.actor_activities) == [1]


@pytest.mark.asyncio
async def test_a_unique_constraint_naming_the_field_is_one_per_branch(db_generic_fk):
    names = {constraint.name for constraint in Like._meta.constraints}
    assert {"one_like_liked_post", "one_like_liked_photo", "target_exclusive_arc"} <= names
    plain_indexes = [index for index in Like._meta.indexes if isinstance(index, tuple)]
    assert plain_indexes == [("author", "liked_post"), ("author", "liked_photo")]
    named_indexes = [index for index in Like._meta.indexes if not isinstance(index, tuple)]
    assert [(index.name, list(index.fields)) for index in named_indexes] == [
        ("recent_likes_liked_post", ["liked_post", "id"]),
        ("recent_likes_liked_photo", ["liked_photo", "id"]),
    ]
    assert [index.field_orders for index in named_indexes] == [["", "DESC"], ["", "DESC"]]
    if not Connections.get("models").features.supports_unique_constraints:
        return
    author = await Author.objects.create(id=1, name="a")
    post = await Post.objects.create(id=1, title="p")
    photo = await Photo.objects.create(id=1, url="u")
    await Like.objects.create(id=1, author=author, target=post)
    await Like.objects.create(id=2, author=author, target=photo)
    with pytest.raises(IntegrityError):
        async with savepoint(Connections.get("models")):
            await Like.objects.create(id=3, author=author, target=post)


@pytest.mark.asyncio
async def test_pydantic_output_is_a_union_told_apart_by_type(db_generic_fk):
    first_post, _second_post, photo, version = await make_comments()
    schema = pydantic_model_creator(Comment)
    assert "post" not in schema.model_fields and "post_id" not in schema.model_fields
    assert "target" in schema.model_fields
    serialized = [item.model_dump()["target"] for item in await schema.from_queryset(Comment.objects.order_by("id"))]
    assert {key: serialized[0][key] for key in ("type", "id", "title")} == {"type": "post", "id": 1, "title": "first"}
    assert {key: serialized[2][key] for key in ("type", "id", "url")} == {"type": "photo", "id": 1, "url": "u"}
    assert serialized[3]["type"] == "article_version"
    assert (serialized[3]["article"], serialized[3]["version"]) == (7, 2)
    json_schema = schema.model_json_schema()
    target_schema = json_schema["properties"]["target"]
    assert len(target_schema["oneOf"]) == 3
    with pytest.raises(NoValuesFetched):
        schema.model_validate(await Comment.objects.get(id=1))


@pytest.mark.asyncio
async def test_pydantic_input_takes_the_type_and_the_key(db_generic_fk):
    _first_post, second_post, _photo, version = await make_targets()
    schema = pydantic_model_creator(Comment, exclude_readonly=True)
    payload = schema.model_validate({"target": {"type": "post", "id": second_post.id}})
    comment = await Comment.objects.create(**payload.model_dump())
    assert await comment.target == second_post
    payload = schema.model_validate({"target": {"type": "article_version", "article": 7, "version": 2}})
    comment = await Comment.objects.create(**payload.model_dump())
    assert (await Comment.objects.get(id=comment.id)).article_version_version == version.version
    with pytest.raises(PydanticValidationError):
        schema.model_validate({"target": {"type": "nothing", "id": 1}})
    with pytest.raises(PydanticValidationError):
        schema.model_validate({"target": {"type": "post"}})
    flat_schema = pydantic_model_creator(Comment, relations_as_ids=True, name="CommentAsIds")
    first_comment = await Comment.objects.get(target=second_post)
    serialized = (await flat_schema.from_hare_orm(first_comment)).model_dump()
    assert serialized["target"] == {"type": "post", "id": second_post.id}


@pytest.mark.asyncio
async def test_a_soft_deleted_or_tenant_hidden_target_is_hidden_as_through_a_foreign_key(db_generic_fk):
    draft = await Draft.objects.create(id=1)
    with Tenancy.scope("north"):
        shop = await Shop.objects.create(id=1, region="north")
    post = await Post.objects.create(id=1, title="p")
    await Mention.objects.create(id=1, mentioned=draft)
    await Mention.objects.create(id=2, mentioned=shop)
    await Mention.objects.create(id=3, mentioned=post)
    await draft.delete()
    # The mention stays; its soft-deleted target reads as gone, as through a foreign key. Joining
    # every branch joins the tenant-scoped one too, so a tenant is active.
    with Tenancy.scope("north"):
        mention = await Mention.objects.select_related("mentioned").get(id=1)
    assert mention.mentioned_draft_id == 1
    assert mention.mentioned is None
    assert await ids(Mention.objects.filter(mentioned__type="mentioned_draft")) == [1]
    with Tenancy.scope("south"):
        assert await Shop.objects.count() == 0
        assert await ids(Mention.objects.filter(mentioned_shop__region="north")) == []
    with Tenancy.scope("north"):
        assert await ids(Mention.objects.filter(mentioned_shop__region="north")) == [2]
        assert await (await Mention.objects.select_related("mentioned").get(id=2)).mentioned == shop


class CommentRequestQuery(RequestQuery[Comment]):
    class Meta:
        queryset = Comment.objects.all()
        filters = (
            FilterField("target", lookups=(Lookup.EXACT, Lookup.IN, Lookup.ISNULL)),
            FilterField("target__type", lookups=(Lookup.EXACT, Lookup.IN, Lookup.NOT)),
        )
        pagination = None


@pytest.mark.asyncio
async def test_request_query_filters_by_target_and_type(db_generic_fk):
    await make_comments()
    CommentRequestQuery.prepare_parameters()
    fields_by_name = CommentRequestQuery.model_fields
    assert get_args(fields_by_name["target__type"].annotation)[0] == Literal["post", "photo", "article_version"]

    async def ids_of(**parameters: Any) -> list[int]:
        return sorted(comment.id for comment in await CommentRequestQuery.model_validate(parameters).fetch())

    assert await ids_of(target="post:1") == [1]
    assert await ids_of(target="article_version:7,2") == [4]
    assert await ids_of(target__in=["post:2", "photo:1"]) == [2, 3]
    assert await ids_of(target__type="post") == [1, 2]
    assert await ids_of(target__type__in=["photo", "article_version"]) == [3, 4]
    assert await ids_of(target__type__not="post") == [3, 4]
    assert await ids_of(target__isnull=False) == [1, 2, 3, 4]
    for wrong in ("post", "video:1", "post:x", "article_version:7"):
        with pytest.raises((InvalidRequestQuery, PydanticValidationError)):
            CommentRequestQuery.model_validate({"target": wrong})
    with pytest.raises((InvalidRequestQuery, PydanticValidationError)):
        CommentRequestQuery.model_validate({"target__type": "video"})


@pytest.mark.asyncio
async def test_a_loaded_relation_with_no_branch_set_reads_none_as_a_foreign_key_does(db_generic_fk):
    post = await Post.objects.create(id=1, title="post")
    await Reaction.objects.create(id=1, subject=post)
    await Reaction.objects.create(id=2)
    for loaded in (Reaction.objects.select_related("subject"), Reaction.objects.prefetch_related("subject")):
        reactions = await loaded.order_by("id")
        assert [reaction.subject for reaction in reactions] == [post, None]
    reaction = await Reaction.objects.get(id=2)
    assert await reaction.subject is None
    reaction.subject = None
    assert reaction.subject is None
