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
            subprocess.check_call(["make", "run"], cwd=projroot)
            # Wait until the appservice container is up
            self.ssl_3scale = ssl.create_default_context(cafile=os.path.join(projroot, '3scale', 'certs', 'ca.crt'))
            self.request(f'https://localhost:8443{config.ROUTE_CONTROL}/ping', retries=5)
        except (subprocess.CalledProcessError, AssertionError, IOError, OSError):
            self.dumpLogs()
            raise

    def dumpLogs(self):
        ids = set(subprocess.check_output(
            ['podman', 'ps', '--all', '--quiet', '--filter', 'pod=webconsoleapp'],
            universal_newlines=True).split())
        ids.update(subprocess.check_output(
            ['podman', 'ps', '--all', '--quiet', '--filter', 'network=consoledot'],
            universal_newlines=True).split())
        for id in ids:
            print('======')
            subprocess.call(['podman', 'ps', '--noheading', '--all', '--filter', f'id={id}'])
            subprocess.call(['podman', 'logs', id])
        if os.getenv('TEST_SIT'):
            input("TEST FAILURE --investigate and press Enter to clean up")

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

        subprocess.check_call(["make", "clean"], cwd=projroot)

    def request(self, url, retries=0):
        b64 = base64.b64encode(b'admin:foobar').decode()

        request = urllib.request.Request(url)
        request.add_header('Authorization', f'Basic {b64}')
        tries = 0

        while tries <= retries:
            try:
                response = urllib.request.urlopen(request, context=self.ssl_3scale, timeout=1)
                if response.status >= 200 and response.status < 300:
                    return response
            except urllib.error.HTTPError as exc:
                if 'Bad Gateway' in str(exc):
                    pass
                else:
                    raise
            except TimeoutError:
                pass

            time.sleep(1)
            tries += 1

        self.fail(f"timeout reached trying to request {url}")

    def testBasic(self):
        response = self.request(f'https://localhost:8443{config.ROUTE_CONTROL}/sessions/new')
        self.assertEqual(response.status, 200)
        sessionid = json.load(response)['id']
        self.assertIsInstance(sessionid, str)

        podman = ['podman', 'run', '-d', '--pod', 'webconsoleapp',
                  '--network', 'consoledot', 'localhost/webconsoleserver']
        cmd = ['websocat', '--basic-auth', 'admin:foobar', '-b', '-k',
               f'wss://host.containers.internal:8443{config.ROUTE_HOST}/sessions/{sessionid}',
               'cmd:cockpit-bridge']
        subprocess.check_call(podman + cmd)

        # Shell
        url = f'https://localhost:8443{config.ROUTE_BROWSER}/sessions/{sessionid}/'
        response = self.request(url)
        self.assertEqual(response.status, 200)
        content = response.read()
        self.assertIn(b'base1/cockpit.js', content)
        self.assertIn(b'id="topnav"', content)

        # Overview frame
        url = f'https://localhost:8443{config.ROUTE_BROWSER}/sessions/{sessionid}/cockpit/@localhost/system/index.html'
        response = self.request(url)
        self.assertEqual(response.status, 200)
        content = response.read()
        self.assertIn(b'base1/cockpit.js', content)
        self.assertIn(b'Overview', content)

    def test3scaleErrors(self):
        # unauthenticated
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f'https://localhost:8443/{config.ROUTE_CONTROL}/sessions/new',
                                   context=self.ssl_3scale)
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.reason, 'Unauthorized')

        # unknown path
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request('https://localhost:8443/bogus/blah')
        self.assertEqual(cm.exception.code, 418)
