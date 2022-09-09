NETWORK = consoledot

containers:
	$(MAKE) -C 3scale container
	$(MAKE) -C appservice container

run:
	[ -z "$$(podman network ls --quiet --filter 'name=$(NETWORK)')" ] || $(MAKE) clean
	podman network create $(NETWORK)
	sed -e "s%XDG_RUNTIME_DIR%$${XDG_RUNTIME_DIR}%" webconsoledot-local.yaml | podman play kube --network $(NETWORK) -

clean:
	podman network rm --time 0 --force $(NETWORK)

all: containers

.PHONY: containers run clean all
