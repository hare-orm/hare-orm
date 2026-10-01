"""RequestQuery.describe_parameters() - what each parameter is, for building a UI: the filter, its
path, lookup, value and relation, the bound it pairs with, an enum's choices, an option's names."""

import datetime
import json
import subprocess
import sys
import textwrap
from typing import Annotated

import pytest

from hare.contrib.request_query import (
    BoundSide,
    CommaSeparated,
    CursorPagination,
    DeletedConfig,
    FieldsConfig,
    Filter,
    FilterField,
    IncludeConfig,
    InPath,
    KeyColumns,
    NoFilter,
    OffsetPagination,
    OrderingConfig,
    ParameterChoice,
    ParameterType,
    RequestQuery,
    SearchConfig,
)
from hare.contrib.request_query.enums import DeletedRows
from hare.query.enums import Lookup, LookupValueShape
from hare.query.expressions import Q
from hare.query.functions import Count
from tests.contrib.request_query.models import Author, Book, BookStatus, LineReturn, Note, OrderLine


class DescribedBookQuery(RequestQuery[Book]):
    title: Annotated[str | None, Filter("title", lookup="icontains")] = None
    author: int | None = None
    author__profile__city: str | None = None
    tag_ids: Annotated[list[int] | None, Filter("tags", lookup="in")] = None
    ids: Annotated[CommaSeparated[int] | None, Filter("id", lookup="in")] = None
    status: BookStatus | None = None
    published_at__gte: datetime.datetime | None = None
    published_at__lte: datetime.datetime | None = None
    popular: bool | None = None
    mode: Annotated[str | None, NoFilter()] = None

    class Meta:
        queryset = Book.objects.all().annotate(line_count=Count("lines"))
        filters = (
            FilterField("line_count", lookups=(Lookup.GTE,)),
            FilterField("published_at", lookups=(Lookup.RANGE,), parameter="published"),
            FilterField("author__rating", lookups=(Lookup.EXACT, Lookup.IN), parameter="rating"),
        )
        search = SearchConfig(fields=("title",))
        ordering = OrderingConfig(fields=("title", "published_at"))
        fields = FieldsConfig(fields=("title", "status"))
        include = IncludeConfig(relations=("author", "tags"))
        pagination = OffsetPagination(default_limit=20)

    def filter_popular(self, value: bool) -> Q:
        return Q(line_count__gte=3) if value else Q(line_count__lt=3)


class DescribedLineReturnQuery(RequestQuery[LineReturn]):
    line: KeyColumns[int, int] | None = None
    line__quantity__gte: int | None = None

    class Meta:
        queryset = LineReturn.objects.all()
        pagination = None


class DescribedOrderLineQuery(RequestQuery[OrderLine]):
    pk: Annotated[KeyColumns[int, int], InPath()]

    class Meta:
        queryset = OrderLine.objects.all()
        ordering = OrderingConfig(default=("order_id",))
        pagination = CursorPagination(default_limit=5)


class DescribedNoteQuery(RequestQuery[Note]):
    class Meta:
        queryset = Note.objects.all()
        deleted = DeletedConfig()
        pagination = None


def describe(request_query_class: type[RequestQuery]) -> dict:
    return {description.name: description for description in request_query_class.describe_parameters()}


@pytest.mark.asyncio
async def test_filters_are_described_with_their_key_path_lookup_and_value(db_request_query):
    descriptions = describe(DescribedBookQuery)
    title = descriptions["title"]
    assert (title.parameter_type, title.filter_key, title.path, title.lookup) == (
        ParameterType.FILTER,
        "title__icontains",
        "title",
        Lookup.ICONTAINS,
    )
    assert (title.value_shape, title.value_type, title.relation, title.nullable) == (
        LookupValueShape.VALUE,
        str,
        None,
        False,
    )
    published_at = descriptions["published_at__gte"]
    assert (published_at.path, published_at.nullable) == ("published_at", True)
    assert published_at.description == "When the book came out"
    line_count = descriptions["line_count__gte"]
    assert (line_count.parameter_type, line_count.filter_key, line_count.value_type) == (
        ParameterType.FILTER,
        "line_count__gte",
        int,
    )


@pytest.mark.asyncio
async def test_aliases_methods_and_parameters_without_a_filter(db_request_query):
    descriptions = describe(DescribedBookQuery)
    assert (descriptions["tag_ids"].filter_key, descriptions["tag_ids"].takes_many_values) == ("tags__in", True)
    assert (descriptions["ids"].filter_key, descriptions["ids"].takes_many_values) == ("id__in", True)
    rating = descriptions["rating"]
    assert (rating.filter_key, rating.path, rating.nullable) == ("author__rating", "author__rating", True)
    assert descriptions["rating__in"].value_shape is LookupValueShape.LIST
    popular = descriptions["popular"]
    assert (popular.parameter_type, popular.filter_key, popular.relation) == (ParameterType.FILTER_METHOD, None, None)
    assert descriptions["mode"].parameter_type is ParameterType.NO_FILTER


@pytest.mark.asyncio
async def test_relations_and_their_keys(db_request_query):
    descriptions = describe(DescribedBookQuery)
    author = descriptions["author"].relation
    assert (author.path, author.model, author.key_fields, author.to_many, author.compares_key) == (
        "author",
        Author,
        ("id",),
        False,
        True,
    )
    city = descriptions["author__profile__city"].relation
    assert (city.path, city.compares_key, city.to_many) == ("author__profile", False, False)
    assert descriptions["author__profile__city"].nullable is True
    tags = descriptions["tag_ids"].relation
    assert (tags.path, tags.to_many, tags.compares_key) == ("tags", True, True)
    assert descriptions["tag_ids"].nullable is True

    composite = describe(DescribedLineReturnQuery)
    line = composite["line"]
    assert (line.relation.model, line.relation.key_fields, line.relation.compares_key) == (
        OrderLine,
        ("order_id", "line_no"),
        True,
    )
    assert line.value_type == (int, int)
    assert composite["line__quantity__gte"].relation.compares_key is False


