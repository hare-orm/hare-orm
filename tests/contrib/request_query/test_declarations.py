"""A request query's declaration is checked against the ORM's description of its filters: the
path, the lookup, the value's shape and type, the dialects and the options."""

from typing import Annotated

import pytest

from hare.contrib.request_query import (
    CursorPagination,
    Filter,
    KeyColumns,
    NoFilter,
    OffsetPagination,
    OrderingConfig,
    RequestQuery,
    SearchConfig,
)
from hare.contrib.test import requires_features
from hare.dialects.postgresql.request_query import PostgresqlRequestQuery
from hare.dialects.sqlite.request_query import SqliteRequestQuery
from hare.exceptions import ConfigurationError
from hare.query.expressions import Q
from hare.query.functions import Count
from tests.contrib.request_query.models import Author, Book, OrderLine


def declaration_error(request_query_class: type[RequestQuery]) -> str:
    with pytest.raises(ConfigurationError) as error:
        request_query_class.get_declaration()
    return str(error.value)


@pytest.mark.asyncio
async def test_a_parameter_naming_no_filter_of_the_model(library):
    class UnknownFieldQuery(RequestQuery[Book]):
        headline: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class UnknownLookupQuery(RequestQuery[Book]):
        title__nearly: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class UnknownRelatedFieldQuery(RequestQuery[Book]):
        name: Annotated[str | None, Filter("author__nme")] = None

        class Meta:
            queryset = Book.objects.all()

    assert "UnknownFieldQuery.headline" in declaration_error(UnknownFieldQuery)
    assert "UnknownLookupQuery.title__nearly" in declaration_error(UnknownLookupQuery)
    message = declaration_error(UnknownRelatedFieldQuery)
    assert "UnknownRelatedFieldQuery.name" in message and "nme" in message


@pytest.mark.asyncio
async def test_the_annotation_must_take_the_value_the_filter_takes(library):
    class WrongTypeQuery(RequestQuery[Book]):
        author: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class ValueForListQuery(RequestQuery[Book]):
        id__in: int | None = None

        class Meta:
            queryset = Book.objects.all()

    class NotBoolQuery(RequestQuery[Book]):
        published_at__isnull: int | None = None

        class Meta:
            queryset = Book.objects.all()

    class ScalarForCompositeKeyQuery(RequestQuery[OrderLine]):
        pk: int | None = None

        class Meta:
            queryset = OrderLine.objects.all()

    class ShortCompositeKeyQuery(RequestQuery[OrderLine]):
        pk: KeyColumns[int] | None = None

        class Meta:
            queryset = OrderLine.objects.all()

    assert "takes int, but the parameter is annotated" in declaration_error(WrongTypeQuery)
    assert "takes list[int]" in declaration_error(ValueForListQuery)
    assert "takes bool" in declaration_error(NotBoolQuery)
    assert "takes tuple[int, int]" in declaration_error(ScalarForCompositeKeyQuery)
    assert "takes tuple[int, int]" in declaration_error(ShortCompositeKeyQuery)


@pytest.mark.asyncio
async def test_annotations_that_take_the_filters_value(library):
    class AcceptedQuery(RequestQuery[Book]):
        id__in: set[int] | None = None
        id__range: tuple[int, int] | None = None
        author__in: tuple[int, ...] | None = None
        title: str | None = None
        published_at__year__in: list[int] | None = None

        class Meta:
            queryset = Book.objects.all()

    declaration = AcceptedQuery.get_declaration()
    assert [item.filter_key for item in declaration.filters] == [
        "id__in",
        "id__range",
        "author__in",
        "title",
        "published_at__year__in",
    ]
    assert sorted(book.title for book in await AcceptedQuery(id__range=(2, 3)).fetch()) == ["Beta", "Gamma"]


