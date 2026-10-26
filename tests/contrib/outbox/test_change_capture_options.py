"""ChangeCapture's extend_many - one call per write statement of a model, with all its rows - and
replace() - a copy of a capture with other options, for a model refining its abstract base's capture."""

import pytest

from hare.contrib.outbox import ChangeCapture, ChangeExtension
from hare.exceptions import ConfigurationError, ValidationError
from hare.instrumentation.enums import RowOperation
from tests.contrib.outbox.capture_models import (
    BatchExtendedRow,
    BatchExtension,
    CapturedAccount,
    CapturedAccountBase,
    CaptureOutboxEvent,
    LabelledChangeCapture,
)


async def get_authors() -> list[str]:
    return [event.headers["author"] for event in await CaptureOutboxEvent.objects.order_by("sequence")]


@pytest.fixture
def batch_calls():
    BatchExtension.calls.clear()
    yield BatchExtension.calls
    BatchExtension.calls.clear()


@pytest.mark.asyncio
async def test_extend_many_is_called_once_per_write_statement_with_its_rows(db_capture, batch_calls):
    await BatchExtendedRow.objects.bulk_create(
        [BatchExtendedRow(id=row_id, name=f"n{row_id}") for row_id in (1, 2, 3)]
    )
    await BatchExtendedRow.objects.filter(id__in=[1, 2]).update(name="same")
    row = await BatchExtendedRow.objects.get(id=3)
    row.name = "single"
    await row.save()

    assert batch_calls == [["n1", "n2", "n3"], ["same", "same"], ["single"]]
    assert await get_authors() == ["N1", "N2", "N3", "SAME", "SAME", "SINGLE"]


@pytest.mark.asyncio
async def test_extend_many_giving_another_number_of_extensions_fails_the_write(db_capture, monkeypatch):
    monkeypatch.setattr(BatchExtendedRow._meta.change_capture, "extend_many", lambda changes: [None])
    with pytest.raises(ValidationError, match="1 extensions for 2 changes"):
        await BatchExtendedRow.objects.bulk_create(
            [BatchExtendedRow(id=1, name="a"), BatchExtendedRow(id=2, name="b")]
        )
    assert await CaptureOutboxEvent.objects.count() == 0


@pytest.mark.asyncio
async def test_an_extension_of_extend_many_is_checked_as_one_of_extend(db_capture, monkeypatch):
    monkeypatch.setattr(
        BatchExtendedRow._meta.change_capture,
        "extend_many",
        lambda changes: [ChangeExtension(payload={"operation": "x"}) for _ in changes],
    )
    with pytest.raises(ValidationError, match="operation"):
        await BatchExtendedRow.objects.create(id=1, name="a")


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"": "a"}, {1: "a"}], ids=["empty_name", "number_name"])
async def test_headers_of_an_extension_are_checked_as_enqueue_checks_them(db_capture, monkeypatch, headers):
    monkeypatch.setattr(
        BatchExtendedRow._meta.change_capture,
        "extend_many",
        lambda changes: [ChangeExtension(headers=headers) for _ in changes],
    )
    with pytest.raises(ValidationError, match="non-empty text names"):
        await BatchExtendedRow.objects.create(id=1, name="a")
    assert await CaptureOutboxEvent.objects.count() == 0


@pytest.mark.database_independent
def test_extend_and_extend_many_are_not_taken_together():
    with pytest.raises(ConfigurationError, match="extend or extend_many"):
        ChangeCapture(CaptureOutboxEvent, extend=lambda change: None, extend_many=lambda changes: [])


@pytest.mark.asyncio
async def test_a_replaced_capture_writes_to_the_same_outbox_with_its_options(db_capture):
    await CapturedAccount.objects.create(id=1, name="ann", token="secret")

    (event,) = await CaptureOutboxEvent.objects.all()
    assert event.topic == "models.capturedaccount.inserted"
    assert event.payload["after"] == {"id": 1, "name": "ann"}
    capture = CapturedAccount._meta.change_capture
    assert isinstance(capture, LabelledChangeCapture)
    assert capture.label == "service"
    assert capture.operations == frozenset({RowOperation.INSERT})
    assert CapturedAccountBase.Meta.change_capture.exclude == ()


@pytest.mark.database_independent
def test_replace_swaps_the_options_of_a_pair_and_checks_the_values():
    def extend(change):
        return None

    def extend_many(changes):
        return [None for _ in changes]

    capture = ChangeCapture(CaptureOutboxEvent, fields=("name",), extend=extend)

    replaced = capture.replace(exclude=["token"], extend_many=extend_many)
    assert (replaced.fields, replaced.exclude, replaced.extend, replaced.extend_many) == (
        None,
        ("token",),
        None,
        extend_many,
    )
    assert (capture.fields, capture.exclude, capture.extend) == (("name",), (), extend)
    assert capture.replace(fields=["id"]).replace(extend=extend).extend_many is None
    with pytest.raises(TypeError, match="takes no nope"):
        capture.replace(nope=1)
    with pytest.raises(TypeError, match="outbox"):
        capture.replace(outbox=CaptureOutboxEvent)
    with pytest.raises(ConfigurationError):
        capture.replace(payload="everything")
    with pytest.raises(ConfigurationError):
        capture.replace(operations=["upsert"])
    with pytest.raises(ConfigurationError, match="fields or exclude"):
        capture.replace(fields=["id"], exclude=["name"])
    with pytest.raises(ConfigurationError, match="extend or extend_many"):
        capture.replace(extend=extend, extend_many=extend_many)


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["topic", "ordering_key"])
async def test_an_empty_topic_or_ordering_key_of_a_change_is_refused(db_capture, monkeypatch, option):
    monkeypatch.setattr(BatchExtendedRow._meta.change_capture, option, lambda change: "")
    with pytest.raises(ValidationError, match="1 to 255 characters"):
        await BatchExtendedRow.objects.create(id=1, name="a")
    assert await CaptureOutboxEvent.objects.count() == 0
