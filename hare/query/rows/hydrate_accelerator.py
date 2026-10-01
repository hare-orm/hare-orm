from __future__ import annotations

import copy
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.log import logger
from hare.fields.base.field import Field
from hare.fields.data.numeric.int_field import IntField
from hare.query.plans.statement_plans import StatementPlans
from hare.query.rows.constants import SNAPSHOT_KEPT_VALUE_TYPES, TEMPORAL_WRITE_CODEC_KINDS
from hare.query.rows.enums import ReadCodecKind, WriteCodecKind
from hare.query.rows.field_codecs import FieldCodecs
from hare.utils import Timezone
from hare.utils.native_modules import NativeModules

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.rows.hydration_layout import HydrationEntry
    from hare.query.rows.value_field import ValueField


class HydrateAccelerator:
    """The optional compiled ``rust.native.rows`` module, which reads the instances of a whole result
    and writes the values of a batch of instances in one call each, and the readers and writers it
    does that with."""

    #: The compiled module - None where it isn't built, or once it turned out incompatible.
    module: ClassVar[Any] = NativeModules.rows
    #: Whether the incompatibility was logged already.
    incompatibility_logged: ClassVar[bool] = False

    @classmethod
    def disable(cls, error: Exception) -> None:
        """Switches the accelerator off for the rest of the process - called once the pure-Python
        path read the same rows a call into it raised a TypeError/AttributeError on, so the error
        was the compiled module's (a build older than its call site), not the data's.

        Args:
            error: The error the call raised.
        """
        if not cls.incompatibility_logged:
            cls.incompatibility_logged = True
            logger.warning(
                "rust.native.rows accelerator raised %s calling into it (%s) - falling back to the "
                "pure-Python hydration path for the rest of this process. Rebuild it with "
                "`make build_native`.",
                type(error).__name__,
                error,
            )
        cls.module = None

    @classmethod
    def run_or_fall_back[Result](cls, accelerated: Callable[[], Result], pure_python: Callable[[], Result]) -> Result:
        """The result of the accelerated call, or of the pure-Python one when the accelerated call
        raises a TypeError/AttributeError. The pure-Python call decides whose fault that was: when
        it fails the same way, the error was the caller's and propagates; when it succeeds, the
        compiled module is out of step with its call site and is switched off.

        Args:
            accelerated: The call into ``rust.native.rows``.
            pure_python: The same work in Python.

        Returns:
            The result.
        """
        try:
            return accelerated()
        except (TypeError, AttributeError) as error:
            result = pure_python()
            cls.disable(error)
            return result

    @staticmethod
    def get_values_reader(model: type[Model], value_fields: tuple[ValueField, ...], db: DatabaseClient) -> Any:
        """The ``rust.native.rows.ValuesReader`` of the columns of a values query under the time zone
        settings active now.

        Args:
            model: The queried model.
            value_fields: What each selected column is read as, in selection order.
            db: The connection the rows are read on.

        Returns:
            The reader.
        """
        types = db.dialect.types
        native_types = db.native_python_types
        zone_name = Timezone.get_aware_zone_name()
        key = (value_fields, types, native_types, zone_name)
        readers = StatementPlans.values_readers
        reader = readers.get_for_model(model, key)
        if reader is None:
            module = HydrateAccelerator.module
            codecs = []
            for value_field in value_fields:
                if value_field.is_native or value_field.field is None:
                    kind, options = ReadCodecKind.AS_IS, cast("dict[str, Any]", {})
                else:
                    kind, options = FieldCodecs.get_expression_read_spec(
                        value_field.name, value_field.field, types, zone_name
                    )
                codecs.append(module.FieldCodec(value_field.name, kind, options))
            reader = module.ValuesReader(codecs)
            readers[(model, *key)] = reader
        return reader

    @staticmethod
    def get_model_reader(
        model: type[Model],
        entries: tuple[HydrationEntry, ...],
        is_partial: bool,
        types: TypeRegistry,
        zone_name: str | None,
    ) -> Any:
        """The ``rust.native.rows.ModelReader`` of a model's columns under a time zone setting - it
        is part of the cache key, so contexts with different settings never share a reader.

        Args:
            model: The model.
            entries: The hydration entry of each column.
            is_partial: Whether only some of the model's columns are selected.
            types: The dialect's type registry.
            zone_name: The zone aware datetimes are read in (``Timezone.get_aware_zone_name()``).

        Returns:
            The reader.
        """
        key = (entries, is_partial, types, zone_name)
        readers = StatementPlans.model_readers
        reader = readers.get_for_model(model, key)
        if reader is None:
            module = HydrateAccelerator.module
            codecs = []
            for entry in entries:
                kind, options = FieldCodecs.get_read_spec(entry, types, zone_name)
                codecs.append(module.FieldCodec(entry[0], kind, options))
            meta = model._meta
            if meta.track_dirty_fields:
                reader = module.ModelReader(
                    model,
                    codecs,
                    is_partial,
                    meta.default_custom_generated_pk,
                    list(meta.direct_fields),
                    SNAPSHOT_KEPT_VALUE_TYPES,
                    copy.deepcopy,
                )
            else:
                reader = module.ModelReader(model, codecs, is_partial, meta.default_custom_generated_pk)
            readers[(model, *key)] = reader
        return reader

    @staticmethod
    def get_lookup_writer(
        field: Field[Any], types: TypeRegistry
    ) -> tuple[Callable[[Any, Any], Any], type | None] | None:
        """The codec converting a filter's value of a field as ``TypeRegistry.get_lookup_value()``
        does - for a field whose filter value is the value it writes, whatever the time zone.

        Args:
            field: The field.
            types: The dialect's type registry.

        Returns:
            The codec's ``write`` taking the value and the model, and the only value type it
            converts (None for every type); None when the field converts its filter values itself.
        """
        lookup_codec = HydrateAccelerator.get_lookup_codec(field, types)
        if lookup_codec is None:
            return None
        codec, value_type = lookup_codec
        return codec.write, value_type

    @staticmethod
    def get_lookup_list_writer(
        field: Field[Any], types: TypeRegistry
    ) -> Callable[[Any, Any], list[Any] | None] | None:
        """The codec converting a list of a field's filter values (``__in``) as
        ``TypeRegistry.get_lookup_value()`` converts each one - kept per field.

        Args:
            field: The field.
            types: The dialect's type registry.

        Returns:
            The codec's ``write_list`` taking the values and the model, giving None for a list
            with a value of another type than the codec converts; None when the field converts its
            filter values itself.
        """
        key = (field.model, field, types)
        writers = StatementPlans.lookup_list_writers
        list_writer = writers.get(key)
        if list_writer is None:
            lookup_codec = HydrateAccelerator.get_lookup_codec(field, types)
            list_writer = (
                False if lookup_codec is None else partial(lookup_codec[0].write_list, value_type=lookup_codec[1])
            )
            writers[key] = list_writer
        return list_writer or None

    @staticmethod
    def get_lookup_codec(field: Field[Any], types: TypeRegistry) -> tuple[Any, type | None] | None:
        """The ``rust.native.rows.FieldCodec`` converting a filter's value of a field as
        ``TypeRegistry.get_lookup_value()`` does, and the only value type it converts (None for
        every type); None when the field converts its filter values itself.

        Args:
            field: The field.
            types: The dialect's type registry.

        Returns:
            The codec and the type.
        """
        module = HydrateAccelerator.module
        field_class = type(field)
        if (
            module is None
            or types.get_lookup_converter(field_class) is not None
            or types.get_db_converter(field_class) is not None
        ):
            return None
        if field_class.to_lookup_value is Field.to_lookup_value:
            value_type = None
        elif field_class.to_lookup_value is IntField.to_lookup_value:
            # Any other value is checked and converted another way than it is written.
            value_type = int
        else:
            return None
        kind, options = FieldCodecs.get_write_spec(field, types, None)
        if kind in TEMPORAL_WRITE_CODEC_KINDS or kind is WriteCodecKind.CALL:
            return None
        codec = module.FieldCodec(field.model_field_name, ReadCodecKind.AS_IS, {}, kind, options)
        return codec, value_type

    @staticmethod
    def get_model_writer(model: type[Model], columns: tuple[str, ...], types: TypeRegistry) -> Any:
        """The ``rust.native.rows.ModelWriter`` of a model's written columns under the time zone
        settings active now.

        Args:
            model: The model.
            columns: The written fields, in column order.
            types: The dialect's type registry.

        Returns:
            The writer.
        """
        zone_name = Timezone.get_aware_zone_name()
        key = (columns, types, zone_name)
        writers = StatementPlans.model_writers
        writer = writers.get_for_model(model, key)
        if writer is None:
            module = HydrateAccelerator.module
            fields_map = model._meta.fields_map
            codecs = []
            for name in columns:
                kind, options = FieldCodecs.get_write_spec(fields_map[name], types, zone_name)
                codecs.append(module.FieldCodec(name, ReadCodecKind.AS_IS, {}, kind, options))
            writer = module.ModelWriter(codecs)
            writers[(model, *key)] = writer
        return writer