@pytest.mark.asyncio
async def test_a_lookup_the_project_registered_brings_its_own_value(library):
    class ScoreQuery(RequestQuery[Author]):
        score__within: tuple[int, int] | None = None

        class Meta:
            queryset = Author.objects.all()
            ordering = OrderingConfig(default=("name",))

    class WrongScoreQuery(RequestQuery[Author]):
        score__within: int | None = None

        class Meta:
            queryset = Author.objects.all()

    authors = await ScoreQuery(score__within=(40, 95)).fetch()
    assert [author.name for author in authors] == ["Anna", "Boris"]
    assert "takes tuple[int, int]" in declaration_error(WrongScoreQuery)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_filter_must_run_on_every_dialect_the_class_serves(library):
    class FullTextQuery(RequestQuery[Book]):
        title__search: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class PostgresqlOnlyLookupQuery(RequestQuery[Author]):
        score__within_on_postgresql: tuple[int, int] | None = None

        class Meta:
            queryset = Author.objects.all()

    class TrigramOnSqliteQuery(SqliteRequestQuery[Book]):
        title__trigram_similar: str | None = None

        class Meta:
            queryset = Book.objects.all()

    assert "doesn't run on sqlite" in declaration_error(FullTextQuery)
    assert "doesn't run on sqlite" in declaration_error(PostgresqlOnlyLookupQuery)
    assert "doesn't run on sqlite" in declaration_error(TrigramOnSqliteQuery)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_dialects_request_query_needs_a_connection_of_that_dialect(library):
    class PostgresqlBookQuery(PostgresqlRequestQuery[Book]):
        title__trigram_similar: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class SqliteBookQuery(SqliteRequestQuery[Book]):
        title__icontains: str | None = None

        class Meta:
            queryset = Book.objects.all()

    assert "is a postgresql request query" in declaration_error(PostgresqlBookQuery)
    assert await SqliteBookQuery(title__icontains="alp").count() == 1


@pytest.mark.asyncio
async def test_search_fields_must_take_text(library):
    class DateSearchQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            search = SearchConfig(fields=("published_at",), lookup="gte")

    class UnknownSearchFieldQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            search = SearchConfig(fields=("title", "summary"))

    assert "not text" in declaration_error(DateSearchQuery)
    assert "summary" in declaration_error(UnknownSearchFieldQuery)


@pytest.mark.asyncio
async def test_orderings_are_checked(library):
    class ToManyOrderingQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            ordering = OrderingConfig(fields=("tags__name",))

    class UnknownOrderingQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            ordering = OrderingConfig(fields=("title",), default=("-rank",))

    class AnnotationCursorQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.annotate(tag_count=Count("tags"))
            ordering = OrderingConfig(fields=("tag_count",))
            pagination = CursorPagination()

    class AnnotationOffsetQuery(RequestQuery[Book]):
        tag_count__gte: int | None = None

        class Meta:
            queryset = Book.objects.annotate(tag_count=Count("tags"))
            ordering = OrderingConfig(fields=("tag_count",))
            pagination = OffsetPagination()

    assert "crosses a relation to many rows" in declaration_error(ToManyOrderingQuery)
    assert "rank" in declaration_error(UnknownOrderingQuery)
    assert "can't order by the annotation" in declaration_error(AnnotationCursorQuery)
    page = await AnnotationOffsetQuery(tag_count__gte=1, ordering="-tag_count").page()
    assert [book.title for book in page.result] == ["Gamma", "Alpha", "Beta", "Epsilon"]


@pytest.mark.asyncio
async def test_a_class_without_a_queryset_only_serves_as_a_base(library):
    class BaseBookQuery(RequestQuery[Book]):
        author: int | None = None

    class ConcreteBookQuery(BaseBookQuery):
        class Meta:
            queryset = Book.objects.all()

    assert "declares no Meta.queryset" in declaration_error(BaseBookQuery)
    assert ConcreteBookQuery in BaseBookQuery.get_concrete_subclasses()
    assert BaseBookQuery not in RequestQuery.get_concrete_subclasses()
    assert sorted(book.title for book in await ConcreteBookQuery(author=1).fetch()) == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_meta_options_are_checked(library):
    class NotQuerysetQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book

    class WrongJoinQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            join_type = "XOR"

    class WrongPaginationQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            pagination = 50

    assert "must be a QuerySet" in declaration_error(NotQuerysetQuery)
    assert "join_type" in declaration_error(WrongJoinQuery)
    assert "Meta.pagination" in declaration_error(WrongPaginationQuery)


