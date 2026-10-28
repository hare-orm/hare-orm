from __future__ import annotations

import copy
import hashlib
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Self, cast

from hare.contrib.outbox.capture.change_extension import ChangeExtension
from hare.contrib.outbox.capture.constants import (
    CHANGE_CAPTURE_OPTION_NAMES,
    CHANGE_ENVELOPE_KEYS,
    CHANGE_OPERATION_NAMES,
    DEFAULT_CHANGE_ORDERING_KEY_TEMPLATE,
    DEFAULT_CHANGE_TOPIC_TEMPLATE,
)
from hare.contrib.outbox.constants import (
    ENQUEUE_ARGUMENT_FIELD_NAMES,
    OUTBOX_KEY_MAX_LENGTH,
    RELAY_MANAGED_FIELD_NAMES,
)
from hare.contrib.outbox.outbox_event import OutboxEvent
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.change_sink import ChangeSink
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import ChangePayload, RowOperation

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.swappable_model_reference import SwappableModelReference
    from hare.instrumentation.capture.captured_change import CapturedChange
    from hare.models import Model

#: A topic or ordering key: a template, or a function of the change.
KeyDeclaration = str | Callable[["CapturedChange"], str | None]
#: What extend_many takes: a function of the changes of one write giving an extension of each, sync or async.
ExtendMany = Callable[
    [list["CapturedChange"]],
    Sequence[ChangeExtension | None] | Awaitable[Sequence[ChangeExtension | None]],
]


