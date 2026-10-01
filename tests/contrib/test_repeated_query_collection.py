"""Tests for `RepeatedQueryDetector.collect()` - scoped, contextvars-based per-shape query counting."""

import asyncio

import pytest

from hare.contrib.repeated_queries import RepeatedQueryCollection, RepeatedQueryDetector
from hare.exceptions import QueryError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from tests.testmodels import Author, Book


async def create_books(count: int) -> list[Book]:
    books = []
    for index in range(count):
        author = await Author.objects.create(name=f"Author {index}")
        books.append(await Book.objects.create(name=f"Book {index}", author=author, rating=1.0))
    return books


def test_shape_key_is_idempotent():
    sql = "SELECT *\n  FROM  book WHERE id = ?"
    shape_key = RepeatedQueryDetector.shape_key(sql)
    assert shape_key == "SELECT * FROM book WHERE id = ?"
    assert RepeatedQueryDetector.shape_key(shape_key) == shape_key


@pytest.mark.asyncio
async def test_lazy_foreign_key_access_in_a_loop_is_one_shape_counted_n_times(db):
    await create_books(4)
    books = await Book.objects.all()

    async with RepeatedQueryDetector.collect() as collection:
        for book in books:
            await book.author

    assert collection.total == 4
    assert len(collection) == 1
    (entry,) = collection.repeated(threshold=2)
    assert entry.count == 4
    assert collection.count_for(entry.sql_sample) == 4
    assert collection.count_for(entry.shape_key) == 4
    assert "author" in entry.sql_sample.lower()
    # The call site ends at this test's own frame, not inside hare's internals.
    innermost_frame_line = entry.call_site.rstrip().splitlines()[-2]
    assert "test_repeated_query_collection.py" in innermost_frame_line
    assert "test_lazy_foreign_key_access_in_a_loop_is_one_shape_counted_n_times" in innermost_frame_line


@pytest.mark.asyncio
async def test_repeated_get_by_different_pk_is_one_shape(db):
    authors = [await Author.objects.create(name=f"Author {index}") for index in range(3)]

    with RepeatedQueryDetector.collect() as collection:
        for author in authors:
            await Author.objects.get(id=author.id)
        await Book.objects.all().count()

    assert collection.total == 4
    assert [entry.count for entry in collection] == [3, 1]
    assert [entry.count for entry in collection.repeated()] == [3]
    assert collection.repeated(threshold=4) == []
    assert collection.count_for("SELECT 1") == 0


@pytest.mark.asyncio
async def test_counts_are_complete_without_waiting_for_hooks(db):
    """Counting is synchronous in the query path, even with an async hook registered."""
    hook_calls = []

    async def slow_hook(event):
        sql = event.sql
        await asyncio.sleep(0.01)
        hook_calls.append(sql)

    Observers.observe(QueryExecuted, slow_hook)
    try:
        async with RepeatedQueryDetector.collect() as collection:
            for _ in range(3):
                await Author.objects.all().count()
            assert collection.total == 3
    finally:
        Observers.unobserve(QueryExecuted, slow_hook)
        await Observers.wait_for_pending()
    assert len(hook_calls) == 3


@pytest.mark.asyncio
async def test_works_while_the_global_detector_is_also_started(db):
    reports = []
    RepeatedQueryDetector.reset_counts()
    async with RepeatedQueryDetector(threshold=2, window_seconds=10.0, action=reports.append):
        async with RepeatedQueryDetector.collect() as collection:
            for _ in range(2):
                await Author.objects.all().count()
    RepeatedQueryDetector.reset_counts()
    assert collection.total == 2
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_mix_counts(db):
    await Author.objects.create(name="Douglas Adams")
    both_started = asyncio.Barrier(2)

    async def run_queries(count: int) -> RepeatedQueryCollection:
        async with RepeatedQueryDetector.collect() as collection:
            await both_started.wait()
            for _ in range(count):
                await Author.objects.filter(name="Douglas Adams").count()
                await asyncio.sleep(0)
        return collection

    first_collection, second_collection = await asyncio.gather(run_queries(2), run_queries(5))

    assert first_collection.total == 2
    assert second_collection.total == 5


