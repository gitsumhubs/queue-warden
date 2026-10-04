# Queue Warden

Queue Warden clears and blocklists downloads that have gone wrong, across Sonarr, Radarr, and
Bookmarkarr. It watches both ends of the pipeline: the download client, for transfers that stop
moving, and each *arr queue, for downloads that finished but failed to import. Anything it
removes is blocklisted at the *arr that owns it, so the same bad release is not picked again on
the next search.

## What it watches

A stalled torrent and a failed import are different failures, visible from different places:

| Failure | Who can see it | Who cannot |
|---------|----------------|------------|
| Transfer stops moving at 40% | download client | the *arr still reads it as "downloading" |
| Download finished, import failed | the *arr queue | the torrent looks perfectly healthy |

Watching only one end leaves the other class of failure to sit in the queue indefinitely.

## Docker Compose

```yaml
---
services:
  queue-warden:
    image: ${QUEUE_WARDEN_IMAGE:-ghcr.io/gitsumhubs/queue-warden:latest}
    container_name: queue-warden
    environment:
      - TZ=${TZ:-Etc/UTC}
      - LOG_LEVEL=${QUEUE_WARDEN_LOG_LEVEL:-INFO}
      # Optional: set these to configure without touching the Settings page.
      - SONARR_URL=${SONARR_URL:-}
      - SONARR_API_KEY=${SONARR_API_KEY:-}
      - RADARR_URL=${RADARR_URL:-}
      - RADARR_API_KEY=${RADARR_API_KEY:-}
      - BOOKMARKARR_URL=${BOOKMARKARR_URL:-}
      - BOOKMARKARR_API_KEY=${BOOKMARKARR_API_KEY:-}
      - DOWNLOAD_CLIENT_URL=${DOWNLOAD_CLIENT_URL:-}
      - DOWNLOAD_CLIENT_USERNAME=${DOWNLOAD_CLIENT_USERNAME:-}
      - DOWNLOAD_CLIENT_PASSWORD=${DOWNLOAD_CLIENT_PASSWORD:-}
      - DRY_RUN=${QUEUE_WARDEN_DRY_RUN:-}
    ports:
      - "${QUEUE_WARDEN_PORT:-3020}:3020"
    # Lets the container reach *arrs and the download client running on the host.
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ${QUEUE_WARDEN_CONFIG_PATH:-./data/config}:/app/config
      - ${QUEUE_WARDEN_STATE_PATH:-./data/state}:/app/state
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:3020/api/status').read()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    restart: unless-stopped
```

```bash
docker compose up -d
```

Then open <http://localhost:3020> and add your *arrs on the **Settings** page. A starter config is
written on first run; anything saved in the UI persists to `./data/config`.

**Dry run is on by default.** Queue Warden reports what it *would* remove and removes nothing
until you turn it off, either on the Settings page or with `QUEUE_WARDEN_DRY_RUN=false`. Give it a
day, check the run history agrees with your judgement, then switch it off.

Both `QUEUE_WARDEN_DRY_RUN` and `DRY_RUN` are accepted, so it works whether you set it in `.env`
alongside the compose file above or directly in a compose `environment:` block of your own.

Inside a container `localhost` means the container itself — use `http://host.docker.internal:8989`
for apps on the Docker host, or `http://sonarr:8989` for apps on a shared Docker network.

## Prerequisites

- Docker with Compose — or Python 3.12+ to run from source
- At least one *arr with an API key
- Optionally a download client (rdt-client, or anything qBittorrent-compatible)

## Running from source

```bash
git clone https://github.com/gitsumhubs/queue-warden.git
cd queue-warden
pip install -r requirements.txt
python main.py
```

To build the container from source instead of pulling the published image:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Run the tests with:

```bash
pip install pytest && python -m pytest tests/ -q
```

## Features

