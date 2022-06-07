FROM docker.io/library/nginx

COPY certs /etc/nginx/certs
COPY htpasswd /etc/nginx/.htpasswd
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 443
