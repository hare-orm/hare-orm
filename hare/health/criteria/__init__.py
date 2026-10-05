"""The criteria a health check judges a connection by - degrading or unhealthy, as the list each is
in (``HealthCheck(degraded_when=..., unhealthy_when=...)``)."""

from __future__ import annotations

from hare.health.criteria.acquire_wait_time import AcquireWaitTime
from hare.health.criteria.all_of import AllOf
from hare.health.criteria.declarations import AcquireTimeouts, ConnectFailures
from hare.health.criteria.health_criterion import HealthCriterion
from hare.health.criteria.ping_failed import PingFailed
from hare.health.criteria.ping_latency import PingLatency
from hare.health.criteria.pool_count_criterion import PoolCountCriterion
from hare.health.criteria.pool_saturation import PoolSaturation
from hare.health.criteria.waiting_requests import WaitingRequests

__all__ = [
    "AcquireTimeouts",
    "AcquireWaitTime",
    "AllOf",
    "ConnectFailures",
    "HealthCriterion",
    "PingFailed",
    "PoolCountCriterion",
    "PingLatency",
    "PoolSaturation",
    "WaitingRequests",
]
