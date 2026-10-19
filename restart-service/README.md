# Restart service

A small internal service that restarts allowlisted Docker containers on request from Queue Warden.
It exists so that only this container can talk to the Docker daemon.

## Why this is separate

Queue Warden reads torrent and queue data that other people's releases control. Giving that daemon
the Docker socket would mean one bug there could control every container on the host. Here the
socket is mounted into this service and nothing else. The only action it can take is restarting one
of a fixed set of containers.

## The allowlist

`ALLOWED_CONTAINERS` in `app.py` is hardcoded to `{"rdtclient"}`. The container name comes from the
URL path and has to match exactly, so `RDTCLIENT`, `rdtclient-2`, and path tricks are all rejected
with 404 before Docker is called. Adding a container is a code change, on purpose.

## The token

Every request needs the header `X-Restart-Token` set to the value of `RESTART_TOKEN`. Missing or wrong
tokens get 401. The comparison uses `hmac.compare_digest`. If `RESTART_TOKEN` is not set, the service
refuses to start.

Set the same value for both services in `.env` (the compose file passes it through):

```bash
RESTART_SERVICE_TOKEN=$(openssl rand -hex 32)
```

## Endpoint

```http
POST /restart/rdtclient
X-Restart-Token: <token>
```

Responses are JSON with `success` and `message`. Every request is logged with the container name and
the result.

## Network

No host port is published. Queue Warden reaches it at `http://restart-service:8080` on the compose
network.

## Running

```bash
docker compose up -d restart-service
```

The service restarts itself after a crash or reboot (`restart: unless-stopped`).
