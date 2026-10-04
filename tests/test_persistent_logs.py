import logging
from logging.handlers import RotatingFileHandler

from main import configure_logging
from warden import logarchive, logbuffer


def test_persisted_completion_error_retains_cursor_and_detector(tmp_path):
    import main

    path = tmp_path / "cleanup.log"
    buffer = logbuffer.RingBufferHandler()
    disk = RotatingFileHandler(path, maxBytes=1024, backupCount=3)
    disk.setFormatter(main.PersistentLogFormatter())
    try:
        first = logging.LogRecord("warden", logging.INFO, "", 0, "Last received", (), None)
        buffer.emit(first)
        cursor = buffer.tail()[-1]["id"]
        disk.emit(first)
        error = logging.LogRecord("warden", logging.ERROR, "", 0, "Direct client removal failed: offline", (), None)
        error.detector = "finished"
        buffer.emit(error)
        disk.emit(error)
        unread_id = buffer.tail()[-1]["id"]
    finally:
        disk.close()

    restored = logbuffer.RingBufferHandler()
    restored.restore(logarchive.restore(path))
    entries = restored.tail(after_id=cursor, detector="finished")
    assert len(entries) == 1
    assert entries[0]["id"] == unread_id
    assert entries[0]["message"] == "Direct client removal failed: offline"


def test_restore_includes_recent_lines_in_rotated_logs(tmp_path):
    path = tmp_path / "cleanup.log"
    for suffix, message in [(".3", "oldest"), (".2", "older"), (".1", "recent"), ("", "current")]:
        path.with_name(path.name + suffix).write_text(f"2026-10-04 12:00:00,123 [INFO] {message}\n")
    assert [entry["message"] for entry in logarchive.restore(path, limit=3)] == ["older", "recent", "current"]


def test_actual_rotation_preserves_live_ids_and_activity_tags(tmp_path):
    from main import PersistentLogFormatter

    path = tmp_path / "cleanup.log"
    buffer = logbuffer.RingBufferHandler()
    disk = RotatingFileHandler(path, maxBytes=240, backupCount=3)
    disk.setFormatter(PersistentLogFormatter())
    try:
        for index in range(5):
            record = logging.LogRecord("warden", logging.INFO, "", 0, f"Cleanup event {index}", (), None)
            record.detector = "finished"
            buffer.emit(record)
            disk.emit(record)
    finally:
        disk.close()

    assert path.with_name(path.name + ".3").exists()
    restored = logarchive.restore(path)
    assert [entry["id"] for entry in restored] == [entry["id"] for entry in buffer.tail(limit=4)]
    assert all(entry["detector"] == "finished" for entry in restored)


def test_log_buffer_restores_completed_activity_from_disk(tmp_path):
    log = tmp_path / "cleanup.log"
    log.write_text("2026-10-04 12:00:00,123 [INFO] [finished] movie waiting\n2026-10-04 12:05:00,123 [WARNING] [client] removed movie (finished 61m ago)\n")
    handler = logbuffer.RingBufferHandler()
    handler.restore(logarchive.tail(archive_path=log))
    handler.emit(logging.LogRecord("warden", logging.WARNING, "", 0, "New event", (), None))
    restored = handler.tail(detector="finished")
    assert [entry["message"] for entry in restored] == ["[finished] movie waiting", "[client] removed movie (finished 61m ago)"]
    assert handler.tail()[-1]["id"] > restored[-1]["id"]


def test_cleanup_messages_are_written_to_persistent_log(tmp_path, monkeypatch):
    path = tmp_path / "logs/cleanup.log"
    monkeypatch.setenv("LOG_PATH", str(path))
    configure_logging()
    logging.getLogger("warden").warning("Completed movie removed", extra={"detector": "finished"})
    assert "Completed movie removed" in path.read_text()


def test_open_viewer_cursor_advances_after_logger_restart():
    before = logbuffer.RingBufferHandler()
    record = logging.LogRecord("warden", logging.INFO, "", 0, "Before restart", (), None)
    record.created = 1000
    before.emit(record)
    cursor = before.tail()[-1]["id"]

    after = logbuffer.RingBufferHandler()
    record = logging.LogRecord("warden", logging.INFO, "", 0, "After restart", (), None)
    record.created = 1001
    after.emit(record)
    assert [entry["message"] for entry in after.tail(after_id=cursor)] == ["After restart"]
