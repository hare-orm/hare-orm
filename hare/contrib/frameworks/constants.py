from __future__ import annotations

from http import HTTPStatus

from hare.contrib.request_query.exceptions import RequestQueryForbidden
from hare.exceptions import DoesNotExist, IntegrityError

#: The status codes from which a response rolls the request's transaction back.
SERVER_ERROR_STATUS_CODE = 500

#: The attribute ``RequestTransaction.skip()`` marks a handler with - the handler runs outside the
#: request's transaction.
SKIP_TRANSACTION_ATTRIBUTE = "hare_skip_transaction"


#: The generic containers whose items are presented one by one in a handler's signature.
CONTAINER_ORIGINS = (list, set, frozenset, tuple)
#: The key of a request's ``RequestTransaction`` in its ASGI scope.
REQUEST_TRANSACTION_SCOPE_KEY = "hare.request_transaction"
#: The HTTP status every framework integration answers an ORM or request query error with - the
#: first error class the error is an instance of. ``InvalidRequestQuery`` isn't here: each
#: framework answers it as it answers its own invalid parameters.
STATUS_BY_ERROR: dict[type[Exception], HTTPStatus] = {
    DoesNotExist: HTTPStatus.NOT_FOUND,
    IntegrityError: HTTPStatus.CONFLICT,
    RequestQueryForbidden: HTTPStatus.FORBIDDEN,
}
