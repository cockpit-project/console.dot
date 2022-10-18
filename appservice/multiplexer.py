import async_timeout
import asyncio
import base64
import enum
import json
import logging
import os
import socket
import uuid
from typing import Dict, List, Union

import httpx
import redis.exceptions
import redis.asyncio
import uvicorn
import websockets
import websockets.exceptions

from starlette.applications import Starlette
from starlette.authentication import (
    requires,
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
    UnauthenticatedUser,
)
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.background import BackgroundTask
from starlette.concurrency import run_until_first_complete
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, JSONResponse, StreamingResponse
from starlette.websockets import WebSocket

import config

API_URL = os.environ['API_URL']
SESSION_INSTANCE_DOMAIN = os.getenv('SESSION_INSTANCE_DOMAIN', '')
PODMAN_SOCKET = '/run/podman/podman.sock'
K8S_SERVICE_ACCOUNT = '/run/secrets/kubernetes.io/serviceaccount'
MY_DIR = os.path.dirname(__file__)

# for testing only
FAKE_AUTHENTICATION = os.environ.get("FAKE_AUTHENTICATION") == "yes"
if FAKE_AUTHENTICATION and not API_URL.startswith("https://localhost:"):
    raise RuntimeError("FAKE_AUTHENTICATION is not supported in production")


class Backend(enum.Enum):
    PODMAN = 0
    K8S = 1


#
# global state
#

# session_id → {status: wait_target or running, ip: session container address}
SESSIONS: Dict[str, Dict[str, str]] = {}
WAIT_RUNNING_FUTURES: Dict[str, List[asyncio.Future]] = {}
# file name → content
STATIC_HTML: Dict[str, str] = {}
BACKEND = None
logger = logging.getLogger('multiplexer')
app = Starlette()


def init():
    global REDIS, STATIC_HTML, BACKEND

    REDIS = redis.asyncio.Redis(host=os.environ['REDIS_SERVICE_HOST'],
                                port=int(os.environ.get('REDIS_SERVICE_PORT', '6379')))
    for html_name in ('wait-session.html', 'closed-session.html'):
        with open(os.path.join(MY_DIR, html_name)) as f:
            STATIC_HTML[html_name] = f.read()

    if os.path.exists(K8S_SERVICE_ACCOUNT):
        BACKEND = Backend.K8S
    elif os.path.exists(PODMAN_SOCKET):
        BACKEND = Backend.PODMAN
    else:
        raise NotImplementedError('cannot create sessions without kubernetes or podman')


#
# Authentication
#
# Parses X-RH-Identity header and sets credentials and user information.
# Requests without header have no credentials and are unauthenticated.

class AuthScope(str, enum.Enum):
    """Credential scopes

    An authenticated user has either 'User' or 'System' scope.
    """
    authenticated = "authenticated"
    user = "User"
    system = "System"


class XRHIdentityUser(SimpleUser):
    """User/System information from x-rh-identity header
    """
    def __init__(
        self, username: Union[int, uuid.UUID], org_id: int, identity_type: str, extra: dict
    ):
        super().__init__(username)
        self.org_id = org_id
        self.identity_type = identity_type
        self.extra = extra

    @property
    def display_name(self) -> str:
        return f"{self.username} ({self.identity_type}, org: {self.org_id})"


class XRHIdentityAuthBackend(AuthenticationBackend):
    """Authenticate User/System by x-rh-identity header
    """
    async def authenticate(self, conn):
        try:
            hdr_b64 = conn.headers["x-rh-identity"]
        except KeyError:
            # no header, unauthenticated
            if FAKE_AUTHENTICATION:
                fake_scope = AuthCredentials(
                    [AuthScope.authenticated, AuthScope.user, AuthScope.system]
                )
                fake_user = XRHIdentityUser(
                    42, org_id=23, identity_type="User", extra={}
                )
                return fake_scope, fake_user
            else:
                return AuthCredentials(), UnauthenticatedUser()

        try:
            identity = json.loads(base64.b64decode(hdr_b64))["identity"]
        except (ValueError, KeyError):
            raise AuthenticationError("Invalid x-rh-identity header")

        org_id = int(identity["org_id"])
        identity_type = identity["type"]

        if identity_type == "User":
            subidentity = identity["user"]
            scope = AuthScope.user
            user = XRHIdentityUser(
                int(subidentity["user_id"]), org_id, identity_type, subidentity
            )
        elif identity_type == "System":
            subidentity = identity["system"]
            scope = AuthScope.system
            user = XRHIdentityUser(
                uuid.UUID(subidentity["cn"]), org_id, identity_type, subidentity
            )
        else:
            raise AuthenticationError("Invalid x-rh-identity header")

        return AuthCredentials([AuthScope.authenticated, scope]), user


