USER = admin
PASSWORD ?= foobar
CONTAINER_NAME=3scale

certs/service-chain.pem: certs/service.pem
	cat certs/service.pem certs/ca.crt > certs/service-chain.pem

htpasswd:
	htpasswd -bc htpasswd $(USER) $(PASSWORD)

certs/service.pem:
	mkdir -p certs && cd certs && sscg

container: certs/service-chain.pem htpasswd
	podman build -t $(CONTAINER_NAME) .

