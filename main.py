#!/usr/bin/env python3
"""
Queue Warden — entrypoint.

Runs the cleanup loop on a schedule and serves the web UI in the foreground, so the whole
thing is one process under systemd or one container under Docker.
"""

import logging
import os
import sys
import threading
import time

from warden import clients as clients_module, config as config_module, logbuffer, state as state_module, web

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def configure_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        stream=sys.stdout,
    )
    # Flask's per-request log is noise next to the cleanup output.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # Feeds the in-app log viewer, which works the same whether this runs under Docker,
    # systemd, or in a terminal.
    logbuffer.install()


class Scheduler:
    def __init__(self, interval_minutes, run):
        self.interval = max(1, interval_minutes) * 60
        self.run = run
        self.next_run = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def set_interval(self, minutes):
        """
        Applied from the settings UI. The sleeping loop is woken so a shortened interval
        takes effect now rather than after the old, longer wait finishes.
        """
        new_interval = max(1, int(minutes)) * 60
        if new_interval == self.interval:
            return
        self.interval = new_interval
        self._wake.set()

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        return thread

    def _loop(self):
        while not self._stop.is_set():
            self.next_run = time.time() + self.interval
            try:
                self.run()
            except Exception:  # noqa: BLE001
                # Already recorded by Runtime.run_once; the loop must survive regardless.
                logging.getLogger("warden").exception("Scheduled run failed")

            # Waits on either signal, so an interval change does not have to sit out the
            # remainder of the previous one.
            self._wake.clear()
            waited = 0.0
            step = 1.0
            while waited < self.interval and not self._stop.is_set() and not self._wake.is_set():
                time.sleep(step)
                waited += step

    def stop(self):
        self._stop.set()


def main():
    configure_logging()
    log = logging.getLogger("warden")

    config_path = os.getenv("CONFIG_PATH", "config.json")
    state_path = os.getenv("STATE_PATH", "state/state.json")

    config = config_module.load_config(config_path)
    state = state_module.load(state_path)

    active = {
        "targets": clients_module.build_targets(config),
        "download_client": clients_module.build_download_client(config),
    }

    log.info("Queue Warden starting")
    log.info("Targets: %s", ", ".join(t.name for t in active["targets"]) or "none")
    log.info("Download client: %s", "enabled" if active["download_client"] else "disabled")
    if config["cleanup"].get("dry_run"):
        log.warning("DRY RUN enabled — nothing will actually be removed")

    runtime = web.Runtime(config, active, state, state_path, scheduler=None, config_path=config_path)
    scheduler = Scheduler(config["cleanup"]["interval_minutes"], runtime.run_once)
    runtime.scheduler = scheduler
    scheduler.start()

    log.info("Scheduler started (every %d minutes)", config["cleanup"]["interval_minutes"])
    log.info("Web UI on http://%s:%s", config["webui"]["host"], config["webui"]["port"])

    app = web.create_app(runtime)
    app.run(host=config["webui"]["host"], port=config["webui"]["port"], threaded=True)


if __name__ == "__main__":
    main()
