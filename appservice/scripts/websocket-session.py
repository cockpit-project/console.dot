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
    except websockets.exceptions.ConnectionClosedError as e:
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


async def main():
    async with websockets.serve(handler, '', 8080):
        await asyncio.Future()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
