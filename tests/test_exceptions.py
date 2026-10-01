import pickle

from hare.exceptions import DoesNotExist, MultipleObjectsReturned, OperationalError, ProtectedError
from tests.testmodels import Tournament


def test_protected_error_round_trips_through_pickle():
    """ProtectedError.__init__ calls super().__init__(message), dropping protected_objects
    from self.args regardless of how it was originally constructed - the default
    BaseException.__reduce__ (args-only) couldn't reconstruct it, so unpickling raised
    TypeError: missing 'protected_objects'."""
    original = ProtectedError("cannot delete: still referenced", [Tournament])

    restored = pickle.loads(pickle.dumps(original))

    assert str(restored) == str(original)
    assert restored.protected_objects == [Tournament]


def test_multiple_objects_returned_with_model_class_round_trips_through_pickle():
    """ObjectLookupError.__init__ only forwards the non-model extra *args to
    super().__init__() - passing `model` as an actual Model CLASS (not a string) left
    self.args empty, so the default BaseException.__reduce__ couldn't reconstruct
    __init__(model, *args), and unpickling raised TypeError: missing 'model'."""
    original = MultipleObjectsReturned(Tournament)

    restored = pickle.loads(pickle.dumps(original))

    assert str(restored) == str(original)
    assert restored.model is Tournament


def test_does_not_exist_with_model_class_round_trips_through_pickle():
    original = DoesNotExist(Tournament)

    restored = pickle.loads(pickle.dumps(original))

    assert str(restored) == str(original)
    assert restored.model is Tournament


def test_does_not_exist_with_string_message_round_trips_through_pickle():
    """When `model` is a plain string (not a Model class), ObjectLookupError treats it as a
    template message instead - already went through self.args and pickled fine, but covered
    here alongside the Model-class case for completeness."""
    original = DoesNotExist("some custom message")

    restored = pickle.loads(pickle.dumps(original))

    assert str(restored) == str(original)
    assert restored.model is None


def test_sql_error_str_names_the_sql_but_not_the_bind_parameters():
    error = OperationalError("constraint failed", sql='INSERT INTO "account" VALUES (?)', params=["hunter2"])

    assert 'INSERT INTO "account" VALUES (?)' in str(error)
    assert "hunter2" not in str(error)
    assert "hunter2" not in repr(error)
    assert error.params == ["hunter2"]
