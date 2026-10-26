"""An HTTP server of the webhook tests."""

import asyncio


class HttpReceiver:
    """A minimal HTTP server recording each request and answering with ``status``, ``body`` and, for a
    redirect, ``location``."""

    def __init__(self, status: int = 200, body: str = "", location: str | None = None) -> None:
        self.status = status
        self.body = body
        self.location = location
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.server: asyncio.Server | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {}
        for line in head.decode().split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append((headers, body))
        response_body = self.body.encode()
        location = f"location: {self.location}\r\n" if self.location is not None else ""
        head = f"HTTP/1.1 {self.status} X\r\ncontent-length: {len(response_body)}\r\n{location}"
        head += "connection: close\r\n\r\n"
        writer.write(head.encode() + response_body)
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/events"

    async def __aexit__(self, *exc_info: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
