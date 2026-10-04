"""
HTTP clients for the download client and for each blocklist target.

Every target exposes the same three methods so the cleanup loop never branches on flavour.
The differences between a Sonarr-compatible *arr and Bookmarkarr are confined here.
"""

import logging

import requests

log = logging.getLogger("warden")

TIMEOUT = 30


class RdtClient:
    """qBittorrent-compatible API exposed by rdt-client."""

    def __init__(self, url, username, password):
        self.url = (url or "").rstrip("/")
        self.username = username
        self.password = password
        self.session = None

    def login(self):
        session = requests.Session()
        session.headers.update({"Referer": f"{self.url}/"})
        response = session.post(
            f"{self.url}/api/v2/auth/login",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={"username": self.username, "password": self.password},
            timeout=20,
        )
        if response.status_code != 200 or "ok" not in response.text.lower():
            raise RuntimeError(f"Download client login failed: {response.status_code}")
        self.session = session
        return self

    def list_torrents(self):
        response = self.session.get(f"{self.url}/api/v2/torrents/info", timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def delete(self, hashes, delete_files=True):
        """Direct removal, for torrents no target claimed. Nothing to blocklist in that case."""
        if not hashes:
            return
        response = self.session.post(
            f"{self.url}/api/v2/torrents/delete",
            data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()


def _status_messages(record):
    """The *arr's own explanation for a warning, flattened to plain strings."""
    messages = []
    for entry in record.get("statusMessages") or []:
        messages.extend(text for text in (entry.get("messages") or []) if text)
    if not messages and record.get("errorMessage"):
        messages.append(record["errorMessage"])
    return messages


class ArrTarget:
    """Sonarr, Radarr, Lidarr, Readarr — anything speaking the v3 queue API."""

    flavour = "arr"

    def __init__(self, name, url, api_key):
        self.name = name
        self.url = (url or "").rstrip("/")
        self.api_key = api_key

    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def ping(self):
        response = requests.get(f"{self.url}/api/v3/system/status", headers=self._headers(), timeout=10)
        response.raise_for_status()
        return True

    def get_queue(self):
        """
        Normalised queue records: id, download_id (torrent hash), title, state.

        `trackedDownloadState` is the field that says whether an import failed;
        `status` alone only reports the transfer. `trackedDownloadStatus` is separate
        again: it says whether the *arr is unhappy with the item, which is the only thing
        that tells a stuck import from one that is about to run.
        """
        response = requests.get(
            f"{self.url}/api/v3/queue",
            headers=self._headers(),
            params={"page": 1, "pageSize": 1000},
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        records = []
        for record in (response.json() or {}).get("records", []):
            records.append(
                {
                    "id": record.get("id"),
                    "download_id": (record.get("downloadId") or "").lower(),
                    "title": record.get("title") or f"Queue item {record.get('id')}",
                    "state": str(record.get("trackedDownloadState") or "").lower(),
                    "status": str(record.get("status") or "").lower(),
                    "tracked_status": str(record.get("trackedDownloadStatus") or "").lower(),
                    "messages": _status_messages(record),
                }
            )
        return records

    def remove(self, queue_id, blocklist=True):
        response = requests.delete(
            f"{self.url}/api/v3/queue/{queue_id}",
            headers=self._headers(),
            params={"blocklist": str(blocklist).lower(), "removeFromClient": "true"},
            timeout=TIMEOUT,
        )
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 202):
            response.raise_for_status()
        return True


class BookmarkarrTarget:
    """
    Bookmarkarr's equivalent contract on /api/v1.

    Two differences from an *arr. It resolves a download from the torrent hash directly, so
    the hash doubles as the removal key and no queue lookup is needed first. And a torrent
    client answers a delete for an unknown hash with success, so Bookmarkarr reports
    `blocklisted` separately in its body — that flag, not the status code, is what tells us
    it actually recognised the release.
    """

    flavour = "bookmarkarr"

    def __init__(self, name, url, api_key):
        self.name = name
        self.url = (url or "").rstrip("/")
        self.api_key = api_key

    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def ping(self):
        response = requests.get(f"{self.url}/api/v1/system/health", headers=self._headers(), timeout=10)
        response.raise_for_status()
        return True

    def get_queue(self):
        response = requests.get(
            f"{self.url}/api/v1/download/queue", headers=self._headers(), timeout=TIMEOUT
        )
        response.raise_for_status()

        records = []
        for item in (response.json() or {}).get("items", []):
            state = str(item.get("status") or "").lower()
            records.append(
                {
                    # Bookmarkarr accepts this same id back on the removal route.
                    "id": item.get("id"),
                    "download_id": (item.get("id") or "").lower(),
                    "title": item.get("title") or f"Queue item {item.get('id')}",
                    "state": state,
                    "status": state,
                }
            )
        return records

    def remove(self, queue_id, blocklist=True):
        response = requests.delete(
            f"{self.url}/api/v1/download/queue/{queue_id}",
            headers=self._headers(),
            params={"blocklist": str(blocklist).lower()},
            timeout=TIMEOUT,
        )
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 202):
            response.raise_for_status()

        if not blocklist:
            return True

        try:
            return bool((response.json() or {}).get("blocklisted", False))
        except ValueError:
            return False


def build_targets(config):
    """Instantiates one client per configured target, preserving config order."""
    targets = []
    for entry in config["targets"]:
        cls = BookmarkarrTarget if entry["flavour"] == "bookmarkarr" else ArrTarget
        targets.append(cls(entry["name"], entry["url"], entry["api_key"]))
    return targets


def build_download_client(config):
    client = config["download_client"]
    if not client.get("enabled"):
        return None
    return RdtClient(client["url"], client.get("username", ""), client.get("password", ""))
