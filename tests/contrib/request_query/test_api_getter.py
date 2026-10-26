"""Values from other services' HTTP APIs: every value of a source's key asked once, each answer item put into
the targets of the objects holding its value - the request modes, batches, repeats, mappings and errors."""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from hare import fields
from hare.contrib.request_query import (
    ApiDataNotFoundError,
    ApiGetter,
    ApiGetterRetry,
    ApiGetterTrigger,
    ApiGetterTriggers,
    ApiRequestError,
    ApiRequestExecuted,
    ApiRequestMode,
    ApiSource,
    ApiValueConversionError,
    ApiValueMapping,
    ApiValueMappingMode,
    ApiValueMissingError,
)
from hare.contrib.request_query.api_getter.api_http_requests import ApiHttpRequests
from hare.exceptions import ConfigurationError
from hare.instrumentation.observers.observers import Observers

pytestmark = pytest.mark.database_independent

USERS = {
    1: {"id": 1, "name": "Anna"},
    2: {"id": 2, "name": "Boris"},
    3: {"id": 3, "name": "Clara"},
    4: {"id": 4, "name": "Dmitry"},
    5: {"id": 5, "name": "Elena"},
}


class FakeService:
    """The other service: answers ``/users`` (repeated ``ids``, one ``id``) and ``/users/<id>``, and keeps
    every request it was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list[httpx.Response] = []
        self.running = 0
        self.most_running = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            # Requests running at the same time overlap here.
            await asyncio.sleep(0.001)
            if self.answers:
                return self.answers.pop(0)
            path = request.url.path
            if path.startswith("/users/"):
                user = USERS.get(int(path.rsplit("/", 1)[-1]))
                return httpx.Response(200, json=user) if user else httpx.Response(404)
            ids = request.url.params.get_list("ids") or request.url.params.get_list("id")
            return httpx.Response(
                200, json={"result": [USERS[int(user_id)] for user_id in ids if int(user_id) in USERS]}
            )
        finally:
            self.running -= 1


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    fake_service = FakeService()
    monkeypatch.setattr(ApiHttpRequests, "transport", httpx.MockTransport(fake_service.handle))
    return fake_service


@pytest.fixture
def delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The pauses before repeats - recorded, not waited."""
    recorded_delays: list[float] = []
    get_delay = ApiGetterRetry.get_delay

    def recording_get_delay(retry: ApiGetterRetry, repeat: int, retry_after: str | None) -> float:
        recorded_delays.append(get_delay(retry, repeat, retry_after))
        return 0.0

    monkeypatch.setattr(ApiGetterRetry, "get_delay", recording_get_delay)
    return recorded_delays


def get_users_source(**options: Any) -> ApiSource:
    settings: dict[str, Any] = {
        "url": "https://users.test/users",
        "key": "user_id",
        "mappings": (ApiValueMapping(set_key="user"),),
        "query_parameter": "ids",
        "match_key": "id",
        "items_path": "result",
        **options,
    }
    return ApiSource(**settings)


def get_rows(*user_ids: int | None) -> list[dict[str, Any]]:
    return [{"id": number, "user_id": user_id} for number, user_id in enumerate(user_ids)]


