#!/usr/bin/env python3
import asyncio
import logging
import os
import sys

import websockets

logger = logging.getLogger(__name__)


async def ws2out(ws):
    try:
        async for message in ws:
            os.write(1, message)
    except websockets.exceptions.ConnectionClosed as e:
        logger.info('ws2out: websocket connection got closed: %s', e)
        return


async def in2ws(reader, ws):
    while True:
        message = await reader.read(4096)
        if not message:
            logger.info('in -> ws: EOF')
            await ws.close()
            break
        await ws.send(message)


async def handler(ws):
    # wrap stdin in an asyncio stream
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    ws2out_task = asyncio.create_task(ws2out(ws))
    in2ws_task = asyncio.create_task(in2ws(reader, ws))
    _done, pending = await asyncio.wait([ws2out_task, in2ws_task],
                                        return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    ws.ws_server.close()


async def main():
    async with websockets.serve(handler, '', 8080) as server:
        await server.wait_closed()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

    # HACK: cockpit-ws does not auto-exit after --local-session ends, or have a flag for it;
    # but we want to clean up the session container, so kill it
    cockpit_ws_pid = os.getppid()
    logger.info('session ended; killing cockpit-ws parent pid %i', cockpit_ws_pid)
    os.kill(cockpit_ws_pid, 9)
