#!/usr/bin/env python3

import base64
import unittest
import subprocess
import os
import time
import urllib.error
import urllib.request
import ssl
import json

from appservice import config

projroot = os.path.dirname(os.path.dirname(__file__))


class IntegrationTest(unittest.TestCase):

    def setUp(self):
        try:
            subprocess.check_call(['make', 'run'], cwd=projroot)
            self.ssl_3scale = ssl.create_default_context(cafile=os.path.join(projroot, '3scale', 'certs', 'ca.crt'))

            # read API_URL as specified in deployment YAML
            self.api_url = subprocess.check_output(['podman', 'exec', 'webconsoleapp-front-end',
                                                    'sh', '-euc', 'echo $API_URL']).decode().strip()
            # Wait until the appservice container is up
            self.request(f'{self.api_url}{config.ROUTE_API}/ping', retries=5)
        except (subprocess.CalledProcessError, AssertionError, IOError, OSError):
            self.dumpLogs()
            raise

    def dumpLogs(self):
        color = os.isatty(1)
        ids = set(subprocess.check_output(
            ['podman', 'ps', '--all', '--quiet', '--filter', 'pod=webconsoleapp'],
            universal_newlines=True).split())
        ids.update(subprocess.check_output(
            ['podman', 'ps', '--all', '--quiet', '--filter', 'network=consoledot'],
            universal_newlines=True).split())
        for id in ids:
            if color:
                print('\033[31;1m', end='', flush=True)
            print('\n======')
            subprocess.call(['podman', 'ps', '--noheading', '--all', '--filter', f'id={id}'])
            if color:
                print('\033[0m', end='', flush=True)
            subprocess.call(['podman', 'logs', id])
        if os.getenv('TEST_SIT'):
            input('TEST FAILURE -- investigate and press Enter to clean up')

    def tearDown(self):
        if hasattr(self._outcome, 'errors'):
            # Python 3.4 - 3.10  (These two methods have no side effects)
            result = self.defaultTestResult()
            self._feedErrorsToResult(result, self._outcome.errors)
        else:
            # Python 3.11+
            result = self._outcome.result
        ok = all(test != self for test, text in result.errors + result.failures)

        if not ok:
            self.dumpLogs()

        subprocess.check_call(['make', 'clean'], cwd=projroot)

    @staticmethod
    def get_auth_request(url):
        b64 = base64.b64encode(b'admin:foobar').decode()

        request = urllib.request.Request(url)
        request.add_header('Authorization', f'Basic {b64}')
        return request

    def request(self, url, retries=0, timeout=1, data=None):
        request = self.get_auth_request(url)
        tries = 0
        last_exc = None
        while tries <= retries:
            try:
                response = urllib.request.urlopen(request, context=self.ssl_3scale, timeout=timeout, data=data)
                if response.status >= 200 and response.status < 300:
                    return response
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if 'Bad Gateway' in str(exc):
                    pass
                else:
                    raise
            except TimeoutError as exc:
                last_exc = exc

            time.sleep(1)
            tries += 1

        self.fail(f'timeout reached trying to request {url}: {last_exc}')

    def wait_status(self, sessionid, expected_status, iterations=10):
        for retry in range(iterations):
            response = self.request(f'{self.api_url}{config.ROUTE_API}/sessions/{sessionid}/status')
            self.assertEqual(response.status, 200)
            status = response.read()
            if status == expected_status:
                break
            time.sleep(0.5)
        else:
            self.fail(f'session status was not updated to {expected_status}, still at {status}')

    def newSession(self, tag='stream9'):
        response = self.request(f'{self.api_url}{config.ROUTE_API}/sessions/new', timeout=10, data=b'')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'application/json')
        sessionid = json.load(response)['id']
        self.assertIsInstance(sessionid, str)

        # initial status
        response = self.request(f'{self.api_url}{config.ROUTE_API}/sessions/{sessionid}/status')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'wait_target')

        # connecting to the session gives placeholder page
        response = self.request(f'{self.api_url}{config.ROUTE_WSS}/sessions/{sessionid}/web/')
        self.assertEqual(response.status, 200)
        content = response.read()
        self.assertIn(b'Waiting for target system to connect', content)

        # API URL is on the container host's localhost; translate for the container DNS
        websocket_url = self.api_url.replace('localhost', 'host.containers.internal').replace('https:', 'wss:')
        podman = ['podman', 'run', '-d', '--pod', 'webconsoleapp',
                  '--volume', './3scale/certs/ca.crt:/etc/pki/ca-trust/source/anchors/3scale-ca.crt:ro',
                  # in production, the bridge connector gets sent to target system via Ansible
                  '--volume', './server:/server:ro',
                  '--network', 'consoledot', f'localhost/webconsoleserver:{tag}']
        cmd = ['sh', '-exc',
               f'update-ca-trust; '
               f'/server/cockpit-bridge-websocket-connector.pyz --basic-auth admin:foobar'
               f' {websocket_url}{config.ROUTE_WSS}/sessions/{sessionid}/ws']

        subprocess.check_call(podman + cmd)

        # successful bridge connection updates status
        response = self.request(f'{self.api_url}{config.ROUTE_API}/sessions/{sessionid}/wait-running')
        self.assertEqual(response.status, 200)
        response = self.request(f'{self.api_url}{config.ROUTE_API}/sessions/{sessionid}/status')
        self.assertEqual(response.read(), b'running')

        return sessionid

    def checkSession(self, sessionid):
        # Shell
        url = f'{self.api_url}{config.ROUTE_WSS}/sessions/{sessionid}/web/'
        response = self.request(url)
        self.assertEqual(response.status, 200)
        content = response.read()
        self.assertIn(b'base1/cockpit.js', content)
        self.assertIn(b'id="topnav"', content)

        # Overview frame
        url = f'{self.api_url}{config.ROUTE_WSS}/sessions/{sessionid}/web/cockpit/@localhost/system/index.html'
        response = self.request(url)
        self.assertEqual(response.status, 200)
        content = response.read()
        self.assertIn(b'base1/cockpit.js', content)
        self.assertIn(b'Overview', content)

    def testSessions(self):
        s1 = self.newSession()
        self.checkSession(s1)

        # can create more than one session
        s2 = self.newSession()
        self.checkSession(s2)
        # first session still works
        self.checkSession(s1)

        # unknown session ID
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request(f'{self.api_url}{config.ROUTE_API}/sessions/123unknown/status')
        self.assertEqual(cm.exception.code, 404)

        # crash container for s2; use --time 0 once we have podman 4.0 everywhere
        subprocess.check_call(['podman', 'rm', '--force', f'session-{s2}'])
        # first session still works
        self.checkSession(s1)
        # second session is broken
        self.wait_status(s2, b'closed')
        # ... and goes to the "closed session" placeholder page
        response = self.request(f'{self.api_url}{config.ROUTE_WSS}/sessions/{s2}/web/')
        self.assertIn(b'Web Console session ended', response.read())

        # can create a new session
        s3 = self.newSession()
        self.checkSession(s3)
        # first session still works
        self.checkSession(s1)

        # tickle cockpit's websocket, so that it starts the session timeout
        response = self.request(f'{self.api_url}{config.ROUTE_WSS}/sessions/{s1}/web/cockpit/socket')
        self.assertEqual(response.status, 200)
        # the test does not run a browser, so nothing keeps the cockpit websocket alive
        # this acts like moving to a different URL, and bridge times out the socket due to
        # lack of pongs after ~ 15s.
        self.wait_status(s1, b'closed', iterations=40)

    def testSessionCentOS8(self):
        s = self.newSession(tag='centos8')
        self.checkSession(s)

    def test3scaleErrors(self):
        # unauthenticated
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f'{self.api_url}{config.ROUTE_API}/sessions/new',
                                   context=self.ssl_3scale)
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.reason, 'Unauthorized')

        # unknown path
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request(f'{self.api_url}/bogus/blah')
        self.assertEqual(cm.exception.code, 418)
