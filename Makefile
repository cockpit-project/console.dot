NETWORK = consoledot
CONTAINER_NAME = webconsoleapp

build: 3scale/certs/service-chain.pem containers

3scale/certs/service-chain.pem:
	mkdir -p 3scale/certs && cd 3scale/certs && sscg
	cat 3scale/certs/service.pem 3scale/certs/ca.crt > $@

containers:
	podman build -t $(CONTAINER_NAME) appservice

run: 3scale/certs/service.pem
	[ -z "$$(podman network ls --quiet --filter 'name=$(NETWORK)')" ] || $(MAKE) clean
	podman network create $(NETWORK)
	[ $$(id -u) -eq 0 ] && systemctl start podman.socket || systemctl --user start podman.socket
	[ $$(id -u) -ne 0 ] || XDG_RUNTIME_DIR=/run; \
	sed -e "s%XDG_RUNTIME_DIR%$${XDG_RUNTIME_DIR}%" webconsoledot-local.yaml | podman play kube --network $(NETWORK) -

clean:
	podman network rm --time 0 --force $(NETWORK)

check:
	python3 -m unittest discover -vs test

all: containers

.PHONY: containers run clean all
