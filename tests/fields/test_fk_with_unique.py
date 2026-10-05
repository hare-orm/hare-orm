import pytest

from hare import prefetch_related_objects
from hare.exceptions import (
    NoValuesFetched,
    QueryError,
    ValidationError,
)
from hare.query.queryset import QuerySet
from tests import testmodels


@pytest.mark.asyncio
async def test_student__empty(db):
    """A missing required FK value (its underlying "*_id" column is a plain IntField) now fails
    fast with ValidationError, matching CharField's own established test_empty pattern - not
    IntegrityError from the DB's NOT NULL constraint."""
    with pytest.raises(ValidationError):
        await testmodels.Student.objects.create()


@pytest.mark.asyncio
async def test_student__create_by_id(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school_id=school.id)
    assert student.school_id == school.id
    assert (await school.students.all())[0] == student


@pytest.mark.asyncio
async def test_student__create_by_name(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([student], "school")
    assert student.school == school
    assert (await school.students.all())[0] == student


@pytest.mark.asyncio
async def test_student__by_name__created_prefetched(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    assert student.school == school
    assert (await school.students.all())[0] == student


@pytest.mark.asyncio
async def test_student__by_name__unfetched(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    student = await testmodels.Student.objects.get(id=student.id)
    assert isinstance(student.school, QuerySet)


@pytest.mark.asyncio
async def test_student__by_name__re_awaited(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([student], "school")
    assert student.school == school
    assert await student.school == school


@pytest.mark.asyncio
async def test_student__by_name__awaited(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    student = await testmodels.Student.objects.get(id=student.id)
    assert await student.school == school
    assert (await school.students.all())[0] == student


@pytest.mark.asyncio
async def test_update_by_name(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    school2 = await testmodels.School.objects.create(id=2048, name="School2")
    student0 = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)

    await testmodels.Student.objects.filter(id=student0.id).update(school=school2)
    student = await testmodels.Student.objects.get(id=student0.id)

    await prefetch_related_objects([student], "school")
    assert student.school == school2
    assert await school.students.all() == []
    assert (await school2.students.all())[0] == student


@pytest.mark.asyncio
async def test_update_by_id(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    school2 = await testmodels.School.objects.create(id=2048, name="School2")
    student0 = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school_id=school.id)

    await testmodels.Student.objects.filter(id=student0.id).update(school_id=school2.id)
    student = await testmodels.Student.objects.get(id=student0.id)

    assert student.school_id == school2.id
    assert await school.students.all() == []
    assert (await school2.students.all())[0] == student


@pytest.mark.asyncio
async def test_delete_by_name(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    del student.school
    with pytest.raises(ValidationError):
        await student.save()


@pytest.mark.asyncio
async def test_student__uninstantiated_create(db):
    school = await testmodels.School(id=1024, name="School1")
    with pytest.raises(QueryError, match="You should first call .save()"):
        await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)


@pytest.mark.asyncio
async def test_student__uninstantiated_iterate(db):
    school = await testmodels.School(id=1024, name="School1")
    with pytest.raises(QueryError, match="This objects hasn't been instanced, call .save()"):
        async for _ in school.students:
            pass


@pytest.mark.asyncio
async def test_student__uninstantiated_await(db):
    school = await testmodels.School(id=1024, name="School1")
    with pytest.raises(QueryError, match="This objects hasn't been instanced, call .save()"):
        await school.students


@pytest.mark.asyncio
async def test_student__unfetched_contains(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        "a" in school.students  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_stduent__unfetched_iter(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        for _ in school.students:
            pass


@pytest.mark.asyncio
async def test_student__unfetched_len(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        len(school.students)


@pytest.mark.asyncio
async def test_student__unfetched_bool(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        bool(school.students)


@pytest.mark.asyncio
async def test_student__unfetched_getitem(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        school.students[0]  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_student__instantiated_create(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    assert (await testmodels.Student.objects.get(id=student.id)).school_id == 1024


@pytest.mark.asyncio
async def test_student__instantiated_iterate(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    assert [item async for item in school.students] == [student]


@pytest.mark.asyncio
async def test_student__instantiated_await(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    assert list(await school.students) == [student]


@pytest.mark.asyncio
async def test_student__fetched_contains(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([school], "students")
    assert student in school.students


@pytest.mark.asyncio
async def test_student__fetched_iter(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([school], "students")
    assert list(school.students) == [student]


@pytest.mark.asyncio
async def test_student__fetched_len(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([school], "students")
    assert len(school.students) == 1


@pytest.mark.asyncio
async def test_student__fetched_bool(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    await prefetch_related_objects([school], "students")
    assert not bool(school.students)
    await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([school], "students")
    assert bool(school.students)


@pytest.mark.asyncio
async def test_student__fetched_getitem(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student = await testmodels.Student.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([school], "students")
    assert school.students[0] == student

    with pytest.raises(IndexError):
        school.students[1]  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_student__filter(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student1 = await testmodels.Student.objects.create(name="Sang-Heon Jeon1", school=school)
    student2 = await testmodels.Student.objects.create(name="Sang-Heon Jeon2", school=school)
    assert await school.students.filter(name="Sang-Heon Jeon1") == [student1]
    assert await school.students.filter(name="Sang-Heon Jeon2") == [student2]
    assert await school.students.filter(name="Sang-Heon Jeon3") == []


@pytest.mark.asyncio
async def test_student__all(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student1 = await testmodels.Student.objects.create(name="Sang-Heon Jeon1", school=school)
    student2 = await testmodels.Student.objects.create(name="Sang-Heon Jeon2", school=school)
    assert set(await school.students.all()) == {student1, student2}


@pytest.mark.asyncio
async def test_student_order_by(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student1 = await testmodels.Student.objects.create(name="Sang-Heon Jeon1", school=school)
    student2 = await testmodels.Student.objects.create(name="Sang-Heon Jeon2", school=school)
    assert await school.students.order_by("-name") == [student2, student1]
    assert await school.students.order_by("name") == [student1, student2]


@pytest.mark.asyncio
async def test_student__limit(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    student1 = await testmodels.Student.objects.create(name="Sang-Heon Jeon1", school=school)
    student2 = await testmodels.Student.objects.create(name="Sang-Heon Jeon2", school=school)
    await testmodels.Student.objects.create(name="Sang-Heon Jeon3", school=school)
    assert await school.students.order_by("name").limit(2) == [student1, student2]


@pytest.mark.asyncio
async def test_student_offset(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    await testmodels.Student.objects.create(name="Sang-Heon Jeon1", school=school)
    student2 = await testmodels.Student.objects.create(name="Sang-Heon Jeon2", school=school)
    student3 = await testmodels.Student.objects.create(name="Sang-Heon Jeon3", school=school)
    assert await school.students.order_by("name").offset(1) == [student2, student3]
