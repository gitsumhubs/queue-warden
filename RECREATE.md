# Queue Warden

## Overview

Queue Warden is a cleanup daemon for a Sonarr/Radarr/Bookmarkarr stack. It removes downloads that
have gone wrong and blocklists the release at the *arr that owns it, so the next search picks
something different instead of the same failing item.

It watches two independent failure modes from the only place each is visible. A stalled transfer
can only be seen at the download client, because the *arr still reports it as downloading. A
failed import can only be seen in the *arr queue, because the torrent itself looks healthy.
Watching one end alone leaves the other class of failure stuck in the queue indefinitely, which
is why both detectors exist rather than one.

Targets are configuration, not code. Adding an *arr is a config entry; the only thing that varies
between them is route shape, which is isolated in `warden/clients.py`. Hardcoding a fixed set of
apps is what makes a new one require a code change instead of a config entry.

## Tech Stack

- Language/Framework: Python 3.12, Flask
- Database: none — state is a JSON file written atomically via a temp file
- Key Dependencies: flask, requests, pytest

## Prerequisites

- Python 3.12+, or Docker with Compose
- At least one *arr reachable with an API key
- Optionally a qBittorrent-compatible download client (rdt-client)

## Environment Variables

All are optional; each overrides the matching `config.json` value. Target variables are derived
from the target's `name`, upper-cased with hyphens as underscores.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| CONFIG_PATH | No | config.json | Path to the config file |
| STATE_PATH | No | state/state.json | Path to persisted state |
| LOG_LEVEL | No | INFO | Python log level |
| SONARR_URL | No | - | Overrides the Sonarr target url |
| SONARR_API_KEY | No | - | Overrides the Sonarr target api key |
| RADARR_URL | No | - | Overrides the Radarr target url |
| RADARR_API_KEY | No | - | Overrides the Radarr target api key |
| BOOKMARKARR_URL | No | - | Overrides the Bookmarkarr target url |
| BOOKMARKARR_API_KEY | No | - | Overrides the Bookmarkarr target api key |
| DOWNLOAD_CLIENT_URL | No | - | Download client base url |
| DOWNLOAD_CLIENT_USERNAME | No | - | Download client username |
| DOWNLOAD_CLIENT_PASSWORD | No | - | Download client password |
| CLEANUP_INTERVAL_MINUTES | No | 5 | Minutes between passes |
| DRY_RUN | No | true | Report only, remove nothing. `QUEUE_WARDEN_DRY_RUN` is accepted too |
| WEBUI_HOST | No | 0.0.0.0 | Web UI bind address |
| WEBUI_PORT | No | 3020 | Web UI port |
| DISCORD_WEBHOOK | No | - | Discord notification webhook |
| SLACK_WEBHOOK | No | - | Slack notification webhook |

## Port Configuration

- Port: 3020
- Registered in portctl as: queue-warden

## Setup Instructions

```bash
git clone https://github.com/gitsumhubs/queue-warden.git
cd queue-warden
pip install -r requirements.txt
python main.py
```

A starter `config.json` is written on first run; configure targets at `/settings` or copy
`config.json.example` and edit it directly.

### Docker (published image)

```bash
curl -O https://raw.githubusercontent.com/gitsumhubs/queue-warden/main/docker-compose.yml
docker compose up -d
```

The compose file pulls `ghcr.io/gitsumhubs/queue-warden:latest` and needs no source checkout
and no pre-written config: a starter `config.json` is created on first run and everything else
is set on the Settings page. `QUEUE_WARDEN_IMAGE` overrides the image for local testing.

### Docker (from source)

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Publishing

Pushing a `vX.Y.Z` tag triggers `.github/workflows/publish-ghcr.yml`, which builds
linux/amd64 and linux/arm64 and pushes to GHCR. `.github/workflows/test.yml` runs pytest on
every push and pull request.

### systemd