@pytest.mark.asyncio
async def test_query_many_asks_each_value_once_in_batches(service: FakeService) -> None:
    rows = get_rows(1, 2, 2, 3, 4, 5, 9, None)
    await ApiGetter.set(rows, [get_users_source(batch_size=2)])
    assert [row["user"] and row["user"]["name"] for row in rows] == [
        "Anna", "Boris", "Boris", "Clara", "Dmitry", "Elena", None, None,
    ]  # fmt: skip
    assert sorted(request.url.params.get_list("ids") for request in service.requests) == [
        ["1", "2"], ["3", "4"], ["5", "9"],
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_a_value_no_item_can_be_matched_to_is_not_asked(service: FakeService) -> None:
    """A dict of a JSON field as a key's value broke the run with a raw TypeError before any request - it is
    left out as None is, its targets empty."""
    rows = [{"id": 0, "user_id": {"id": 1}}, {"id": 1, "user_id": 2}]
    await ApiGetter.set(rows, [get_users_source()])
    assert [row["user"] and row["user"]["name"] for row in rows] == [None, "Boris"]
    assert [request.url.params.get_list("ids") for request in service.requests] == [["2"]]


@pytest.mark.asyncio
async def test_query_one_and_path_ask_one_value_per_request(service: FakeService) -> None:
    rows = get_rows(1, 3, 3)
    await ApiGetter.set(rows, [get_users_source(request_mode=ApiRequestMode.QUERY_ONE, query_parameter="id")])
    assert [row["user"]["name"] for row in rows] == ["Anna", "Clara", "Clara"]
    path_rows = get_rows(2, 8)
    await ApiGetter.set(
        path_rows,
        [
            ApiSource(
                url="https://users.test/users/{user_id}",
                key="user_id",
                mappings=(ApiValueMapping(set_key="user_name", key="name"),),
                request_mode=ApiRequestMode.PATH,
            )
        ],
    )
    assert [row["user_name"] for row in path_rows] == ["Boris", None]
    assert [request.url.path for request in service.requests[-2:]] == ["/users/2", "/users/8"]


@pytest.mark.asyncio
async def test_a_value_in_the_url_is_quoted(service: FakeService) -> None:
    service.answers.append(httpx.Response(200, json={"name": "slashed"}))
    rows = [{"code": "a/b?c"}]
    await ApiGetter.set(
        rows,
        [
            ApiSource(
                url="https://codes.test/codes/{code}",
                key="code",
                mappings=(ApiValueMapping(set_key="title", key="name"),),
                request_mode=ApiRequestMode.PATH,
            )
        ],
    )
    assert rows[0]["title"] == "slashed"
    assert service.requests[0].url.raw_path == b"/codes/a%2Fb%3Fc"


@pytest.mark.asyncio
async def test_key_type_matches_items_by_the_objects_values(service: FakeService) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    service.answers.append(
        httpx.Response(200, json=[{"uid": str(second), "name": "second"}, {"uid": "not-a-uuid", "name": "x"}])
    )
    rows = [{"owner": {"uid": first}}, {"owner": {"uid": second}}]
    await ApiGetter.set(
        rows,
        [
            ApiSource(
                url="https://users.test/by-uid",
                key="owner.uid",
                mappings=(ApiValueMapping(set_key="owner.name", key="name"),),
                query_parameter="uid",
                key_type=uuid.UUID,
            )
        ],
    )
    assert [row["owner"].get("name") for row in rows] == [None, "second"]


@pytest.mark.asyncio
async def test_lists_on_the_way_of_a_key_are_walked(service: FakeService) -> None:
    rows = [{"members": [{"user_id": 1}, {"user_id": 2}]}, {"members": []}, {"members": [{"user_id": 1}]}]
    await ApiGetter.set(
        rows,
        [get_users_source(key="members.user_id", mappings=(ApiValueMapping(set_key="members.name", key="name"),))],
    )
    assert [[member["name"] for member in row["members"]] for row in rows] == [["Anna", "Boris"], [], ["Anna"]]
    assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_a_source_asks_at_most_its_concurrency_at_a_time(service: FakeService) -> None:
    rows = get_rows(1, 2, 3, 4, 5)
    await ApiGetter.set(
        rows, [get_users_source(request_mode=ApiRequestMode.QUERY_ONE, query_parameter="id", concurrency=2)]
    )
    assert len(service.requests) == 5
    assert service.most_running == 2


@pytest.mark.asyncio
async def test_sources_are_asked_at_the_same_time_and_applied_in_their_order(service: FakeService) -> None:
    rows = get_rows(1)
    first = get_users_source(mappings=(ApiValueMapping(set_key="label", key="name"),))
    second = get_users_source(mappings=(ApiValueMapping(set_key="label", key="id"),))
    await ApiGetter.set(rows, [first, second])
    assert rows[0]["label"] == 1
    assert service.most_running == 2


@pytest.mark.asyncio
async def test_a_failed_request_is_repeated(service: FakeService, delays: list[float]) -> None:
    service.answers.extend([httpx.Response(503), httpx.Response(429, headers={"Retry-After": "3"})])
    rows = get_rows(1)
    await ApiGetter.set(rows, [get_users_source(retry=ApiGetterRetry(attempts=2, max_delay_seconds=2.5))])
    assert rows[0]["user"]["name"] == "Anna"
    assert len(service.requests) == 3
    assert 0 <= delays[0] <= 0.5
    assert delays[1] == 2.5


@pytest.mark.asyncio
async def test_a_broken_connection_is_repeated_until_the_attempts_run_out(
    monkeypatch: pytest.MonkeyPatch, delays: list[float]
) -> None:
    attempts = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(ApiHttpRequests, "transport", httpx.MockTransport(refuse))
    rows = get_rows(1)
    await ApiGetter.set(rows, [get_users_source(retry=ApiGetterRetry(attempts=3))])
    assert rows[0]["user"] is None
    assert len(attempts) == 4
    assert len(delays) == 3
    with pytest.raises(ApiRequestError, match="the request for 1 failed") as error:
        await ApiGetter.set(get_rows(1), [get_users_source(required=True, name="users")])
    assert error.value.source_name == "users"
    assert isinstance(error.value.__cause__, httpx.ConnectError)


def test_retry_after_is_read_as_seconds_or_as_a_date() -> None:
    assert ApiGetterRetry.read_retry_after(" 7 ") == 7.0
    moment = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=60)
    seconds = ApiGetterRetry.read_retry_after(email.utils.format_datetime(moment, usegmt=True))
    assert seconds is not None and 55 <= seconds <= 60
    assert ApiGetterRetry.read_retry_after("soon") is None
    assert ApiGetterRetry(max_delay_seconds=1).get_delay(0, "60") == 1


