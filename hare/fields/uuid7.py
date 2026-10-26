from __future__ import annotations

import os
import threading
import time
import uuid
from typing import ClassVar

from hare.fields.constants import UUID7_FACTORY, UUID7_RANDOM_B_MASK, UUID7_RANDOM_MASK


class Uuid7:
    """Version 7 UUIDs made in Python (RFC 9562) - time-ordered and, within a process, increasing: a later one
    is greater, so an index over them grows at its end. Python 3.14's ``uuid.uuid7`` where there is one, with
    the same layout and order elsewhere: 48 bits of milliseconds, then 74 random bits that a UUID of the same
    millisecond takes from the last one plus one.

    Example::

        id = fields.UUIDField(primary_key=True, default=Uuid7.new)
    """

    #: The millisecond and the 74 random bits of the last UUID made in this process.
    last_timestamp: ClassVar[int] = 0
    last_random: ClassVar[int] = 0
    lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def new(cls) -> uuid.UUID:
        """A new version 7 UUID, greater than every one made before in this process.

        Returns:
            The UUID.
        """
        if UUID7_FACTORY is not None:
            return UUID7_FACTORY()
        timestamp = time.time_ns() // 1_000_000
        random_bits = int.from_bytes(os.urandom(10)) & UUID7_RANDOM_MASK
        with cls.lock:
            # The same millisecond, or a clock gone back: the one after the last.
            if timestamp <= cls.last_timestamp:
                timestamp, random_bits = cls.last_timestamp, cls.last_random + 1
                if random_bits > UUID7_RANDOM_MASK:
                    timestamp, random_bits = timestamp + 1, 0
            cls.last_timestamp, cls.last_random = timestamp, random_bits
        random_a, random_b = random_bits >> 62, random_bits & UUID7_RANDOM_B_MASK
        return uuid.UUID(int=timestamp << 80 | 7 << 76 | random_a << 64 | 0b10 << 62 | random_b)
