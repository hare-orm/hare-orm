"""RequestQuery.for_model() - a request query class built while the application runs: checked at
once, of a dialect when called on a dialect's request query, held weakly, and following every
registration made after Hare.init()."""

import gc
import weakref

import pytest

from hare.contrib.request_query import (
    FilterField,
    OrderingConfig,
    PostgresqlRequestQuery,
    RequestQuery,
    SearchConfig,
    SqliteRequestQuery,
)
from hare.core.registries import Registries
from hare.exceptions import ConfigurationError
from hare.fields import CharField
from hare.query.enums import Connector, Lookup
from hare.query.expressions import Q
from hare.query.filters import FieldLookup
from hare.sql.terms import Term
from tests.contrib.request_query.models import Author, Book


@pytest.mark.asyncio
async def test_a_built_class_queries_like_a_declared_one(library):
    book_query = RequestQuery.for_model(
        Book,
        filters=(FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)), FilterField("author__name")),
        search=SearchConfig(fields=("title",)),
        ordering=OrderingConfig(fields=("title",), default=("title",)),
    )
    assert book_query.__name__ == "BookRequestQuery"
    assert issubclass(book_query, RequestQuery)
    page = await book_query.from_query_string("author__name=Anna&ordering=-title").page()
    assert [book.title for book in page.result] == ["Beta", "Alpha"]
    either_query = RequestQuery.for_model(
        Book,
        name="EitherBookQuery",
        queryset=Book.objects.filter(author__name="Anna"),
        filters=(FilterField("title"), FilterField("status")),
        join_type=Connector.OR,
        pagination=None,
    )
    assert either_query.__name__ == "EitherBookQuery"
    titles = [book.title for book in await either_query.from_query_string("title=Alpha&status=draft").fetch()]
    expected = await Book.objects.filter(Q(title="Alpha") | Q(status="draft"), author__name="Anna").values_list(
        "title", flat=True
    )
    assert sorted(titles) == sorted(expected)
    assert len(expected) > 1


@pytest.mark.asyncio
async def test_a_wrong_class_is_refused_when_it_is_built(db_request_query):
    with pytest.raises(ConfigurationError, match="has no field 'missing'"):
        RequestQuery.for_model(Book, filters=(FilterField("missing"),))
    with pytest.raises(ConfigurationError, match="takes a model class"):
        RequestQuery.for_model("Book")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="takes a queryset of Book"):
        RequestQuery.for_model(Book, queryset=Author.objects.all())  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="name must be an identifier"):
        RequestQuery.for_model(Book, name="not a name")


@pytest.mark.asyncio
async def test_a_dialects_request_query_builds_a_class_of_its_dialect(db_request_query):
    dialect_name = Book.get_connection().dialect.name
    for base in (PostgresqlRequestQuery, SqliteRequestQuery):
        if base.dialect_name == dialect_name:
            own_query = base.for_model(Book, filters=(FilterField("title"),))
            assert issubclass(own_query, base)
            assert own_query.dialect_name == dialect_name
        else:
            with pytest.raises(ConfigurationError, match=f"is on a {dialect_name} connection"):
                base.for_model(Book, filters=(FilterField("title"),))


@pytest.mark.asyncio
async def test_a_built_class_nothing_holds_is_gone(db_request_query):
    book_query = RequestQuery.for_model(Book, name="ForgottenBookQuery", filters=(FilterField("title"),))
    book_query.describe_parameters()
    reference = weakref.ref(book_query)
    del book_query
    gc.collect()
    assert reference() is None
    assert all(item.__name__ != "ForgottenBookQuery" for item in RequestQuery.get_concrete_subclasses())
    assert all(item.__name__ != "ForgottenBookQuery" for item in RequestQuery.prepared_classes)
    assert all(item.__name__ != "ForgottenBookQuery" for item in RequestQuery.declarations.owners)


def reversed_lookup(field: CharField | None) -> FieldLookup:
    def operator(term: Term, value: str) -> Term:
        return term == value[::-1]

    return FieldLookup(operator)


@pytest.mark.asyncio
async def test_a_lookup_registered_after_init_is_used_on_the_next_query(library):
    name_query = RequestQuery.for_model(Author, filters=(FilterField("name"),))
    name_query.get_declaration()
    assert RequestQuery.declarations.get_owner_bucket(name_query)

    class ReversedNameQuery(RequestQuery[Author]):
        class Meta:
            queryset = Author.objects.all()
            filters = (FilterField("name", lookups=("rq_reversed",)),)

    with pytest.raises(ConfigurationError, match="rq_reversed"):
        ReversedNameQuery.get_declaration()
    CharField.register_lookup("rq_reversed", reversed_lookup)
    try:
        assert not RequestQuery.declarations.get_owner_bucket(name_query)
        rows = await ReversedNameQuery.from_query_string("name__rq_reversed=annA").fetch()
        assert [row.name for row in rows] == ["Anna"]
        (description,) = [item for item in ReversedNameQuery.describe_parameters() if item.name == "name__rq_reversed"]
        assert (description.lookup, description.path) == ("rq_reversed", "name")
    finally:
        del CharField.registered_lookups["rq_reversed"]
        Registries.changed()
    with pytest.raises(ConfigurationError, match="rq_reversed"):
        ReversedNameQuery.get_declaration()
