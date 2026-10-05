from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pydantic import ConfigDict

from hare.contrib.pydantic.descriptions.pydantic_meta_data import PydanticMetaData
from hare.contrib.pydantic.models.pydantic_model import PydanticModel

if TYPE_CHECKING:
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator
    from hare.models import Model


class PydanticMetaReading:
    """The PydanticMeta options of a model - collected from the model and its bases, overridden by the
    creator's arguments - that decide which fields the pydantic model gets and how."""

    @staticmethod
    def collect_pydantic_meta(model: type[Model]) -> PydanticMetaData:
        """Merges the ``PydanticMeta`` of every class in ``model``'s MRO declaring its own - with several
        abstract bases, none may silently win by base order. List settings
        (include/exclude/computed) accumulate; scalar settings and ``model_config`` take the most
        derived class's value.
        """
        accumulated: PydanticMetaData | None = None
        for klass in reversed(model.__mro__):
            meta = klass.__dict__.get("PydanticMeta")
            if meta is None:
                continue
            klass_meta = PydanticMetaData.from_pydantic_meta(meta)
            if accumulated is None:
                accumulated = klass_meta
            else:
                accumulated = dataclasses.replace(
                    klass_meta,
                    include=accumulated.include + klass_meta.include,
                    exclude=accumulated.exclude + klass_meta.exclude,
                    computed=accumulated.computed + klass_meta.computed,
                )
        return accumulated if accumulated is not None else PydanticMetaData()

    @staticmethod
    def get_meta(
        model: type[Model],
        meta_override: type | None,
        exclude: tuple[str, ...],
        include: tuple[str, ...],
        computed: tuple[str, ...],
        allow_cycles: bool | None,
        sort_alphabetically: bool | None,
        max_recursion: int | None,
        model_config: ConfigDict | None,
    ) -> PydanticMetaData:
        """The options of a model's schema: its ``PydanticMeta``, an override's, then the call's arguments.

        Args:
            model: The model.
            meta_override: A class whose attributes replace the model's ``PydanticMeta`` ones.
            exclude: The fields left out.
            include: The fields kept - every field when empty.
            computed: The computed fields.
            allow_cycles: Whether relations may lead back to a model in the schema.
            sort_alphabetically: Whether the fields are sorted by name.
            max_recursion: How deep relations are followed.
            model_config: The Pydantic config of the schema.

        Returns:
            The options.
        """
        meta_from_class = PydanticMetaReading.collect_pydantic_meta(model)
        if meta_override:
            meta_from_class = meta_from_class.construct_pydantic_meta(meta_override)
        meta = meta_from_class.finalize_meta(
            exclude=exclude,
            include=include,
            computed=computed,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            max_recursion=max_recursion,
            model_config=model_config,
        )
        return dataclasses.replace(
            meta,
            include=PydanticMetaReading.with_generic_branches(model, meta.include),
            exclude=PydanticMetaReading.with_generic_branches(model, meta.exclude),
        )

    @staticmethod
    def with_generic_branches(model: type[Model], names: tuple[str, ...]) -> tuple[str, ...]:
        """Adds the branches of each generic foreign key named - and their key fields - to names a
        schema includes or leaves out: the field stands for them.

        Args:
            model: The model.
            names: Field names, possibly naming generic foreign keys.

        Returns:
            ``names`` followed by the branch names and key fields of the generic foreign keys it names.
        """
        meta = model._meta
        branch_names = [
            name
            for generic_name in names
            if generic_name in meta.generic_foreign_key_fields
            for branch_name in meta.generic_foreign_key_fields[generic_name].branch_names
            for name in (branch_name, *meta.fields_map[branch_name].source_fields)  # type: ignore[attr-defined]
        ]
        return names + tuple(branch_names)

    @staticmethod
    def initialize_pconfig(creator: PydanticModelCreator) -> ConfigDict:
        """The Pydantic config of the schema: the meta's, a title, extra fields forbidden and bytes as base64
        in JSON.

        Args:
            creator: The creator of the schema.

        Returns:
            The config.
        """
        pconfig: ConfigDict = PydanticModel.model_config.copy()
        if creator.meta.model_config:
            pconfig.update(creator.meta.model_config)
        if "title" not in pconfig:
            pconfig["title"] = creator._title
        if "extra" not in pconfig:
            pconfig["extra"] = "forbid"
        # BinaryField values are arbitrary bytes - JSON carries them as base64 (the schema says
        # so too), not as UTF-8 text that non-UTF-8 bytes can't be encoded to.
        pconfig.setdefault("ser_json_bytes", "base64")
        pconfig.setdefault("val_json_bytes", "base64")
        return pconfig
