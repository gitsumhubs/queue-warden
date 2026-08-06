"""
Optional Discord/Slack notifications.

Deliberately fire-and-forget: a webhook outage must never fail a cleanup run that already
succeeded, so every failure here is logged and swallowed.
"""

import logging

import requests

log = logging.getLogger("warden")


def _post(url, payload):
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except Exception as error:  # noqa: BLE001
        log.warning("Notification failed: %s", error)


def notify_run(config, deleted, errors):
    settings = config["notifications"]
    threshold = settings.get("notify_threshold", 5)

    should_notify = len(deleted) >= threshold or (errors and settings.get("notify_on_error", True))
    if not should_notify:
        return

    lines = [f"**Queue Warden** removed {len(deleted)} item(s)"]
    for victim in deleted[:10]:
        lines.append(f"• {victim.title} — {victim.reason}")
    if len(deleted) > 10:
        lines.append(f"• …and {len(deleted) - 10} more")
    if errors:
        lines.append(f"\n{len(errors)} error(s): " + "; ".join(errors[:3]))

    message = "\n".join(lines)

    if settings.get("discord_webhook"):
        _post(settings["discord_webhook"], {"content": message[:1900]})
    if settings.get("slack_webhook"):
        _post(settings["slack_webhook"], {"text": message[:3000]})
