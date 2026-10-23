import datetime
from typing import Annotated

from hare.contrib.request_query import (
    CommaSeparated,
    CursorPagination,
    DeletedConfig,
    FieldsConfig,
    Filter,
    IncludeConfig,
    KeyColumns,
    LatestVersions,
    NoFilter,
    OffsetPagination,
    OrderingConfig,
    RequestQuery,
    ScrollPagination,
    SearchConfig,
)
from hare.query.enums import Connector
from hare.query.expressions import Q
from hare.query.functions import Count
from tests.contrib.request_query.models import (
    Article,
    Author,
    Book,
    BookStatus,
    LineReturn,
    Note,
    OrderLine,
    Refund,
    Ticket,
)


class BookQuery(RequestQuery[Book]):
    id: int | None = None
    id__in: list[int] | None = None
    ids: Annotated[CommaSeparated[int] | None, Filter("id", lookup="in")] = None
    title__icontains: str | None = None
    status: BookStatus | None = None
    author: int | None = None
    author__name__iexact: str | None = None
    author_ids: Annotated[list[int] | None, Filter("author", lookup="in")] = None
    author_keys: Annotated[list[int] | None, Filter("author__pk", lookup="in")] = None
    tag_ids: Annotated[list[int] | None, Filter("tags", lookup="in")] = None
    tag_name: Annotated[str | None, Filter("tags__name")] = None
    author_city: Annotated[str | None, Filter("author__profile__city")] = None
    author_without_profile: Annotated[bool | None, Filter("author__profile", lookup="isnull")] = None
    published_year: Annotated[int | None, Filter("published_at__year")] = None
    published_since: Annotated[datetime.datetime | None, Filter("published_at", lookup="gte")] = None
    compact: Annotated[bool | None, NoFilter()] = None
    author_rating_at_least: int | None = None
    tag_count_at_least: int | None = None

    class Meta:
        queryset = Book.objects.all()
        search = SearchConfig(fields=("title", "author__name"))
        ordering = OrderingConfig(fields=("title", "published_at", "author__name"))
        pagination = OffsetPagination(default_limit=2, max_limit=10)

    def filter_author_rating_at_least(self, value: int) -> Q:
        return Q(author__rating__gte=value)

    async def filter_tag_count_at_least(self, value: int) -> Q | None:
        if value <= 0:
            return None
        book_ids = (
            await Book.objects.annotate(tag_count=Count("tags"))
            .filter(tag_count__gte=value)
            .values_list("id", flat=True)
        )
        return Q(id__in=list(book_ids))


class PublishedBookQuery(BookQuery):
    async def get_access_condition(self) -> Q | None:
        return Q(status=BookStatus.PUBLISHED)


class EitherBookQuery(RequestQuery[Book]):
    title__icontains: str | None = None
    author: int | None = None

    class Meta:
        queryset = Book.objects.all()
        join_type = Connector.OR
        pagination = None


class TitledBookQuery(RequestQuery[Book]):
    id: int | None = None

    class Meta:
        queryset = Book.objects.all()
        ordering = OrderingConfig(default=("title",))
        pagination = OffsetPagination(default_limit=10)

    async def after_fetch(self, items: list[Book]) -> list[Book]:
        for item in items:
            item.shouted_title = item.title.upper()
        return items


class AuthorQuery(RequestQuery[Author]):
    profile__city: str | None = None
    profile__isnull: bool | None = None
    books__title__icontains: str | None = None
    books__tags: int | None = None

    class Meta:
        queryset = Author.objects.all()
        search = SearchConfig(fields=("name", "books__title"))
        ordering = OrderingConfig(default=("name",))
        pagination = None


class OrderLineQuery(RequestQuery[OrderLine]):
    pk: KeyColumns[int, int] | None = None
    pk__in: list[KeyColumns[int, int]] | None = None
    order_id: int | None = None
    book__author: int | None = None

    class Meta:
        queryset = OrderLine.objects.all()
        ordering = OrderingConfig(fields=("quantity", "book__title"))
        pagination = CursorPagination(default_limit=2)


class LineReturnQuery(RequestQuery[LineReturn]):
    line: KeyColumns[int, int] | None = None
    line__in: list[KeyColumns[int, int]] | None = None
    line__pk__in: list[KeyColumns[int, int]] | None = None
    line__book__title: str | None = None

    class Meta:
        queryset = LineReturn.objects.all()
        pagination = None


class RefundQuery(RequestQuery[Refund]):
    line_return__line: KeyColumns[int, int] | None = None
    line_return__line__pk__in: list[KeyColumns[int, int]] | None = None
    line_return__line__book__author__name: str | None = None

    class Meta:
        queryset = Refund.objects.all()
        pagination = None


class CatalogBookQuery(RequestQuery[Book]):
    status: BookStatus | None = None
    author: int | None = None
    author__in: list[int] | None = None
    author_id__in: list[int] | None = None
    tags__in: list[int] | None = None
    published_at__year: int | None = None

    class Meta:
        queryset = Book.objects.all()
        search = SearchConfig(fields=("title", "author__name"), split_words=True)
        ordering = OrderingConfig(fields=("title",), default=("title",))
        fields = FieldsConfig(fields=("title", "status", "author_id"))
        include = IncludeConfig(relations=("author", "author__profile", "tags"))
        pagination = OffsetPagination(default_limit=10)


class BookScrollQuery(RequestQuery[Book]):
    author: int | None = None

    class Meta:
        queryset = Book.objects.all()
        ordering = OrderingConfig(fields=("title",), default=("title",))
        pagination = ScrollPagination(default_limit=2)


class NoteQuery(RequestQuery[Note]):
    marks__label: str | None = None

    class Meta:
        queryset = Note.objects.all()
        ordering = OrderingConfig(default=("id",))
        deleted = DeletedConfig()
        pagination = None


class HiddenDeletedNoteQuery(NoteQuery):
    async def may_see_deleted(self) -> bool:
        return False


class TrashedNoteQuery(RequestQuery[Note]):
    class Meta:
        queryset = Note.objects.all()
        ordering = OrderingConfig(default=("id",))
        deleted = DeletedConfig(parameter="trashed")
        pagination = None


class ArticleQuery(RequestQuery[Article]):
    title: str | None = None
    status: BookStatus | None = None

    class Meta:
        queryset = Article.objects.all()
        ordering = OrderingConfig(default=("title",))
        versions = LatestVersions()
        pagination = OffsetPagination()


class PublishedArticleQuery(ArticleQuery):
    class Meta:
        queryset = Article.objects.filter(status=BookStatus.PUBLISHED)


class VisibleArticleQuery(ArticleQuery):
    async def get_access_condition(self) -> Q | None:
        return Q(status=BookStatus.PUBLISHED)


class TicketQuery(RequestQuery[Ticket]):
    subject__icontains: str | None = None

    class Meta:
        queryset = Ticket.objects.all()
        ordering = OrderingConfig(default=("id",))
        pagination = OffsetPagination()
