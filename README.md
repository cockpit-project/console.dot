# Console.dot

Local [podman](https://podman.io/) test environment for deploying
[cockpit-ws](https://quay.io/repository/cockpit/ws) similiarly to what happens
on [3scale](https://www.3scale.net/).  Each "session" consists of a
`cockpit/ws` container running in the cloud, and some target machine that
connects [cockpit-bridge](https://cockpit-project.org/guide/latest/cockpit-bridge.1) via a websocket to the ws container.

With that, the target machine does not need to have any open port. The browser
only connects to 3scale, which proxies to the session pod, which then
communicates to the target machine to get a Cockpit session for that machine.

The "3scale" container runs nginx with a self signed certificate. The default
basic auth username and password is `admin:foobar`.

This requires `podman` and `sscg` to be available on the host.

## Usage

 - Build the credentials, custom container, and connector zipapps:
   ```
   make
   ```

 - Run the 3scale and app service containers:
   ```
   make run
   ```

 - Prepare some target machine on which you want to get a Cockpit session; this can just be a local VM.
   It needs to have Cockpit ≥ 275 installed, at least the `cockpit-system` and `cockpit-bridge` packages.
   You also need to copy `server/cockpit-bridge-websocket-connector.pyz` to the target machine (in the
   final product this will be transmitted through Ansible):
   ```
   scp server/cockpit-bridge-websocket-connector.pyz target_machine:/tmp/
   ```

 - Register a new session:
   ```
   curl -X POST -u admin:foobar --cacert 3scale/certs/ca.crt https://localhost:8443/api/webconsole/v1/sessions/new
   ```

   This returns the session ID in a JSON object:
   ```json
   {"id": "f835d542-b9ac-4329-a16a-b935036b4aa5"}
   ```

 - Open the session in a browser:

   https://localhost:8443/wss/webconsole/v1/sessions/SESSION_ID/web/

   This is a stub page that waits until the target machine connects.

 - Connect the target machine to the ws session container. In a VM with a
   recent systemd nss-myhostname (like Fedora), you can use the `_gateway`
   name, otherwise check `ip route`. If you use a remote machine, connect to
   the host name/IP which runs the console.dot containers. You may have to
   open the port on your local firewall, e.g. `firewall-cmd --add-port=8443/tcp`.
   Replace `SESSION_ID` with the UUID that the `/new` call returned.
   Run this command as the user for which you want to get a Cockpit session:
   ```
   /tmp/cockpit-bridge-websocket-connector.pyz --basic-auth admin:foobar -k wss://_gateway:8443/wss/webconsole/v1/sessions/SESSION_ID/ws
   ```

   This should cause the stub page to automatically reload, and show the actual Cockpit UI.

 - Clean up:
   ```
   make clean
   ```

 - Run the integration tests:
   ```
   make check
   ```

## Running on Kubernetes

The app service can also be deployed on Kubernetes, in particular the
[cockpit-dev project on the staging instance](https://console-openshift-console.apps.c-rh-c-eph.8p0c.p1.openshiftapps.com/k8s/ns/cockpit-dev/pods). This requires you to be inside the Red Hat VPN. Make sure you select the right project with

    oc project cockpit-dev

There is the [app service image BuildConfig](./webconsoleapp-k8s-buildconfig.yaml) and the [pods and services](./webconsoleapp-k8s.yaml).
Both get deployed with

    make k8s-deploy


1. Validate that you can reach the deployment with:

    curl -u user:password https://test.cloud.redhat.com/api/webconsole/v1/ping

   You should get a "pong" response.

2. Prepare a VM to act as the target machine like above for local podman deployment.

3. The hardest part: Due to a [misconfiguration of 3scale](https://issues.redhat.com/browse/COCKPIT-795?focusedCommentId=20703283&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel#comment-20703283), browsers don't ask for basic auth. So you need to set up a local proxy that provides the `Authorization:` header for 3scale:

   - Download Linux binaries from https://mitmproxy.org/, unpack them
   - Run the proxy for adding a header to send basic auth:

         ./mitmdump -k -H "/authorization/Basic bW1hcnVzYWstZXQ6MTIzNDU2Nzg5"

   - Run `firefox -P`, create a new "mitm-consoledot" profile
   - Configure the network proxy to be `localhost:8080`, also use it for https.
   - Go to "View certificates", "Authorities", "Import", and import ~/.mitmproxy/mitmproxy-ca-cert.pem , so that the local proxy's https certificate is trusted

4. Request a new session from the API:

       curl -X POST -u user:password https://test.cloud.redhat.com/api/webconsole/v1/sessions/new

   This will respond with a Session ID, like this:

       {"id": "8fee318f-aeeb-413e-ab2f-eeb505f2ec0b"}

   Check the session status:

       curl -u user:password https://test.cloud.redhat.com/api/webconsole/v1/sessions/SESSIONID/status

   It should be "wait_target".

5. In the mitmproxy Firefox profile, open https://test.cloud.redhat.com/wss/webconsole/v1/sessions/SESSIONID/web/ to get the "waiting for target machine" stub page.

6. Connect the target VM to the session pod:

      /tmp/cockpit-bridge-websocket-connector.pyz --basic-auth user:password -k wss://test.cloud.redhat.com/wss/webconsole/v1/sessions/SESSIONID/ws

   You should now also get a Cockpit UI for the user you started the bridge as. If you check the session status again, it should be "running".

You can run

    make k8s-clean

to remove all resources from Kubernetes again.
