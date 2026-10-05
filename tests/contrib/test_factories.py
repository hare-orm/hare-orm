"""``hare.contrib.factories``: objects made for tests from declarations - sequences, lazy values,
related objects, traits, many-to-many links, composite and generic keys, the tenant of the scope."""

import itertools

import pytest
import pytest_asyncio

from hare.contrib.factories import (
    Faker,
    Iterator,
    LazyAttribute,
    LazyFunction,
    ManyToMany,
    ModelFactory,
    RelatedFactory,
    Sequence,
    SubFactory,
    Trait,
)
from hare.contrib.test import RollbackIsolation, capture_queries, hare_test_context
from hare.exceptions import ConfigurationError
from hare.models.tenancy.tenancy import Tenancy
from tests.factories_models import Article, Label, Member, Note, StoreItem, Team, Version

IDS = itertools.count(1)


class TeamFactory(ModelFactory[Team]):
    id = LazyFunction(lambda: next(IDS))
    name = Sequence(lambda number: f"team{number}")


class MemberFactory(ModelFactory[Member]):
    id = LazyFunction(lambda: next(IDS))
    name = Sequence(lambda number: f"member{number}")
    email = LazyAttribute(lambda member: f"{member.name}@{member.domain}")
    team = SubFactory(TeamFactory, name="core")

    class Params:
        domain = "example.com"
        staff = Trait(is_staff=True, name="admin")


class LabelFactory(ModelFactory[Label]):
    id = LazyFunction(lambda: next(IDS))
    name = Iterator(["python", "orm"])


class ArticleFactory(ModelFactory[Article]):
    id = LazyFunction(lambda: next(IDS))
    title = "Untitled"
    author = SubFactory(MemberFactory)
    labels = ManyToMany(LabelFactory, size=2)


class AuthorFactory(MemberFactory):
    articles = RelatedFactory("tests.contrib.test_factories.PlainArticleFactory", "author", size=2)


class PlainArticleFactory(ModelFactory[Article]):
    id = LazyFunction(lambda: next(IDS))
    title = Sequence(lambda number: f"article{number}")


class VersionFactory(ModelFactory[Version]):
    article_number = 7
    number = Sequence(lambda number: number + 1)


class NoteFactory(ModelFactory[Note]):
    id = LazyFunction(lambda: next(IDS))
    target = SubFactory(VersionFactory)


class StoreItemFactory(ModelFactory[StoreItem]):
    id = LazyFunction(lambda: next(IDS))
    name = Sequence(lambda number: f"item{number}")


@pytest_asyncio.fixture(scope="module")
async def database():
    async with hare_test_context(["tests.factories_models"]) as context:
        yield context


@pytest_asyncio.fixture
async def db(database):
    async with RollbackIsolation(database) as context:
        yield context


@pytest.mark.asyncio
async def test_create_fills_every_field(db):
    MemberFactory.reset_sequence()
    member = await MemberFactory.create()
    assert (member.name, member.email, member.is_staff) == ("member0", "member0@example.com", False)
    assert (await member.team).name == "core"
    assert await Member.objects.filter(pk=member.pk).exists()


@pytest.mark.asyncio
async def test_given_values_parameters_and_traits_win(db):
    team = await TeamFactory.create(name="given")
    member = await MemberFactory.create(team=team, domain="hare.dev", staff=True)
    assert (member.name, member.email, member.is_staff) == ("admin", "admin@hare.dev", True)
    assert member.team_id == team.id
    assert await Team.objects.count() == 1


def test_build_saves_nothing():
    member = MemberFactory.build(name="ann")
    assert member.email == "ann@example.com"
    assert member.team_id is None
    assert not member._saved_in_db


@pytest.mark.asyncio
async def test_many_to_many_links_are_made_or_given(db):
    article = await ArticleFactory.create()
    assert sorted(label.name for label in await article.labels.all()) == ["orm", "python"]
    label = await LabelFactory.create(name="given")
    article = await ArticleFactory.create(labels=[label])
    assert [linked.name for linked in await article.labels.all()] == ["given"]


@pytest.mark.asyncio
async def test_related_rows_point_at_the_object(db):
    author = await AuthorFactory.create()
    assert await Article.objects.filter(author=author).count() == 2
    author = await AuthorFactory.create(articles=3)
    assert await Article.objects.filter(author=author).count() == 3


@pytest.mark.asyncio
async def test_a_batch_without_related_objects_is_one_insert(db):
    async with capture_queries() as counter:
        items = await LabelFactory.create_batch(5)
    assert counter.count == 1
    assert len(items) == 5
    assert await Label.objects.count() == 5


@pytest.mark.asyncio
async def test_a_batch_with_related_objects_is_made_one_by_one(db):
    members = await MemberFactory.create_batch(3)
    assert len({member.email for member in members}) == 3
    assert await Team.objects.count() == 3


@pytest.mark.asyncio
async def test_composite_and_generic_keys(db):
    VersionFactory.reset_sequence()
    versions = await VersionFactory.create_batch(2)
    assert [(version.article_number, version.number) for version in versions] == [(7, 1), (7, 2)]
    note = await NoteFactory.create()
    target = await note.target
    assert isinstance(target, Version)
    team = await TeamFactory.create()
    note = await NoteFactory.create(target=team)
    assert await note.target == team


@pytest.mark.asyncio
async def test_the_tenant_comes_from_the_scope(db):
    with Tenancy.scope("msk"):
        item = await StoreItemFactory.create()
        batch = await StoreItemFactory.create_batch(2)
    assert item.store == "msk"
    assert {obj.store for obj in batch} == {"msk"}


def test_a_factory_names_its_model():
    class Nameless(ModelFactory):  # type: ignore[type-arg]
        pass

    with pytest.raises(ConfigurationError, match="ModelFactory\\[TheModel\\]"):
        Nameless.build()


def test_faker_values():
    faker_value = Faker("name")
    try:
        import faker  # noqa: F401
    except ModuleNotFoundError:
        with pytest.raises(ConfigurationError, match="pip install faker"):
            faker_value.evaluate(0)
    else:
        assert isinstance(faker_value.evaluate(0), str)
