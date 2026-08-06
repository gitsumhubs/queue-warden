"""
Web UI and JSON API.

Built around what makes a cleanup daemon reviewable day to day: a run history you can scroll,
lifetime metrics, per-target connection tests, and a manual trigger — including a dry run, so
a pass can be inspected before anything is actually removed.
"""

import logging
import os
import threading

from flask import Flask, jsonify, redirect, render_template, request

from . import (
    cleanup as cleanup_module,
    clients as clients_module,
    config as config_module,
    logbuffer,
    notify,
    state as state_module,
)

log = logging.getLogger("warden")

# Templates live at the project root, but this module is inside the package, so Flask's
# default lookup would miss them. Resolved absolutely so the working directory cannot
# change the answer.
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


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
            targets=[target.name for target in runtime.clients["targets"]],
            dry_run=runtime.config["cleanup"].get("dry_run", False),
        )

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
        return render_template("logs.html", config=runtime.config)

    @app.route("/api/logs")
    def api_logs():
        handler = logbuffer.get_handler()
        if handler is None:
            return jsonify(logs=[])

        after = request.args.get("after", type=int)
        return jsonify(
            logs=handler.tail(
                limit=request.args.get("limit", default=300, type=int),
                after_id=after,
                level=request.args.get("level"),
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
