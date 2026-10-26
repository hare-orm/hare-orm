from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Hashable, Iterable, Mapping
from typing import Any, cast
from urllib.parse import quote

from hare.contrib.request_query.api_getter.api_http_requests import ApiHttpRequests
from hare.contrib.request_query.api_getter.api_source import ApiSource
from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping
from hare.contrib.request_query.api_getter.api_value_paths import ApiValuePaths
from hare.contrib.request_query.api_getter.constants import (
    API_BATCH_NOT_FOUND_MESSAGE,
    API_BATCH_REQUEST_FAILED_MESSAGE,
    API_DATA_NOT_FOUND_MESSAGE,
    API_NO_KEY_VALUES_MESSAGE,
    API_NOT_FOUND_STATUS_CODE,
    API_REQUEST_FAILED_MESSAGE,
    API_TARGET_VALUE_MISSING_MESSAGE,
    API_VALUE_CONVERSION_FAILED_MESSAGE,
)
from hare.contrib.request_query.api_getter.declarations import ApiTargetValue
from hare.contrib.request_query.api_getter.enums import ApiGetterTrigger, ApiRequestMode, ApiValueMappingMode
from hare.contrib.request_query.exceptions import (
    ApiDataNotFoundError,
    ApiRequestError,
    ApiValueConversionError,
    ApiValueMissingError,
)


