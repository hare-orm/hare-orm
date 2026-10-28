"""The registry of the field contract matrix is complete: every field class of hare is exported for models or
named as one no model declares, and every exported one has a sample - a field class added to hare fails
here until the contract tests check it."""

import pytest

from hare.exceptions import ConfigurationError
from tests.contracts.constants import FIELD_CLASSES_NOT_DECLARED
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.hare_registry import HareRegistry

pytestmark = pytest.mark.database_independent


def test_every_field_class_is_exported_or_declared_by_no_model():
    exported = HareRegistry.get_exported_field_classes()
    unclassified = [
        path
        for path in map(HareRegistry.get_path, HareRegistry.get_field_classes())
        if path not in exported and path not in FIELD_CLASSES_NOT_DECLARED
    ]
    assert unclassified == []


def test_the_classes_no_model_declares_exist():
    paths = {HareRegistry.get_path(field_class) for field_class in HareRegistry.get_field_classes()}
    assert sorted(set(FIELD_CLASSES_NOT_DECLARED) - paths) == []


def get_declared_sample_classes() -> set[str]:
    """The classes of the samples the run declares - one needing a package the run lacks to be declared
    (PhoneField with region=, JSONField of a pydantic model) is left out; its class has another sample."""
    sampled = set()
    for sample in FIELD_SAMPLES:
        try:
            sampled.add(HareRegistry.get_path(sample.get_field_class()))
        except (ConfigurationError, ImportError):
            continue
    return sampled


def test_every_exported_field_class_has_a_sample():
    sampled = get_declared_sample_classes()
    declared = set(HareRegistry.get_exported_field_classes()) - set(FIELD_CLASSES_NOT_DECLARED)
    assert sorted(declared - sampled) == []


def test_the_sample_names_are_unique():
    names = [sample.name for sample in FIELD_SAMPLES]
    assert len(names) == len(set(names))
