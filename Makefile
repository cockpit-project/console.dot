PORT ?= 8443
NETWORK=consoledot

.PHONY: containers
containers:
	make -C 3scale container
	make -C appservice container

run:
	podman network create $(NETWORK)
	sed -e "s%XDG_RUNTIME_DIR%$${XDG_RUNTIME_DIR}%" webconsoledot-local.yaml | podman play kube --network $(NETWORK) -

clean:
	podman network rm --time 0 --force $(NETWORK)

all: containers
