from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.capture.change_extension import ChangeExtension
from hare.contrib.outbox.capture.constants import (
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
from hare.instrumentation.enums import ChangePayload, RowOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.captured_change import CapturedChange
    from hare.models import Model

#: A topic or ordering key: a template, or a function of the change.
KeyDeclaration = str | Callable[["CapturedChange"], str | None]


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
            captured model.
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

    Raises:
        ConfigurationError: An argument of the wrong type.
    """

    def __init__(
        self,
        outbox: type[OutboxEvent],
        *,
        operations: Iterable[RowOperation | str] = tuple(RowOperation),
        payload: ChangePayload | str = ChangePayload.AFTER,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
        topic: KeyDeclaration = DEFAULT_CHANGE_TOPIC_TEMPLATE,
        ordering_key: KeyDeclaration | None = DEFAULT_CHANGE_ORDERING_KEY_TEMPLATE,
        extend: Callable[[CapturedChange], ChangeExtension | None | Awaitable[ChangeExtension | None]] | None = None,
    ) -> None:
        if not isinstance(outbox, type) or not issubclass(outbox, OutboxEvent):
            raise ConfigurationError(f"ChangeCapture outbox must be an OutboxEvent model, got {outbox!r}")
        try:
            self.operations = frozenset(RowOperation(operation) for operation in operations)
            self.payload = ChangePayload(payload)
        except ValueError as error:
            raise ConfigurationError(f"ChangeCapture: {error}") from None
        if not self.operations:
            raise ConfigurationError("ChangeCapture operations must name at least one operation")
        if isinstance(fields, str) or isinstance(exclude, str):
            raise ConfigurationError("ChangeCapture fields and exclude take a list of field names, not a string")
        if fields is not None and exclude:
            raise ConfigurationError("ChangeCapture takes fields or exclude, not both")
        for declaration_name, declaration in (("topic", topic), ("ordering_key", ordering_key)):
            if declaration is not None and not isinstance(declaration, str) and not callable(declaration):
                raise ConfigurationError(f"ChangeCapture {declaration_name} must be a template or a function")
        if topic is None or (isinstance(topic, str) and not topic):
            raise ConfigurationError("ChangeCapture topic must be a non-empty template or a function")
        if extend is not None and not callable(extend):
            raise ConfigurationError(f"ChangeCapture extend must be a function, got {extend!r}")
        self.outbox = outbox
        self.fields = tuple(fields) if fields is not None else None
        self.exclude = tuple(exclude)
        self.topic = topic
        self.ordering_key = ordering_key
        self.extend = extend

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

        Raises:
            ValidationError: The key isn't text of 1 to 255 characters.
        """
        if declaration is None:
            return None
        key = (
            declaration.format(**self.get_template_values(model, change))
            if isinstance(declaration, str)
            else declaration(change)
        )
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= OUTBOX_KEY_MAX_LENGTH):
            raise ValidationError(
                f"{model.__name__}: a captured change's topic or ordering key must be a string of "
                f"1..{OUTBOX_KEY_MAX_LENGTH} characters, got {key!r}"
            )
        return key

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
            envelope["before"] = dict(change.before) if change.before is not None else None
            envelope["after"] = dict(change.after) if change.after is not None else None
        envelope["occurred_at"] = change.occurred_at
        return envelope

    async def get_extension(self, model: type[Model], change: CapturedChange) -> ChangeExtension | None:
        """What ``extend`` adds to a change's event.

        Args:
            model: The model.
            change: The change.

        Returns:
            The extension, None without one.

        Raises:
            TypeError: ``extend`` gave something other than a ``ChangeExtension`` or None.
            ValidationError: It sets a key of the envelope, or a field the outbox sets itself.
        """
        if self.extend is None:
            return None
        extension = self.extend(change)
        if inspect.isawaitable(extension):
            extension = await extension
        if extension is None:
            return None
        if not isinstance(extension, ChangeExtension):
            raise TypeError(f"{model.__name__}: ChangeCapture extend must give a ChangeExtension or None")
        colliding_keys = sorted(CHANGE_ENVELOPE_KEYS & set(extension.payload or ()))
        if colliding_keys:
            raise ValidationError(f"ChangeExtension.payload can't set the envelope's {', '.join(colliding_keys)}")
        managed_names = sorted(
            (ENQUEUE_ARGUMENT_FIELD_NAMES | RELAY_MANAGED_FIELD_NAMES) & set(extension.extra_field_values or ())
        )
        if managed_names:
            raise ValidationError(f"ChangeExtension.extra_field_values can't set {', '.join(managed_names)}")
        return extension

    async def write(self, connection: DatabaseClient, model: type[Model], changes: list[CapturedChange]) -> None:
        outbox = self.outbox
        tenant_field = outbox._meta.tenant_field
        events: list[OutboxEvent] = []
        for change in changes:
            payload = self.get_envelope(model, change)
            headers: dict[str, Any] = {}
            field_values: dict[str, Any] = {}
            extension = await self.get_extension(model, change)
            if extension is not None:
                payload.update(extension.payload or {})
                headers.update(extension.headers or {})
                field_values.update(extension.extra_field_values or {})
            if tenant_field and tenant_field not in field_values and change.tenant is not None:
                field_values[tenant_field] = change.tenant
            events.append(
                outbox(
                    topic=self.get_key(self.topic, model, change),
                    payload=payload,
                    ordering_key=self.get_key(self.ordering_key, model, change),
                    headers=headers,
                    **field_values,
                )
            )
        # Each event carries the tenant of its row - a write of rows of several tenants (a cascade, an
        # all_tenants() update) writes them all.
        manager = outbox.objects.all_tenants() if tenant_field else outbox.objects.all()
        await manager.using(connection).bulk_create(events)
        await outbox.signal_wakeup(connection, {event.topic for event in events})
