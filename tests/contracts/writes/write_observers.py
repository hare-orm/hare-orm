"""The observers of the write matrix - every one is checked against every write path."""

from tests.contracts.writes.observers.auto_now_observer import AutoNowObserver
from tests.contracts.writes.observers.change_capture_observer import ChangeCaptureObserver
from tests.contracts.writes.observers.encryption_observer import EncryptionObserver
from tests.contracts.writes.observers.optimistic_lock_observer import OptimisticLockObserver
from tests.contracts.writes.observers.rows_changed_observer import RowsChangedObserver
from tests.contracts.writes.observers.tenant_scope_observer import TenantScopeObserver
from tests.contracts.writes.observers.validators_observer import ValidatorsObserver

WRITE_OBSERVERS = (
    RowsChangedObserver,
    ChangeCaptureObserver,
    ValidatorsObserver,
    AutoNowObserver,
    OptimisticLockObserver,
    TenantScopeObserver,
    EncryptionObserver,
)
