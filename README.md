# Console.dot

Test env. for reverse proxying cockpit-ws similiar to console's env. by using a
podman container which runs nginx with a self signed certificate. The default
basic auth username and password is `admin:foobar`.

Creating the container, requires `podman` and `sscg` to be available on the host.

```
make containers
```

To run the cloud:

```
make run
```

Cleaning up:

```
make clean
```
