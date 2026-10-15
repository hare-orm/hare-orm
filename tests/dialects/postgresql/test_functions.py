from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.ranges import (
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
    Range,
)
from hare.dialects.postgresql.functions.aggregates import BoolAnd, BoolOr, JSONBAgg, StringAgg
from hare.exceptions import FieldError, ValidationError
from hare.query.expressions import F, Q
from hare.query.functions import ArrayAgg, ArrayItem, Count, Sum
from hare.query.functions.datetime import (
    ExtractDay,
    ExtractMonth,
    ExtractYear,
    TruncDay,
    TruncMonth,
    TruncYear,
)
from tests.dialects.postgresql.models_agg import AggAuthor, AggBook, AggEntry, AggPeriod
from tests.utils.timezone_context import override_timezone


@pytest.fixture
def utc():
    return UTC


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_agg_and_string_agg(db_agg):
    tolstoy = await AggAuthor.objects.create(name="Tolstoy")
    chekhov = await AggAuthor.objects.create(name="Chekhov")
    await AggBook.objects.create(title="War and Peace", author=tolstoy, rating=9, published=datetime(2020, 3, 15))
    await AggBook.objects.create(title="Anna Karenina", author=tolstoy, rating=8, published=datetime(2020, 7, 1))
    await AggBook.objects.create(
        title="The Cherry Orchard", author=chekhov, rating=7, published=datetime(2019, 11, 20)
    )

    rows = (
        await AggAuthor.objects.all()
        .annotate(
            titles=ArrayAgg("books__title"),
            joined=StringAgg("books__title", ", "),
            high_rated=StringAgg("books__title", ", ", _filter=Q(books__rating__gte=8)),
        )
        .group_by("id", "name")
        .order_by("name")
        .values("name", "titles", "joined", "high_rated")
    )
    by_name = {r["name"]: r for r in rows}

    assert set(by_name["Tolstoy"]["titles"]) == {"War and Peace", "Anna Karenina"}
    assert set(by_name["Tolstoy"]["joined"].split(", ")) == {"War and Peace", "Anna Karenina"}
    assert set(by_name["Tolstoy"]["high_rated"].split(", ")) == {"War and Peace", "Anna Karenina"}
    assert by_name["Chekhov"]["titles"] == ["The Cherry Orchard"]
    assert by_name["Chekhov"]["high_rated"] is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_agg_distinct(db_agg):
    author = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(title="A", author=author, rating=8, published=datetime(2020, 1, 1))
    await AggBook.objects.create(title="B", author=author, rating=8, published=datetime(2020, 1, 2))
    await AggBook.objects.create(title="C", author=author, rating=9, published=datetime(2020, 1, 3))

    rows = (
        await AggAuthor.objects.all()
        .annotate(
            ratings=ArrayAgg("books__rating"),
            distinct_ratings=ArrayAgg("books__rating", distinct=True),
        )
        .filter(id=author.id)
        .values("ratings", "distinct_ratings")
    )
    assert sorted(rows[0]["ratings"]) == [8, 8, 9]
    assert sorted(rows[0]["distinct_ratings"]) == [8, 9]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_trunc_year_month_day(db_agg, utc):
    author = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(title="War and Peace", author=author, rating=9, published=datetime(2020, 3, 15))

    row = (
        await AggBook.objects.all()
        .annotate(year=TruncYear("published"), month=TruncMonth("published"), day=TruncDay("published"))
        .filter(title="War and Peace")
        .values("year", "month", "day")
    )
    assert row[0]["year"] == datetime(2020, 1, 1, tzinfo=utc)
    assert row[0]["month"] == datetime(2020, 3, 1, tzinfo=utc)
    assert row[0]["day"] == datetime(2020, 3, 15, tzinfo=utc)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_extract_year_month_day(db_agg):
    author = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(title="War and Peace", author=author, rating=9, published=datetime(2020, 3, 15))

    row = (
        await AggBook.objects.all()
        .annotate(
            year=ExtractYear("published"),
            month=ExtractMonth("published"),
            day=ExtractDay("published"),
        )
        .filter(title="War and Peace")
        .values("year", "month", "day")
    )
    assert row[0]["year"] == 2020
    assert row[0]["month"] == 3
    assert row[0]["day"] == 15


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_extract_year_used_as_filter(db_agg):
    author = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(title="A", author=author, rating=8, published=datetime(2020, 1, 1))
    await AggBook.objects.create(title="B", author=author, rating=8, published=datetime(2021, 1, 1))

    books = await AggBook.objects.all().annotate(year=ExtractYear("published")).filter(year=2021)
    assert [b.title for b in books] == ["B"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_jsonb_agg(db_agg):
    tolstoy = await AggAuthor.objects.create(name="Tolstoy")
    chekhov = await AggAuthor.objects.create(name="Chekhov")
    await AggBook.objects.create(title="War and Peace", author=tolstoy, rating=9, published=datetime(2020, 3, 15))
    await AggBook.objects.create(title="Anna Karenina", author=tolstoy, rating=8, published=datetime(2020, 7, 1))
    await AggBook.objects.create(
        title="The Cherry Orchard", author=chekhov, rating=7, published=datetime(2019, 11, 20)
    )

    rows = (
        await AggAuthor.objects.all()
        .annotate(titles=JSONBAgg("books__title"))
        .group_by("id", "name")
        .order_by("name")
        .values("name", "titles")
    )
    by_name = {r["name"]: r for r in rows}
    assert set(by_name["Tolstoy"]["titles"]) == {"War and Peace", "Anna Karenina"}
    assert by_name["Chekhov"]["titles"] == ["The Cherry Orchard"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bool_and_bool_or(db_agg):
    all_in_print = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(
        title="A", author=all_in_print, rating=8, published=datetime(2020, 1, 1), in_print=True
    )
    await AggBook.objects.create(
        title="B", author=all_in_print, rating=8, published=datetime(2020, 1, 2), in_print=True
    )

    mixed = await AggAuthor.objects.create(name="Chekhov")
    await AggBook.objects.create(title="C", author=mixed, rating=7, published=datetime(2019, 1, 1), in_print=True)
    await AggBook.objects.create(title="D", author=mixed, rating=7, published=datetime(2019, 1, 2), in_print=False)

    none_in_print = await AggAuthor.objects.create(name="Gogol")
    await AggBook.objects.create(
        title="E", author=none_in_print, rating=6, published=datetime(2018, 1, 1), in_print=False
    )

    rows = (
        await AggAuthor.objects.all()
        .annotate(all_printed=BoolAnd("books__in_print"), any_printed=BoolOr("books__in_print"))
        .group_by("id", "name")
        .order_by("name")
        .values("name", "all_printed", "any_printed")
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Tolstoy"]["all_printed"] is True
    assert by_name["Tolstoy"]["any_printed"] is True
    assert by_name["Chekhov"]["all_printed"] is False
    assert by_name["Chekhov"]["any_printed"] is True
    assert by_name["Gogol"]["all_printed"] is False
    assert by_name["Gogol"]["any_printed"] is False


MOSCOW = ZoneInfo("Europe/Moscow")


async def create_entries() -> tuple[AggAuthor, AggAuthor]:
    author = await AggAuthor.objects.create(name="Tolstoy")
    empty_author = await AggAuthor.objects.create(name="Nobody")
    await AggEntry.objects.create(
        author=author,
        label="a",
        payload={"k": 1},
        duration=timedelta(hours=1),
        happened_at=datetime(2024, 1, 31, 22, 30, tzinfo=UTC),
        price=Decimal("12.34"),
        release_date=date(2024, 2, 17),
    )
    await AggEntry.objects.create(author=author, label="b")
    return author, empty_author


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_agg_decodes_elements_through_the_field(db_agg):
    """ARRAY_AGG elements come back as the aggregated field's Python values."""
    author, _ = await create_entries()

    with override_timezone(use_timezone=True, timezone="Europe/Moscow"):
        rows = (
            await AggAuthor.objects.all()
            .annotate(
                payloads=ArrayAgg("entries__payload"),
                durations=ArrayAgg("entries__duration"),
                moments=ArrayAgg("entries__happened_at"),
                prices=ArrayAgg("entries__price"),
                release_dates=ArrayAgg("entries__release_date"),
                filtered_moments=ArrayAgg("entries__happened_at", _filter=Q(entries__label="a")),
                distinct_payloads=ArrayAgg("entries__payload", distinct=True),
            )
            .group_by("id", "name")
            .order_by("name")
            .values(
                "name",
                "payloads",
                "durations",
                "moments",
                "prices",
                "release_dates",
                "filtered_moments",
                "distinct_payloads",
            )
        )
    by_name = {row["name"]: row for row in rows}
    tolstoy = by_name["Tolstoy"]

    assert sorted(tolstoy["payloads"], key=lambda payload: payload is None) == [{"k": 1}, None]
    assert sorted(tolstoy["durations"], key=lambda duration: duration is None) == [timedelta(hours=1), None]
    moments = [moment for moment in tolstoy["moments"] if moment is not None]
    assert moments == [datetime(2024, 2, 1, 1, 30, tzinfo=MOSCOW)]
    assert moments[0].utcoffset() == timedelta(hours=3)
    assert Decimal("12.34") in tolstoy["prices"]
    assert date(2024, 2, 17) in tolstoy["release_dates"]
    assert [moment for moment in tolstoy["filtered_moments"] if moment is not None] == moments
    assert {"k": 1} in tolstoy["distinct_payloads"]
    assert by_name["Nobody"]["payloads"] == [None]
    # _filter= leaves the author's one NULL joined row out - no row to aggregate, so NULL.
    assert by_name["Nobody"]["filtered_moments"] is None

    ordered = await AggAuthor.objects.filter(id=author.id).annotate(labels=ArrayAgg("entries__label")).values("labels")
    assert sorted(ordered[0]["labels"]) == ["a", "b"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_agg_of_no_rows_is_none(db_agg):
    await create_entries()

    rows = (
        await AggAuthor.objects.filter(name="Nobody")
        .annotate(moments=ArrayAgg("entries__happened_at", _filter=Q(entries__label="zzz")))
        .group_by("id")
        .values("moments")
    )
    assert rows == [{"moments": None}]
    assert (
        await AggEntry.objects.filter(label="missing").annotate(labels=ArrayAgg("label")).group_by("id").values() == []
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_jsonb_agg_decodes_elements_through_the_field(db_agg):
    """JSONB_AGG elements come back as the aggregated field's Python values."""
    author, _ = await create_entries()

    with override_timezone(use_timezone=True, timezone="Europe/Moscow"):
        rows = (
            await AggAuthor.objects.filter(id=author.id)
            .annotate(
                payloads=JSONBAgg("entries__payload"),
                durations=JSONBAgg("entries__duration"),
                moments=JSONBAgg("entries__happened_at"),
                prices=JSONBAgg("entries__price"),
                release_dates=JSONBAgg("entries__release_date"),
            )
            .group_by("id")
            .values("payloads", "durations", "moments", "prices", "release_dates")
        )
    row = rows[0]
    assert {"k": 1} in row["payloads"]
    assert timedelta(hours=1) in row["durations"]
    assert datetime(2024, 2, 1, 1, 30, tzinfo=MOSCOW) in row["moments"]
    assert None in row["moments"]
    assert [price for price in row["prices"] if price is not None] == [Decimal("12.34")]
    assert date(2024, 2, 17) in row["release_dates"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_extract_and_trunc_use_the_configured_zone(db_agg):
    """Extract*/Trunc* take the date part in Hare's zone, like the __day/__month lookups."""
    await create_entries()

    with override_timezone(use_timezone=True, timezone="Europe/Moscow"):
        rows = (
            await AggEntry.objects.filter(label="a")
            .annotate(
                year=ExtractYear("happened_at"),
                month=ExtractMonth("happened_at"),
                day=ExtractDay("happened_at"),
                month_start=TruncMonth("happened_at"),
                day_start=TruncDay("happened_at"),
                year_start=TruncYear("happened_at"),
            )
            .values("year", "month", "day", "month_start", "day_start", "year_start")
        )
        assert await AggEntry.objects.filter(happened_at__day=1).count() == 1
        grouped = (
            await AggEntry.objects.filter(label="a")
            .annotate(month_start=TruncMonth("happened_at"), day=ExtractDay("happened_at"))
            .group_by("month_start", "day")
            .values("month_start", "day")
        )
    row = rows[0]
    assert (row["year"], row["month"], row["day"]) == (2024, 2, 1)
    assert type(row["day"]) is int
    assert row["month_start"] == datetime(2024, 2, 1, tzinfo=MOSCOW)
    assert row["month_start"].utcoffset() == timedelta(hours=3)
    assert row["day_start"] == datetime(2024, 2, 1, tzinfo=MOSCOW)
    assert row["year_start"] == datetime(2024, 1, 1, tzinfo=MOSCOW)
    assert grouped == [{"month_start": datetime(2024, 2, 1, tzinfo=MOSCOW), "day": 1}]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_trunc_and_extract_of_a_date_field_return_a_date_and_int(db_agg):
    await create_entries()

    rows = (
        await AggEntry.objects.filter(label="a")
        .annotate(
            month_start=TruncMonth("release_date"),
            year_start=TruncYear("release_date"),
            day=ExtractDay("release_date"),
        )
        .values("month_start", "year_start", "day")
    )
    assert rows == [{"month_start": date(2024, 2, 1), "year_start": date(2024, 1, 1), "day": 17}]
    assert type(rows[0]["month_start"]) is date


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_string_agg_of_a_non_text_field(db_agg):
    author = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(title="A", author=author, rating=8, published=datetime(2020, 1, 1))
    await AggBook.objects.create(title="B", author=author, rating=9, published=datetime(2020, 1, 2))
    await AggBook.objects.create(title="C", author=author, rating=9, published=datetime(2020, 1, 3))

    rows = (
        await AggAuthor.objects.filter(id=author.id)
        .annotate(
            ratings=StringAgg("books__rating", ","),
            distinct_ratings=StringAgg("books__rating", ",", distinct=True),
        )
        .group_by("id")
        .values("ratings", "distinct_ratings")
    )
    assert sorted(rows[0]["ratings"].split(",")) == ["8", "9", "9"]
    assert sorted(rows[0]["distinct_ratings"].split(",")) == ["8", "9"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_extract_and_trunc_of_a_naive_datetime_agree_with_the_day_lookup(db_agg):
    """Under use_timezone=False Extract*/Trunc* use the local system zone, like the __day lookup."""
    await create_entries()

    with override_timezone(use_timezone=False):
        rows = (
            await AggEntry.objects.filter(label="a")
            .annotate(day=ExtractDay("happened_at"), day_start=TruncDay("happened_at"))
            .values("day", "day_start", "happened_at")
        )
        row = rows[0]
        assert await AggEntry.objects.filter(happened_at__day=row["day"]).count() == 1
    assert row["day"] == row["happened_at"].day
    assert row["day_start"] == row["happened_at"].replace(hour=0, minute=0, second=0, microsecond=0)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_aggregate_filter_leaves_rows_out_instead_of_collecting_null(db_agg):
    """`_filter=` used to feed a failing row to the aggregate as NULL - ArrayAgg/JSONBAgg
    collected `[None, 9]`, and a group with no matching row got `[None]` instead of NULL."""
    tolstoy = await AggAuthor.objects.create(name="Tolstoy")
    chekhov = await AggAuthor.objects.create(name="Chekhov")
    await AggBook.objects.create(title="War and Peace", author=tolstoy, rating=9, published=datetime(2020, 3, 15))
    await AggBook.objects.create(title="Resurrection", author=tolstoy, rating=5, published=datetime(2020, 5, 1))
    await AggBook.objects.create(title="Anna Karenina", author=tolstoy, rating=8, published=datetime(2020, 7, 1))
    await AggBook.objects.create(
        title="The Cherry Orchard", author=chekhov, rating=7, published=datetime(2019, 11, 20)
    )

    rows = (
        await AggAuthor.objects.annotate(
            high=ArrayAgg("books__rating", _filter=Q(books__rating__gte=8)),
            high_json=JSONBAgg("books__rating", _filter=Q(books__rating__gte=8)),
            distinct_high=ArrayAgg("books__rating", distinct=True, _filter=Q(books__rating__gte=8)),
            joined=StringAgg("books__title", ", ", _filter=Q(books__rating__gte=8)),
            high_count=Count("books", _filter=Q(books__rating__gte=8)),
            high_sum=Sum("books__rating", _filter=Q(books__rating__gte=8)),
        )
        .order_by("name")
        .values("name", "high", "high_json", "distinct_high", "joined", "high_count", "high_sum")
    )

    assert rows[0] == {
        "name": "Chekhov",
        "high": None,
        "high_json": None,
        "distinct_high": None,
        "joined": None,
        "high_count": 0,
        "high_sum": None,
    }
    assert sorted(rows[1]["high"]) == [8, 9]
    assert sorted(rows[1]["high_json"]) == [8, 9]
    assert rows[1]["distinct_high"] == [8, 9]
    assert set(rows[1]["joined"].split(", ")) == {"War and Peace", "Anna Karenina"}
    assert rows[1]["high_count"] == 2
    assert rows[1]["high_sum"] == 17


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_aggregate_filter_value_changes_between_runs_of_one_query_shape(db_agg):
    """The FILTER condition's own value is substituted when a query of the same shape runs again."""
    tolstoy = await AggAuthor.objects.create(name="Tolstoy")
    await AggBook.objects.create(title="War and Peace", author=tolstoy, rating=9, published=datetime(2020, 3, 15))
    await AggBook.objects.create(title="Resurrection", author=tolstoy, rating=5, published=datetime(2020, 5, 1))

    results = []
    for minimum_rating in (8, 1, 10):
        values = await AggAuthor.objects.annotate(
            ratings=ArrayAgg("books__rating", _filter=Q(books__rating__gte=minimum_rating))
        ).values_list("ratings", flat=True)
        results.append(sorted(values[0]) if values[0] else values[0])

    assert results == [[9], [5, 9], None]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_jsonb_agg_of_range_fields_decodes_each_range(db_agg):
    """A range reaches a JSONB_AGG array as its text form (`[1,5)`) - it used to crash with
    AttributeError instead of decoding to a Range."""
    author = await AggAuthor.objects.create(name="A")
    moment = datetime(2024, 1, 1, 12, tzinfo=UTC)
    await AggPeriod.objects.create(
        author=author,
        span=Range(1, 5),
        decimal_span=Range(Decimal("1.10"), Decimal("2.5"), upper_inc=True),
        date_span=Range(date(2024, 1, 1), None),
        moment_span=Range(moment, moment + timedelta(hours=1)),
    )
    await AggPeriod.objects.create(author=author, span=Range(is_empty=True), moment_span=Range(None, moment))

    row = (
        await AggAuthor.objects.annotate(
            spans=JSONBAgg("periods__span"),
            decimal_spans=JSONBAgg("periods__decimal_span"),
            date_spans=JSONBAgg("periods__date_span"),
            moment_spans=JSONBAgg("periods__moment_span"),
        ).values("spans", "decimal_spans", "date_spans", "moment_spans")
    )[0]

    assert sorted(row["spans"], key=lambda value: value.is_empty) == [
        Range(1, 5),
        Range(None, None, lower_inc=False, upper_inc=False, is_empty=True),
    ]
    assert Range(Decimal("1.10"), Decimal("2.5"), upper_inc=True) in row["decimal_spans"]
    assert Range(date(2024, 1, 1), None) in row["date_spans"]
    assert Range(moment, moment + timedelta(hours=1)) in row["moment_spans"]
    assert Range(None, moment, lower_inc=False) in row["moment_spans"]


@pytest.mark.parametrize(
    ("field", "text", "expected"),
    [
        (IntRangeField(), "[1,5)", Range(1, 5)),
        (IntRangeField(), "empty", Range(None, None, lower_inc=False, upper_inc=False, is_empty=True)),
        (IntRangeField(), "(,5)", Range(None, 5, lower_inc=False)),
        (DecimalRangeField(), "[1.10,2.5]", Range(Decimal("1.10"), Decimal("2.5"), upper_inc=True)),
        (DateRangeField(), "[2024-01-01,infinity)", Range(date(2024, 1, 1), date.max)),
        (
            DateTimeRangeField(),
            '["2024-01-01 12:00:00+00","2024-01-02 13:30:00.5+05:30")',
            Range(
                datetime(2024, 1, 1, 12, tzinfo=UTC),
                datetime(2024, 1, 2, 8, 0, 0, 500000, tzinfo=UTC),
            ),
        ),
    ],
)
def test_range_field_parses_postgres_range_text(field, text, expected):
    parsed = field.from_db_value(text)

    assert parsed == expected


def test_range_field_rejects_text_that_is_not_a_range():
    with pytest.raises(ValidationError, match="is not a range value"):
        IntRangeField().from_db_value("1,5")
    with pytest.raises(ValidationError, match="is not a range value"):
        IntRangeField().from_db_value("[a,5)")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_aggregate_annotation_has_array_lookups(db_agg):
    """An ArrayAgg annotation is filtered with the array lookups - `__contains` used to run a
    text LIKE (AttributeError), `__overlap`/`__len` were unknown filter params."""
    tolstoy = await AggAuthor.objects.create(name="Tolstoy")
    chekhov = await AggAuthor.objects.create(name="Chekhov")
    await AggBook.objects.create(title="War and Peace", author=tolstoy, rating=9, published=datetime(2020, 3, 15))
    await AggBook.objects.create(title="Anna Karenina", author=tolstoy, rating=8, published=datetime(2020, 7, 1))
    await AggBook.objects.create(
        title="The Cherry Orchard", author=chekhov, rating=7, published=datetime(2019, 11, 20)
    )

    def names(**filters):
        return (
            AggAuthor.objects.annotate(titles=ArrayAgg("books__title"), ratings=JSONBAgg("books__rating"))
            .filter(**filters)
            .order_by("name")
            .values_list("name", flat=True)
        )

    assert await names(titles__contains=["War and Peace"]) == ["Tolstoy"]
    assert await names(titles__overlap=["The Cherry Orchard", "Anna Karenina"]) == ["Chekhov", "Tolstoy"]
    assert await names(titles__len=1) == ["Chekhov"]
    assert await names(titles__contained_by=["The Cherry Orchard", "Nothing"]) == ["Chekhov"]
    assert await names(titles=["The Cherry Orchard"]) == ["Chekhov"]
    assert await names(ratings__contains=[9]) == ["Tolstoy"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_lookup_on_an_annotation_that_is_not_an_array_is_rejected(db_agg):
    await AggAuthor.objects.create(name="Tolstoy")

    with pytest.raises(FieldError, match="__overlap needs an annotation whose value is an array"):
        await AggAuthor.objects.annotate(book_count=Count("books")).filter(book_count__overlap=[1])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_array_item_annotation_of_nested_array_compares_with_a_list(db_agg):
    """ArrayItem of a nested array is itself an array - comparing it used to fail with
    AttributeError on the element field bound to no model."""
    author = await AggAuthor.objects.create(name="A")
    first = await AggPeriod.objects.create(author=author, matrix=[[1, 2], [3, 4]], tags=["x", "y"])
    await AggPeriod.objects.create(author=author, matrix=[[5, 6]], tags=["z"])

    assert await AggPeriod.objects.annotate(row=ArrayItem("matrix", 0)).filter(row=[1, 2]).values_list(
        "id", flat=True
    ) == [first.id]
    assert await AggPeriod.objects.annotate(labels=F("tags")).filter(labels__contains=["y"]).values_list(
        "id", flat=True
    ) == [first.id]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_nested_array_lookup_with_a_scalar_element_names_the_field(db_agg):
    with pytest.raises(ValidationError, match=r"^matrix\[0\]: expected a list/tuple/set, got 1"):
        await AggPeriod.objects.filter(matrix__contains=[1])
