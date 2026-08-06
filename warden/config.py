"""
Configuration loading, environment overrides, and validation.

Targets are a list rather than fixed sonarr/radarr keys so a new *arr is a config entry
instead of a code change — that was the thing which kept Bookmarkarr out of the previous
two daemons for as long as it was.
"""

import json
import logging
import os

log = logging.getLogger("warden")

# Each flavour differs only in its route shape and how it reports success. Anything
# Sonarr-compatible (Radarr, Lidarr, Readarr) speaks "arr".
FLAVOURS = ("arr", "bookmarkarr")

DEFAULT_CONFIG = {
    "targets": [
        {"name": "Sonarr", "flavour": "arr", "url": "http://sonarr:8989", "api_key": "", "enabled": True},
        {"name": "Radarr", "flavour": "arr", "url": "http://radarr:7878", "api_key": "", "enabled": True},
        {"name": "Bookmarkarr", "flavour": "bookmarkarr", "url": "http://bookmarkarr:5000", "api_key": "", "enabled": False},
    ],
    "download_client": {
        "type": "rdtclient",
        "url": "http://rdt-client:6500",
        "username": "",
        "password": "",
        "enabled": True,
    },
    "detectors": {
        # Download-client side: the torrent is not moving.
        "stalled": {
            "enabled": True,
            "states": ["stalleddl", "stalledup", "error", "missingfiles"],
            "stuck_min_age_minutes": 10,
            "stuck_min_streak": 2,
            "finished_threshold_minutes": 60,
        },
        # Queue side: the download finished and the *arr could not file it.
        "failed_import": {
            "enabled": True,
            "states": ["importblocked", "failed", "downloadfailed"],
        },
    },
    "cleanup": {
        "interval_minutes": 5,
        "blocklist": True,
        "dry_run": False,
    },
    "webui": {"host": "0.0.0.0", "port": 3020},
    "notifications": {
        "discord_webhook": "",
        "slack_webhook": "",
        "notify_threshold": 5,
        "notify_on_error": True,
    },
    "state": {"prune_days": 7, "max_history": 200, "max_runs": 100},
}


def _merge(base, override):
    """Recursive merge so a partial config file still gets every default."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(config):
    """
    Environment overrides, for container deployments where editing JSON is awkward.

    Targets are addressed by name, upper-cased: SONARR_URL, BOOKMARKARR_API_KEY, and so on.
    A target named in the environment but absent from the file is appended, so a container
    can be configured entirely without a config file.
    """
    for target in config["targets"]:
        prefix = target["name"].upper().replace("-", "_")
        target["url"] = os.getenv(f"{prefix}_URL", target.get("url", ""))
        target["api_key"] = os.getenv(f"{prefix}_API_KEY", target.get("api_key", ""))

    client = config["download_client"]
    client["url"] = os.getenv("DOWNLOAD_CLIENT_URL", client.get("url", ""))
    client["username"] = os.getenv("DOWNLOAD_CLIENT_USERNAME", client.get("username", ""))
    client["password"] = os.getenv("DOWNLOAD_CLIENT_PASSWORD", client.get("password", ""))

    cleanup = config["cleanup"]
    if os.getenv("CLEANUP_INTERVAL_MINUTES"):
        cleanup["interval_minutes"] = int(os.environ["CLEANUP_INTERVAL_MINUTES"])
    if os.getenv("DRY_RUN"):
        cleanup["dry_run"] = os.environ["DRY_RUN"].lower() in ("1", "true", "yes")

    webui = config["webui"]
    webui["host"] = os.getenv("WEBUI_HOST", webui["host"])
    if os.getenv("WEBUI_PORT"):
        webui["port"] = int(os.environ["WEBUI_PORT"])

    notifications = config["notifications"]
    notifications["discord_webhook"] = os.getenv("DISCORD_WEBHOOK", notifications["discord_webhook"])
    notifications["slack_webhook"] = os.getenv("SLACK_WEBHOOK", notifications["slack_webhook"])

    return config


def load_config(path="config.json"):
    """Loads config, applies defaults and environment overrides, and drops unusable targets."""
    raw = {}
    if os.path.exists(path):
        try:
            with open(path) as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError) as error:
            # Defaults alone cannot reach anything, so this is fatal rather than silent.
            raise SystemExit(f"Invalid config at {path}: {error}") from error

    config = _apply_env(_merge(DEFAULT_CONFIG, raw))

    usable = []
    for target in config["targets"]:
        if not target.get("enabled", True):
            continue
        if target.get("flavour") not in FLAVOURS:
            log.warning("Ignoring target %s: unknown flavour %r", target.get("name"), target.get("flavour"))
            continue
        if not target.get("url") or not target.get("api_key"):
            # Not an error: this is how an install without Bookmarkarr stays quiet.
            log.info("Skipping target %s: no url or api key configured", target.get("name"))
            continue
        usable.append(target)

    config["targets"] = usable

    client = config["download_client"]
    client["enabled"] = bool(client.get("enabled", True) and client.get("url"))

    if not usable:
        log.warning("No usable targets configured — nothing will be blocklisted")

    return config