@pytest.mark.asyncio
async def test_nested_blocks_count_inner_queries_in_both(db):
    async with RepeatedQueryDetector.collect() as outer_collection:
        await Author.objects.all().count()
        async with RepeatedQueryDetector.collect() as inner_collection:
            await Author.objects.all().count()
            await Book.objects.all().count()
        await Book.objects.all().count()

    assert inner_collection.total == 2
    assert outer_collection.total == 4
    assert [entry.count for entry in outer_collection] == [2, 2]


@pytest.mark.asyncio
async def test_child_task_created_inside_the_block_is_counted(db):
    async def count_authors() -> int:
        return await Author.objects.all().count()

    async with RepeatedQueryDetector.collect() as collection:
        await asyncio.gather(*(asyncio.create_task(count_authors()) for _ in range(3)))

    assert collection.total == 3


@pytest.mark.asyncio
async def test_queries_after_exit_are_ignored(db):
    release_child = asyncio.Event()

    async def query_later():
        await release_child.wait()
        await Author.objects.all().count()

    async with RepeatedQueryDetector.collect() as collection:
        child_task = asyncio.create_task(query_later())
        await Author.objects.all().count()
    release_child.set()
    await child_task
    await Author.objects.all().count()

    assert collection.total == 1
    assert Observers.context_observers.get() == ()


@pytest.mark.asyncio
async def test_collection_cannot_be_entered_twice(db):
    collection = RepeatedQueryDetector.collect()
    with collection:
        with pytest.raises(QueryError), collection:
            pass


def test_repeated_rejects_threshold_below_one():
    with pytest.raises(ValueError):
        RepeatedQueryDetector.collect().repeated(threshold=0)


@pytest.mark.asyncio
async def test_failing_observer_does_not_break_the_query(db):
    def failing_observer(event):
        raise RuntimeError("observer failure")

    with Observers.observing(QueryExecuted, failing_observer):
        assert await Author.objects.all().count() == 0


@pytest.mark.asyncio
async def test_exit_in_another_task_than_the_enter_does_not_raise(db):
    """An async generator fixture's teardown can run in a different task than its setup - the
    observer token can't be reset there, which used to raise ValueError."""
    author = await Author.objects.create(name="Author")
    collection = RepeatedQueryDetector.collect()

    async def enter_and_query() -> None:
        collection.__enter__()
        await Author.objects.get(id=author.id)

    await asyncio.create_task(enter_and_query())
    await asyncio.create_task(collection.__aexit__(None, None, None))

    assert collection.total == 1
    assert not collection.is_open
    await Author.objects.get(id=author.id)
    assert collection.total == 1
    with collection:
        await Author.objects.get(id=author.id)
    assert collection.total == 2


@pytest.mark.asyncio
async def test_async_generator_teardown_in_another_task_exits_cleanly(db):
    author = await Author.objects.create(name="Author")

    async def collection_fixture():
        async with RepeatedQueryDetector.collect() as collection:
            yield collection

    fixture = collection_fixture()
    collection = await fixture.__anext__()
    await Author.objects.get(id=author.id)
    with pytest.raises(StopAsyncIteration):
        await asyncio.create_task(fixture.__anext__())

    assert collection.total == 1
    assert not collection.is_open


@pytest.mark.asyncio
async def test_exit_in_another_task_removes_the_observer_there(db):
    observers_before = Observers.context_observers.get()
    collection = RepeatedQueryDetector.collect()
    collection.__enter__()
    observers_while_open = Observers.context_observers.get()

    async def exit_collection() -> tuple:
        await collection.__aexit__(None, None, None)
        return Observers.context_observers.get()

    observers_after_exit_in_other_task = await asyncio.create_task(exit_collection())

    assert observers_after_exit_in_other_task == observers_before
    assert len(observers_while_open) == len(observers_before) + 1
    Observers.context_observers.set(observers_before)
