"""Reacting to events: the log, opening a cycle from one, and running an n8n workflow."""

import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from reconciler import app
from reconciler.actions import ActionContext, Services, build_action
from reconciler.adapters.n8n import ConsoleWorkflows, N8nWorkflows
from reconciler.core import Cycle, Milestone, Status
from reconciler.definition import GENESIS_STAGE, ProcessDefinition
from reconciler.finder import RecordedEvents, SignalFinder

PROCESSES = pathlib.Path(__file__).resolve().parent / "fixtures" / "processes"


# --- the definition ---------------------------------------------------------------------------

def _definition(**overrides):
    base = {"name": "p", "cycle_key": "p_{event_id}",
            "opens": [{"via": "event", "event": "thing_happened"}],
            "stages": [{"name": "a", "kind": "act", "action": "do.a"}]}
    base.update(overrides)
    return ProcessDefinition.model_validate(base)


def test_an_event_opener_names_its_event():
    assert _definition().opens[0].event == "thing_happened"
    with pytest.raises(ValidationError, match="requires an `event`"):
        _definition(opens=[{"via": "event"}])


def test_only_an_event_opener_may_name_an_event():
    with pytest.raises(ValidationError, match="must not carry an `event`"):
        _definition(opens=[{"via": "manual", "event": "thing_happened"}])


def test_a_process_opened_by_an_event_must_key_its_cycles_by_the_event():
    with pytest.raises(ValidationError, match="event_id"):
        _definition(cycle_key="p_{year}")


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --- opening a cycle from an event ------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.cycle_list = []

    def cycles(self, definition):
        return list(self.cycle_list)


class FakeLog:
    def __init__(self, events):
        self.events = events
        self.asked = []

    def signals(self, name, since):
        self.asked.append((name, since))
        return [(ts, body) for ts, body in self.events if body["name"] == name]


class FakeSink:
    def __init__(self, store):
        self.store = store
        self.recorded = []
        self.milestones = {}

    def record(self, cycle, stage, payload, *, now=None):
        self.recorded.append((cycle.key, stage))
        if stage == GENESIS_STAGE:
            self.store.cycle_list.append(cycle)
        # As the real sink does: the cycle key rides in every recorded body.
        milestone = Milestone(stage=stage, timestamp=now or cycle.opened_at,
                              payload={"cycle_key": cycle.key, **payload})
        self.milestones[stage] = milestone
        return milestone


def _body(event_id, name="site_check_requested", **payload):
    """An event as the Postgres log returns it: the payload with its envelope beside it."""
    return {**payload, "id": event_id, "name": name, "user_id": "demo",
            "timestamp": 1790000000}


@pytest.fixture
def wired(monkeypatch):
    store = FakeStore()
    log = FakeLog([(1790000000, _body("e-1", domain="example.com")),
                   (1790000060, _body("e-2", domain="example.org"))])
    sink = FakeSink(store)
    monkeypatch.setattr(app, "milestone_store", lambda: store)
    monkeypatch.setattr(app, "event_log", lambda: log)
    monkeypatch.setattr(app, "sink_for", lambda options, definition: sink)
    return store, log, sink


OPTIONS = app.RunOptions(processes_dir=PROCESSES)


def test_each_event_opens_one_cycle_carrying_the_event(wired):
    store, log, sink = wired

    opened = app.open_from_events("site_check", options=OPTIONS, now=1790000100)

    assert [c.key for c in opened] == ["site_check_e-1", "site_check_e-2"]
    assert opened[0].context == {"event": _body("e-1", domain="example.com"),
                                 "event_at": 1790000000}
    assert opened[0].user_id == "demo"
    assert sink.recorded == [("site_check_e-1", GENESIS_STAGE),
                             ("site_check_e-2", GENESIS_STAGE)]
    # The read is bounded by the opener's lookback, a week by default.
    assert log.asked == [("site_check_requested", 1790000100 - 7 * 24 * 3600)]


def test_opening_twice_opens_nothing_the_second_time(wired):
    store, log, sink = wired
    app.open_from_events("site_check", options=OPTIONS, now=1790000100)

    assert app.open_from_events("site_check", options=OPTIONS, now=1790000200) == []
    assert len(sink.recorded) == 2


def test_a_process_with_no_event_opener_reads_no_log(monkeypatch):
    monkeypatch.setattr(app, "event_log", lambda: pytest.fail("the log was read"))
    monkeypatch.setattr(app, "milestone_store", lambda: pytest.fail("the store was read"))

    assert app.open_from_events("monthly_invoice", options=OPTIONS) == []


# --- running a workflow -----------------------------------------------------------------------

class FakeWorkflows:
    def __init__(self, answer=None):
        self.answer = answer or {"status": 200}
        self.calls = []

    def run(self, *, path, body, timeout=None):
        self.calls.append((path, body, timeout))
        return self.answer


CYCLE = Cycle(key="site_check_e-1", process="site_check", user_id="demo", opened_at=1790000100,
              context={"event": _body("e-1", domain="example.com"), "event_at": 1790000000})


def test_a_workflow_stage_sends_the_cycle_and_records_the_answer():
    workflows = FakeWorkflows({"status": 200, "ms": 180})
    ctx = ActionContext(cycle=CYCLE, milestones={},
                        config={"path": "site-check", "input": {"timeout_ms": 5000}},
                        services=Services(workflows=workflows))

    recorded = build_action("n8n.workflow")(ctx)

    assert recorded == {"workflow": "site-check", "result": {"status": 200, "ms": 180}}
    ((path, body, _),) = workflows.calls
    assert path == "site-check"
    assert body == {"cycle_key": "site_check_e-1", "process": "site_check", "user_id": "demo",
                    "context": CYCLE.context, "input": {"timeout_ms": 5000}}