@pytest.mark.asyncio
async def test_parameter_markers_are_checked(library):
    with pytest.warns(UserWarning, match="shadows an attribute"):

        class RequestParameterQuery(RequestQuery[Book]):
            request: str | None = None

            class Meta:
                queryset = Book.objects.all()

    class MarkerAndMethodQuery(RequestQuery[Book]):
        author_name: Annotated[str | None, Filter("author__name")] = None

        class Meta:
            queryset = Book.objects.all()

        def filter_author_name(self, value: str) -> Q:
            return Q(author__name=value)

    class NoFilterAndMethodQuery(RequestQuery[Book]):
        mode: Annotated[str | None, NoFilter()] = None

        class Meta:
            queryset = Book.objects.all()

        def filter_mode(self, value: str) -> Q:
            return Q()

    class TwoMarkersQuery(RequestQuery[Book]):
        title: Annotated[str | None, Filter("title"), NoFilter()] = None

        class Meta:
            queryset = Book.objects.all()

    assert "named 'request'" in declaration_error(RequestParameterQuery)
    assert "both a Filter marker and filter_author_name()" in declaration_error(MarkerAndMethodQuery)
    assert "marked NoFilter but has filter_mode()" in declaration_error(NoFilterAndMethodQuery)
    assert "more than one Filter/NoFilter marker" in declaration_error(TwoMarkersQuery)


@pytest.mark.asyncio
async def test_check_declarations_lists_every_wrong_class(library):
    class CheckedBase(RequestQuery[Book]):
        pass

    class RightQuery(CheckedBase):
        title: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class FirstWrongQuery(CheckedBase):
        headline: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class SecondWrongQuery(CheckedBase):
        author: str | None = None

        class Meta:
            queryset = Book.objects.all()

    with pytest.raises(ConfigurationError) as error:
        CheckedBase.check_declarations()
    message = str(error.value)
    assert "FirstWrongQuery.headline" in message and "SecondWrongQuery.author" in message
    assert "RightQuery" not in message
    assert RightQuery.get_declaration().filters[0].filter_key == "title"


def test_options_check_their_values():
    with pytest.raises(ConfigurationError, match="default_limit <= max_limit"):
        OffsetPagination(default_limit=0)
    with pytest.raises(ConfigurationError, match="default_limit <= max_limit"):
        OffsetPagination(default_limit=50, max_limit=10)
    with pytest.raises(ConfigurationError, match="max_limit <= 100000"):
        CursorPagination(max_limit=1_000_000)
    with pytest.raises(ConfigurationError, match="must be an int"):
        OffsetPagination(default_limit=True)
    with pytest.raises(ConfigurationError, match="must differ"):
        OffsetPagination(limit_parameter="page", offset_parameter="page")
    with pytest.raises(ConfigurationError, match="identifiers"):
        CursorPagination(cursor_parameter="next page")
    with pytest.raises(ConfigurationError, match="without a direction"):
        OrderingConfig(fields=("-title",))
    with pytest.raises(ConfigurationError, match="non-empty tuple"):
        SearchConfig(fields=())
    with pytest.raises(ConfigurationError, match="join_type"):
        SearchConfig(fields=("title",), join_type="ANY")
    with pytest.raises(ConfigurationError, match="non-empty string"):
        Filter("")


def test_options_add_their_parameters():
    class ParametersQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            search = SearchConfig(fields=("title",), parameter="q")
            ordering = OrderingConfig(fields=("title",), parameter="sort")
            pagination = CursorPagination(limit_parameter="size", cursor_parameter="after")

    class InheritedParametersQuery(ParametersQuery):
        title: str | None = None

    assert list(ParametersQuery.model_fields) == ["q", "sort", "size", "after"]
    assert list(InheritedParametersQuery.model_fields) == ["title", "q", "sort", "size", "after"]
    assert ParametersQuery.model_fields["size"].default == 100
