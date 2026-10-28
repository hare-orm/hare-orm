"""prefetch_related_objects() of instances of several models: each lookup starts from the relation of each
instance's own model - a relation of one name leads to another model's rows on another."""

import pytest

from hare import Prefetch, fields, prefetch_related_objects
from hare.contrib.test import capture_queries, hare_test_context
from hare.models import Model

pytestmark = pytest.mark.sqlite_only

DB_URL = "sqlite+aiosqlite://:memory:"


class Person(Model):
    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=30, unique=True, default="person")


class Team(Model):
    id = fields.IntField(primary_key=True)


class Article(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.ForeignKeyField("models.Person", related_name="articles", null=True)


class Home(Model):
    id = fields.IntField(primary_key=True)
    person = fields.ForeignKeyField("models.Person", related_name="owner")


class Board(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.ForeignKeyField("models.Team", related_name="boards", null=True)


class Note(Model):
    id = fields.IntField(primary_key=True)
    article = fields.ForeignKeyField("models.Article", related_name="items")


class Card(Model):
    id = fields.IntField(primary_key=True)
    board = fields.ForeignKeyField("models.Board", related_name="items")


class Memo(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.ForeignKeyField("models.Person", related_name="memos")


class CodedArticle(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.ForeignKeyField("models.Person", to_field="code", related_name="coded_articles")


@pytest.mark.asyncio
async def test_each_model_s_relation_of_one_name_reads_the_model_s_related_rows():
    """The relation of the first instance's model was read for every instance: a Board's owner got a Person,
    failing as a value of the wrong model, and its items the Notes of the Article of the same key."""
    async with hare_test_context([__name__], db_url=DB_URL):
        await Person.objects.create(id=1)
        await Team.objects.create(id=1)
        await Article.objects.create(id=1, owner_id=1)
        await Board.objects.create(id=1, owner_id=1)
        await Note.objects.create(id=10, article_id=1)
        await Card.objects.create(id=20, board_id=1)
        article = await Article.objects.get(id=1)
        board = await Board.objects.get(id=1)

        await prefetch_related_objects([article, board], "owner", "items")

        assert (type(article.owner), type(board.owner)) == (Person, Team)
        assert [note.id for note in article.items] == [10]
        assert [card.id for card in board.items] == [20]


@pytest.mark.asyncio
async def test_forward_and_reverse_relations_of_one_name_do_not_share_a_prefetcher():
    async with hare_test_context([__name__], db_url=DB_URL):
        await Person.objects.create(id=1)
        await Article.objects.create(id=1, owner_id=1)
        await Home.objects.create(id=1, person_id=1)
        article = await Article.objects.get(id=1)
        person = await Person.objects.get(id=1)

        await prefetch_related_objects([article, person], "owner")

        assert article.owner.id == 1
        assert [home.id for home in person.owner] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_compatible_forward_relations_share_the_query(nested):
    async with hare_test_context([__name__], db_url=DB_URL):
        await Person.objects.create(id=1, code="first")
        await Person.objects.create(id=2, code="second")
        await Article.objects.create(id=1, owner_id=1)
        await Memo.objects.create(id=1, owner_id=2)
        article = await Article.objects.get(id=1)
        memo = await Memo.objects.get(id=1)
        lookups = (
            (Prefetch("owner", Person.objects.all(), to_attribute="writer"), "writer__articles")
            if nested
            else ("owner",)
        )
        async with capture_queries() as queries:
            await prefetch_related_objects([article, memo], *lookups)
        assert len(queries.queries) == (2 if nested else 1)
        owners = [getattr(obj, "writer" if nested else "owner") for obj in (article, memo)]
        assert [owner.id for owner in owners] == [1, 2]
        if nested:
            assert [[row.id for row in owner.articles] for owner in owners] == [[1], []]


@pytest.mark.asyncio
async def test_forward_relations_with_different_target_fields():
    async with hare_test_context([__name__], db_url=DB_URL):
        await Person.objects.create(id=1, code="first")
        await Person.objects.create(id=2, code="second")
        await Article.objects.create(id=1, owner_id=1)
        await CodedArticle.objects.create(id=1, owner_id="second")
        article = await Article.objects.get(id=1)
        coded_article = await CodedArticle.objects.get(id=1)
        async with capture_queries() as queries:
            await prefetch_related_objects([article, coded_article], "owner")
        assert [article.owner.id, coded_article.owner.id] == [1, 2]
        assert len(queries.queries) == 1
