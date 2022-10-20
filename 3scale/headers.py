#!/usr/bin/env python3
USER_JSON = """
{
    "identity": {
        "user": {
            "username": "johndoe",
            "locale": "none",
            "is_org_admin": true,
            "is_active": true,
            "email": "johndoe@webconsole.test",
            "is_internal": false,
            "first_name": "John",
            "last_name": "Doe",
            "user_id": "7"
        },
        "account_number": "23",
        "org_id": "42",
        "auth_type": "basic-auth",
        "internal": {
            "cross_access": false,
            "auth_time": 0,
            "org_id": "42"
        },
        "type": "User"
    },
    "entitlements": {
        "insights": {
            "is_trial": false,
            "is_entitled": true
        },
        "rhel": {
            "is_trial": false,
            "is_entitled": true
        }
    }
}
"""

SYSTEM_JSON = """
{
    "identity": {
        "org_id": "42",
        "internal": {
            "org_id": "42",
            "cross_access": false,
            "auth_time": 900
        },
        "system": {
            "cn": "c1ad0ff6-e1f0-4ad9-bc6f-82e7ee383ee4",
            "cert_type": "system"
        },
        "account_number": "37",
        "auth_type": "cert-auth",
        "type": "System"
    },
    "entitlements": {
        "insights": {
            "is_trial": false,
            "is_entitled": true
        },
        "rhel": {
            "is_trial": false,
            "is_entitled": true
        }
    }
}
"""

import json
import base64

for data in SYSTEM_JSON, USER_JSON:
    # remove pretty printing
    dump = json.dumps(json.loads(data))
    encoded = base64.b64encode(dump.encode("utf-8")).decode("ascii")
    print(f'set $x_rh_identity "{encoded}";')
