"""Meta.filters: the parameters of the listed fields and lookups, typed and described by the ORM."""

import datetime
from typing import Annotated, get_args

import pytest

from hare.contrib.request_query import Filter, FilterField, InvalidRequestQuery, RequestQuery, SearchConfig
from hare.exceptions import ConfigurationError
from hare.models.tenancy import Tenancy
from hare.query.enums import Lookup
from hare.query.expressions.raw_sql import RawSQL
from hare.query.functions import Count
from tests.contrib.request_query.models import Book, BookStatus, OrderLine, Ticket, TicketPriority


class FilteredBookQuery(RequestQuery[Book]):
    author_ids: Annotated[list[int] | None, Filter("author", lookup="in")] = None

    class Meta:
        queryset = Book.objects.all()
        filters = (
            FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("author", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("author__name", lookups=(Lookup.ICONTAINS,)),
            FilterField("published_at", lookups=(Lookup.GTE, Lookup.LTE, Lookup.RANGE)),
            FilterField("author__profile", lookups=(Lookup.ISNULL,)),
            FilterField("tags", lookups=(Lookup.IN,)),
        )
        pagination = None


class FilteredBookSubQuery(FilteredBookQuery):
    title__icontains: str | None = None


class FilteredLineQuery(RequestQuery[OrderLine]):
    class Meta:
        queryset = OrderLine.objects.all()
        filters = (
            FilterField("pk", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("book", lookups=(Lookup.EXACT,)),
        )
        pagination = None


class FilteredTicketQuery(RequestQuery[Ticket]):
    class Meta:
        queryset = Ticket.objects.all()
        filters = (
            FilterField("priority", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("subject", lookups=(Lookup.ICONTAINS,)),
        )
        pagination = None


def annotation_of(request_query_class: type[RequestQuery], parameter: str):
    request_query_class.prepare_parameters()
    return request_query_class.model_fields[parameter].annotation


def titles(books: list[Book]) -> list[str]:
    return sorted(book.title for book in books)


@pytest.mark.asyncio
async def test_each_lookup_is_a_parameter_typed_by_the_orm(library):
    assert annotation_of(FilteredBookQuery, "status") == str | None
    assert annotation_of(FilteredBookQuery, "status__in") == list[str] | None
    assert annotation_of(FilteredBookQuery, "author") == int | None
    assert annotation_of(FilteredBookQuery, "author__in") == list[int] | None
    assert annotation_of(FilteredBookQuery, "author__name__icontains") == str | None
    assert annotation_of(FilteredBookQuery, "published_at__gte") == datetime.datetime | None
    assert (
        annotation_of(FilteredBookQuery, "published_at__range") == tuple[datetime.datetime, datetime.datetime] | None
    )
    assert annotation_of(FilteredBookQuery, "author__profile__isnull") == bool | None
    assert annotation_of(FilteredBookQuery, "tags__in") == list[int] | None


@pytest.mark.asyncio
async def test_the_parameters_filter(library):
    assert titles(await FilteredBookQuery(status=BookStatus.DRAFT).fetch()) == ["Beta", "Epsilon"]
    query = FilteredBookQuery.from_query_string("author__in=1&author__in=3&author__name__icontains=ann")
    assert titles(await query.fetch()) == ["Alpha", "Beta"]
    since = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert titles(await FilteredBookQuery(published_at__gte=since).fetch()) == ["Alpha", "Beta", "Epsilon"]
    assert titles(await FilteredBookQuery(author__profile__isnull=True).fetch()) == ["Epsilon"]
    assert titles(await FilteredBookQuery.from_query_string("tags__in=2&author_ids=1").fetch()) == ["Beta"]
    with pytest.raises(InvalidRequestQuery):
        FilteredBookQuery(author="anna")


@pytest.mark.asyncio
async def test_a_subclass_gets_the_parameters_of_its_filters(library):
    assert annotation_of(FilteredBookSubQuery, "status__in") == list[str] | None
    query = FilteredBookSubQuery(status=BookStatus.PUBLISHED, title__icontains="a")
    assert titles(await query.fetch()) == ["Alpha", "Delta", "Gamma"]


@pytest.mark.asyncio
async def test_a_composite_key_and_an_enum(library):
    assert get_args(get_args(annotation_of(FilteredLineQuery, "pk"))[0])[0] == tuple[int, int]
    lines = await FilteredLineQuery.from_query_string("pk__in=1,2&pk__in=3,1").fetch()
    assert sorted(line.pk for line in lines) == [(1, 2), (3, 1)]
    assert annotation_of(FilteredTicketQuery, "priority") == TicketPriority | None
    assert annotation_of(FilteredTicketQuery, "priority__in") == list[TicketPriority] | None
    assert annotation_of(FilteredTicketQuery, "subject__icontains") == str | None
    await Ticket.objects.create(id=1, company_id=1, subject="printer", priority=TicketPriority.HIGH)
    await Ticket.objects.create(id=2, company_id=1, subject="mail")
    with Tenancy.scope(1):
        assert [ticket.id for ticket in await FilteredTicketQuery.from_query_string("priority=2").fetch()] == [1]
    with pytest.raises(InvalidRequestQuery):
        FilteredTicketQuery(priority=3)


@pytest.mark.asyncio
async def test_parameters_are_described_by_the_fields(library):
    FilteredBookQuery.prepare_parameters()
    fields = FilteredBookQuery.model_fields
    assert fields["published_at__gte"].description == "When the book came out"
    assert fields["published_at__range"].description == (
        "When the book came out Two values, from and to: repeat the parameter."
    )
    assert fields["status__in"].description == "Repeat the parameter for each value."
    assert fields["author_ids"].description == "Repeat the parameter for each value."
    assert fields["status"].description is None
    assert "Repeat the parameter" in FilteredBookQuery.model_json_schema()["properties"]["tags__in"]["description"]


@pytest.mark.asyncio
async def test_the_declaration_checks_the_parameters(library):
    declaration = FilteredBookQuery.get_declaration()
    filter_keys = {
        filter_declaration.parameter: filter_declaration.filter_key for filter_declaration in declaration.filters
    }
    assert filter_keys["status__in"] == "status__in"
    assert filter_keys["author_ids"] == "author__in"


@pytest.mark.asyncio
async def test_wrong_filters_are_refused(library):
    def preparing_error(request_query_class: type[RequestQuery]) -> str:
        with pytest.raises(ConfigurationError) as error:
            request_query_class.prepare_parameters()
        return str(error.value)

    def query_filtering_by(meta_filters) -> type[RequestQuery]:
        class WrongQuery(RequestQuery[Book]):
            class Meta:
                queryset = Book.objects.all()
                filters = meta_filters
                pagination = None

        return WrongQuery

    class DeclaringQuery(RequestQuery[Book]):
        status: str | None = None

        class Meta:
            queryset = Book.objects.all()
            filters = (FilterField("status", lookups=(Lookup.EXACT,)),)
            pagination = None

    class SearchingQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            filters = (FilterField("search", lookups=(Lookup.EXACT,)),)
            search = SearchConfig(fields=("title",))
            pagination = None

    assert "must be a tuple of FilterField" in preparing_error(query_filtering_by({"status": ["exact"]}))
    assert "must be a tuple of FilterField" in preparing_error(query_filtering_by(("status",)))
    twice = (FilterField("status", lookups=(Lookup.IN,)), FilterField("status", lookups=(Lookup.IN,)))
    assert "twice" in preparing_error(query_filtering_by(twice))
    missing = (FilterField("missing", lookups=(Lookup.EXACT,)),)
    assert "Meta.filters" in preparing_error(query_filtering_by(missing))
    assert "already has" in preparing_error(DeclaringQuery)
    assert "already has" in preparing_error(SearchingQuery)


@pytest.mark.asyncio
async def test_annotations_by_their_type_and_an_untyped_one_refused(library):
    class TaggedBookQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.annotate(tag_count=Count("tags"), raw_rank=RawSQL("1"))
            filters = (FilterField("tag_count", lookups=(Lookup.GTE,)),)
            pagination = None

    class RankedBookQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.annotate(raw_rank=RawSQL("1"))
            filters = (FilterField("raw_rank", lookups=(Lookup.EXACT,)),)
            pagination = None

    assert annotation_of(TaggedBookQuery, "tag_count__gte") == int | None
    assert titles(await TaggedBookQuery(tag_count__gte=2).fetch()) == ["Gamma"]
    with pytest.raises(ConfigurationError, match="declare its parameter with the type it takes"):
        RankedBookQuery.prepare_parameters()


def test_a_filter_field_is_checked_when_declared():
    with pytest.raises(ConfigurationError, match="field path"):
        FilterField("author.name")
    with pytest.raises(ConfigurationError, match="non-empty tuple of lookups"):
        FilterField("status", lookups=())
    with pytest.raises(ConfigurationError, match="non-empty tuple of lookups"):
        FilterField("status", lookups=["in"])
    with pytest.raises(ConfigurationError, match="twice"):
        FilterField("status", lookups=(Lookup.EXACT, "exact"))
    with pytest.raises(ConfigurationError, match="parameter must be an identifier"):
        FilterField("status", parameter="the status")
    with pytest.raises(ConfigurationError, match="description must be text"):
        FilterField("status", description=1)
    assert FilterField("status").get_filter_keys() == {"status": "status"}
    assert FilterField("author__profile__city", lookups=(Lookup.EXACT, "in"), parameter="city").get_filter_keys() == {
        "city": "author__profile__city",
        "city__in": "author__profile__city__in",
    }


class CityBookQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        filters = (
            FilterField("author__profile__city", lookups=(Lookup.EXACT, Lookup.IN), parameter="city"),
            FilterField("published_at", lookups=(Lookup.GTE, Lookup.LTE), parameter="published", description="Out"),
        )
        pagination = None


@pytest.mark.asyncio
async def test_a_parameter_named_otherwise_filters_by_its_path(library):
    assert annotation_of(CityBookQuery, "city__in") == list[str] | None
    assert Filter("author__profile__city__in") in CityBookQuery.model_fields["city__in"].metadata
    assert titles(await CityBookQuery(city="Moscow").fetch()) == ["Alpha", "Beta"]
    assert titles(await CityBookQuery.from_query_string("city__in=Kazan&city__in=Moscow").fetch()) == [
        "Alpha",
        "Beta",
        "Delta",
        "Gamma",
    ]
    filter_keys = {item.parameter: item.filter_key for item in CityBookQuery.get_declaration().filters}
    assert filter_keys["city"] == "author__profile__city"
    assert filter_keys["published__gte"] == "published_at__gte"
    assert CityBookQuery.model_fields["published__gte"].description == "Out"
    with pytest.raises(InvalidRequestQuery):
        CityBookQuery(published__gte=datetime.datetime(2026, 2, 1), published__lte=datetime.datetime(2026, 1, 1))
