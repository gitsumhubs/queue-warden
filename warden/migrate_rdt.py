"""One-time, offline RDT Cleanup settings/history import; original files are preserved."""
import argparse
import json
from pathlib import Path
import shutil

from . import config as config_module, state as state_module


def merge(source_config, source_state, config, state):
    """Import historical counts once, keeping QW credentials, timers and live policy."""
    if state.get("migrations", {}).get("rdt-cleanup"):
        return False
    config.setdefault("detectors", {}).setdefault("finished", {}).update({
        "enabled": True,
        "threshold_minutes": source_config.get("cleanup", {}).get("finished_threshold_minutes", 60),
    })
    source_metrics = source_state.get("metrics", {})
    metrics = state["metrics"]
    metrics["total_deleted"] += source_metrics.get("total_deletions", 0)
    reasons = source_metrics.get("deletions_by_reason", {})
    finished = reasons.get("finished", 0)
    stalled = sum(count for reason, count in reasons.items() if reason != "finished")
    by_detector = metrics.setdefault("by_detector", {})
    by_detector["finished"] = by_detector.get("finished", 0) + finished
    by_detector["stalled"] = by_detector.get("stalled", 0) + stalled

    imported = []
    for item in source_metrics.get("deletion_history", []):
        detector = "finished" if str(item.get("reason", "")).startswith("finished") else "stalled"
        imported.append({"timestamp": item["timestamp"], "title": item.get("name", "Unknown"),
                         "reason": item.get("reason", ""), "detector": detector,
                         "category": detector, "target": None, "hash": "", "source": "rdt-cleanup"})
    state["history"] = sorted(state.get("history", []) + imported, key=lambda item: item["timestamp"], reverse=True)[:config["state"]["max_history"]]
    imported_runs = []
    for run in source_metrics.get("runs", []):
        imported_runs.append({"timestamp": run["timestamp"], "deleted": run.get("deleted", 0),
                              "errors": [run["error"]] if run.get("error") else [],
                              "duration_seconds": None, "dry_run": False, "items": [], "source": "rdt-cleanup"})
    state["runs"] = sorted(state.get("runs", []) + imported_runs, key=lambda run: run["timestamp"], reverse=True)[:config["state"]["max_runs"]]
    metrics["total_runs"] += len(imported_runs)
    state.setdefault("migrations", {})["rdt-cleanup"] = {
        "timestamp": state_module.now_iso(), "total_deleted": source_metrics.get("total_deletions", 0),
        "finished": finished, "history_entries": len(imported), "available_runs": len(imported_runs),
    }
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Original rdt-cleanup-v2 directory")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    source_config = json.loads((args.source / "config.json").read_text())
    source_state = json.loads((args.source / "state/state.json").read_text())
    config, errors = config_module.normalise(json.loads(args.config.read_text()))
    state = state_module._empty()
    if args.state.exists():
        # An import must never silently discard existing history as the daemon's
        # cache-tolerant load does. Abort before making backups or writing anything.
        existing = json.loads(args.state.read_text())
        if not isinstance(existing, dict) or not isinstance(existing.get("metrics", {}), dict):
            parser.error("Destination state has an invalid structure")
        state.update(existing)
        state["metrics"] = {**state_module._empty()["metrics"], **existing.get("metrics", {})}
    changed = merge(source_config, source_state, config, state)
    config, errors = config_module.normalise(config)
    if errors:
        parser.error("; ".join(errors))
    if not changed:
        print("RDT Cleanup already imported; no changes made")
        return
    args.archive.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.config, args.archive / "qw-config-before-rdt.json")
    if args.state.exists():
        shutil.copy2(args.state, args.archive / "qw-state-before-rdt.json")
    shutil.copy2(args.source / "state/state.json", args.archive / "rdt-state.json")
    source_log = args.source / "logs/cleanup.log"
    if source_log.exists():
        shutil.copy2(source_log, args.archive / "rdt-cleanup.log")
    config_module.save_config(config, str(args.config))
    # Unlike daemon persistence, CLI write failures must propagate to the caller.
    config_module.save_config(state, str(args.state))
    print(json.dumps(state["migrations"]["rdt-cleanup"]))


if __name__ == "__main__":
    main()
