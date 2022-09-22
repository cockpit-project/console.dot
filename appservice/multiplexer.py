import async_timeout
import asyncio
import json
import logging
import os
import uuid

import httpx
import redis.exceptions
import redis.asyncio
import uvicorn
import websockets

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_until_first_complete
from starlette.responses import PlainTextResponse, JSONResponse, StreamingResponse
from starlette.websockets import WebSocket

import config

API_URL = os.environ['API_URL']
SESSION_INSTANCE_DOMAIN = os.getenv('SESSION_INSTANCE_DOMAIN', '')
PODMAN_SOCKET = '/run/podman/podman.sock'

# states: wait_target or running
SESSIONS = {}

REDIS = redis.asyncio.Redis(host=os.environ['REDIS_SERVICE_HOST'],
                            port=int(os.environ.get('REDIS_SERVICE_PORT', '6379')))
logger = logging.getLogger('multiplexer')
app = Starlette()


@app.route(f'{config.ROUTE_API}/ping')
async def handle_ping(request):
    return PlainTextResponse('pong')


async def new_session_podman(sessionid):
    name = f'session-{sessionid}'
    body = {
        'image': 'quay.io/rhn_engineering_mpitt/ws',
        'name': name,
        # for local debugging
        # 'command': ['sleep', 'infinity'],
        # XXX: http://localhost:8080 origin is for directly connecting to appservice, without 3scale
        'command': ['sh', '-exc',
                    f"mkdir -p /tmp/conf/cockpit; "
                    f"printf '[Webservice]\nUrlRoot={config.ROUTE_WSS}/sessions/{sessionid}/web\\n"
                    f"Origins = {API_URL} http://localhost:8080\\n'"
                    "> /tmp/conf/cockpit/cockpit.conf;"
                    "export XDG_CONFIG_DIRS=/tmp/conf;"
                    "exec /usr/libexec/cockpit-ws --for-tls-proxy --local-session=socat-session.sh"],
        'netns': {'nsmode': 'bridge'},
        # deprecated; use this with podman ≥ 4: 'Networks': {'consoledot': {}},
        'cni_networks': ['consoledot'],
        'user': 'cockpit-wsinstance',
    }

    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=PODMAN_SOCKET)) as podman:
        response = await podman.post('http://none/v1.12/libpod/containers/create', data=json.dumps(body).encode())
        status = response.status_code
        content = response.text

        if status >= 200 and status < 300:
            logger.debug('/new: creating container succeeded with %i: %s; starting container', status, content)
            response = await podman.post(f'http://none/v1.12/libpod/containers/{name}/start')
            status = response.status_code
            content = response.text

    return status, content


@app.route(f'{config.ROUTE_API}/sessions/new')
async def handle_session_new(request):
    sessionid = str(uuid.uuid4())
    assert sessionid not in SESSIONS

    if os.path.exists(PODMAN_SOCKET):
        pod_status, content = await new_session_podman(sessionid)
    else:
        # TODO: support k8s API
        raise NotImplementedError('cannot create sessions other than podman')

    if pod_status >= 200 and pod_status < 300:
        response = JSONResponse({'id': sessionid})
        await update_session(sessionid, 'wait_target')
    else:
        response = PlainTextResponse(f'creating session container failed: {content}', status_code=pod_status)

    return response


@app.route(f'{config.ROUTE_API}/sessions/{{sessionid}}/status')
async def handle_session_status(request):
    sessionid = request.path_params['sessionid']
    try:
        return PlainTextResponse(SESSIONS[sessionid])
    except KeyError:
        return PlainTextResponse('unknown session ID', status_code=404)


async def ws_up2down(recv_ws: WebSocket, send_ws: websockets.WebSocketClientProtocol):
    while True:
        msg = await recv_ws.receive()
        if msg['type'] == 'websocket.receive':
            data = msg.get('text') or msg.get('bytes')
            await send_ws.send(data)
        elif msg['type'] == 'websocket.disconnect':
            break


async def ws_down2up(recv_ws: websockets.WebSocketClientProtocol, send_ws: WebSocket):
    while True:
        data = await recv_ws.recv()
        if isinstance(data, str):
            await send_ws.send_text(data)
        else:
            await send_ws.send_bytes(data)


async def websocket_forward(upstream_ws: WebSocket, target_url: str):
    await upstream_ws.accept()
    headers = []
    origin = None
    for k, v in upstream_ws.scope['headers']:
        if k == b'origin':
            origin = v.decode()
        # XXX: do we need to forward any other headers?

    logging.debug('websocket_forward %s → %s; origin %s', upstream_ws.url.path, target_url, origin)

    downstream_ws = await websockets.connect(
        target_url,
        subprotocols=upstream_ws.scope['subprotocols'],
        origin=origin,
        extra_headers=headers,
    )
    await run_until_first_complete(
        (ws_up2down, {'recv_ws': upstream_ws, 'send_ws': downstream_ws}),
        (ws_down2up, {'recv_ws': downstream_ws, 'send_ws': upstream_ws}),
    )
    await downstream_ws.close()


