"""
Web UI and JSON API.

Built around what makes a cleanup daemon reviewable day to day: a run history you can scroll,
lifetime metrics, per-target connection tests, and a manual trigger — including a dry run, so
a pass can be inspected before anything is actually removed.
"""

import logging
import os
import threading

from flask import Flask, jsonify, redirect, render_template, request, send_file

from . import (
    cleanup as cleanup_module,
    clients as clients_module,
    config as config_module,
    detectors,
    logbuffer,
    logarchive,
    notify,
    state as state_module,
)

log = logging.getLogger("warden")

# Templates live at the project root, but this module is inside the package, so Flask's
# default lookup would miss them. Resolved absolutely so the working directory cannot
# change the answer.
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _waiting(runtime):
    """
    Downloads currently being timed by the import-pending rule, soonest first.

    Shown on the dashboard because the wait is also the window to import something by hand,
    and that is only usable if it is visible before the item is removed rather than after.
    """
    wait = runtime.config["detectors"]["failed_import"].get("pending_warning_minutes", 0)
    now = detectors.now_ts()
    rows = []
    # Copied before iterating: a pass on the scheduler thread adds and drops timers.
    for target, timers in list(runtime.state.get("pending", {}).items()):
        for entry in list(timers.values()):
            waited = (now - entry.get("first_seen", now)) // 60
            rows.append(
                {
                    "target": target,
                    "title": entry.get("title", ""),
                    "message": entry.get("message", ""),
                    "waited": waited,
                    "remaining": max(0, wait - waited),
                }
            )
    return sorted(rows, key=lambda row: row["remaining"])


def _finished(runtime):
    settings = runtime.config["detectors"].get("finished", {})
    if not settings.get("enabled", False) or runtime.clients["download_client"] is None:
        return []
    wait = settings.get("threshold_minutes", 60)
    now = detectors.now_ts()
    rows = []
    for item in list(runtime.state.get("finished", [])):
        age = max(0, now - item["finished_at"]) // 60
        remaining = max(0, wait - age)
        rows.append({**item, "age": age, "remaining": remaining,
                     "status": item.get("hold") or ("Waiting for age" if remaining else "Ready for next pass")})
    return sorted(rows, key=lambda row: row["remaining"])


