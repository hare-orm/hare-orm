import asyncio
import json
from functools import partial
from types import SimpleNamespace

import pytest

from hare.contrib.application_lifecycle import ApplicationLifecycle
from hare.core.hare_context import HareContext


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "initialization",
        "running",
        pytest.param("robyn_startup", marks=pytest.mark.requires("robyn")),
        pytest.param("robyn_shutdown", marks=pytest.mark.requires("robyn")),
    ],
)
@pytest.mark.parametrize("cancelled", [False, True], ids=["error", "cancellation"])
@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleaned", "cleanup_error"])
async def test_application_lifecycle_preserves_work_and_cleanup_errors(monkeypatch, phase, cancelled, cleanup_fails):
    primary = asyncio.CancelledError() if cancelled else ValueError("application failed")
    cleanup = RuntimeError("cleanup failed")
    closed = []
    previous_context = HareContext.get_current()
    previous_application = ApplicationLifecycle.active.get()

    async def initialize(context, **kwargs):
        if phase == "initialization":
            raise primary
        context._connections = SimpleNamespace(aliases=lambda: [])

    async def close(context):
        assert HareContext.get_current() is context
        closed.append(context)
        if cleanup_fails:
            raise cleanup

    async def fail():
        raise primary

    monkeypatch.setattr(HareContext, "init", initialize)
    monkeypatch.setattr(HareContext, "close_connections", close)
    lifecycle = ApplicationLifecycle({})
    if phase.startswith("robyn_"):
        from hare.contrib.frameworks.robyn import HareRobyn

        app = SimpleNamespace(
            hare=lifecycle,
            application_startup_handler=fail if phase == "robyn_startup" else None,
            application_shutdown_handler=fail if phase == "robyn_shutdown" else None,
            run_application_handler=HareRobyn.run_application_handler,
        )
        if phase == "robyn_shutdown":
            await HareRobyn.start_hare(app)

    with pytest.raises(BaseExceptionGroup if cleanup_fails else type(primary)) as caught:
        if phase == "initialization":
            await lifecycle.start()
        elif phase == "running":
            async with lifecycle.running():
                raise primary
        elif phase == "robyn_startup":
            await HareRobyn.start_hare(app)
        else:
            await HareRobyn.stop_hare(app)

    if cleanup_fails:
        assert caught.value.exceptions == (primary, cleanup)
    else:
        assert caught.value is primary
    assert len(closed) == 1
    assert lifecycle.context is None
    assert HareContext.get_current() is previous_context
    assert ApplicationLifecycle.active.get() is previous_application
    await lifecycle.stop()
    assert len(closed) == 1


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "framework",
    [
        pytest.param("fastapi", marks=pytest.mark.requires("fastapi", "http")),
        pytest.param("litestar", marks=pytest.mark.requires("litestar")),
        pytest.param("robyn", marks=pytest.mark.requires("robyn")),
    ],
)
async def test_applications_use_explicit_contexts_and_leave_borrowed_contexts_open(framework):
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}}
    first, second = HareContext(), HareContext()
    for context in (first, second):
        await context.init(config=config, connect=False, _enable_global_fallback=False)
    previous_fallback = HareContext.global_context
    apps = []
    shared_router = None

    def make_app(context):
        nonlocal shared_router
        if framework == "fastapi":
            from fastapi import WebSocket
            from fastapi.responses import JSONResponse
            from fastapi.testclient import TestClient

            from hare.contrib.frameworks.fastapi import HareFastAPI

            async def failure(request, error):
                return JSONResponse({"context": id(HareContext.require_current())}, status_code=500)

            app = HareFastAPI(hare_config=config, hare_context=context, exception_handlers={Exception: failure})

            @app.get("/context")
            async def current():
                return {"context": id(HareContext.require_current())}

            @app.get("/fail")
            async def fail():
                raise ValueError("request failure")

            @app.websocket("/socket")
            async def socket_handler(socket: WebSocket):
                await socket.accept()
                await socket.send_json({"context": id(HareContext.require_current())})
                await socket.close()
        elif framework == "litestar":
            from litestar import Litestar, Response, WebSocket, get, websocket
            from litestar.testing import TestClient

            from hare.contrib.frameworks.litestar import HarePlugin

            @get("/context")
            async def current() -> dict[str, int]:
                return {"context": id(HareContext.require_current())}

            def failure(request, error):
                return Response({"context": id(HareContext.require_current())}, status_code=500)

            @get("/fail")
            async def fail() -> dict[str, int]:
                raise ValueError("request failure")

            @websocket("/socket")
            async def socket_handler(socket: WebSocket) -> None:
                await socket.accept()
                await socket.send_json({"context": id(HareContext.require_current())})
                await socket.close()

            app = Litestar(
                route_handlers=[current, fail, socket_handler],
                plugins=[HarePlugin(config, hare_context=context)],
                exception_handlers={Exception: failure},
            )
        else:
            from robyn import Headers, Response
            from robyn.authentication import AuthenticationHandler, BearerGetter
            from robyn.robyn import Identity
            from robyn.testing import TestClient

            from hare.contrib.frameworks.robyn import HareRobyn, HareSubRouter

            app = HareRobyn(__file__, hare_config=config, hare_context=context)

            class Authentication(AuthenticationHandler):
                async def authenticate(self, request):
                    return Identity({"context": str(id(HareContext.require_current()))})

            app.configure_authentication(Authentication(BearerGetter()))

            @app.get("/private", auth_required=True)
            async def private(request):
                return {"context": int(request.identity.claims["context"])}

            def failure(error):
                return Response(
                    status_code=500,
                    headers=Headers({"Content-Type": "application/json"}),
                    description=json.dumps({"context": id(HareContext.require_current())}),
                )

            app.exception(failure)

            @app.get("/context")
            async def current():
                return {"context": id(HareContext.require_current())}

            @app.get("/fail")
            async def fail():
                raise ValueError("request failure")

            if shared_router is None:
                shared_router = HareSubRouter(prefix="/shared")

                @shared_router.get("/context")
                async def shared():
                    return {"context": id(HareContext.require_current())}

            app.include_router(shared_router)

        apps.append(app)
        client = TestClient if framework == "robyn" else partial(TestClient, raise_server_exceptions=False)
        return client, app

    def requests(first_client, first_app, second_client, second_app):
        previous_context = HareContext.get_current()
        with first_client(first_app) as client_a, second_client(second_app) as client_b:
            assert client_a.get("/context").json() == {"context": id(first)}
            assert client_a.get("/fail").json() == {"context": id(first)}
            assert client_b.get("/fail").json() == {"context": id(second)}
            if framework == "robyn":
                assert client_a.get("/private").json() == {"context": id(first)}
                assert client_b.get("/private").json() == {"context": id(second)}
                assert client_a.get("/shared/context").json() == {"context": id(first)}
                assert client_b.get("/shared/context").json() == {"context": id(second)}
            else:
                for client, context in ((client_a, first), (client_b, second)):
                    with client.websocket_connect("/socket") as socket:
                        assert socket.receive_json() == {"context": id(context)}
            assert client_b.get("/context").json() == {"context": id(second)}
            assert client_a.get("/context").json() == {"context": id(first)}
            assert HareContext.get_current() is previous_context

    try:
        first_client, first_app = make_app(first)
        second_client, second_app = make_app(second)
        if framework == "robyn":
            for app in apps:
                await app.start_hare()
        await asyncio.to_thread(requests, first_client, first_app, second_client, second_app)
        if framework == "robyn":
            for app in apps:
                await app.stop_hare()
        assert first._inited and second._inited
        assert HareContext.global_context is previous_fallback
    finally:
        if framework == "robyn":
            for app in apps:
                await app.hare.stop()
        await first.close_connections()
        await second.close_connections()


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.requires("robyn")
async def test_shared_websocket_router_keeps_context_for_background_work_and_close():
    from hare.contrib.frameworks.robyn import HareRobyn, HareSubRouter

    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}}
    contexts = [HareContext(), HareContext()]
    apps = []
    started = asyncio.Queue()
    release = {str(index): asyncio.Event() for index in range(2)}
    observed = []
    router = HareSubRouter(prefix="/shared")

    @router.websocket("/socket")
    async def socket_handler(socket):
        observed.append(("work", socket.id, HareContext.require_current()))
        started.put_nowait(socket.id)
        try:
            await release[socket.id].wait()
        finally:
            observed.append(("finish", socket.id, HareContext.require_current()))

    @socket_handler.on_connect
    def connect(socket):
        observed.append(("connect", socket.id, HareContext.require_current()))

    @socket_handler.on_close
    def close(socket):
        observed.append(("close", socket.id, HareContext.require_current()))

    previous = HareContext.get_current()
    try:
        for index, context in enumerate(contexts):
            await context.init(config=config, connect=False, _enable_global_fallback=False)
            app = HareRobyn(__file__, hare_config=config, hare_context=context)
            app.include_router(router)
            apps.append(app)
            await app.start_hare()
            await app.web_socket_router.routes["/shared/socket"]["connect"].handler(
                SimpleNamespace(id=str(index), message_channel=None)
            )
            assert HareContext.get_current() is previous
        assert {await started.get(), await started.get()} == {"0", "1"}
        for index, app in enumerate(apps):
            release[str(index)].set()
            await app.web_socket_router.routes["/shared/socket"]["close"].handler(
                SimpleNamespace(id=str(index), message_channel=None)
            )
            assert HareContext.get_current() is previous
        for phase, identity, context in observed:
            assert context is contexts[int(identity)], (phase, identity)
        assert len(observed) == 8
    finally:
        for event in release.values():
            event.set()
        for app in apps:
            await app.stop_hare()
        for context in contexts:
            await context.close_connections()
