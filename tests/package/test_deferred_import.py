from typing import ClassVar

import pytest

from hare.classes.deferred_import import DeferredImport


@pytest.mark.database_independent
def test_deferred_import_reads_the_object_and_puts_it_on_the_class():
    class Holder:
        ordered_dict_class: ClassVar[DeferredImport[type]] = DeferredImport("collections", "OrderedDict")

    from collections import OrderedDict

    assert isinstance(Holder.__dict__["ordered_dict_class"], DeferredImport)
    assert Holder.ordered_dict_class is OrderedDict
    assert Holder.__dict__["ordered_dict_class"] is OrderedDict


@pytest.mark.database_independent
def test_deferred_import_read_through_an_instance_or_a_subclass_sets_it_on_the_declaring_class():
    class Holder:
        counter_class: ClassVar[DeferredImport[type]] = DeferredImport("collections", "Counter")

    class Child(Holder):
        pass

    from collections import Counter

    assert Child().counter_class is Counter
    assert Holder.__dict__["counter_class"] is Counter
    assert "counter_class" not in Child.__dict__


@pytest.mark.database_independent
def test_deferred_import_of_a_missing_name_raises_at_the_read():
    class Holder:
        missing: ClassVar[DeferredImport[type]] = DeferredImport("collections", "NoSuchName")

    with pytest.raises(AttributeError):
        Holder.missing
    assert isinstance(Holder.__dict__["missing"], DeferredImport)
