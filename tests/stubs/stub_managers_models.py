"""Models whose stubs declare managers of the project's own querysets and fields of an abstract base."""

from typing import TypeVar

from hare import fields
from hare.models import Model
from hare.query.managers.manager import Manager
from hare.query.queryset import QuerySet

TModel = TypeVar("TModel", bound=Model)


class Stamped(Model):
    created = fields.DatetimeField(null=True)
    note = fields.TextField(null=True)

    class Meta:
        abstract = True


class ArticleQuerySet(QuerySet["Article"]):
    def published(self) -> "ArticleQuerySet":
        return self.filter(title__startswith="p")


class RecentQuerySet(QuerySet[TModel]):
    def recent(self) -> "RecentQuerySet[TModel]":
        return self.order_by("-id")


class Article(Stamped):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    objects = Manager(ArticleQuerySet)
    recent_ones = Manager(RecentQuerySet)

    class Meta:
        table = "stub_managers_article"