```ini
[Unit]
Description=Queue Warden
After=network.target

[Service]
WorkingDirectory=/opt/queue-warden
ExecStart=/usr/bin/python3 /opt/queue-warden/main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Directory Structure

| Path | Purpose |
|------|---------|
| `main.py` | Entrypoint: scheduler thread plus Flask in the foreground |
| `warden/config.py` | Defaults, file load, env overrides, target validation |
| `warden/clients.py` | `RdtClient`, `ArrTarget`, `BookmarkarrTarget` |
| `warden/detectors.py` | Stall detection and failed-import detection |
| `warden/cleanup.py` | Orchestration: gather victims, claim, remove |
| `warden/state.py` | Persisted stall streaks, run history, lifetime metrics |
| `warden/web.py` | Flask app, JSON API, `Runtime` shared handle |
| `warden/notify.py` | Discord/Slack webhooks |
| `templates/index.html` | Dashboard |
| `tests/` | pytest suite |

## Configuration Files

`config.json` holds live API keys and is gitignored; `config.json.example` is the committed
template. Tracking the live config instead would mean a routine `git add -A` publishes real
credentials to a public repository.

## Running the Project

```bash
python main.py                                   # dev
docker compose up -d --build                     # prod
docker run --rm -v "$PWD":/work -w /work python:3.12-slim \
  bash -lc 'pip install -q -r requirements.txt pytest && python -m pytest tests/ -q'
```

## Design Notes

Dry run defaults to on. A first run happens before anyone has reviewed what this daemon considers
removable, and the alternative is a service that starts deleting and blocklisting the moment
credentials are filled in. An existing config file keeps whatever it already says, so this only
affects fresh installs.

Both `DRY_RUN` and `QUEUE_WARDEN_DRY_RUN` are read. The compose file maps the prefixed name down
to the bare one, but anyone writing their own compose sets the prefixed name directly in
`environment:` — silently ignoring it would hand them a live daemon when they explicitly asked for
a dry run.

Supplying a URL and API key for a target through the environment also enables that target.
Without it, a target shipped disabled by default can never be switched on by environment alone,
and an env-only deployment silently drops it.

The starter config is written *after* environment overrides are applied, so the file on disk
reflects what is actually running. Persisting bare defaults first made a configured daemon look
unconfigured, which is a confusing thing to debug.

`Runtime.run_once` is serialised behind a lock. Two overlapping passes would both see the same
victims, and the second would report failures for items the first had already removed.

A torrent can trip both detectors at once — stalled at the client *and* failed in the queue.
Victims are de-duplicated by hash before removal, otherwise the metrics double-count and the log
shows a cleanup that never happened.

Stall detection requires a streak of consecutive zero-speed checks plus a minimum age, never a
single reading. Torrents idle briefly between pieces all the time, and acting on one sample would
kill healthy downloads on thin swarms.

Completed torrents sitting at 0 B/s are seeding, not stuck, and are explicitly excluded.

Bookmarkarr reports `blocklisted` in its response body separately from the HTTP status. A torrent
client answers a delete for an unknown hash with success, so status alone cannot tell the caller
whether the release was actually recognised. Queue Warden keys off that flag, and treats a `false`
as "not claimed" so the item falls through to direct client removal rather than being recorded as
a successful blocklist.

State is written to a temp file and renamed, so an interrupted write cannot leave a half-parsed
file that the next start would discard. A corrupt state file is discarded with a warning rather
than being fatal: it costs stall streaks and history, not correctness.

## Troubleshooting

**Nothing is ever removed.** Check `/api/status` for `targets` — an empty list means no target had
both a url and an api key. Unconfigured targets are skipped silently by design.

**Bookmarkarr items are removed but never blocklisted.** Bookmarkarr must be 0.1.17 or newer;
earlier versions ignore the `blocklist` query parameter.

**Healthy downloads are being removed.** Raise `stuck_min_age_minutes` and `stuck_min_streak`.
Slow swarms can sit at 0 B/s for long stretches while still being alive.

**Web UI returns 500 with `TemplateNotFound`.** The templates directory must sit beside `main.py`.
It is resolved absolutely from the package location, so this only happens if the tree is split up.

**Run history is empty after a restart.** `STATE_PATH` is not persisted. Under Docker, mount
`./data/state:/app/state`.

**Targets configured in the file are ignored under Docker.** An environment variable that is
present but empty used to override the file, and compose passes every variable through with an
empty default (`SONARR_URL=${SONARR_URL:-}`). Only non-empty environment values count as
overrides now; if this reappears, check `_env()` in `warden/config.py`.

**Targets cannot be reached from inside the container.** `localhost` means the container. Use
`host.docker.internal` for services on the Docker host, or the container name for services on a
shared Docker network. The Test button on the Settings page confirms it immediately.