def create_app(runtime):
    """`runtime` carries the live config, clients, state, and scheduler."""
    app = Flask(__name__, template_folder=TEMPLATE_DIR)

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            metrics=runtime.state["metrics"],
            runs=runtime.state["runs"][:20],
            history=runtime.state["history"][:50],
            waiting=_waiting(runtime),
            finished=_finished(runtime),
            targets=[target.name for target in runtime.clients["targets"]],
            config=runtime.config,
            next_run=runtime.scheduler.next_run,
        )

    @app.route("/api/status")
    def status():
        metrics = runtime.state["metrics"]
        return jsonify(
            last_run=metrics.get("last_run"),
            next_run=runtime.scheduler.next_run,
            total_deleted=metrics.get("total_deleted", 0),
            total_runs=metrics.get("total_runs", 0),
            by_detector=metrics.get("by_detector", {}),
            by_target=metrics.get("by_target", {}),
            last_error=metrics.get("last_error"),
            waiting_on_import=len(_waiting(runtime)),
            waiting_on_finished=len(_finished(runtime)),
            targets=[target.name for target in runtime.clients["targets"]],
            dry_run=runtime.config["cleanup"].get("dry_run", False),
            fast_mode=getattr(runtime.scheduler, "fast_mode", False),
        )

    @app.route("/api/finished")
    def finished_items():
        return jsonify(items=_finished(runtime))

    @app.route("/api/fast-mode", methods=["POST"])
    def fast_mode():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return jsonify(success=False, message="enabled must be true or false"), 400
        runtime.scheduler.set_fast_mode(data["enabled"])
        return jsonify(success=True, fast_mode=runtime.scheduler.fast_mode)

    @app.route("/api/runs")
    def runs():
        return jsonify(runs=runtime.state["runs"][: int(request.args.get("limit", 20))])

    @app.route("/api/history")
    def history():
        return jsonify(history=runtime.state["history"][: int(request.args.get("limit", 50))])

    @app.route("/api/test/<name>")
    def test_target(name):
        for target in runtime.clients["targets"]:
            if target.name.lower() == name.lower():
                try:
                    target.ping()
                    return jsonify(success=True, message=f"{target.name} connected")
                except Exception as error:  # noqa: BLE001
                    return jsonify(success=False, message=str(error)), 502
        return jsonify(success=False, message=f"No target named {name}"), 404

    @app.route("/run", methods=["POST"])
    def run_now():
        # Backgrounded so the button returns immediately; a full pass can take a while when
        # several targets are slow to answer.
        force = request.form.get("force_stuck") == "1"
        dry = request.form.get("dry_run") == "1"
        threading.Thread(target=runtime.run_once, kwargs={"force_stuck": force, "dry_run": dry}, daemon=True).start()
        return redirect("/")

    @app.route("/settings")
    def settings():
        return render_template(
            "settings.html",
            config=runtime.config,
            targets=runtime.config.get("all_targets", runtime.config["targets"]),
            flavours=config_module.FLAVOURS,
            saved=request.args.get("saved") == "1",
        )

    @app.route("/api/config", methods=["GET"])
    def get_config():
        config = {key: value for key, value in runtime.config.items() if key != "all_targets"}
        config["targets"] = runtime.config.get("all_targets", runtime.config["targets"])
        return jsonify(config)

    @app.route("/api/config", methods=["POST"])
    def save_config():
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(success=False, errors=["Request body was not valid JSON"]), 400

        config, errors = config_module.normalise(payload)
        if errors:
            # Rejected wholesale rather than partially applied: a half-saved config is
            # harder to reason about than an unchanged one.
            return jsonify(success=False, errors=errors), 400

        try:
            runtime.apply_config(config)
        except OSError as error:
            return jsonify(success=False, errors=[f"Could not write config: {error}"]), 500

        return jsonify(success=True, message="Settings saved", targets=[t.name for t in runtime.clients["targets"]])

    @app.route("/logs")
    def logs_page():
        return render_template("logs.html", config=runtime.config, rdt_archive=logarchive.path().is_file())

    @app.route("/logs/rdt-archive")
    def rdt_archive():
        if not logarchive.path().is_file():
            return jsonify(error="No RDT log archive available"), 404
        return send_file(logarchive.path(), as_attachment=True, download_name="rdt-cleanup.log", mimetype="text/plain")

    @app.route("/logs/current")
    def current_log():
        filename = os.path.abspath(os.getenv("LOG_PATH", "logs/cleanup.log"))
        if not os.path.isfile(filename):
            return jsonify(error="No persisted Queue Warden log available"), 404
        return send_file(filename, as_attachment=True, download_name="queue-warden.log", mimetype="text/plain")

    @app.route("/api/logs")
    def api_logs():
        if request.args.get("source") == "rdt":
            return jsonify(logs=logarchive.tail(
                limit=request.args.get("limit", default=300, type=int),
                after_id=request.args.get("after", type=int),
                level=request.args.get("level"), detector=request.args.get("detector"),
            ))
        handler = logbuffer.get_handler()
        if handler is None:
            return jsonify(logs=[])

        after = request.args.get("after", type=int)
        return jsonify(
            logs=handler.tail(
                limit=request.args.get("limit", default=300, type=int),
                after_id=after,
                level=request.args.get("level"),
                detector=request.args.get("detector"),
            )
        )

    @app.route("/api/logs/clear", methods=["POST"])
    def clear_logs():
        handler = logbuffer.get_handler()
        if handler is not None:
            handler.clear()
        return jsonify(success=True)

    @app.route("/clear-error", methods=["POST"])
    def clear_error():
        state_module.clear_error(runtime.state)
        state_module.save(runtime.state_path, runtime.state)
        return redirect("/")

    return app


class Runtime:
    """Shared handle so the scheduler and the web UI act on the same state."""

    def __init__(self, config, clients, state, state_path, scheduler, config_path="config.json"):
        self.config = config
        self.clients = clients
        self.state = state
        self.state_path = state_path
        self.config_path = config_path
        self.scheduler = scheduler
        self._lock = threading.Lock()

    def apply_config(self, config):
        """
        Persists a new config and rebuilds everything derived from it.

        Done under the run lock so a pass in flight finishes against the config it started
        with, rather than having its targets swapped out mid-loop.
        """
        with self._lock:
            config_module.save_config(
                {key: value for key, value in config.items() if key != "all_targets"},
                self.config_path,
            )

            config["all_targets"] = config["targets"]
            config["targets"] = config_module.usable_targets(config)
            config["download_client"]["enabled"] = bool(
                config["download_client"].get("enabled", True) and config["download_client"].get("url")
            )

            self.config = config
            self.clients = {
                "targets": clients_module.build_targets(config),
                "download_client": clients_module.build_download_client(config),
            }
            if self.scheduler is not None:
                self.scheduler.set_interval(config["cleanup"]["interval_minutes"])

            log.info(
                "Settings saved — targets: %s",
                ", ".join(t.name for t in self.clients["targets"]) or "none",
            )

    def run_once(self, force_stuck=False, dry_run=None):
        # Serialised: two overlapping passes would both see the same victims and the second
        # would report failures for items the first already removed.
        with self._lock:
            original = self.config["cleanup"].get("dry_run", False)
            if dry_run is not None:
                self.config["cleanup"]["dry_run"] = dry_run
            try:
                deleted, errors = cleanup_module.run_cleanup(
                    self.clients, self.config, self.state, force_stuck
                )
                if errors:
                    state_module.record_error(self.state, "; ".join(errors[:3]))
                notify.notify_run(self.config, deleted, errors)
                return deleted, errors
            except Exception as error:  # noqa: BLE001
                log.exception("Cleanup run failed")
                state_module.record_error(self.state, error)
                return [], [str(error)]
            finally:
                self.config["cleanup"]["dry_run"] = original
                state_module.save(self.state_path, self.state)
