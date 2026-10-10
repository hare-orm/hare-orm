import pytest

from tests.testmodels import MyAbstractBaseModel, MyDerivedModel, MyOtherDerivedModel


@pytest.mark.asyncio
async def test_basic(db):
    """Test basic model inheritance with abstract base model."""
    model = MyDerivedModel(name="test")
    assert hasattr(MyAbstractBaseModel(), "name")
    assert hasattr(model, "created_at")
    assert hasattr(model, "modified_at")
    assert hasattr(model, "name")
    assert hasattr(model, "first_name")
    await model.save()
    assert model.created_at is not None
    assert model.modified_at is not None


def test_sibling_concrete_models_do_not_share_field_instances():
    """Two concrete models sharing the same abstract base (id, name) and the same mixin
    (created_at, modified_at) used to end up with the literal same Field objects in both
    fields_map - field.model = new_class (set right after class creation) then silently
    repointed the FIRST sibling's fields onto the SECOND sibling's class."""
    for field_name in ("id", "name", "created_at", "modified_at"):
        derived_field = MyDerivedModel._meta.fields_map[field_name]
        other_field = MyOtherDerivedModel._meta.fields_map[field_name]
        assert derived_field is not other_field
        assert derived_field.model is MyDerivedModel
        assert other_field.model is MyOtherDerivedModel