@pytest.mark.asyncio
async def test_a_required_source_raises_for_what_it_lacks(service: FakeService) -> None:
    with pytest.raises(ApiDataNotFoundError, match="no data for 9"):
        await ApiGetter.set(get_rows(1, 9), [get_users_source(required=True)])
    service.answers.append(httpx.Response(500))
    with pytest.raises(ApiRequestError, match="500"):
        await ApiGetter.set(get_rows(1, 2), [get_users_source(required=True)])
    service.answers.append(httpx.Response(200, content=b"<html>"))
    with pytest.raises(ApiRequestError, match="isn't JSON"):
        await ApiGetter.set(get_rows(1), [get_users_source(required=True)])
    with pytest.raises(ApiDataNotFoundError):
        await ApiGetter.set(
            get_rows(7),
            [
                get_users_source(
                    url="https://users.test/users/{user_id}", request_mode=ApiRequestMode.PATH, required=True
                )
            ],
        )
    with pytest.raises(ApiValueMissingError, match="hold no value"):
        await ApiGetter.set(get_rows(None), [get_users_source(required=True)])
    rows = get_rows(1)
    service.answers.append(httpx.Response(500))
    await ApiGetter.set(rows, [get_users_source()])
    assert rows[0]["user"] is None


@pytest.mark.asyncio
async def test_mappings_settle_a_target_by_their_mode_default_and_requirement(service: FakeService) -> None:
    rows = [{"user_id": 1, "nickname": None, "login": "anna_l"}, {"user_id": 9, "nickname": None, "login": None}]
    await ApiGetter.set(
        rows,
        [
            get_users_source(
                mappings=(
                    ApiValueMapping(set_key="title", key="name"),
                    ApiValueMapping(set_key="display", key="name", mode=ApiValueMappingMode.NOT_EMPTY),
                )
            )
        ],
        [
            ApiValueMapping(set_key="display", key="login"),
            ApiValueMapping(set_key="nickname", key="login", default="anonymous"),
        ],
    )
    assert [(row["title"], row["display"], row["nickname"]) for row in rows] == [
        ("Anna", "anna_l", "anna_l"),
        (None, None, "anonymous"),
    ]
    with pytest.raises(ApiValueMissingError, match="title: a required value"):
        await ApiGetter.set(
            get_rows(9), [get_users_source(mappings=(ApiValueMapping(set_key="title", key="name", required=True),))]
        )


@pytest.mark.asyncio
async def test_a_value_is_read_through_a_field_and_a_function(service: FakeService) -> None:
    service.answers.append(httpx.Response(200, json={"result": [{"id": 1, "joined": "2026-03-01T12:00:00+00:00"}]}))
    rows = get_rows(1)
    await ApiGetter.set(
        rows,
        [
            get_users_source(
                mappings=(
                    ApiValueMapping(set_key="joined", key="joined", field=fields.DatetimeField()),
                    ApiValueMapping(
                        set_key="joined_year",
                        key="joined",
                        field=fields.DatetimeField(),
                        convert=lambda value: value.year,
                    ),
                )
            )
        ],
    )
    assert rows[0]["joined"] == datetime.datetime(2026, 3, 1, 12, tzinfo=datetime.UTC)
    assert rows[0]["joined_year"] == 2026
    service.answers.append(httpx.Response(200, json={"result": [{"id": 1, "joined": "never"}]}))
    with pytest.raises(ApiValueConversionError, match="joined: the value can't be read"):
        await ApiGetter.set(
            get_rows(1),
            [
                get_users_source(
                    mappings=(ApiValueMapping(set_key="joined", key="joined", field=fields.DatetimeField()),)
                )
            ],
        )


