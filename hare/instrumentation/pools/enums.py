from enum import StrEnum


class PoolMetricEvent(StrEnum):
    WAIT = "wait"
    CONNECT = "connect"
    TIMEOUT = "timeout"
    CONNECT_FAILURE = "connect_failure"
