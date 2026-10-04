from main import Scheduler


def test_fast_mode_runs_on_a_thirty_second_interval(monkeypatch):
    scheduler = Scheduler(5, lambda: None)
    scheduler.set_fast_mode(True)
    waits = []
    def wait(timeout):
        waits.append(timeout)
        scheduler.stop()
    monkeypatch.setattr(scheduler._wake, "wait", wait)
    scheduler._loop()
    assert waits == [30]


def test_leaving_fast_mode_restores_latest_configured_interval(monkeypatch):
    scheduler = Scheduler(5, lambda: None)
    scheduler.set_fast_mode(True)
    scheduler.set_interval(8)
    scheduler.set_fast_mode(False)
    waits = []
    def wait(timeout):
        waits.append(timeout)
        scheduler.stop()
    monkeypatch.setattr(scheduler._wake, "wait", wait)
    scheduler._loop()
    assert waits == [480]
