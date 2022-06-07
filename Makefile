PORT ?= 8443
CONTAINER_NAME=console-nginx
USER = admin
PASSWORD ?= foobar

certs/service-chain.pem: certs/service.pem
	cat certs/service.pem certs/ca.crt > certs/service-chain.pem

htpasswd:
	htpasswd -bc htpasswd $(USER) $(PASSWORD)

certs/service.pem:
	mkdir -p certs && cd certs && sscg

container: certs/service-chain.pem htpasswd
	podman build -t $(CONTAINER_NAME) .

run:
	podman run --rm -p $(PORT):443 $(CONTAINER_NAME)

all: certs container
