"""Expressions taking other expressions: a subquery of a queryset, a Case whose branches are any expression -
as the docs write them."""

from hare.query.expressions import Case, Exists, F, OuterReference, Subquery, Value, When
from tests.contrib.mypy.models import Label, Volume


async def nested() -> None:
    label_titles = Label.objects.filter(volumes=OuterReference("pk")).values("name")[:1]
    Volume.objects.annotate(first_label=Subquery(label_titles))
    Volume.objects.annotate(first_label=Subquery(Label.objects.values_list("name", flat=True)[:1]))
    has_labels = Exists(Label.objects.filter(volumes=OuterReference("pk")))
    Volume.objects.annotate(
        rating=Case(When(price__gte=4, then=Value("good")), default=Value("other")),
        labelled=Case(When(has_labels, then=Value(True)), default=Value(False)),
        nested=Case(When(price__gte=4, then=Case(When(price__gte=8, then=F("price")), default=0)), default=None),
        counted=Case(When(price__gte=4, then=Subquery(label_titles)), default=Value("")),
    )


async def common_table() -> None:
    Volume.objects.with_cte("titled", Volume.objects.filter(title__startswith="A").values("id"))