- **Stall detection** — removes torrents in `stalleddl`, `stalledup`, `error`, or `missingfiles`
  immediately, and torrents stuck at 0 B/s once they have been quiet for a configurable age and
  a run of consecutive checks. A single zero reading never counts: torrents idle briefly between
  pieces all the time.
- **Failed-import detection** — removes queue items the *arr has already given up on
  (`importBlocked`, `failed`, `downloadFailed`). Anything still queued, downloading, or importing
  is left alone.
- **Stuck-import detection** — some imports never fail outright: the *arr cannot work out what the
  file is, puts a warning on it, and leaves it in `importPending` until someone imports it by
  hand. These are removed once the warning has stood for a configurable wait (2 hours by default).
  A healthy download passes through `importPending` too, so an item with no warning is never
  timed, and the timer restarts if the warning clears. The dashboard lists what is waiting and
  when it is due, so there is time to import something by hand first.
- **Storage-outage guard** — when storage or an import path goes away, every finished download
  picks up a warning at once. If more than a set number are waiting (10 by default), none are
  removed and the run reports an error instead.
- **Blocklist and re-search** — removal goes through the owning *arr with `blocklist=true`, so the
  release is recorded as bad and a different one is grabbed.
- **Any number of targets** — Sonarr, Radarr, Lidarr, Readarr, and Bookmarkarr are config entries,
  not code. An unconfigured target is skipped silently.
- **Dry run** — see exactly what a pass would remove without removing anything.
- **Run history and metrics** — every pass is recorded with what it removed and why, split by
  detector and by target.
- **Notifications** — optional Discord and Slack webhooks.
- **Resilient** — one unreachable target is logged and skipped; the rest of the run continues.

## Configuration

| Key | Description |
|-----|-------------|
| `targets[]` | One entry per *arr: `name`, `flavour` (`arr` or `bookmarkarr`), `url`, `api_key`, `enabled` |
| `download_client` | `url`, `username`, `password`; set `enabled: false` to run queue-side only |
| `detectors.stalled` | `states`, `stuck_min_age_minutes`, `stuck_min_streak` |
| `detectors.failed_import` | `states` to treat as terminal; `pending_warning_minutes` (default 120, 0 turns it off) and `pending_warning_limit` (default 10) for imports left pending with a warning |
| `cleanup.interval_minutes` | How often a pass runs (default 5) |
| `cleanup.blocklist` | Whether removal also blocklists (default true) |
| `cleanup.dry_run` | Report only, remove nothing |
| `webui.host` / `webui.port` | Bind address and port (default 3020) |
| `notifications.*` | Discord/Slack webhooks and threshold |

Every value can be overridden by environment variable. Targets are addressed by name:
`SONARR_API_KEY`, `BOOKMARKARR_URL`, and so on. Supplying both a URL and an API key for a target
through the environment also enables it, so a target that ships disabled (Bookmarkarr) does not
need a config file edit as well. See RECREATE.md for the full list.

## Tech Stack

- Language: Python 3.12
- Web: Flask
- HTTP: requests
- Tests: pytest
- No database — state is a JSON file

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /api/status` | Last run, next run, lifetime metrics, configured targets, downloads waiting on import |
| `GET /api/runs?limit=20` | Recent run summaries |
| `GET /api/history?limit=50` | Recently removed items with reasons |
| `GET /api/test/<name>` | Connection test for one target |
| `POST /run` | Trigger a pass (`dry_run=1`, `force_stuck=1`) |
| `POST /clear-error` | Dismiss the last recorded error |

## Notes

Upgrading from 0.1.x turns stuck-import detection on with its defaults, because a config file
written before the rule existed inherits them. Set the wait to 0 on the Settings page to keep the
old behaviour. Nothing is removed during the first wait, and the dashboard shows what is being
timed.

Bookmarkarr needs **0.1.17 or newer**, which is the release that added
`DELETE /api/v1/download/queue/{id}?blocklist=true`. Against older versions the call is accepted
but nothing is blocklisted, and Queue Warden correctly reports that nothing was removed.
