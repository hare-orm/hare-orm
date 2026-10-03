from hare.exceptions import QueryError


class HareSqlException(QueryError):
    pass


class QueryException(HareSqlException):
    pass


class GroupingException(HareSqlException):
    pass


class CaseException(HareSqlException):
    pass


class JoinException(HareSqlException):
    pass


class SetOperationException(HareSqlException):
    pass


class FunctionException(HareSqlException):
    pass
