"""A plan key holds every setting written into the SQL text: each ``QueryOptions`` setting and each
``QuerySpecification`` slot is declared with its part in the key, and a setting left out of the declarations is
refused when the class loads."""

from typing import Any, ClassVar

import pytest

from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.query_specification import QuerySpecification


class StubGroupingSet:
    """A grouping set with its own plan key."""

    def get_plan_key(self) -> tuple[str, ...]:
        return ("rollup", "name")


#: A set value of each setting of ``QueryOptions.SQL_SETTINGS``.
SET_SQL_SETTINGS: dict[str, Any] = {
    "select_for_update": True,
    "select_for_update_nowait": True,
    "select_for_update_skip_locked": True,
    "select_for_update_of": {"self"},
    "select_for_update_strength": "share",
    "reverse_result_order": True,
    "distinct_on": ("name",),
    "group_bys": ("name",),
    "grouping_set": StubGroupingSet(),
    "table_sample": "sample",
    "alias_keys": {"alias"},
    "is_single_row_of_slice": True,
    "default_ordering_disabled": True,
    "fields_for_select": ("name",),
    "deferred_fields": ("name",),
    "deferred_related_fields": {"author"},
    "explicitly_select_related": {"author"},
}


def test_every_sql_setting_is_given_a_value():
    assert set(SET_SQL_SETTINGS) == set(QueryOptions.SQL_SETTINGS)


@pytest.mark.parametrize("name", QueryOptions.SQL_SETTINGS)
def test_each_sql_setting_changes_the_plan_key(name):
    options = QueryOptions.DEFAULT.updated(**{name: SET_SQL_SETTINGS[name]})
    assert options.get_plan_key_part() != QueryOptions.DEFAULT.get_plan_key_part()


def test_the_default_settings_make_their_part_without_building_it():
    assert QueryOptions.DEFAULT.get_plan_key_part() is QueryOptions.DEFAULT_PLAN_KEY_PART
    assert QueryOptions.DEFAULT_PLAN_KEY_PART == QueryOptions.DEFAULT.build_plan_key_part()


def test_a_table_sample_keeps_no_plan():
    assert not QueryOptions.DEFAULT.keeps_no_plan()
    assert QueryOptions.DEFAULT.updated(table_sample="sample").keeps_no_plan()
    assert not QueryOptions.DEFAULT.updated(distinct_on=("name",)).keeps_no_plan()


def test_an_unclassified_setting_is_refused():
    class OptionsWithANewSetting(QueryOptions):
        DEFAULTS: ClassVar[dict[str, Any]] = {**QueryOptions.DEFAULTS, "new_setting": False}

    with pytest.raises(TypeError, match=r"unclassified: \['new_setting'\]"):
        OptionsWithANewSetting.raise_if_settings_unclassified()


def test_an_unclassified_specification_slot_is_refused():
    class SpecWithANewSlot(QuerySpecification, abstract=True):
        SPECIFICATION_SLOTS: ClassVar[tuple[str, ...]] = (*QuerySpecification.SPECIFICATION_SLOTS, "_new_slot")

    with pytest.raises(TypeError, match=r"unclassified: \['_new_slot'\]"):
        SpecWithANewSlot.raise_if_slots_unclassified()