app.add_middleware(AuthenticationMiddleware, backend=XRHIdentityAuthBackend())


@app.route(f'{config.ROUTE_API}/ping')
async def handle_ping(request: Request):
    return PlainTextResponse('pong')


async def new_session_podman(sessionid):
    name = f'session-{sessionid}'
    body = {
        'image': 'localhost/webconsoleapp',
        'name': name,
        # for local debugging
        # 'command': ['sleep', 'infinity'],
        'command': ['/cockpit-ws-session.sh'],
        'env': {'API_URL': API_URL, 'ROUTE_WSS': config.ROUTE_WSS, 'SESSION_ID':  sessionid},
        'netns': {'nsmode': 'bridge'},
        # deprecated; use this with podman ≥ 4: 'Networks': {'consoledot': {}},
        'cni_networks': ['consoledot'],
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


async def new_session_k8s(sessionid):
    name = f'session-{sessionid}'
    with open(os.path.join(K8S_SERVICE_ACCOUNT, 'namespace')) as f:
        namespace = f.read().strip()
    with open(os.path.join(K8S_SERVICE_ACCOUNT, 'token')) as f:
        authorization = 'Bearer ' + f.read().strip()

    async with httpx.AsyncClient(verify=os.path.join(K8S_SERVICE_ACCOUNT, 'ca.crt')) as http:
        response = await http.post(f'https://kubernetes.default.svc/api/v1/namespaces/{namespace}/pods',
                                   headers={
                                       'Authorization': authorization,
                                       'Content-Type': 'application/yaml'
                                   },
                                   data=f'''
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  labels:
    app: webconsoleapp-session
spec:
  hostname: {name}
  # subdomain must match Service name in webconsoleapp-k8s.yaml
  subdomain: webconsoleapp-sessions
  restartPolicy: Never
  containers:
  - name: ws
    # FIXME: make this dynamic
    image: image-registry.openshift-image-registry.svc:5000/cockpit-dev/webconsoleapp
    # command: ["sleep", "infinity"]
    command: ['/cockpit-ws-session.sh']
    ports:
      - name: ws
        containerPort: 8080
      - name: web
        containerPort: 9090
    env:
      - name: API_URL
        value: {API_URL}
      - name: ROUTE_WSS
        value: {config.ROUTE_WSS}
      - name: SESSION_ID
        value: {sessionid}
'''.encode())
        return response.status_code, response.text


@app.route(f'{config.ROUTE_API}/sessions/new', methods=['POST'])
@requires([AuthScope.authenticated, AuthScope.user])
async def handle_session_new(request: Request):
    global SESSIONS

    sessionid = str(uuid.uuid4())
    assert sessionid not in SESSIONS

    if BACKEND == Backend.K8S:
        logger.debug('new_session: creating %s with k8s', sessionid)
        pod_status, content = await new_session_k8s(sessionid)
    elif BACKEND == Backend.PODMAN:
        logger.debug('new_session: creating %s with podman', sessionid)
        pod_status, content = await new_session_podman(sessionid)
    else:
        raise NotImplementedError(f'unknown backend {BACKEND}')

    logger.debug('new_session result status %i, content: %s', pod_status, content)

    if pod_status >= 200 and pod_status < 300:
        session_hostname = f'session-{sessionid}{SESSION_INSTANCE_DOMAIN}'
        # resolve and cache IP addresses now, to avoid DNS lag/trouble during proxying
        for retry in range(30):
            try:
                addr = socket.gethostbyname(session_hostname)
            except socket.gaierror as e:
                logger.debug('resolving %s failed, attempt #%i: %s', session_hostname, retry, e)
                await asyncio.sleep(1)
                continue

            logger.debug('session pod %s resolves to %s', session_hostname, addr)
            SESSIONS[sessionid] = {'ip': addr, 'status': None}
            await update_session(sessionid, 'wait_target')
            response = JSONResponse({'id': sessionid})
            break
        else:
            response = PlainTextResponse('timed out waiting for session container to resolve in DNS', status_code=500)
    else:
        response = PlainTextResponse(f'creating session container failed: {content}', status_code=pod_status)

    return response


@app.route(f'{config.ROUTE_API}/sessions/{{sessionid}}/status')
@requires([AuthScope.authenticated])
async def handle_session_status(request: Request):
    sessionid = request.path_params['sessionid']
    try:
        return PlainTextResponse(SESSIONS[sessionid]['status'])
    except KeyError:
        return PlainTextResponse('unknown session ID', status_code=404)


@app.route(f'{config.ROUTE_API}/sessions/{{sessionid}}/wait-running')
@requires([AuthScope.authenticated])
async def handle_session_wait_running(request: Request):
    sessionid = request.path_params['sessionid']
    if sessionid not in SESSIONS:
        return PlainTextResponse('unknown session ID', status_code=404)

    f = asyncio.get_running_loop().create_future()
    WAIT_RUNNING_FUTURES.setdefault(sessionid, []).append(f)
    # watch_redis() resolves f
    return PlainTextResponse(await f)


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
        try:
            data = await recv_ws.recv()
        except websockets.exceptions.ConnectionClosed as e:
            logger.info('%s closed: %s', send_ws.url.path, e)
            break

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
@requires([AuthScope.authenticated])
async def handle_session_id_bridge(websocket: WebSocket):
    '''reverse-proxy bridge websocket to session pod'''

    sessionid = websocket.path_params['sessionid']
    if sessionid not in SESSIONS:
        await websocket.close(reason='unknown session ID', code=404)
        return

    if SESSIONS[sessionid]['status'] == 'wait_target':
        asyncio.create_task(update_session(sessionid, 'running'))
    ip = SESSIONS[sessionid]['ip']
    await websocket_forward(websocket, f'ws://{ip}:8080{websocket.url.path}')
    await update_session(sessionid, 'closed')


@app.websocket_route(f'{config.ROUTE_WSS}/sessions/{{sessionid}}/web/{{path:path}}')
@requires([AuthScope.authenticated])
async def handle_session_id_ws(websocket: WebSocket):
    '''reverse-proxy cockpit websocket to session pod'''

    sessionid = websocket.path_params['sessionid']
    if sessionid not in SESSIONS:
        await websocket.close(reason='unknown session ID', code=404)
        return
    ip = SESSIONS[sessionid]['ip']
    await websocket_forward(websocket, f'ws://{ip}:9090{websocket.url.path}')
    await update_session(sessionid, 'closed')


@app.route(f'{config.ROUTE_WSS}/sessions/{{sessionid}}/web/{{path:path}}', methods=['GET', 'HEAD'])
@requires([AuthScope.authenticated])
async def handle_session_id_http(request: Request):
    '''reverse-proxy cockpit HTTP to session pod'''

    upstream_req = request
    sessionid = upstream_req.path_params['sessionid']
    session = SESSIONS.get(sessionid)
    if session is None:
        return PlainTextResponse('unknown session ID', status_code=404)
    if session['status'] == 'closed':
        return HTMLResponse(STATIC_HTML['closed-session.html'])
    elif session['status'] != 'running':
        return HTMLResponse(STATIC_HTML['wait-session.html'])

    target_url = f'http://{session["ip"]}:9090{upstream_req.url.path}'

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

                    # resolve wait-running futures
                    logger.debug('watch_redis WAIT_RUNNING_FUTURES before: %s', WAIT_RUNNING_FUTURES)
                    for sessionid, wait_futures in WAIT_RUNNING_FUTURES.items():
                        if SESSIONS.get(sessionid, {}).get('status') != 'running':
                            continue

                        for f in wait_futures.copy():
                            logger.debug('session %s status is running, resolving wait-running', sessionid)
                            f.set_result(None)
                            wait_futures.remove(f)
                    logger.debug('watch_redis WAIT_RUNNING_FUTURES after: %s', WAIT_RUNNING_FUTURES)

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
    SESSIONS[session_id]['status'] = status
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
    init()
    uvicorn.run(app, host='0.0.0.0', port=8080)
