from __future__ import annotations

#: The ranges of ClickHouse's integer types.
UINT8_RANGE = (0, 2**8 - 1)
UINT16_RANGE = (0, 2**16 - 1)
UINT32_RANGE = (0, 2**32 - 1)
UINT64_RANGE = (0, 2**64 - 1)
UINT128_RANGE = (0, 2**128 - 1)
UINT256_RANGE = (0, 2**256 - 1)
INT128_RANGE = (-(2**127), 2**127 - 1)
INT256_RANGE = (-(2**255), 2**255 - 1)
#: The digits a float32 is written with at most - its shortest text that reads back as the same
#: float32 is searched from FLOAT32_MIN_DIGITS up.
FLOAT32_MIN_DIGITS = 6
FLOAT32_MAX_DIGITS = 9
#: The largest finite float32.
FLOAT32_MAX = 3.4028234663852886e38
#: The longest FixedString - ClickHouse's own bound is far above any sensible fixed width.
FIXED_STRING_MAX_LENGTH = 65535
#: The byte a FixedString pads its value with.
FIXED_STRING_PAD_BYTE = b"\x00"
