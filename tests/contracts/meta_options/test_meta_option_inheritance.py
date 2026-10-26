"""Meta option x inheritance matrix: every option an abstract base declares reaches a concrete model the way
the option declared in the model's own Meta does - with no Meta, a bare Meta, and a Meta of the base's; a
concrete model overriding the option, or naming None for it, gets that. The registry is complete: every
option hare takes is sampled or named with why not."""

import pytest

from hare.models.class_building.extension_model_options import ExtensionModelOptions
from hare.models.enums import ModelOption
from tests.contracts.constants import META_OPTIONS_NOT_SAMPLED
from tests.contracts.hare_registry import HareRegistry
from tests.contracts.meta_options.enums import MetaVariant
from tests.contracts.meta_options.meta_inheritance_models import MetaInheritanceModels
from tests.contracts.meta_options.meta_option_samples import META_OPTION_SAMPLES

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("variant", list(MetaVariant))
@pytest.mark.parametrize("sample", [sample.get_parameter() for sample in META_OPTION_SAMPLES])
def test_a_concrete_model_gets_an_option_of_its_abstract_base_as_declared(sample, variant):
    concrete, control = MetaInheritanceModels.make(sample, variant)
    assert MetaInheritanceModels.get_option_value(concrete, sample.option) == (
        MetaInheritanceModels.get_option_value(control, sample.option)
    )
    assert not concrete._meta.abstract


@pytest.mark.parametrize("variant", list(MetaVariant))
def test_a_table_inherited_from_the_abstract_base_is_an_explicit_table(variant):
    sample = next(sample for sample in META_OPTION_SAMPLES if sample.option == "table")
    concrete, control = MetaInheritanceModels.make(sample, variant)
    assert concrete._meta.table_is_explicit == control._meta.table_is_explicit


def test_every_option_hare_takes_is_sampled_or_named_with_why_not():
    HareRegistry.get_installed_modules()
    taken = {option.value for option in ModelOption} | set(ExtensionModelOptions.names)
    sampled = {sample.option for sample in META_OPTION_SAMPLES}
    assert sorted(taken - sampled - set(META_OPTIONS_NOT_SAMPLED)) == []


def test_the_options_named_as_not_sampled_exist_and_are_not_sampled():
    taken = {option.value for option in ModelOption}
    sampled = {sample.option for sample in META_OPTION_SAMPLES}
    assert sorted(set(META_OPTIONS_NOT_SAMPLED) - taken) == []
    assert sorted(set(META_OPTIONS_NOT_SAMPLED) & sampled) == []