@app.websocket_route(f'{config.ROUTE_WSS}/sessions/{{sessionid}}/ws')
async def handle_session_id_bridge(ws: WebSocket):
    '''reverse-proxy bridge websocket to session pod'''

    sessionid = ws.path_params['sessionid']
    if sessionid not in SESSIONS:
        await ws.close(reason='unknown session ID', code=404)
        return

    if SESSIONS[sessionid] == 'wait_target':
        asyncio.create_task(update_session(sessionid, 'running'))
    await websocket_forward(ws, f'ws://session-{sessionid}{SESSION_INSTANCE_DOMAIN}:8080{ws.url.path}')


@app.websocket_route(f'{config.ROUTE_WSS}/sessions/{{sessionid}}/web/{{path:path}}')
async def handle_session_id_ws(ws: WebSocket):
    '''reverse-proxy cockpit websocket to session pod'''

    sessionid = ws.path_params['sessionid']
    if sessionid not in SESSIONS:
        await ws.close(reason='unknown session ID', code=404)
        return
    await websocket_forward(ws, f'ws://session-{sessionid}{SESSION_INSTANCE_DOMAIN}:9090{ws.url.path}')


@app.route(f'{config.ROUTE_WSS}/sessions/{{sessionid}}/web/{{path:path}}', methods=['GET', 'HEAD'])
async def handle_session_id_http(upstream_req):
    '''reverse-proxy cockpit HTTP to session pod'''

    sessionid = upstream_req.path_params['sessionid']
    if sessionid not in SESSIONS:
        return PlainTextResponse('unknown session ID', status_code=404)

    target_url = f'http://session-{sessionid}{SESSION_INSTANCE_DOMAIN}:9090{upstream_req.url.path}'

    client = httpx.AsyncClient()
    downstream_req = client.build_request(
        method=upstream_req.method,
        url=target_url,
        headers=upstream_req.headers.items(),
        params=upstream_req.query_params,
        cookies=upstream_req.cookies,
    )
    downstream_response = await client.send(downstream_req, stream=True)
    return StreamingResponse(
        downstream_response.aiter_raw(),
        headers=dict(downstream_response.headers),
        background=BackgroundTask(downstream_response.aclose)
    )


async def watch_redis(channel):
    global SESSIONS
    while True:
        try:
            async with async_timeout.timeout(1):
                message = await channel.get_message(ignore_subscribe_messages=True)
                if message is not None and message['channel'] == b'sessions':
                    logger.debug('got redis sessions update: %s', message['data'])
                    try:
                        SESSIONS = json.loads(message['data'].decode())
                    except json.decoder.JSONDecodeError as e:
                        logger.warning('invalid JSON, starting without sessions: %s', e)
                        SESSIONS = {}

                await asyncio.sleep(0.01)
        except asyncio.TimeoutError:
            pass


@app.on_event('startup')
async def init_sessions():
    global SESSIONS

    pubsub = REDIS.pubsub()
    # wait for Redis service to be up
    for retry in range(10):
        try:
            await pubsub.subscribe('sessions')
            break
        except redis.exceptions.ConnectionError as e:
            logger.warning('Failed to connect to Redis, retry %i: %s', retry, e)
            await asyncio.sleep(retry * retry + 1)
    else:
        raise RuntimeError('timed out trying to connect to Redis')

    asyncio.create_task(watch_redis(pubsub))

    sessions = await REDIS.get('sessions')
    if sessions is None:
        SESSIONS = {}
    else:
        try:
            SESSIONS = json.loads(sessions)
        except json.decoder.JSONDecodeError:
            SESSIONS = {}

    logger.debug('initial sessions: %s', SESSIONS)


async def update_session(session_id, status):
    global SESSIONS
    SESSIONS[session_id] = status
    dumped_sessions = json.dumps(SESSIONS)
    await REDIS.set('sessions', dumped_sessions)
    await REDIS.publish('sessions', dumped_sessions)


# Terrifying hack around broken 3scale Connection: header; see https://issues.redhat.com/browse/RHCLOUD-21326
# uvicorn's H11Protocol.handle_events() can't be tapped into, so we need to monkey-patch
# h11.Connection.next_event() to deliver non-broken headers; otherwise uvicorn refuses these paths with
# "Unsupported upgrade request".
import h11  # noqa: E402
h11.Connection.next_event_real = h11.Connection.next_event


def hack_h11_con_next_event(self):
    res = h11.Connection.next_event_real(self)
    if type(res) == h11.Request:
        connection_idx = None
        has_upgrade = False
        for i, (name, value) in enumerate(res.headers):
            if name == b'connection' and b'Upgrade' in value:
                connection_idx = i
            if name == b'upgrade':
                has_upgrade = True

        if connection_idx is not None and not has_upgrade:
            res.headers._full_items[connection_idx] = (
                    res.headers._full_items[connection_idx][0],  # raw name
                    res.headers._full_items[connection_idx][1],  # normalized name
                    res.headers._full_items[connection_idx][2].replace(b'Upgrade', b''))  # value
            logger.debug('hack_h11_con_next_event on %s: fixing broken Connection: header', res.target)

    return res


h11.Connection.next_event = hack_h11_con_next_event
# End hack

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    uvicorn.run(app, host='0.0.0.0', port=8080)
