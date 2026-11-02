#!/usr/bin/env python3
"""A plain TCP relay in front of Ovis, inside this container and in the codex-ovis-gate container.

    python3 scripts/ovis_forward.py LISTEN_HOST LISTEN_PORT TARGET_HOST TARGET_PORT

Why it exists. Docker delivers the published port to the container's own
address, so a server bound to 127.0.0.1 never sees it and Ovis had to bind
0.0.0.0. Ovis judges exposure by its BIND address alone (`lanExposed` in
server/access.ts): a wildcard bind with no token and nothing paired shows "Anyone
who can reach 0.0.0.0 on your network can type into every pane" on every page,
and there is no setting for it. Bound to 127.0.0.1 behind this relay, Ovis sees
a loopback bind and loopback connections.

Who gets in does not change. Without a token Ovis was already letting in every
request that reached the port; with one, `decideAccess` checks the token before
it looks at loopback, so a token is still required of every connection that
comes through here. The port itself stays published on the host's 127.0.0.1
only (scripts/ovis.sh, the gate).

Bytes are relayed unchanged in both directions (HTTP, server-sent events and
WebSocket alike) and a half-close is passed on, so a client that finishes
sending still gets its whole answer.
"""

from __future__ import annotations

import asyncio
import sys

CHUNK = 64 * 1024


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def relay(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter,
                target: tuple[str, int]) -> None:
    try:
        upstream_r, upstream_w = await asyncio.open_connection(*target)
    except OSError:
        client_w.close()
        return
    try:
        await asyncio.gather(pump(client_r, upstream_w), pump(upstream_r, client_w))
    finally:
        for w in (client_w, upstream_w):
            w.close()


async def serve(listen: tuple[str, int], target: tuple[str, int]) -> None:
    server = await asyncio.start_server(lambda r, w: relay(r, w, target), *listen, reuse_address=True)
    print(f"ovis-forward {listen[0]}:{listen[1]} -> {target[0]}:{target[1]}", flush=True)
    async with server:
        await server.serve_forever()


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    _, lhost, lport, thost, tport = argv
    try:
        asyncio.run(serve((lhost, int(lport)), (thost, int(tport))))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
