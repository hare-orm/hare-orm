import pytest

from tests.testmodels import ManagerModel, ManagerModelExtra


def test_meta_manager_is_not_shared_between_abstract_base_subclasses():
    """ModelMeta.__new__ merges every abstract ancestor's Meta attributes into each concrete
    subclass's OWN Meta class dict (a plain dict update) - a Meta.manager declared on an
    abstract base used to end up as the LITERAL SAME Manager object on every concrete subclass
    unless MetaInfo.__init__ gave each one its own copy. ModelMeta.__new__ then mutates
    manager._model = new_class in place, so the shared object's _model silently ended up
    pointing at whichever subclass was defined LAST - every earlier subclass's own manager
    (and thus .all()/.filter()/etc.) queried and hydrated as that last subclass instead of
    itself. Only reproducible with two subclasses that BOTH inherit the manager unmodified -
    unlike ManagerModel/ManagerModelExtra above, where ManagerModel declares its own
    Meta.manager, leaving only one real consumer of AbstractManagerModel's shared instance."""
    from hare import fields
    from hare.models import Model
    from hare.query.manager import Manager

    class AbstractShared(Model):
        name = fields.CharField(max_length=50)

        class Meta:
            abstract = True
            manager = Manager()

    class FirstChild(AbstractShared):
        class Meta:
            table = "meta_manager_first_child"

    class SecondChild(AbstractShared):
        class Meta:
            table = "meta_manager_second_child"

    assert FirstChild._meta.manager is not SecondChild._meta.manager
    assert FirstChild._meta.manager._model is FirstChild
    assert SecondChild._meta.manager._model is SecondChild


def test_meta_manager_copy_preserves_custom_constructor_state():
    """The per-subclass copy MetaInfo.__init__ now makes must preserve any extra instance state
    a custom Manager subclass's own __init__ sets on itself (e.g. StatusManager's queryset_cls
    above) - not just re-run the subclass's __init__ with no arguments, which would silently
    fall back to its own defaults and lose whatever the abstract base's Meta.manager was
    actually configured with."""
    from hare import fields
    from hare.models import Model
    from hare.query.manager import Manager
    from hare.query.queryset import QuerySet

    class TaggedQuerySet(QuerySet):
        pass

    class TaggedManager(Manager):
        def __init__(self, queryset_class=None, tag="untagged"):
            super().__init__(queryset_class)
            self.tag = tag

    class AbstractTagged(Model):
        name = fields.CharField(max_length=50)

        class Meta:
            abstract = True
            manager = TaggedManager(TaggedQuerySet, tag="tagged")

    class TaggedChild(AbstractTagged):
        class Meta:
            table = "meta_manager_tagged_child"

    assert TaggedChild._meta.manager.queryset_class is TaggedQuerySet
    assert TaggedChild._meta.manager.tag == "tagged"
    assert isinstance(TaggedChild.objects, TaggedQuerySet)


@pytest.mark.asyncio
async def test_manager(db):
    """Test custom manager functionality with active status filtering."""
    m1 = await ManagerModel.objects.create()
    m2 = await ManagerModel.objects.create(status=1)

    assert await ManagerModel.objects.all().active().count() == 1
    assert await ManagerModel.all_objects.count() == 2

    assert await ManagerModel.objects.all().active().get_or_none(pk=m1.pk) is None
    assert await ManagerModel.all_objects.get_or_none(pk=m1.pk) is not None
    assert await ManagerModel.objects.get_or_none(pk=m2.pk) is not None

    await ManagerModelExtra.objects.create(extra="extra")
    assert await ManagerModelExtra.all_objects.count() == 1
    assert await ManagerModelExtra.objects.all().count() == 1