class ChangeCapture(ChangeSink):
    """Captures the changes of a model into a transactional outbox - ``Meta.change_capture =
    ChangeCapture(MyOutboxEvent)``. Every ORM write of the model writes an outbox event per changed
    row in its own transaction: saves, deletes, restores, ``update()``, bulk writes, the rows
    ``on_delete`` reaches - a database's own ``ON DELETE CASCADE``/``SET NULL`` included - and the rows
    of a many-to-many through model declaring it; raw SQL doesn't. Declared on an abstract base
    model, it captures every model built on it, each under its own name.

    The event's payload is the envelope ``{"model": "shop.Order", "operation": "updated", "pk": 7,
    "changed": ["status"], "before": {...}, "after": {...}, "occurred_at": "..."}`` - a composite
    key as an object of its fields; ``before``/``after`` as ``payload`` asks.

    Args:
        outbox: The concrete ``OutboxEvent`` model the events go to - on the connection of the
            captured model: its class, its ``"app.Model"`` label or a ``swappable()`` setting, found as
            the models are bound.
        operations: The operations captured - every one by default.
        payload: What an event holds of its row: ``KEYS`` - its key only; ``AFTER`` - the row as the
            write left it (as it was, for a delete); ``BEFORE_AND_AFTER`` - both, which needs
            ``Meta.track_dirty_fields``.
        fields: The fields an event holds - every field written in the model's table but the
            ``sensitive`` and encrypted ones by default; those only when named here. A relation is
            held by its key.
        exclude: Fields left out of the default ones.
        topic: The event's topic - a template of ``{app}``, ``{model}``, ``{table}`` and
            ``{operation}`` (``inserted``/``updated``/``deleted``), or a function of the change.
            ``"{app}.{model}.{operation}"`` in lower case by default - ``shop.order.updated``.
        ordering_key: The event's ordering key - a template of ``{label}`` (``shop.Order``),
            ``{pk}``, ``{app}``, ``{model}`` and ``{table}``, or a function of the change; the row's
            own (``"{label}:{pk}"``) by default, so one row's events are delivered in order. None for
            no ordering.
        extend: A function of the change - sync or async - giving a ``ChangeExtension`` to add to
            its event, or None; run at the write, in its transaction.
        extend_many: A function of the changes of one write statement - sync or async - giving the
            ``ChangeExtension`` (or None) of each, in their order: one call per statement of each model,
            where ``extend`` is called per row - for an extension read from the database for many rows at
            once. Not with ``extend``.

    Raises:
        ConfigurationError: An unknown operation or payload, both ``fields`` and ``exclude``, both
            ``extend`` and ``extend_many``.
    """

    def __init__(
        self,
        outbox: type[OutboxEvent] | str | SwappableModelReference,
        *,
        operations: Iterable[RowOperation | str] = tuple(RowOperation),
        payload: ChangePayload | str = ChangePayload.AFTER,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
        topic: KeyDeclaration = DEFAULT_CHANGE_TOPIC_TEMPLATE,
        ordering_key: KeyDeclaration | None = DEFAULT_CHANGE_ORDERING_KEY_TEMPLATE,
        extend: Callable[[CapturedChange], ChangeExtension | None | Awaitable[ChangeExtension | None]] | None = None,
        extend_many: ExtendMany | None = None,
    ) -> None:
        if fields is not None and exclude:
            raise ConfigurationError("ChangeCapture takes fields or exclude, not both")
        if extend is not None and extend_many is not None:
            raise ConfigurationError("ChangeCapture takes extend or extend_many, not both")
        #: The outbox model as it was given - a label is found as the registry links the models.
        self.outbox_reference = outbox
        self._outbox: type[OutboxEvent] | None = outbox if isinstance(outbox, type) else None
        self.operations: frozenset[RowOperation] = self.get_checked_option("operations", operations)
        self.payload: ChangePayload = self.get_checked_option("payload", payload)
        self.fields: tuple[str, ...] | None = self.get_checked_option("fields", fields)
        self.exclude: tuple[str, ...] = self.get_checked_option("exclude", exclude)
        self.topic = topic
        self.ordering_key = ordering_key
        self.extend = extend
        self.extend_many = extend_many

    @staticmethod
    def get_checked_option(name: str, value: Any) -> Any:
        """An option's value as the capture keeps it - checked and normalized.

        Args:
            name: The option, of ``CHANGE_CAPTURE_OPTION_NAMES``.
            value: Its value.

        Returns:
            The value kept.

        Raises:
            ConfigurationError: An unknown operation or payload.
        """
        try:
            if name == "operations":
                return frozenset(RowOperation(operation) for operation in value)
            if name == "payload":
                return ChangePayload(value)
        except ValueError as error:
            raise ConfigurationError(f"ChangeCapture: {error}") from None
        if name == "fields":
            return tuple(value) if value is not None else None
        if name == "exclude":
            return tuple(value)
        return value

    def replace(self, **options: Any) -> Self:
        """A copy with other options and the same outbox - for a model refining the capture of its abstract
        base: ``change_capture = Base.Meta.change_capture.replace(exclude=("secret",))``. ``fields`` replaces
        ``exclude`` and the other way round, ``extend`` replaces ``extend_many`` and the other way round; a
        subclass's own attributes are kept.

        Args:
            options: Options of the constructor besides ``outbox``.

        Returns:
            The copy.

        Raises:
            TypeError: An option the capture doesn't take.
            ConfigurationError: An option's value, as the constructor refuses it, or both of a pair given.
        """
        unknown_names = sorted(set(options) - CHANGE_CAPTURE_OPTION_NAMES)
        if unknown_names:
            raise TypeError(f"ChangeCapture.replace() takes no {', '.join(unknown_names)}")
        if "fields" in options and "exclude" in options and options["fields"] is not None and options["exclude"]:
            raise ConfigurationError("ChangeCapture takes fields or exclude, not both")
        if options.get("extend") is not None and options.get("extend_many") is not None:
            raise ConfigurationError("ChangeCapture takes extend or extend_many, not both")
        capture = copy.copy(self)
        if "fields" in options:
            capture.exclude = ()
        if "exclude" in options:
            capture.fields = None
        if "extend" in options:
            capture.extend_many = None
        if "extend_many" in options:
            capture.extend = None
        for name, value in options.items():
            setattr(capture, name, ChangeCapture.get_checked_option(name, value))
        return capture

    @property
    def outbox(self) -> type[OutboxEvent]:
        """The outbox model - a label given found in the registry, else in the current context.

        Raises:
            ConfigurationError: The label names no model.
        """
        if self._outbox is None:
            # Local import: the context imports the models, which import this module.
            from hare.core.hare_context import HareContext

            self._outbox = cast("type[OutboxEvent]", HareContext.require_current().get_model(self.outbox_reference))
        return self._outbox

    def link_models(self, apps: Apps) -> None:
        if not isinstance(self.outbox_reference, type):
            self._outbox = cast("type[OutboxEvent]", apps.get_model(self.outbox_reference))

    def get_field_names(self, model: type[Model]) -> tuple[str, ...]:
        meta = model._meta
        if self.outbox._meta.abstract:
            raise ConfigurationError(f"{model.__name__}: ChangeCapture outbox {self.outbox.__name__} is abstract")
        if model is self.outbox or self.outbox._meta.change_capture is not None:
            raise ConfigurationError(f"{model.__name__}: an outbox model can't capture changes itself")
        if self.payload is ChangePayload.BEFORE_AND_AFTER and not meta.track_dirty_fields:
            raise ConfigurationError(
                f"{model.__name__}: ChangeCapture(payload=BEFORE_AND_AFTER) needs Meta.track_dirty_fields = True - "
                "a saved instance's values before the write come from its snapshot"
            )
        for template_name, template in (("topic", self.topic), ("ordering_key", self.ordering_key)):
            if isinstance(template, str):
                try:
                    template.format(app="app", model="model", table="table", operation="updated", label="a.B", pk=1)
                except (KeyError, IndexError, ValueError) as error:
                    raise ConfigurationError(
                        f"{model.__name__}: ChangeCapture {template_name} {template!r} isn't a template of the "
                        f"known names: {error}"
                    ) from None
        projection = meta.fields_db_projection
        if self.fields is not None:
            names: list[str] = []
            for name in self.fields:
                names.extend(self.get_source_names(model, name))
            return tuple(dict.fromkeys(names))
        excluded = {source_name for name in self.exclude for source_name in self.get_source_names(model, name)}
        return tuple(
            name
            for name in projection
            if name not in excluded
            and not meta.fields_map[name].sensitive
            and not isinstance(meta.fields_map[name], EncryptedFieldBase)
        )

    @staticmethod
    def get_source_names(model: type[Model], name: str) -> tuple[str, ...]:
        """The fields written in the model's table a named field is held by.

        Args:
            model: The model.
            name: A field written in its table, or a forward relation.

        Returns:
            The field, or the relation's key fields.

        Raises:
            ConfigurationError: The name is neither.
        """
        meta = model._meta
        field = meta.fields_map.get(name)
        if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
            return tuple(field.source_fields)
        if name in meta.fields_db_projection:
            return (name,)
        raise ConfigurationError(
            f"{model.__name__}: ChangeCapture names {name!r} - not a field written in the model's table"
        )

    def get_written_models(self) -> tuple[type[Model], ...]:
        return (self.outbox,)

    @staticmethod
    def get_template_values(model: type[Model], change: CapturedChange) -> dict[str, Any]:
        """The values a topic or ordering key template reads.

        Args:
            model: The model.
            change: The change.

        Returns:
            The values by name.
        """
        meta = model._meta
        pk = change.pk
        return {
            "app": (meta.app or "").lower(),
            "model": model.__name__.lower(),
            "table": meta.db_table,
            "operation": CHANGE_OPERATION_NAMES[change.operation],
            "label": f"{meta.app}.{model.__name__}",
            "pk": ":".join(str(part) for part in pk) if isinstance(pk, tuple) else str(pk),
        }

    def get_key(self, declaration: KeyDeclaration | None, model: type[Model], change: CapturedChange) -> str | None:
        """A topic or ordering key of a change.

        Args:
            declaration: The template or function.
            model: The model.
            change: The change.

        Returns:
            The key, None without one.
        """
        if declaration is None:
            return None
        if isinstance(declaration, str):
            return self.get_shortened_key(declaration.format(**self.get_template_values(model, change)))
        return declaration(change)

    @staticmethod
    def get_shortened_key(key: str) -> str:
        """A key made from a template, cut to the length an outbox key may have - a long primary key
        makes a long one: its start kept, then the SHA-256 of the whole key, so different keys stay
        different.

        Args:
            key: The key.

        Returns:
            The key itself when it fits, else the shortened one.
        """
        if len(key) <= OUTBOX_KEY_MAX_LENGTH:
            return key
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"{key[: OUTBOX_KEY_MAX_LENGTH - len(digest) - 1]}#{digest}"

    def get_envelope(self, model: type[Model], change: CapturedChange) -> dict[str, Any]:
        """The payload of a change's event.

        Args:
            model: The model.
            change: The change.

        Returns:
            The envelope.
        """
        meta = model._meta
        pk = change.pk
        envelope: dict[str, Any] = {
            "model": f"{meta.app}.{model.__name__}",
            "operation": CHANGE_OPERATION_NAMES[change.operation],
            "pk": dict(zip(meta.primary_key_attribute_names, pk, strict=True)) if isinstance(pk, tuple) else pk,
            "changed": list(change.changed) if change.changed is not None else None,
        }
        if self.payload is not ChangePayload.KEYS:
            envelope["before"] = ChangeCapture.get_row_values(model, change.before)
            envelope["after"] = ChangeCapture.get_row_values(model, change.after)
        envelope["occurred_at"] = change.occurred_at
        return envelope

    @staticmethod
    def get_row_values(model: type[Model], values: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """A captured row's values as its event holds them - each in its field's JSON form
        (``Field.get_json_value()``): a geometry as GeoJSON, a range as its mapping, a float JSON has no
        number for as its text, at any depth of a container.

        Args:
            model: The row's model.
            values: The row's values by field name, None when not captured.

        Returns:
            The values.

        Raises:
            TypeError: A value has no JSON form.
        """
        if values is None:
            return None
        fields_map = model._meta.fields_map
        return {name: fields_map[name].get_json_value(value) for name, value in values.items()}

    async def get_extensions(self, model: type[Model], changes: list[CapturedChange]) -> list[ChangeExtension | None]:
        """What ``extend`` - once per change - or ``extend_many`` - once per write - adds to the changes'
        events.

        Args:
            model: The model.
            changes: The changes of one write statement.

        Returns:
            The extension of each change, in their order.

        Raises:
            ValidationError: An extension sets a key of the envelope or a field the outbox sets itself, or
                extend_many gives another number of extensions than changes.
        """
        if self.extend_many is None:
            return [await self.get_extension(model, change) for change in changes]
        extensions = self.extend_many(changes)
        if inspect.isawaitable(extensions):
            extensions = await extensions
        extension_list = list(extensions)
        if len(extension_list) != len(changes):
            raise ValidationError(f"extend_many gave {len(extension_list)} extensions for {len(changes)} changes")
        return [ChangeCapture.check_extension(extension) for extension in extension_list]

    async def get_extension(self, model: type[Model], change: CapturedChange) -> ChangeExtension | None:
        """What ``extend`` adds to a change's event.

        Args:
            model: The model.
            change: The change.

        Returns:
            The extension, None without one.

        Raises:
            ValidationError: It sets a key of the envelope, or a field the outbox sets itself.
        """
        if self.extend is None:
            return None
        extension = self.extend(change)
        if inspect.isawaitable(extension):
            extension = await extension
        return ChangeCapture.check_extension(extension)

    @staticmethod
    def check_extension(extension: ChangeExtension | None) -> ChangeExtension | None:
        """An extension, checked.

        Args:
            extension: The extension, None for none.

        Returns:
            The extension.

        Raises:
            ValidationError: It sets a key of the envelope, or a field the outbox sets itself, or its
                headers aren't a mapping of non-empty text names.
        """
        if extension is None:
            return None
        OutboxEvent.validate_headers(extension.headers)
        colliding_keys = sorted(CHANGE_ENVELOPE_KEYS & set(extension.payload or ()))
        if colliding_keys:
            raise ValidationError(f"ChangeExtension.payload can't set the envelope's {', '.join(colliding_keys)}")
        managed_names = sorted(
            (ENQUEUE_ARGUMENT_FIELD_NAMES | RELAY_MANAGED_FIELD_NAMES) & set(extension.extra_field_values or ())
        )
        if managed_names:
            raise ValidationError(f"ChangeExtension.extra_field_values can't set {', '.join(managed_names)}")
        return extension

    def check_connection(self, connection: DatabaseClient) -> None:
        """Refuses a write whose events the outbox can't take on its connection - before the write runs.

        Args:
            connection: The connection of the write.

        Raises:
            ConfigurationError: The outbox's wakeup can't signal on the connection's database.
            QueryError: The outbox has ``Meta.tenant_schema`` and no single tenant is active.
        """
        outbox = self.outbox
        outbox.raise_if_wakeup_unsupported(connection)
        if outbox._meta.checks_tenant_client:
            # The events go on the connection of the captured write - checked as the outbox's own query is.
            outbox._meta.check_tenant_client(connection)

    async def write(self, connection: DatabaseClient, model: type[Model], changes: list[CapturedChange]) -> None:
        """Writes an outbox event of each change, in the transaction of the write.

        Args:
            connection: The connection of the write.
            model: The model whose rows changed.
            changes: The changes.

        Raises:
            ValidationError: An extension sets a key of the envelope or a field the outbox sets itself,
                extend_many gives another number of extensions than changes, or a topic or ordering key is
                empty text.
        """
        outbox = self.outbox
        tenant_field = outbox._meta.tenant_field
        events: list[OutboxEvent] = []
        extensions = await self.get_extensions(model, changes)
        for change, extension in zip(changes, extensions, strict=True):
            payload = self.get_envelope(model, change)
            headers: dict[str, Any] = {}
            field_values: dict[str, Any] = {}
            if extension is not None:
                payload.update(extension.payload or {})
                headers.update(extension.headers or {})
                field_values.update(extension.extra_field_values or {})
            if tenant_field and tenant_field not in field_values and change.tenant is not None:
                field_values[tenant_field] = change.tenant
            topic = self.get_key(self.topic, model, change)
            ordering_key = self.get_key(self.ordering_key, model, change)
            OutboxEvent.validate_keys(topic, None, ordering_key)
            events.append(
                outbox(topic=topic, payload=payload, ordering_key=ordering_key, headers=headers, **field_values)
            )
        # Each event carries the tenant of its row - a write of rows of several tenants (a cascade, an
        # all_tenants() update) writes them all.
        manager = outbox.objects.all_tenants() if tenant_field else outbox.objects.all()
        # The events are rows of the outbox's model, not of the captured write - reported of their own,
        # a delete reported as a whole too.
        with ChangeEvents.reporting_separately():
            await manager.using(connection).bulk_create(events)
        await outbox.signal_wakeup(connection, {event.topic for event in events})
