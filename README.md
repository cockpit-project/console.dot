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

 - Build the credentials and custom container:
   ```
   make
   ```

 - Run the 3scale and app service containers:
   ```
   make run
   ```

 - Register a new session:
   ```
   curl -u admin:foobar -k https://localhost:8443/api/webconsole/v1/sessions/new
   ```

   This returns the session ID in a JSON object:
   ```json
   {"id": "f835d542-b9ac-4329-a16a-b935036b4aa5"}
   ```

 - Pick some target machine/VM on which you want to get a Cockpit session; this can just be a local VM.
   It needs to have Cockpit ≥ 275 installed, at least the `cockpit-system` and `cockpit-bridge` packages.
   You also need to install [websocat](https://github.com/vi/websocat) for the time being:
   ```
   curl -L -o /tmp/websocat https://github.com/vi/websocat/releases/download/v1.10.0/websocat.x86_64-unknown-linux-musl
   chmod a+x /tmp/websocat
   ```

 - Connect the target machine to the ws session container. In a VM with a
   recent systemd nss-myhostname (like Fedora), you can use the `_gateway`
   name, otherwise check `ip route`. If you use a remote machine, connect to
   the host name/IP which runs the console.dot containers.  Replace
   `SESSION_ID` with the UUID that the `/new` call returned.
   Run this command as the user for which you want to get a Cockpit session:
   ```
   /tmp/websocat --basic-auth admin:foobar -b -k wss://_gateway:8443/wss/webconsole-ws/v1/sessions/SESSION_ID cmd:cockpit-bridge
   ```

 - Open Cockpit in a browser:

   https://localhost:8443/wss/webconsole-http/v1/sessions/SESSION_ID/

 - Clean up:
   ```
   make clean
   ```
