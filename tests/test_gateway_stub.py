"""The stand-in gateway accepts exactly what the reconciler's own adapters send."""

import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

from reconciler.adapters.ingest import GatewayEvents

_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "gateway_stub.py"
_spec = importlib.util.spec_from_file_location("gateway_stub", _PATH)
gateway_stub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gateway_stub)


@pytest.fixture
def base_url():
    server = gateway_stub.serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def test_it_takes_an_event_from_the_real_adapter_and_logs_it(base_url, capsys):
    GatewayEvents(base_url, app="tasks").emit({"event_name": "email_task_closed",
                                               "user_id": "demo"})
    line = json.loads(capsys.readouterr().out.strip())
    assert line == {"app": "tasks", "event_name": "email_task_closed", "user_id": "demo"}


def test_health_answers(base_url):
    with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
        assert response.status == 200


@pytest.mark.parametrize("path,body,status", [
    ("/sensors/tasks", b"not json", 400),
    ("/sensors/tasks", b'{"payload": []}', 400),
    ("/sensors/tasks", b'{"other": {}}', 400),
    ("/sensors/", b'{"payload": {}}', 404),
    ("/elsewhere", b'{"payload": {}}', 404),
])
def test_a_request_it_cannot_use_is_refused(base_url, path, body, status):
    assert _post(base_url + path, body) == status
