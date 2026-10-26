from __future__ import annotations

import importlib
import re
from typing import Any

from hare import fields
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP
from tests.contracts.meta_options.enums import MetaVariant
from tests.contracts.meta_options.meta_option_sample import MetaOptionSample


class MetaInheritanceModels:
    """The models of the Meta option matrix: an abstract base declaring an option, a concrete model under it
    in one of the ways of ``MetaVariant``, and a control declaring the value the concrete model must come to
    straight in its own ``Meta``."""

    @staticmethod
    def get_columns(sample: MetaOptionSample) -> dict[str, Any]:
        """The fields every model of the matrix has - those the options name; a model of an extension has its
        own key and version, so only the name.

        Args:
            sample: The option's sample.

        Returns:
            The fields by name, made anew.
        """
        if sample.base_model_path is not None:
            return {"name": fields.CharField(max_length=30, null=True)}
        key = {} if sample.option == "primary_key" else {"id": fields.IntField(primary_key=True)}
        return {
            **key,
            "name": fields.CharField(max_length=30, null=True),
            "company_id": fields.IntField(null=True),
            "owner_id": fields.IntField(null=True),
            "version": fields.IntField(default=0),
            "revision": fields.IntField(default=0),
            "deleted_at": fields.DatetimeField(null=True),
            "removed_at": fields.DatetimeField(null=True),
        }

    @staticmethod
    def get_base_class(sample: MetaOptionSample) -> type[Model]:
        """The class the abstract base of an option is built on.

        Args:
            sample: The option's sample.

        Returns:
            ``Model``, or the model of the extension that reads the option.
        """
        if sample.base_model_path is None:
            return Model
        module_name, class_name = sample.base_model_path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), class_name)

    @staticmethod
    def make_model(
        class_name: str,
        bases: tuple[type, ...],
        meta_options: dict[str, Any] | None,
        meta_bases: tuple[type, ...] = (),
        columns: dict[str, Any] | None = None,
    ) -> type[Model]:
        """One model.

        Args:
            class_name: The class name.
            bases: The base classes.
            meta_options: What its ``Meta`` declares, None for no ``Meta``.
            meta_bases: The base classes of the ``Meta``.
            columns: The fields it declares.

        Returns:
            The model.
        """
        namespace: dict[str, Any] = {"__module__": __name__}
        namespace.update(columns or {})
        if meta_options is not None:
            namespace["Meta"] = type("Meta", meta_bases, meta_options)
        return type(class_name, bases, namespace)

    @staticmethod
    def make(sample: MetaOptionSample, variant: MetaVariant) -> tuple[type[Model], type[Model]]:
        """The concrete model of a variant, and its control.

        Args:
            sample: The option's sample.
            variant: How the concrete model declares its ``Meta``.

        Returns:
            The concrete model and the control.
        """
        suffix = f"{sample.option.title().replace('_', '')}{variant.title().replace('_', '')}"
        model_base = MetaInheritanceModels.get_base_class(sample)
        columns = MetaInheritanceModels.get_columns(sample)
        base_meta = {"abstract": True, **sample.companion_options, sample.option: sample.make_base_value()}
        base = MetaInheritanceModels.make_model(f"Base{suffix}", (model_base,), base_meta, columns=columns)
        table_meta = {"app": CONTRACT_APP, "table": f"concrete_{suffix.lower()}"}
        if sample.option in {"app", "table"}:
            table_meta = {}
        declared: dict[str, Any]
        meta_bases: tuple[type, ...] = ()
        if variant is MetaVariant.NO_META:
            concrete_meta = None
            declared = {sample.option: sample.make_base_value()}
        elif variant is MetaVariant.BARE_META:
            concrete_meta, declared = dict(table_meta), {sample.option: sample.make_base_value()}
        elif variant is MetaVariant.META_OF_BASE:
            concrete_meta, declared = dict(table_meta), {sample.option: sample.make_base_value()}
            meta_bases = (base.Meta,)
        elif variant is MetaVariant.OVERRIDDEN:
            declared = {sample.option: sample.make_override_value()}
            concrete_meta = {**table_meta, **declared}
        else:
            declared = {sample.option: sample.cleared_value}
            concrete_meta = {**table_meta, **declared}
        concrete = MetaInheritanceModels.make_model(f"Concrete{suffix}", (base,), concrete_meta, meta_bases)
        control_meta = {**table_meta, **sample.companion_options, **declared}
        control = MetaInheritanceModels.make_model(
            f"Control{suffix}", (model_base,), control_meta, columns=MetaInheritanceModels.get_columns(sample)
        )
        return concrete, control

    @staticmethod
    def get_option_value(model: type[Model], option: str) -> str:
        """What a model holds of an option, as text comparable between models.

        Args:
            model: The model.
            option: The option.

        Returns:
            The value's text, without the addresses of objects.
        """
        meta = model._meta
        if option == "primary_key":
            return repr(meta.has_primary_key)
        value = getattr(meta, option) if hasattr(meta, option) else getattr(meta.meta, option, None)
        return re.sub(r" at 0x[0-9a-fA-F]+", "", repr(value))
