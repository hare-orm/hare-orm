"""A request query's declaration is checked against the ORM's description of its filters: the
path, the lookup, the value's shape and type, the dialects and the options."""

from types import SimpleNamespace
from typing import Annotated

import pytest

from hare.contrib.request_query import (
    CursorPagination,
    Filter,
    FilterField,
    KeyColumns,
    NoFilter,
    OffsetPagination,
    OrderingConfig,
    RequestQuery,
    SearchConfig,
)
from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping
from hare.contrib.test import requires_features
from hare.dialects.postgresql.request_query import PostgresqlRequestQuery
from hare.dialects.sqlite.request_query import SqliteRequestQuery
from hare.exceptions import ConfigurationError
from hare.query.expressions import Q
from hare.query.functions import Count
from tests.contrib.request_query.models import Author, Book, Edition, OrderLine


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

    class JsonPathCursorQuery(RequestQuery[Edition]):
        class Meta:
            queryset = Edition.objects.all()
            ordering = OrderingConfig(fields=("details__pages",))
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
    assert "not a path inside one" in declaration_error(JsonPathCursorQuery)
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


@pytest.mark.database_independent
def test_options_check_their_values():
    with pytest.raises(ConfigurationError, match="default_limit <= max_limit"):
        OffsetPagination(default_limit=0)
    with pytest.raises(ConfigurationError, match="default_limit <= max_limit"):
        OffsetPagination(default_limit=50, max_limit=10)
    with pytest.raises(ConfigurationError, match="max_limit <= 100000"):
        CursorPagination(max_limit=1_000_000)
    with pytest.raises(ConfigurationError, match="must be an int"):
        OffsetPagination(default_limit=True)
    with pytest.raises(ConfigurationError, match="without a direction"):
        OrderingConfig(fields=("-title",))


@pytest.mark.database_independent
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


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"pagination": "offset"}, r"Meta\.pagination requires .*get_page\(\)"),
        ({"ordering": ("title",)}, r"Meta\.ordering requires .*fields, default"),
        ({"filters": ("title",)}, r"Meta\.filters item requires get_filter_keys\(\), description"),
        ({"fields": ["title"]}, r"Meta\.fields requires .*apply\(\), fields"),
        ({"queryset": 5}, r"Meta\.queryset requires model"),
        ({"join_type": None}, r"Meta\.join_type takes AND or OR"),
        ({"filters": 5}, r"Meta\.filters must be iterable"),
        ({"filters": [SimpleNamespace(get_filter_keys=None, description=None)]}, r"requires get_filter_keys\(\)$"),
        ({"api_getter": ["title"]}, r"Meta\.api_getter items must be ApiSource or ApiValueMapping"),
    ],
    ids=[
        "pagination",
        "ordering",
        "filters",
        "fields",
        "queryset",
        "join_type",
        "not-iterable",
        "not-callable",
        "api-item",
    ],
)
@pytest.mark.asyncio
async def test_a_meta_option_missing_required_structure_is_refused(library, options, message):
    """It failed far from the declaration with a bare AttributeError of the value."""
    with pytest.raises(ConfigurationError, match=message):
        meta = type("Meta", (), {"queryset": Book.objects.all(), "__qualname__": "WrongOptionQuery.Meta", **options})
        query_class = type(
            "WrongOptionQuery",
            (RequestQuery[Book],),
            {"Meta": meta, "__module__": __name__, "__qualname__": "WrongOptionQuery"},
        )
        query_class.get_declaration()


@pytest.mark.asyncio
async def test_a_strenum_option_takes_a_member_s_value(library):
    """join_type = "OR" worked as Connector.OR - a check of the option's type mustn't refuse it."""
    meta = type("Meta", (), {"queryset": Book.objects.all(), "join_type": "OR", "__qualname__": "ValueJoinQuery.Meta"})
    query_class = type(
        "ValueJoinQuery",
        (RequestQuery[Book],),
        {"Meta": meta, "__module__": __name__, "__qualname__": "ValueJoinQuery"},
    )
    query_class.get_declaration()
    assert query_class.get_meta_option("join_type") == "OR"


@pytest.mark.parametrize("container", [tuple, list])
@pytest.mark.asyncio
async def test_meta_collections_accept_compatible_filter_objects(library, container):
    title_filter = FilterField("title", description="The exact title")

    class BookQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            filters = container(
                [SimpleNamespace(get_filter_keys=title_filter.get_filter_keys, description=title_filter.description)]
            )
            api_getter = container([ApiValueMapping(set_key="display_title", key="title")])

    declaration = BookQuery.get_declaration()
    assert BookQuery.model_fields["title"].description == "The exact title"
    assert len(declaration.api_mappings) == 1
    objs = await BookQuery(title="Alpha").fetch()
    assert [obj.title for obj in objs] == ["Alpha"]
    assert objs[0].display_title == "Alpha"


@pytest.mark.asyncio
async def test_pagination_accepts_an_object_implementing_its_contract(library):
    page_option = OffsetPagination(default_limit=1)

    class BookQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all().order_by("id")
            pagination = SimpleNamespace(
                get_parameter_fields=page_option.get_parameter_fields,
                get_parameters=page_option.get_parameters,
                check_request=page_option.check_request,
                check_model=page_option.check_model,
                check_ordering=page_option.check_ordering,
                get_page=page_option.get_page,
            )

    page = await BookQuery().page()
    assert list(BookQuery.model_fields) == ["limit", "offset"]
    assert page.count == 5
    assert [obj.title for obj in page.result] == ["Alpha"]
