from __future__ import annotations

import datetime

#: The pytest option turning plan verification on.
VERIFY_PLANS_OPTION = "--verify-plans"
#: The marker of a test whose runs on a plan are compared after its body.
PLAN_VERIFICATION_AFTER_TEST_MARKER = "plan_verification_after_test"
#: How far from now a datetime parameter is taken for the current moment a build took.
CURRENT_MOMENT_TOLERANCE = datetime.timedelta(seconds=5)
#: How every Fernet token starts - its version byte and timestamp, base64-encoded.
FERNET_TOKEN_PREFIX = "gAAAAA"
