PORT ?= 8443
NETWORK=consoledot

.PHONY: containers
containers:
	$(MAKE) -C 3scale container
	$(MAKE) -C appservice container

run:
	[ -z "$$(podman network ls --quiet --filter 'name=consoledot')" ] || $(MAKE) clean
	podman network create $(NETWORK)
	[ $$(id -u) -ne 0 ] || XDG_RUNTIME_DIR=/run; \
	sed -e "s%XDG_RUNTIME_DIR%$${XDG_RUNTIME_DIR}%" webconsoledot-local.yaml | podman play kube --network $(NETWORK) -

clean:
	podman network rm --time 0 --force $(NETWORK)

all: containers
