import async_timeout
import asyncio
import json
import logging
import os
import uuid

import aiohttp
from aiohttp import web
import redis.asyncio as redis

import config

logger = logging.getLogger('multiplexer')

API_URL = os.environ['API_URL']
SESSION_INSTANCE_DOMAIN = os.getenv('SESSION_INSTANCE_DOMAIN', '')

PODMAN_SOCKET = '/run/podman/podman.sock'
# states: wait_target or running
SESSIONS = {}
REDIS = redis.Redis(host=os.environ['REDIS_SERVICE_HOST'], port=int(os.environ.get('REDIS_SERVICE_PORT', '6379')))


async def _wsforward(ws_from, ws_to):
    async for msg in ws_from:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await ws_to.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await ws_to.send_bytes(msg.data)
        elif ws_to.closed:
            await ws_to.close(code=ws_to.close_code, message=msg.extra)
        else:
            raise ValueError(f'unexpected ws message type: {msg.type}')


class Handlers:
    def __init__(self):
        self.ws_client_sessions = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        for s in self.ws_client_sessions.values():
            if not s.closed:
                await s.close()

    def handle_ping(self, request):
        return web.Response(text='pong')

    async def new_session_podman(self, sessionid):
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

        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(PODMAN_SOCKET)) as podman:
            async with podman.post('http://none/v1.12/libpod/containers/create',
                                   data=json.dumps(body).encode()) as response:
                status = response.status
                content = await response.text()

            if status >= 200 and status < 300:
                logger.debug('/new: creating container succeeded with %i: %s; starting container', status, content)
                async with podman.post(f'http://none/v1.12/libpod/containers/{name}/start') as response:
                    status = response.status
                    content = await response.text()

        return status, content

    async def handle_session_new(self, request):
        sessionid = str(uuid.uuid4())
        assert sessionid not in SESSIONS

        if os.path.exists(PODMAN_SOCKET):
            pod_status, content = await self.new_session_podman(sessionid)
        else:
            # TODO: support k8s API
            raise NotImplementedError('cannot create sessions other than podman')

        if pod_status >= 200 and pod_status < 300:
            response = web.json_response({'id': sessionid})
            await update_session(sessionid, 'wait_target')
        else:
            response = web.Response(status=pod_status, text=f'creating session container failed: {content}')

        return response

    async def handle_session_status(self, request):
        sessionid = request.match_info['sessionid']
        try:
            return web.Response(text=SESSIONS[sessionid])
        except KeyError:
            return web.HTTPNotFound(text='unknown session ID')

    async def handle_session_id(self, upstream_req):
        sessionid = upstream_req.match_info['sessionid']
        if sessionid not in SESSIONS:
            return web.HTTPNotFound(text='unknown session ID')

        path = upstream_req.match_info['path']
        if path.startswith('web/'):
            target_url = f'http://session-{sessionid}{SESSION_INSTANCE_DOMAIN}:9090{upstream_req.path_qs}'
        elif path == 'ws':
            target_url = f'http://session-{sessionid}{SESSION_INSTANCE_DOMAIN}:8080{upstream_req.path_qs}'
            if SESSIONS[sessionid] == 'wait_target':
                await update_session(sessionid, 'running')
        else:
            return web.HTTPNotFound(text=f'invalid session path prefix: {path}')

        # reverse-proxy the request to session pod

        # lazily initialize per-session HTTP client
        if sessionid not in self.ws_client_sessions or self.ws_client_sessions[sessionid].closed:
            cs = aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
            self.ws_client_sessions[sessionid] = cs
        else:
            cs = self.ws_client_sessions[sessionid]

        if (upstream_req.method == 'GET'
                and 'upgrade' in upstream_req.headers.get('Connection', '').lower()
                and upstream_req.headers.get('Upgrade') == 'websocket'):
            # it's a websocket upgrade request
            async with cs.ws_connect(target_url, headers=dict(upstream_req.headers)) as downstream_ws_client:
                upstream_ws_response = web.WebSocketResponse()
                upstream_ws_response._headers = downstream_ws_client._response._headers.copy()
                await upstream_ws_response.prepare(upstream_req)
                await asyncio.gather(
                    _wsforward(downstream_ws_client, upstream_ws_response),
                    _wsforward(upstream_ws_response, downstream_ws_client))
                return upstream_ws_response

        else:
            # it's an plain HTTP request
            async with cs.request(
                upstream_req.method,
                target_url,
                headers=upstream_req.headers,
                data=upstream_req.content,
                allow_redirects=False,
            ) as downstream_response:
                h = downstream_response.headers.copy()

                if h.get('Transfer-Encoding') == 'chunked':
                    upstream_resp = web.StreamResponse(
                        status=downstream_response.status,
                        reason=downstream_response.reason,
                        headers=h,
                    )
                    upstream_resp.enable_chunked_encoding()
                    await upstream_resp.prepare(upstream_req)
                    async for data, _ in downstream_response.content.iter_chunks():
                        await upstream_resp.write(data)
                    await upstream_resp.write_eof()
                    return upstream_resp
                else:
                    upstream_resp = web.Response(
                        status=downstream_response.status,
                        reason=downstream_response.reason,
                        headers=h,
                        body=await downstream_response.content.read(),
                    )

                return upstream_resp


async def init_sessions():
    global SESSIONS
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


async def watch_redis(channel):
    global SESSIONS
    while True:
        try:
            async with async_timeout.timeout(1):
                message = await channel.get_message(ignore_subscribe_messages=True)
                if message is not None and message['channel'] == b'sessions':
                    logger.debug("got redis sessions update: %s", message['data'])
                    try:
                        SESSIONS = json.loads(message['data'].decode())
                    except json.decoder.JSONDecodeError as e:
                        logger.warning("invalid JSON, starting without sessions: %s", e)
                        SESSIONS = {}

                await asyncio.sleep(0.01)
        except asyncio.TimeoutError:
            pass


async def main():
    async with Handlers() as handlers:
        pubsub = REDIS.pubsub()
        await pubsub.subscribe('sessions')
        asyncio.create_task(watch_redis(pubsub))
        await init_sessions()

        app = web.Application()
        app.router.add_route('GET', f'{config.ROUTE_API}/ping', handlers.handle_ping)
        app.router.add_route('GET', f'{config.ROUTE_API}/sessions/new', handlers.handle_session_new)
        app.router.add_route('GET', f'{config.ROUTE_API}/sessions/{{sessionid}}/status', handlers.handle_session_status)
        app.router.add_route('*', f'{config.ROUTE_WSS}/sessions/{{sessionid}}/{{path:.*}}', handlers.handle_session_id)

        runner = web.AppRunner(app, auto_decompress=False)
        await runner.setup()
        await web.TCPSite(runner, port=8080).start()
        while True:
            await asyncio.sleep(60)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
