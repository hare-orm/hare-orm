from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.models import Model


__all__ = [
    "HareError",
    "ConfigurationError",
    "FieldError",
    "QueryError",
    "NoValuesFetched",
    "IncompleteInstanceError",
    "ValidationError",
    "UnSupportedError",
    "ObjectLookupError",
    "MultipleObjectsReturned",
    "DoesNotExist",
    "TransactionManagementError",
    "DistributedTransactionPartiallyCommittedError",
    "DistributedTransactionCommitAmbiguousError",
    "DatabaseError",
    "OperationalError",
    "TransactionRetryError",
    "TooManyParametersError",
    "CascadeDepthLimitError",
    "IntegrityError",
    "StaleObjectError",
    "ProtectedError",
    "DBConnectionError",
    "PoolTimeoutError",
    "NonExistentTimeError",
    "DecryptionError",
]


class HareError(Exception):
    """
    Base of every exception hare raises.
    """


class ConfigurationError(HareError):
    """
    The configuration is invalid: settings, connections, apps, a model's ``Meta``, a field's
    declaration or a manager - found while hare is set up, or the first time it is used.
    """


class FieldError(HareError):
    """
    The FieldError exception is raised when there is a problem with a model field.
    """


class QueryError(HareError, ValueError):
    """
    A call can't be run as given: a wrong argument, a combination of query methods SQL can't
    express, or an operation on an instance in the wrong state (unsaved, partially loaded, of
    another tenant). Nothing is sent to the database.
    """


class NoValuesFetched(HareError):
    """
    The NoValuesFetched exception is raised when the related model was never fetched. Not a
    ValueError, unlike QueryError: a validation hook (a pydantic validator) would turn it into a
    validation error of the value.
    """


class IncompleteInstanceError(HareError):
    """
    The IncompleteInstanceError exception is raised when a partial model is attempted to be persisted.
    Not a ValueError, for the same reason as NoValuesFetched.
    """


class ValidationError(HareError, ValueError):
    """
    The ValidationError is raised when validators of field validate failed.
    """


class UnSupportedError(HareError):
    """
    The connection's database, dialect, driver or server version can't do what was asked - raised
    before anything is sent. A mistake in the declaration itself, whatever the database, is a
    ConfigurationError instead.
    """


class ObjectLookupError(HareError):
    """
    Base of the errors of a lookup that expects exactly one object.

    Args:
        model: The queried model, or the whole message.
        args: The message, when ``model`` is the model.
    """

    TEMPLATE = ""

    def __init__(self, model: type[Model] | str, *args: Any) -> None:
        self.model: type[Model] | None = None
        if isinstance(model, str):
            args = (model, *args)
        else:
            self.model = model
        super().__init__(*args)

    def __str__(self) -> str:
        if self.model is None or self.args:
            return super().__str__()
        return self.TEMPLATE.format(self.model.__name__)

    def __reduce__(self) -> tuple[type[ObjectLookupError], tuple[Any, ...]]:
        # The model isn't kept in self.args - the default BaseException.__reduce__ (args-only)
        # couldn't rebuild __init__(model, *args).
        if self.model is not None:
            return self.__class__, (self.model, *self.args)
        return self.__class__, self.args


class MultipleObjectsReturned(ObjectLookupError):
    """
    The MultipleObjectsReturned exception is raised when doing a ``.get()`` operation,
    and more than one object is returned.
    """

    TEMPLATE = 'Multiple objects returned for "{}", expected exactly one'


class DoesNotExist(ObjectLookupError, LookupError):
    """
    The DoesNotExist exception is raised when expecting data, such as a ``.get()`` operation.
    """

    TEMPLATE = 'Object "{}" does not exist'


class TransactionManagementError(HareError):
    """
    The TransactionManagementError is raised when any transaction error occurs.
    """


class DistributedTransactionPartiallyCommittedError(TransactionManagementError):
    """``Transactions.distributed()`` committed - the coordinator's COMMIT succeeded - but some
    participants didn't finish ``COMMIT PREPARED``. Their writes stay prepared until ``hare
    distributed-recover`` resolves them.

    Args:
        message: The error message.
        xid: The distributed transaction's GID.
        coordinator_alias: The coordinator's alias.
        pending_participant_aliases: The participants still needing ``COMMIT PREPARED``.
    """

    def __init__(
        self,
        message: str,
        xid: str,
        coordinator_alias: str,
        pending_participant_aliases: list[str],
    ) -> None:
        self.xid = xid
        self.coordinator_alias = coordinator_alias
        self.pending_participant_aliases = pending_participant_aliases
        super().__init__(message)

    def __reduce__(
        self,
    ) -> tuple[type[DistributedTransactionPartiallyCommittedError], tuple[str, str, str, list[str]]]:
        return self.__class__, (self.args[0], self.xid, self.coordinator_alias, self.pending_participant_aliases)


class DistributedTransactionCommitAmbiguousError(TransactionManagementError):
    """The coordinator's commit of ``Transactions.distributed()`` failed or its outcome is unknown -
    whether the transaction committed isn't known. The prepared participants are left alone; ``hare
    distributed-recover`` resolves them from the decision row.

    Args:
        message: The error message.
        xid: The distributed transaction's GID.
        coordinator_alias: The coordinator's alias.
        participant_aliases: Every participant, each still holding a prepared transaction.
    """

    def __init__(
        self,
        message: str,
        xid: str,
        coordinator_alias: str,
        participant_aliases: list[str],
    ) -> None:
        self.xid = xid
        self.coordinator_alias = coordinator_alias
        self.participant_aliases = participant_aliases
        super().__init__(message)

    def __reduce__(
        self,
    ) -> tuple[type[DistributedTransactionCommitAmbiguousError], tuple[str, str, str, list[str]]]:
        return self.__class__, (self.args[0], self.xid, self.coordinator_alias, self.participant_aliases)


