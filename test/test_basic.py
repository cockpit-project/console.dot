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

projroot = os.path.dirname(os.path.dirname(__file__))


class IntegrationTest(unittest.TestCase):

    def setUp(self):
        subprocess.check_call(["make", "containers"], cwd=projroot)
        subprocess.check_call(["make", "run"], cwd=projroot)
        # Wait until the appservice container is up
        self.context = ssl.create_default_context(cafile=os.path.join(projroot, '3scale', 'certs', 'ca.crt'))
        self.context.check_hostname = False
        self.request('https://localhost:8443/api/webconsole/v1/ping', retries=20)

    def tearDown(self):
        subprocess.check_call(["make", "clean"], cwd=projroot)

    def request(self, url, retries=0):
        b64 = base64.b64encode(b'admin:foobar').decode()

        request = urllib.request.Request(url)
        request.add_header('Authorization', f'Basic {b64}')
        tries = 0

        while tries <= retries:
            try:
                response = urllib.request.urlopen(request, context=self.context, timeout=1)
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

        assert "timeout reached trying to request an URL"

    def testBasic(self):
        response = self.request('https://localhost:8443/api/webconsole/v1/sessions/new')
        self.assertEqual(response.status, 200)
        sessionid = json.load(response)['id']
        self.assertIsInstance(sessionid, str)
