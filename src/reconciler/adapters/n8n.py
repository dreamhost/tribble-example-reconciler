"""Running an n8n workflow through its webhook, and taking its answer.

The other n8n adapters are wires with a credential on them: one question (`gmail`), one write
(`labels`, `mail`). This is the general one. A workflow is *work somebody built in n8n* — fetch
from an API, look something up, transform it — and the reconciler's part is only to start it
with the cycle's facts and record what came back.

    POST <N8N_WEBHOOK_BASE_URL>/webhook/<path>
    {"cycle_key": ..., "process": ..., "user_id": ..., "context": {...}, "input": {...}}

The workflow must answer with a JSON object (a "Respond to Webhook" node, or the webhook's
"when last node finishes" mode). That object is the stage's result.

**The call is synchronous on purpose.** The answer becomes the milestone, and a recorded
milestone is what stops the next pass calling the workflow again. Work too slow to answer in
one request should instead answer at once and report later by posting an event, which an
`await` stage with `source: events` then waits for.

**No retries.** A failure raises and the stage stays unrecorded, so the next scheduled pass
calls the workflow again: the schedule is the retry. That makes the call at-least-once, so a
workflow with side effects should de-duplicate on `cycle_key`, which is sent for that reason.
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("reconciler.adapters.n8n")


class N8nWorkflows:
    """Runs workflows on one n8n instance."""

    def __init__(self, *, base_url: str, token: str | None = None,
                 header: str = "X-Relay-Token", timeout: float = 60.0):
        self._base = base_url.rstrip("/")
        self._token = token
        self._header = header
        self._timeout = timeout

    def run(self, *, path: str, body: dict, timeout: float | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers[self._header] = self._token
        request = urllib.request.Request(
            f"{self._base}/webhook/{path.strip('/')}",
            data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"n8n workflow {path!r} failed: HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"n8n workflow {path!r} unreachable: {e}") from e

        try:
            answer = json.loads(raw)
        except ValueError as e:
            raise RuntimeError(f"n8n workflow {path!r} did not answer with JSON") from e
        # A webhook that returns "all items" answers with a list; one item is the common case.
        if isinstance(answer, list) and len(answer) == 1:
            answer = answer[0]
        if not isinstance(answer, dict):
            raise RuntimeError(f"n8n workflow {path!r} must answer with one JSON object, "
                               f"got {type(answer).__name__}")
        logger.info("n8n workflow %s answered with %d field(s)", path, len(answer))
        return answer


class ConsoleWorkflows:
    """Runs nothing; says what it would have run. The `--dry-run` half."""

    def run(self, *, path: str, body: dict, timeout: float | None = None) -> dict:
        logger.info("[dry-run] would run n8n workflow %s with %s", path,
                    json.dumps(body, sort_keys=True)[:300])
        return {"dry_run": True}