class DatabaseError(HareError):
    """The database refused a statement or would refuse the data - the driver's errors and the
    integrity checks hare runs in the database's place. ``str()`` names the SQL but never the
    parameters, which may hold secrets - read ``params`` when debugging.

    Args:
        sql: The SQL text, if known.
        parameters: Its bind parameters, if known.
    """

    def __init__(self, *args: Any, sql: str | None = None, parameters: list[Any] | None = None) -> None:
        super().__init__(*args)
        self.sql = sql
        self.parameters = parameters

    def __str__(self) -> str:
        # Local import: hare.sql imports this module.
        from hare.sql.terms.parameters.query_parameters import QueryParameters

        base = super().__str__()
        if isinstance(self.parameters, QueryParameters):
            # A driver may write a value into its message - a sensitive field's never shows.
            base = DatabaseError.hide_values(base, self.parameters.get_hidden_values())
        if self.sql is None:
            return base
        return f"{base} (sql={self.sql!r})"

    @staticmethod
    def hide_values(message: str, values: list[Any]) -> str:
        """A message with each of the values, as text and as its ``repr()``, written as
        ``<hidden>``.

        Args:
            message: The message.
            values: The values never shown.

        Returns:
            The message.
        """
        # Local import: hare.sql imports this module.
        from hare.sql.constants import HIDDEN_PARAMETER_TEXT

        for value in values:
            for text in sorted({repr(value), str(value)}, key=len, reverse=True):
                if text:
                    message = message.replace(text, HIDDEN_PARAMETER_TEXT)
        return message


class OperationalError(DatabaseError):
    """
    The database failed to run a statement.
    """


class TransactionRetryError(OperationalError):
    """The database aborted the statement because of a concurrent transaction, and running the whole
    transaction again can succeed: a serialization failure, a deadlock broken on this side, a
    database busy with another writer. The driver decides (``Driver.is_retryable``). Inside
    ``atomic()`` the transaction is rolled back as the error leaves the block - retry the block,
    never one statement.
    """


class TooManyParametersError(OperationalError):
    """
    The TooManyParametersError is raised when a statement binds more parameters than the driver or
    the database accepts in one statement - the connection itself stays usable.
    """


class CascadeDepthLimitError(OperationalError):
    """A hard DELETE's native ``ON DELETE CASCADE`` stopped at the database's recursion limit. Part of
    the chain may be gone already. A client with such a limit declares
    ``Features.cascade_depth_limit`` and raises this (SQLite: ``SqliteTriggerRecursionLimitError``);
    ``Model.delete()``/``QuerySet.delete()`` roll the attempt back and finish the cascade in Python.
    A raw DELETE raises it as is.
    """


class IntegrityError(DatabaseError):
    """
    A write would break the data's integrity - a constraint the database reported, or a
    ``RESTRICT``/``PROTECT`` relation hare checked in the database's place.
    """


class StaleObjectError(IntegrityError):
    """``save()`` of a model with ``Meta.optimistic_lock_field`` found the row changed since it was
    read - the UPDATE's version check matched nothing.

    Args:
        message: The error message.
        model: The model of the instance.
        pk: Its primary key.
        expected_version: The version the instance was read at.
    """

    def __init__(self, message: str, model: type[Model], pk: Any, expected_version: Any) -> None:
        self.model = model
        self.pk = pk
        self.expected_version = expected_version
        super().__init__(message)

    def __reduce__(self) -> tuple[type[StaleObjectError], tuple[str, type[Model], Any, Any]]:
        # The default args-only BaseException.__reduce__ can't rebuild model/pk/expected_version.
        return self.__class__, (self.args[0], self.model, self.pk, self.expected_version)


class ProtectedError(IntegrityError):
    """
    The ProtectedError exception is raised when deleting an object would leave related objects
    with an ``on_delete=PROTECT`` foreign key pointing at it.
    """

    def __init__(self, message: str, protected_objects: list[Model]) -> None:
        self.protected_objects = protected_objects
        super().__init__(message)

    def __reduce__(self) -> tuple[type[ProtectedError], tuple[str, list[Model]]]:
        # The default args-only BaseException.__reduce__ can't rebuild protected_objects.
        return self.__class__, (self.args[0], self.protected_objects)


class DBConnectionError(DatabaseError, ConnectionError):
    """
    The DBConnectionError is raised when problems with connecting to db occurs
    """


class PoolTimeoutError(DBConnectionError):
    """Waiting for a connection of a pool ran out of the connection's ``pool_acquire_timeout`` -
    every connection stayed taken. The pool is busy, not broken: the next call may get one.
    """


class NonExistentTimeError(HareError, ValueError):
    """
    The NonExistentTimeError is raised by ``Timezone.make_aware()`` when a naive datetime falls
    in a DST spring-forward gap - a wall-clock time skipped entirely by the zone's transition,
    with no real UTC instant it can represent.
    """


class DecryptionError(HareError, ValueError):
    """
    The DecryptionError is raised when a value read from an encrypted field can't be decrypted -
    the configured encryption key isn't the one it was written with, or the stored value is not a
    valid token.
    """
