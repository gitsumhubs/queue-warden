from warden import clients


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_arr_queue_carries_the_warning_and_its_reason(monkeypatch):
    payload = {
        "records": [
            {
                "id": 5,
                "downloadId": "ABC",
                "title": "Stuck Show",
                "status": "completed",
                "trackedDownloadState": "importPending",
                "trackedDownloadStatus": "warning",
                "statusMessages": [
                    {"title": "Stuck Show", "messages": ["No files found are eligible for import"]}
                ],
            }
        ]
    }
    monkeypatch.setattr(clients.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    record = clients.ArrTarget("Sonarr", "http://sonarr:8989", "key").get_queue()[0]

    assert record["state"] == "importpending"
    assert record["tracked_status"] == "warning"
    assert record["messages"] == ["No files found are eligible for import"]


def test_a_healthy_record_has_no_messages(monkeypatch):
    payload = {"records": [{"id": 6, "downloadId": "DEF", "title": "Fine", "trackedDownloadStatus": "ok"}]}
    monkeypatch.setattr(clients.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    record = clients.ArrTarget("Sonarr", "http://sonarr:8989", "key").get_queue()[0]

    assert record["tracked_status"] == "ok"
    assert record["messages"] == []


def test_arr_queue_reads_every_page_before_proving_an_import_is_absent(monkeypatch):
    def get(url, **kwargs):
        page = kwargs["params"]["page"]
        if page == 1:
            return FakeResponse({"records": [{"id": n, "downloadId": f"h{n}"} for n in range(1000)], "totalRecords": 1001})
        assert page == 2
        return FakeResponse({"records": [{"id": 1000, "downloadId": "FINISHED"}], "totalRecords": 1001})
    monkeypatch.setattr(clients.requests, "get", get)
    records = clients.ArrTarget("Sonarr", "http://sonarr:8989", "key").get_queue()
    assert len(records) == 1001
    assert records[-1]["download_id"] == "finished"