class ApiGetter:
    """Fills objects with values other services' HTTP APIs give (``ApiSource``) and with values of their own
    attributes (``ApiValueMapping`` without a source): every value of a source's key the objects hold is asked
    once - the sources at the same time, a source's requests within its ``concurrency`` and ``batch_size`` -
    and each answer item is put into the targets of the objects holding its value."""

    @classmethod
    async def set(
        cls,
        objs: list[Any],
        sources: Iterable[ApiSource] = (),
        mappings: Iterable[ApiValueMapping] = (),
        *,
        trigger: ApiGetterTrigger = ApiGetterTrigger.READ,
        requested_names: frozenset[str] | None = None,
    ) -> None:
        """Fills the objects.

        Args:
            objs: The objects.
            sources: The APIs.
            mappings: Values taken from the objects themselves.
            trigger: What the objects come from - a source runs on the triggers it allows.
            requested_names: The fields a request asks for - a source none of whose targets is asked for
                isn't asked; None for every source.

        Raises:
            ApiDataNotFoundError: A required source answers ``404 Not Found`` or has no item for a value.
            ApiRequestError: A required source can't be reached, answers an error or no JSON.
            ApiValueMissingError: A required source has no value to ask for, or a required target stays
                empty.
            ApiValueConversionError: A mapping's ``field`` or ``convert`` refuses a value.
        """
        if not objs:
            return
        target_values: dict[tuple[int, str], ApiTargetValue] = {}
        for mapping in mappings:
            for root in objs:
                value = cls.get_converted(None, mapping, ApiValuePaths.get_value_at_path(root, mapping.key))
                for container in ApiValuePaths.get_containers(root, mapping.set_key):
                    cls.apply_mapping_value(target_values, container, mapping, value)
        running_sources = [
            source
            for source in sources
            if source.triggers.runs_on(trigger)
            and (requested_names is None or source.get_target_names() & requested_names)
        ]
        if running_sources:
            async with ApiHttpRequests() as http_requests:
                answers = await cls.run_together([cls.ask(http_requests, source, objs) for source in running_sources])
            # In the sources' order - a target several sources set keeps its value by their order.
            for source, (containers_by_value, items_by_value) in zip(running_sources, answers, strict=True):
                cls.apply_source_values(target_values, source, containers_by_value, items_by_value)
        cls.apply_target_values(target_values)

    @staticmethod
    async def run_together[Result](awaitables: list[Awaitable[Result]]) -> list[Result]:
        """Runs awaitables at the same time - the others cancelled once one raises.

        Args:
            awaitables: The awaitables.

        Returns:
            Their results, in their order.
        """
        tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @classmethod
    async def ask(
        cls, http_requests: ApiHttpRequests, source: ApiSource, objs: list[Any]
    ) -> tuple[dict[Any, list[Any]], dict[Any, Any]]:
        """Asks a source for every value of its key the objects hold.

        Args:
            http_requests: The requests of the run.
            source: The source.
            objs: The objects.

        Returns:
            The containers holding each value - under None those holding no value, whose targets stay
            empty - and the answer item of each value, None for a value without one.
        """
        containers_by_value: dict[Any, list[Any]] = {}
        key_name = source.get_key_name()
        for root in objs:
            for container in ApiValuePaths.get_containers(root, source.key):
                values = [
                    value
                    for value in ApiValuePaths.to_list(ApiValuePaths.get_child(container, key_name))
                    if cls.can_be_asked(value)
                ]
                for value in values or [None]:
                    containers_by_value.setdefault(value, []).append(container)
        values = [value for value in containers_by_value if value is not None]
        if not values:
            if source.required:
                raise ApiValueMissingError(
                    API_NO_KEY_VALUES_MESSAGE.format(source=source.get_name(), key=source.key), source.get_name()
                )
            return containers_by_value, {}
        if source.request_mode is ApiRequestMode.QUERY_MANY:
            batches = [values[start : start + source.batch_size] for start in range(0, len(values), source.batch_size)]
            items_by_value: dict[Any, Any] = {}
            for batch_items in await cls.run_together(
                [cls.fetch_batch(http_requests, source, batch) for batch in batches]
            ):
                items_by_value.update(batch_items)
        else:
            items = await cls.run_together([cls.fetch_one(http_requests, source, value) for value in values])
            items_by_value = dict(zip(values, items, strict=True))
        return containers_by_value, items_by_value

    @staticmethod
    def can_be_asked(value: Any) -> bool:
        """Whether a value of a source's key is asked for - None isn't, nor a value no answer item can be
        matched back to (a dict of a JSON field: one that can't be hashed).

        Args:
            value: The value.

        Returns:
            Whether it is.
        """
        if value is None:
            return False
        try:
            hash(value)
        except TypeError:
            return False
        return True

    @classmethod
    async def fetch_one(cls, http_requests: ApiHttpRequests, source: ApiSource, value: Any) -> Any:
        """Asks a source for one value - in the URL or as its query parameter.

        Args:
            http_requests: The requests of the run.
            source: The source.
            value: The value.

        Returns:
            The answer item of the value, None without one.
        """
        if source.request_mode is ApiRequestMode.PATH:
            placeholder = "{" + source.get_key_name() + "}"
            response, error = await http_requests.get(
                source, source.url.replace(placeholder, quote(str(value), safe="")), None
            )
        else:
            response, error = await http_requests.get(source, source.url, {cast("str", source.query_parameter): value})
        items = cls.read_items(source, response, error, [value])
        if items is None:
            return None
        if source.match_key is None:
            item = items[0] if items else None
        else:
            item = next((item for item in items if cls.get_item_value(source, item, source.match_key) == value), None)
        if item is None and source.required:
            raise ApiDataNotFoundError(
                API_DATA_NOT_FOUND_MESSAGE.format(source=source.get_name(), value=value), source.get_name()
            )
        return item

    @classmethod
    async def fetch_batch(cls, http_requests: ApiHttpRequests, source: ApiSource, values: list[Any]) -> dict[Any, Any]:
        """Asks a source for a batch of values in one request - each one a repetition of its query parameter.

        Args:
            http_requests: The requests of the run.
            source: The source.
            values: The values.

        Returns:
            The answer item of each value - None for a value without one.
        """
        response, error = await http_requests.get(source, source.url, {cast("str", source.query_parameter): values})
        items = cls.read_items(source, response, error, values)
        if items is None:
            return {}
        match_key = source.match_key or source.get_key_name()
        items_by_value: dict[Any, Any] = {}
        for item in items:
            item_value = cls.get_item_value(source, item, match_key)
            if item_value is not None and isinstance(item_value, Hashable):
                items_by_value[item_value] = item
        for value in values:
            if value not in items_by_value and source.required:
                raise ApiDataNotFoundError(
                    API_DATA_NOT_FOUND_MESSAGE.format(source=source.get_name(), value=value), source.get_name()
                )
        return {value: items_by_value.get(value) for value in values}

    @staticmethod
    def read_items(source: ApiSource, response: Any, error: Exception | None, values: list[Any]) -> list[Any] | None:
        """The items of a source's answer.

        Args:
            source: The source.
            response: The answer, None when the connection failed.
            error: What failed the connection, None with an answer.
            values: The values asked for.

        Returns:
            The items; None when the request failed and the source isn't required.

        Raises:
            ApiDataNotFoundError: A required source answered ``404 Not Found``.
            ApiRequestError: A required source couldn't be reached, answered an error or no JSON, or its answer
                couldn't be read.
        """
        name = source.get_name()
        if len(values) == 1:
            not_found_message = API_DATA_NOT_FOUND_MESSAGE.format(source=name, value=values[0])
            failed_message = API_REQUEST_FAILED_MESSAGE.format(source=name, value=values[0])
        else:
            not_found_message = API_BATCH_NOT_FOUND_MESSAGE.format(source=name)
            failed_message = API_BATCH_REQUEST_FAILED_MESSAGE.format(source=name)
        if error is not None:
            if source.required:
                raise ApiRequestError(failed_message, name) from error
            return None
        if response.status_code == API_NOT_FOUND_STATUS_CODE:
            if source.required:
                raise ApiDataNotFoundError(not_found_message, name)
            return None
        if not response.is_success:
            if source.required:
                raise ApiRequestError(f"{failed_message} - {response.status_code}", name)
            return None
        try:
            answer = response.json()
        except ValueError as json_error:
            if source.required:
                raise ApiRequestError(f"{failed_message} - the answer isn't JSON", name) from json_error
            return None
        return ApiValuePaths.get_items(answer, source.items_path)

    @staticmethod
    def get_item_value(source: ApiSource, item: Any, match_key: str) -> Any:
        """The value an answer item answers - read through the source's ``key_type``.

        Args:
            source: The source.
            item: The item.
            match_key: The item's key holding the value.

        Returns:
            The value, None for an item without one or one ``key_type`` refuses.
        """
        if not isinstance(item, Mapping):
            return None
        value = item.get(match_key)
        if value is None or source.key_type is None:
            return value
        try:
            return source.key_type(value)
        except Exception:
            # A value the type refuses answers no value of the objects.
            return None

    @staticmethod
    def get_converted(source: ApiSource | None, mapping: ApiValueMapping, value: Any) -> Any:
        """A value read through its mapping's ``field`` and ``convert``.

        Args:
            source: The source the value came from, None for the object's own.
            mapping: The mapping.
            value: The value.

        Returns:
            The value converted.

        Raises:
            ApiValueConversionError: The mapping's field or function refused the value.
        """
        try:
            return mapping.get_converted(value)
        except Exception as error:
            source_name = source.get_name() if source is not None else None
            raise ApiValueConversionError(
                API_VALUE_CONVERSION_FAILED_MESSAGE.format(target=mapping.set_key, error=error), source_name
            ) from error

    @classmethod
    def apply_source_values(
        cls,
        target_values: dict[tuple[int, str], ApiTargetValue],
        source: ApiSource,
        containers_by_value: dict[Any, list[Any]],
        items_by_value: dict[Any, Any],
    ) -> None:
        """Puts a source's answer items into the targets of the objects holding their values.

        Args:
            target_values: The values of the targets so far.
            source: The source.
            containers_by_value: The containers holding each value.
            items_by_value: The answer item of each value.
        """
        for value, containers in containers_by_value.items():
            item = items_by_value.get(value)
            for mapping in source.mappings:
                mapped_value = ApiValuePaths.get_value_at_path(item, mapping.key) if item is not None else None
                converted_value = cls.get_converted(source, mapping, mapped_value)
                for container in containers:
                    cls.apply_mapping_value(target_values, container, mapping, converted_value)

    @staticmethod
    def apply_mapping_value(
        target_values: dict[tuple[int, str], ApiTargetValue], container: Any, mapping: ApiValueMapping, value: Any
    ) -> None:
        """Settles a mapping's value for its target in a container, by the mapping's ``mode``.

        Args:
            target_values: The values of the targets so far.
            container: The object holding the target.
            mapping: The mapping.
            value: Its value.
        """
        set_name = mapping.get_set_name()
        target_key = (id(container), set_name)
        target_value = target_values.get(target_key)
        if target_value is None:
            existing_value = ApiValuePaths.get_child(container, set_name)
            target_value = ApiTargetValue(
                container=container,
                set_name=set_name,
                set_key=mapping.set_key,
                value=existing_value,
                has_value=existing_value is not None,
            )
            target_values[target_key] = target_value
        if mapping.required:
            target_value.required = True
        if mapping.default is not None:
            target_value.default = mapping.default
        if mapping.mode is ApiValueMappingMode.LAST:
            target_value.value, target_value.has_value = value, value is not None
        elif not target_value.has_value and value is not None:
            target_value.value, target_value.has_value = value, True

    @staticmethod
    def apply_target_values(target_values: dict[tuple[int, str], ApiTargetValue]) -> None:
        """Sets the settled value of every target - its default when empty.

        Args:
            target_values: The values of the targets.

        Raises:
            ApiValueMissingError: A required target stays empty.
        """
        for target_value in target_values.values():
            if target_value.value is None and target_value.default is not None:
                target_value.value = target_value.default
            if target_value.value is None and target_value.required:
                raise ApiValueMissingError(API_TARGET_VALUE_MISSING_MESSAGE.format(target=target_value.set_key))
            ApiValuePaths.set_child(target_value.container, target_value.set_name, target_value.value)
