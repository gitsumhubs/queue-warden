"""
Restarts allowlisted containers on request from Queue Warden.

This is the only part of the system with Docker access, and that is deliberate. Queue Warden
reads torrent and queue data that other people's releases control, so a bug there must not be
able to reach Docker. Here the only action is "restart one container from a fixed set", and
the name comes from the URL path and has to match the allowlist exactly.
"""

import hmac
import logging
import os
import sys

import docker
from flask import Flask, jsonify, request

# The only containers this service will ever restart. Adding one is a code change on purpose.
ALLOWED_CONTAINERS = {"rdtclient"}

log = logging.getLogger("restart-service")


def load_token():
    token = os.getenv("RESTART_TOKEN", "")
    if not token:
        raise SystemExit("RESTART_TOKEN is not set; refusing to start without an access token")
    return token


def create_app(token, docker_client):
    app = Flask(__name__)

    @app.post("/restart/<name>")
    def restart(name):
        provided = request.headers.get("X-Restart-Token", "")
        if not hmac.compare_digest(provided.encode(), token.encode()):
            log.warning("restart %s rejected: missing or wrong token", name)
            return jsonify(success=False, message="missing or wrong token"), 401

        if name not in ALLOWED_CONTAINERS:
            log.warning("restart %s rejected: not in allowlist", name)
            return jsonify(success=False, message=f"{name} is not an allowed container"), 404

        try:
            docker_client.containers.get(name).restart(timeout=20)
        except Exception as error:  # noqa: BLE001
            log.error("restart %s failed: %s", name, error)
            return jsonify(success=False, message=str(error)), 502

        log.info("restart %s succeeded", name)
        return jsonify(success=True, message=f"restarted {name}")

    return app


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    app = create_app(load_token(), docker.from_env())
    # Internal only: the compose file publishes no host port for this service.
    app.run(host="0.0.0.0", port=8080)