@pytest.mark.asyncio
async def test_a_source_runs_on_its_triggers_and_requested_targets(service: FakeService) -> None:
    on_create = get_users_source(triggers=ApiGetterTriggers(on_read=False, on_create=True))
    rows = get_rows(1)
    await ApiGetter.set(rows, [on_create])
    assert "user" not in rows[0]
    await ApiGetter.set(rows, [on_create], trigger=ApiGetterTrigger.CREATE)
    assert rows[0]["user"]["name"] == "Anna"
    await ApiGetter.set(rows := get_rows(1), [on_create], trigger=ApiGetterTrigger.UPDATE)
    assert "user" not in rows[0]
    await ApiGetter.set(rows, [get_users_source()], requested_names=frozenset({"title"}))
    assert "user" not in rows[0]
    assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_headers_of_a_function_are_asked_once_a_run(service: FakeService) -> None:
    calls = []

    async def get_headers() -> dict[str, str]:
        calls.append(1)
        return {"X-Service-Token": "secret"}

    await ApiGetter.set(
        get_rows(1, 2, 3),
        [get_users_source(request_mode=ApiRequestMode.QUERY_ONE, query_parameter="id", headers=get_headers)],
    )
    assert calls == [1]
    assert {request.headers["X-Service-Token"] for request in service.requests} == {"secret"}


@pytest.fixture
def executed_requests() -> Iterator[list[ApiRequestExecuted]]:
    events: list[ApiRequestExecuted] = []
    with Observers.observing(ApiRequestExecuted, events.append):
        yield events


@pytest.mark.asyncio
async def test_each_attempt_is_reported_without_the_headers(
    service: FakeService, delays: list[float], executed_requests: list[ApiRequestExecuted]
) -> None:
    service.answers.append(httpx.Response(502))
    await ApiGetter.set(
        get_rows(1),
        [get_users_source(name="users", headers={"X-Service-Token": "secret"}, retry=ApiGetterRetry(attempts=1))],
    )
    assert [(event.source_name, event.status_code, event.attempt) for event in executed_requests] == [
        ("users", 502, 0),
        ("users", 200, 1),
    ]
    assert "secret" not in json.dumps([repr(event) for event in executed_requests])


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: get_users_source(query_parameter=None), "query_parameter"),
        (lambda: get_users_source(request_mode=ApiRequestMode.PATH), "PATH url"),
        (lambda: ApiValueMapping(set_key="user", required=True, default={}), "required and default"),
    ],
)
def test_contradicting_settings_are_refused(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        build()


def test_retry_after_of_other_digits_than_ascii_is_no_number() -> None:
    """ "²" is a digit to str.isdigit() - float() refused it, and the whole run failed."""
    assert ApiGetterRetry.read_retry_after("²") is None
    assert ApiGetterRetry.read_retry_after("١٢") is None


@pytest.mark.asyncio
async def test_an_answer_that_can_t_be_read_is_a_failed_request(service: FakeService) -> None:
    """A body that doesn't decode raised httpx's DecodingError out of the run - not ApiRequestError."""
    broken_body = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))
    service.answers.append(broken_body)
    rows = get_rows(1)
    await ApiGetter.set(rows, [get_users_source()])
    assert rows[0]["user"] is None
    service.answers.append(
        httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))
    )
    with pytest.raises(ApiRequestError) as error:
        await ApiGetter.set(get_rows(1), [get_users_source(required=True)])
    assert isinstance(error.value.__cause__, httpx.DecodingError)


@pytest.mark.asyncio
async def test_an_event_shows_the_url_without_its_query_and_credentials(
    service: FakeService, executed_requests: list[ApiRequestExecuted]
) -> None:
    """The query of the source's URL - a key in it - reached the events and the log."""
    await ApiGetter.set(
        get_rows(1), [get_users_source(url="https://robot:password@users.test/users?apikey=SECRET#part")]
    )
    assert [event.url for event in executed_requests] == ["https://users.test/users"]


def test_the_headers_of_a_source_stay_out_of_its_repr() -> None:
    source = get_users_source(headers={"Authorization": "Bearer SECRET"})
    assert "SECRET" not in repr(source)


@pytest.mark.asyncio
async def test_no_objects_ask_no_source(service: FakeService) -> None:
    """A required source raised ApiValueMissingError for an empty list of objects."""
    await ApiGetter.set([], [get_users_source(required=True)])
    assert service.requests == []


@pytest.mark.asyncio
async def test_a_path_ends_at_a_text_or_a_number(service: FakeService) -> None:
    """A path on into a text read its attributes - "name.upper" gave the method."""
    rows = get_rows(1)
    await ApiGetter.set(rows, [get_users_source(mappings=(ApiValueMapping(set_key="shout", key="name.upper"),))])
    assert rows[0]["shout"] is None