def test_a_workflow_stage_can_send_an_earlier_stage_s_result():
    workflows = FakeWorkflows()
    earlier = Milestone(stage="checked", timestamp=1, payload={"result": {"status": 200}})
    ctx = ActionContext(cycle=CYCLE, milestones={"checked": earlier},
                        config={"path": "store-report", "from": ["checked"]},
                        services=Services(workflows=workflows))

    build_action("n8n.workflow")(ctx)

    assert workflows.calls[0][1]["input"] == {"checked": {"status": 200}}


def test_a_workflow_stage_stops_loudly_when_it_cannot_run():
    run = build_action("n8n.workflow")
    with pytest.raises(RuntimeError, match="N8N_WEBHOOK_BASE_URL"):
        run(ActionContext(cycle=CYCLE, milestones={}, config={"path": "site-check"}))
    with pytest.raises(ValueError, match="needs a `path`"):
        run(ActionContext(cycle=CYCLE, milestones={}, services=Services(workflows=FakeWorkflows())))


@pytest.fixture
def n8n():
    seen = {}
    answers = {"/webhook/site-check": (200, b'{"status": 200}'),
               "/webhook/as-list": (200, b'[{"status": 200}]'),
               "/webhook/text": (200, b"done"),
               "/webhook/broken": (500, b"{}")}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["token"] = self.headers.get("X-Relay-Token")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, data = answers.get(self.path, (404, b"{}"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    server, url = _serve(Handler)
    yield url, seen
    server.shutdown()
    server.server_close()


def test_the_adapter_posts_to_the_webhook_with_the_token(n8n):
    url, seen = n8n
    workflows = N8nWorkflows(base_url=f"{url}/", token="s3cret")

    assert workflows.run(path="/site-check", body={"cycle_key": "k"}) == {"status": 200}
    assert seen == {"token": "s3cret", "body": {"cycle_key": "k"}}
    # n8n answers with a list when a webhook returns all items; one item is unwrapped.
    assert workflows.run(path="as-list", body={}) == {"status": 200}


@pytest.mark.parametrize("path, message", [
    ("broken", "HTTP 500"), ("missing", "HTTP 404"), ("text", "did not answer with JSON")])
def test_a_workflow_that_fails_raises_so_the_stage_stays_unrecorded(n8n, path, message):
    url, _ = n8n
    with pytest.raises(RuntimeError, match=message):
        N8nWorkflows(base_url=url).run(path=path, body={})


def test_a_dry_run_calls_nothing():
    assert ConsoleWorkflows().run(path="site-check", body={}) == {"dry_run": True}


# --- end to end, with nothing real behind it --------------------------------------------------

def test_an_event_becomes_a_cycle_that_runs_a_workflow_and_waits_for_the_report(wired, monkeypatch):
    store, log, sink = wired
    workflows = FakeWorkflows({"status": 200})
    monkeypatch.setattr(app, "services_for", lambda options: Services(workflows=workflows))
    monkeypatch.setattr(app, "finders",
                        lambda: {"events": SignalFinder(RecordedEvents(log))})
    definition = app.definition_named("site_check", PROCESSES)

    (cycle, _) = app.open_from_events("site_check", options=OPTIONS, now=1790000100)
    outcome = app.advance_one(definition, cycle, {}, OPTIONS, sink)

    # The workflow ran and its answer was recorded; the report has not arrived.
    assert outcome.advanced == ["checked"]
    assert outcome.waiting_on == ["reported"]

    # A report for ANOTHER cycle does not close this one; its own does.
    recorded = {"checked": sink.milestones["checked"]}
    log.events.append((1790000150, _body("e-8", "site_check_reported", cycle_key="other")))
    assert app.advance_one(definition, cycle, recorded, OPTIONS, sink).waiting_on == ["reported"]

    log.events.append((1790000160, _body("e-9", "site_check_reported",
                                         cycle_key="site_check_e-1")))
    assert app.advance_one(definition, cycle, recorded, OPTIONS, sink).status is Status.COMPLETE
    assert len(workflows.calls) == 1


# --- the store, and the gateway's credentials -------------------------------------------------

def test_postgres_is_a_store_of_its_own_name_configured_with_database_url(monkeypatch):
    monkeypatch.setenv("RECONCILER_STORE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgres://app:pw@tribble-1:5432/app")

    assert type(app.milestone_store()).__name__ == "NeonMilestones"
    assert type(app.event_log()).__name__ == "NeonMilestones"
    assert "events" in app.finders()
    # Vector is the only writer there, so the reconciler has none of its own.
    assert app._writer(app.RunOptions()) is None


def test_the_mysql_store_has_no_event_log_and_says_so(monkeypatch):
    monkeypatch.setenv("RECONCILER_STORE", "mysql")
    monkeypatch.setenv("MYSQL_DATABASE_URL", "mysql://app:pw@tribble-1:3306/app")

    with pytest.raises(app.ConfigurationError, match="Postgres store"):
        app.event_log()
    assert "events" not in app.finders()


def test_the_gateway_is_sent_basic_credentials_only_when_both_are_set(monkeypatch):
    from reconciler.adapters.ingest import request_headers

    monkeypatch.delenv("VECTOR_USERNAME", raising=False)
    monkeypatch.delenv("VECTOR_PASSWORD", raising=False)
    assert app._vector_auth() is None
    assert "Authorization" not in request_headers(None)

    monkeypatch.setenv("VECTOR_USERNAME", "ingest")
    with pytest.raises(app.ConfigurationError, match="both"):
        app._vector_auth()

    monkeypatch.setenv("VECTOR_PASSWORD", "s3cret")
    assert request_headers(app._vector_auth())["Authorization"] == "Basic aW5nZXN0OnMzY3JldA=="
