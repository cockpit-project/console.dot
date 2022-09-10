NETWORK = consoledot
CONTAINER_NAME = webconsoleapp
SERVER_CONTAINER_NAME = webconsoleserver

build: 3scale/certs/service-chain.pem containers

3scale/certs/service-chain.pem:
	mkdir -p 3scale/certs && cd 3scale/certs && sscg --subject-alt-name localhost
	cat 3scale/certs/service.pem 3scale/certs/ca.crt > $@

containers:
	podman build -t $(CONTAINER_NAME) appservice
	podman build -t $(SERVER_CONTAINER_NAME) server
	podman pull quay.io/rhn_engineering_mpitt/ws

run: 3scale/certs/service-chain.pem
	[ -z "$$(podman network ls --quiet --filter 'name=$(NETWORK)')" ] || $(MAKE) clean
	podman network create $(NETWORK)
	[ $$(id -u) -eq 0 ] && systemctl start podman.socket || systemctl --user start podman.socket
	[ $$(id -u) -ne 0 ] || XDG_RUNTIME_DIR=/run; \
	sed -e "s%XDG_RUNTIME_DIR%$${XDG_RUNTIME_DIR}%" webconsoledot-local.yaml | podman play kube --network $(NETWORK) -

# --time only supported in podman >= 4
clean:
	if podman network rm --help | grep -q -- --time; then \
	    podman network rm --time 0 --force $(NETWORK); \
	else \
	    podman network rm --force $(NETWORK); \
	fi

check:
	python3 -m unittest discover -vs test

all: containers

.PHONY: containers run clean all
