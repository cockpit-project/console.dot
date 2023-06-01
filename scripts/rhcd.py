#!/usr/bin/python

import os
import os.path
import subprocess
import sys
import tempfile
import time

import requests
from requests.auth import HTTPBasicAuth


INVENTORY_ID = "68422d7e-4cb8-4567-82b5-2d15dfc9ed78"
STATUS_URL = f"https://_gateway:8443/api/webconsole/v1/sessions/inventory/{INVENTORY_ID}"
PLAYBOOK_URL = "https://_gateway:8443/api/webconsole/v1/sessions/{SESSION_ID}/playbook"
AUTH = HTTPBasicAuth("admin", "foobar")
# CERT = "/tmp/ca.crt"
PIDFILE = "/tmp/bridge-connector.pid"


while True:
    if os.path.isfile(PIDFILE):
        print("blocked on pidfile")
        time.sleep(5)
        continue

    r = requests.get(STATUS_URL, auth=AUTH, verify=False)
    if r.status_code != 200:
        print("waiting on status")
        time.sleep(1)
        continue

    session_id = r.text
    print(f"Resolved session id {session_id}")
    # Fetch playbook
    with tempfile.NamedTemporaryFile(suffix=".yml") as fp:
        r = requests.get(PLAYBOOK_URL.format(SESSION_ID=session_id), auth=AUTH, verify=False)
        # Should never happen
        if r.status_code != 200:
            print(r)
            sys.exit(1)

        fp.write(r.text.encode())
        fp.flush()  # ??? BUG?!
        print(fp.name)
        subprocess.check_call(["ansible-playbook", fp.name])
