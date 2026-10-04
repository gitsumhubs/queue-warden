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
            # Import-pending is not in the list above because every healthy download passes
            # through it. It only counts when the *arr has put a warning on the item and the
            # warning has stood for this long. 0 turns the rule off.
            "pending_warning_minutes": 120,
            # More than this many waiting at once points at storage or an import path, not
            # at the releases, so nothing is removed and the run reports an error instead.
            "pending_warning_limit": 10,
        },
    },
    "cleanup": {
        "interval_minutes": 5,
        "blocklist": True,
        # Safe by default. A first run happens before anyone has reviewed what this daemon
        # considers removable, and the alternative is a service that starts deleting and
        # blocklisting the moment credentials are filled in. The dashboard badges dry-run
        # prominently, and an existing config file keeps whatever it already says.
        "dry_run": True,
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
        url = _env(f"{prefix}_URL", target.get("url", ""))
        api_key = _env(f"{prefix}_API_KEY", target.get("api_key", ""))

        # Configuring a target through the environment is a request to use it. Without this,
        # a target shipped disabled by default (Bookmarkarr) can never be switched on by env
        # alone, and an env-only deployment silently drops it.
        if (os.getenv(f"{prefix}_URL") or os.getenv(f"{prefix}_API_KEY")) and url and api_key:
            target["enabled"] = True

        target["url"] = url
        target["api_key"] = api_key

    client = config["download_client"]
    client["url"] = _env("DOWNLOAD_CLIENT_URL", client.get("url", ""))
    client["username"] = _env("DOWNLOAD_CLIENT_USERNAME", client.get("username", ""))
    client["password"] = _env("DOWNLOAD_CLIENT_PASSWORD", client.get("password", ""))

    cleanup = config["cleanup"]
    if os.getenv("CLEANUP_INTERVAL_MINUTES"):
        cleanup["interval_minutes"] = int(os.environ["CLEANUP_INTERVAL_MINUTES"])
    # Both spellings are accepted. The compose file maps QUEUE_WARDEN_DRY_RUN (a .env-level
    # name, prefixed to avoid colliding with other services) down to DRY_RUN, but anyone
    # writing their own compose sets the prefixed name directly in `environment:` — and
    # silently ignoring it hands them a live daemon when they asked for a dry run.
    dry_run = _env("DRY_RUN", None) or _env("QUEUE_WARDEN_DRY_RUN", None)
    if dry_run is not None:
        cleanup["dry_run"] = str(dry_run).lower() in ("1", "true", "yes")

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

    failed_import = config.get("detectors", {}).get("failed_import", {})
    wait = failed_import.get("pending_warning_minutes")
    if not isinstance(wait, int) or wait < 0:
        errors.append("Import-pending wait must be a whole number of minutes, or 0 to turn it off")
    limit = failed_import.get("pending_warning_limit")
    if not isinstance(limit, int) or limit < 1:
        errors.append("Import-pending limit must be a whole number, at least 1")

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
    is_first_run = not os.path.exists(path)

    if os.path.exists(path):
        try:
            with open(path) as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError) as error:
            # Defaults alone cannot reach anything, so this is fatal rather than silent.
            raise SystemExit(f"Invalid config at {path}: {error}") from error

    config = _apply_env(_merge(DEFAULT_CONFIG, raw))

    if is_first_run:
        # Written *after* environment overrides so the file on disk reflects what is actually
        # running. Persisting bare defaults first made a configured daemon look unconfigured,
        # which is a confusing thing to debug.
        try:
            save_config(config, path)
            log.info("Wrote starter config to %s — review targets at /settings", path)
        except OSError as error:
            log.warning("Could not create %s: %s", path, error)

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
