from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pydantic import ConfigDict

from hare.contrib.pydantic.descriptions.pydantic_meta_data import PydanticMetaData
from hare.contrib.pydantic.models.pydantic_model import PydanticModel

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator
    from hare.models import Model


class PydanticMetaReading:
    """The PydanticMeta options of a model - collected from the model and its bases, overridden by the
    creator's arguments - that decide which fields the pydantic model gets and how."""

    @staticmethod
    def collect_pydantic_meta(cls: type[Model]) -> PydanticMetaData:
        """Merges the ``PydanticMeta`` of every class in ``cls``'s MRO declaring its own - with several
        abstract bases, none may silently win by base order. List settings
        (include/exclude/computed) accumulate; scalar settings and ``model_config`` take the most
        derived class's value.
        """
        accumulated: PydanticMetaData | None = None
        for klass in reversed(cls.__mro__):
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
        cls: type[Model],
        meta_override: type | None,
        exclude: tuple[str, ...],
        include: tuple[str, ...],
        computed: tuple[str, ...],
        allow_cycles: bool | None,
        sort_alphabetically: bool | None,
        max_recursion: int | None,
        model_config: ConfigDict | None,
    ) -> PydanticMetaData:
        meta_from_class = PydanticMetaReading.collect_pydantic_meta(cls)
        if meta_override:
            meta_from_class = meta_from_class.construct_pydantic_meta(meta_override)
        return meta_from_class.finalize_meta(
            exclude=exclude,
            include=include,
            computed=computed,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            max_recursion=max_recursion,
            model_config=model_config,
        )

    @staticmethod
    def initialize_pconfig(creator: PydanticModelCreator) -> ConfigDict:
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
