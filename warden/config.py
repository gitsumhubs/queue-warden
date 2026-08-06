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


def _env(name, fallback):
    """
    Environment override that ignores blanks.

    Compose files commonly pass every variable through with an empty default
    (`SONARR_URL=${SONARR_URL:-}`), which makes the variable *present but empty*. Treating
    that as an override would silently wipe whatever is in the config file, so only a
    non-empty value counts.
    """
    value = os.getenv(name)
    return value if value not in (None, "") else fallback


def _apply_env(config):
    """
    Environment overrides, for container deployments where editing JSON is awkward.

    Targets are addressed by name, upper-cased: SONARR_URL, BOOKMARKARR_API_KEY, and so on.
    A target named in the environment but absent from the file is appended, so a container
    can be configured entirely without a config file.
    """
    for target in config["targets"]:
        prefix = target["name"].upper().replace("-", "_")
        target["url"] = _env(f"{prefix}_URL", target.get("url", ""))
        target["api_key"] = _env(f"{prefix}_API_KEY", target.get("api_key", ""))

    client = config["download_client"]
    client["url"] = _env("DOWNLOAD_CLIENT_URL", client.get("url", ""))
    client["username"] = _env("DOWNLOAD_CLIENT_USERNAME", client.get("username", ""))
    client["password"] = _env("DOWNLOAD_CLIENT_PASSWORD", client.get("password", ""))

    cleanup = config["cleanup"]
    if os.getenv("CLEANUP_INTERVAL_MINUTES"):
        cleanup["interval_minutes"] = int(os.environ["CLEANUP_INTERVAL_MINUTES"])
    if os.getenv("DRY_RUN"):
        cleanup["dry_run"] = os.environ["DRY_RUN"].lower() in ("1", "true", "yes")

    webui = config["webui"]
    webui["host"] = _env("WEBUI_HOST", webui["host"])
    if os.getenv("WEBUI_PORT"):
        webui["port"] = int(os.environ["WEBUI_PORT"])

    notifications = config["notifications"]
    notifications["discord_webhook"] = _env("DISCORD_WEBHOOK", notifications["discord_webhook"])
    notifications["slack_webhook"] = _env("SLACK_WEBHOOK", notifications["slack_webhook"])

    return config


def normalise(raw):
    """
    Applies defaults and validation to a config *without* touching the environment.

    Used when saving from the settings UI: environment overrides are deployment-level and
    must not be baked into the file, or a container would permanently inherit whatever the
    env said at the moment someone pressed Save.
    """
    config = _merge(DEFAULT_CONFIG, raw)

    errors = []
    for index, target in enumerate(config.get("targets", [])):
        if not target.get("name"):
            errors.append(f"Target {index + 1} has no name")
        if target.get("flavour") not in FLAVOURS:
            errors.append(f"Target {target.get('name') or index + 1} has an unknown type")

    interval = config.get("cleanup", {}).get("interval_minutes")
    if not isinstance(interval, int) or interval < 1:
        errors.append("Cleanup interval must be a whole number of minutes, at least 1")

    port = config.get("webui", {}).get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append("Web UI port must be between 1 and 65535")

    stalled = config.get("detectors", {}).get("stalled", {})
    if stalled.get("stuck_min_streak", 1) < 1:
        errors.append("Stuck streak must be at least 1 — a single reading is normal for a healthy torrent")

    return config, errors


def save_config(config, path="config.json"):
    """Writes via a temp file so an interrupted save cannot truncate a working config."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    temp = f"{path}.tmp"
    with open(temp, "w") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def usable_targets(config):
    """The subset of targets that can actually be contacted, in config order."""
    usable = []
    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        if target.get("flavour") not in FLAVOURS:
            continue
        if not target.get("url") or not target.get("api_key"):
            continue
        usable.append(target)
    return usable


def load_config(path="config.json"):
    """Loads config, applies defaults and environment overrides, and drops unusable targets."""
    raw = {}
    if not os.path.exists(path):
        # First run: write the defaults out so `docker compose up` needs no prior setup and
        # the Settings page has a real file to save back to.
        try:
            save_config(DEFAULT_CONFIG, path)
            log.info("Wrote starter config to %s — configure targets at /settings", path)
        except OSError as error:
            log.warning("Could not create %s: %s", path, error)

    if os.path.exists(path):
        try:
            with open(path) as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError) as error:
            # Defaults alone cannot reach anything, so this is fatal rather than silent.
            raise SystemExit(f"Invalid config at {path}: {error}") from error

    config = _apply_env(_merge(DEFAULT_CONFIG, raw))

    for target in config["targets"]:
        if target.get("enabled", True) and target.get("flavour") not in FLAVOURS:
            log.warning("Ignoring target %s: unknown type %r", target.get("name"), target.get("flavour"))

    usable = usable_targets(config)
    # Kept on the config so the settings UI can still show and edit a target that is
    # currently unusable, rather than silently losing it on the next save.
    config["all_targets"] = config["targets"]
    config["targets"] = usable

    client = config["download_client"]
    client["enabled"] = bool(client.get("enabled", True) and client.get("url"))

    if not usable:
        log.warning("No usable targets configured — nothing will be blocklisted")

    return config
