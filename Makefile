NETWORK = consoledot
CONTAINER_NAME = webconsoleapp
SERVER_CONTAINER_NAME = webconsoleserver
PORT_3SCALE = 8443
PATTERNFLY_VERSION = 4.217.1

build: 3scale/certs/service-chain.pem server/cockpit-bridge-websocket-connector.pyz containers

3scale/certs/service-chain.pem:
	mkdir -p 3scale/certs
	cd 3scale/certs && sscg --subject-alt-name localhost --subject-alt-name host.containers.internal \
		--client-file=client.crt --client-key-file=client.key
	cat 3scale/certs/service.pem 3scale/certs/ca.crt > $@

# bundle https://pypi.org/project/websockets; it's packaged everywhere, but we
# don't want to install anything on target machines
# chmod is a hack around https://github.com/python/cpython/issues/96867
server/cockpit-bridge-websocket-connector.pyz: server/cockpit-bridge-websocket-connector
	rm -rf tmp/pyz
	mkdir -p tmp/pyz
	cp $< tmp/pyz/cockpit_bridge_websocket_connector.py
	python3 -m pip install --python-version 3.6 --no-deps --no-compile --target tmp/pyz/ websockets
	mv tmp/pyz/websockets tmp/pyz/websockets36
	sed -i '/\bimport\b/ s/\bwebsockets\b/websockets36/' tmp/pyz/websockets36/*.py
	python3 -m pip install --no-deps --no-compile --target tmp/pyz/ websockets
	find tmp/pyz/ -name '*.c' -or -name '*.so' -delete
	python3 -m zipapp --python="/usr/bin/env python3" --compress --output $@ --main cockpit_bridge_websocket_connector:main tmp/pyz
	chmod a+x $@

containers:
	podman build -t $(CONTAINER_NAME) appservice
	podman build -t $(SERVER_CONTAINER_NAME):stream9 --build-arg tag=stream9 server
	podman build -t $(SERVER_CONTAINER_NAME):centos8 --build-arg tag=stream8 server

run: 3scale/certs/service-chain.pem
	[ -z "$$(podman network ls --quiet --filter 'name=$(NETWORK)')" ] || $(MAKE) clean
	podman network create $(NETWORK)
	[ $$(id -u) -eq 0 ] && systemctl start podman.socket || systemctl --user start podman.socket
	[ $$(id -u) -ne 0 ] || XDG_RUNTIME_DIR=/run; \
	sed -e "s%{XDG_RUNTIME_DIR}%$${XDG_RUNTIME_DIR}%" \
	    -e "s%{PORT_3SCALE}%$(PORT_3SCALE)%" \
	    webconsoleapp-local.yaml | podman play kube --network $(NETWORK) -

# --time only supported in podman >= 4
clean:
	if podman network rm --help | grep -q -- --time; then \
	    podman network rm --time 0 --force $(NETWORK); \
	else \
	    podman network rm --force $(NETWORK); \
	fi

check: server/cockpit-bridge-websocket-connector.pyz
	python3 -m unittest discover -vs test

k8s-clean:
	oc delete -f webconsoleapp-k8s.yaml --ignore-not-found=true
	oc get pods --selector app=webconsoleapp-session -o name | xargs -rn1 oc delete --force=true
	oc delete -f webconsoleapp-k8s-buildconfig.yaml --ignore-not-found=true

k8s-deploy: k8s-clean
	oc create -f webconsoleapp-k8s-buildconfig.yaml
	oc start-build build-webconsoleapp --follow
	oc create -f webconsoleapp-k8s.yaml

appservice/patternfly.css:
	rm -rf tmp/patternfly
	mkdir -p tmp/patternfly
	cd tmp/patternfly && npm install @patternfly/patternfly@$(PATTERNFLY_VERSION)
	cd tmp/patternfly/ && cat \
		node_modules/@patternfly/patternfly/patternfly-base.css \
		node_modules/@patternfly/patternfly/components/EmptyState/empty-state.css \
		node_modules/@patternfly/patternfly/components/Spinner/spinner.css \
		node_modules/@patternfly/patternfly/components/Page/page.css \
		node_modules/@patternfly/patternfly/components/Masthead/masthead.css \
		node_modules/@patternfly/patternfly/components/Content/content.css \
		> patternfly.css
	gzip tmp/patternfly/patternfly.css > appservice/patternfly.css

.PHONY: containers run clean build k8s-clean k8s-deploy