@pytest.mark.asyncio
async def test_an_enum_gives_its_choices(db_request_query):
    status = describe(DescribedBookQuery)["status"]
    assert status.choices == tuple(ParameterChoice(value=member.value, label=member.name) for member in BookStatus)
    deleted = describe(DescribedNoteQuery)["deleted"]
    assert deleted.parameter_type is ParameterType.DELETED
    assert deleted.allowed_values == tuple(DeletedRows)
    assert deleted.choices == tuple(ParameterChoice(value=member.value, label=member.name) for member in DeletedRows)


@pytest.mark.asyncio
async def test_bounds_pair_with_each_other(db_request_query):
    descriptions = describe(DescribedBookQuery)
    lower = descriptions["published_at__gte"]
    upper = descriptions["published_at__lte"]
    assert (lower.bound, lower.paired_parameters) == (BoundSide.LOWER, ("published_at__lte",))
    assert (upper.bound, upper.paired_parameters) == (BoundSide.UPPER, ("published_at__gte",))
    published = descriptions["published__range"]
    assert (published.bound, published.paired_parameters, published.value_shape) == (
        BoundSide.RANGE,
        (),
        LookupValueShape.RANGE,
    )
    assert descriptions["title"].bound is None


@pytest.mark.asyncio
async def test_options_give_what_a_request_may_choose(db_request_query):
    descriptions = describe(DescribedBookQuery)
    assert descriptions["search"].parameter_type is ParameterType.SEARCH
    ordering = descriptions["ordering"]
    assert (ordering.parameter_type, ordering.allowed_values) == (ParameterType.ORDERING, ("title", "published_at"))
    assert (descriptions["fields"].parameter_type, descriptions["fields"].allowed_values) == (
        ParameterType.FIELDS,
        ("title", "status"),
    )
    assert (descriptions["include"].parameter_type, descriptions["include"].allowed_values) == (
        ParameterType.INCLUDE,
        ("author", "tags"),
    )
    limit = descriptions["limit"]
    assert (limit.parameter_type, limit.default, limit.required) == (ParameterType.LIMIT, 20, False)
    assert (descriptions["offset"].parameter_type, descriptions["offset"].default) == (ParameterType.OFFSET, 0)
    assert list(descriptions) == list(DescribedBookQuery.model_fields)

    cursor_descriptions = describe(DescribedOrderLineQuery)
    assert cursor_descriptions["cursor"].parameter_type is ParameterType.CURSOR
    key = cursor_descriptions["pk"]
    assert (key.in_path, key.required, key.value_type) == (True, True, (int, int))


DESCRIBE_WITHOUT_CONNECTIONS_SCRIPT = textwrap.dedent(
    """
    import json
    from hare import Hare, HareConfig
    from hare.contrib.request_query import FilterField, PostgresqlRequestQuery, RequestQuery
    from hare.core.context import HareContext

    Hare.bind_models(
        HareConfig.from_db_url(
            "postgresql://user:password@127.0.0.1:1/unused", {"models": ["tests.contrib.request_query.models"]}
        )
    )
    from tests.contrib.request_query.models import Book

    result = {"context": HareContext.get_current() is not None}
    for base in (RequestQuery, PostgresqlRequestQuery):
        query_class = base.for_model(Book, filters=(FilterField("status"), FilterField("author__name")))
        result[base.__name__] = [
            [description.name, str(description.parameter_type), description.filter_key]
            for description in query_class.describe_parameters()
        ]
    print(json.dumps(result))
    """
)


def test_parameters_are_described_without_connections():
    completed = subprocess.run(
        [sys.executable, "-c", DESCRIBE_WITHOUT_CONNECTIONS_SCRIPT], capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    expected = [
        ["status", "filter", "status"],
        ["author__name", "filter", "author__name"],
        ["limit", "limit", None],
        ["offset", "offset", None],
    ]
    assert result == {"context": False, "RequestQuery": expected, "PostgresqlRequestQuery": expected}


@pytest.mark.asyncio
async def test_a_registration_while_the_declaration_is_built_does_not_drop_the_filters(db_request_query, monkeypatch):
    """A registration while the declaration is built (a driver or dialect loaded on first use)
    forgot the half-prepared class - its filter parameters were put back - and the declaration
    built from it then described no filter; the class is built again now."""
    from hare.contrib.request_query.declaration import DeclarationBuilder
    from hare.core.registries import Registries

    original_build = DeclarationBuilder.build
    registrations = []
    forgotten = []

    def build_with_a_registration(self):
        if not registrations:
            registrations.append(True)
            Registries.changed()
            forgotten.append(self.request_query_class not in RequestQuery.prepared_classes)
        return original_build(self)

    monkeypatch.setattr(DeclarationBuilder, "build", build_with_a_registration)
    query_class = RequestQuery.for_model(Book, filters=(FilterField("status"), FilterField("author__name")))
    names = [description.name for description in query_class.describe_parameters()]
    assert registrations == [True]
    assert forgotten == [True]
    assert names[:2] == ["status", "author__name"]
