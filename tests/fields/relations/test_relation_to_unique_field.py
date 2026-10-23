import pytest

from hare import prefetch_related_objects
from hare.exceptions import (
    QueryError,
    ValidationError,
)
from hare.query.queryset import QuerySet
from tests.shared_models import testmodels

# A foreign key and a one-to-one field to School's unique non-key `id` field. `read_reverse` gives the
# awaitable the school's reverse relation reads, `as_reverse` what it holds for the given related objects.
RELATIONS = [
    pytest.param(
        testmodels.Student,
        "students",
        lambda school: school.students.all(),
        list,
        id="student",
    ),
    pytest.param(
        testmodels.Principal,
        "principal",
        lambda school: school.principal,
        lambda related: related[0] if related else None,
        id="principal",
    ),
]
RELATION_FIELDS = ("model_class", "related_name", "read_reverse", "as_reverse")


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_create_by_id(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school_id=school.id)
    assert obj.school_id == school.id
    assert await read_reverse(school) == as_reverse([obj])


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_create_by_name(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([obj], "school")
    assert obj.school == school
    assert await read_reverse(school) == as_reverse([obj])


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_by_name__created_prefetched(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    assert obj.school == school
    assert await read_reverse(school) == as_reverse([obj])


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_by_name__unfetched(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    obj = await model_class.objects.get(id=obj.id)
    assert isinstance(obj.school, QuerySet)


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_by_name__re_awaited(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([obj], "school")
    assert obj.school == school
    assert await obj.school == school


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_by_name__awaited(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    obj = await model_class.objects.get(id=obj.id)
    assert await obj.school == school
    assert await read_reverse(school) == as_reverse([obj])


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_update_by_name(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    school2 = await testmodels.School.objects.create(id=2048, name="School2")
    obj0 = await model_class.objects.create(name="Sang-Heon Jeon", school=school)

    await model_class.objects.filter(id=obj0.id).update(school=school2)
    obj = await model_class.objects.get(id=obj0.id)

    await prefetch_related_objects([obj], "school")
    assert obj.school == school2
    assert await read_reverse(school) == as_reverse([])
    assert await read_reverse(school2) == as_reverse([obj])


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_update_by_id(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    school2 = await testmodels.School.objects.create(id=2048, name="School2")
    obj0 = await model_class.objects.create(name="Sang-Heon Jeon", school_id=school.id)

    await model_class.objects.filter(id=obj0.id).update(school_id=school2.id)
    obj = await model_class.objects.get(id=obj0.id)

    assert obj.school_id == school2.id
    assert await read_reverse(school) == as_reverse([])
    assert await read_reverse(school2) == as_reverse([obj])


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_delete_by_name(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    del obj.school
    with pytest.raises(ValidationError):
        await obj.save()


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_uninstantiated_create(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School(id=1024, name="School1")
    with pytest.raises(QueryError, match="You should first call .save()"):
        await model_class.objects.create(name="Sang-Heon Jeon", school=school)


@pytest.mark.parametrize(RELATION_FIELDS, RELATIONS)
@pytest.mark.asyncio
async def test_instantiated_create(db, model_class, related_name, read_reverse, as_reverse):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    obj = await model_class.objects.create(name="Sang-Heon Jeon", school=school)
    assert (await model_class.objects.get(id=obj.id)).school_id == 1024


@pytest.mark.asyncio
async def test_principal__fetched_bool(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    await prefetch_related_objects([school], "principal")
    assert not bool(school.principal)
    await testmodels.Principal.objects.create(name="Sang-Heon Jeon", school=school)
    await prefetch_related_objects([school], "principal")
    assert bool(school.principal)


@pytest.mark.asyncio
async def test_principal__filter(db):
    school = await testmodels.School.objects.create(id=1024, name="School1")
    principal = await testmodels.Principal.objects.create(name="Sang-Heon Jeon1", school=school)
    assert await school.principal.filter(name="Sang-Heon Jeon1") == principal
    assert await school.principal.filter(name="Sang-Heon Jeon2") is None
