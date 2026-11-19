"""Running an n8n workflow as a stage, and recording what it returned.

Config:
    path:     the workflow's webhook path, e.g. `site-check` (required)
    input:    extra fields to send, as written (optional)
    from:     stages whose recorded results to send along, by name (optional)
    timeout:  seconds to wait for the answer (optional)

The workflow receives the cycle's identity and context (for a cycle opened by an event, that is
the event), plus `input`. Its JSON answer is recorded under `result`, so a later stage, or
anyone reading the `milestones` table, finds the collected data there.
"""

import logging

from reconciler.actions import ActionContext, register_action

logger = logging.getLogger("reconciler.actions.workflow")


@register_action("n8n.workflow")
def run_workflow(ctx: ActionContext) -> dict:
    path = ctx.config.get("path")
    if not path:
        raise ValueError(f"cycle {ctx.cycle.key}: `n8n.workflow` needs a `path` in its config")
    if ctx.services.workflows is None:
        raise RuntimeError(
            f"cycle {ctx.cycle.key}: `n8n.workflow` needs N8N_WEBHOOK_BASE_URL to be set")

    sent = dict(ctx.config.get("input") or {})
    for stage in ctx.config.get("from") or []:
        if stage not in ctx.milestones:
            raise ValueError(f"cycle {ctx.cycle.key}: `from` names {stage!r}, which has not "
                             f"been recorded (is it in this stage's `after`?)")
        recorded = ctx.milestones[stage].payload
        sent[stage] = recorded.get("result", recorded.get("evidence", recorded))

    body = {"cycle_key": ctx.cycle.key, "process": ctx.cycle.process,
            "user_id": ctx.cycle.user_id, "context": ctx.cycle.context, "input": sent}
    result = ctx.services.workflows.run(path=path, body=body,
                                        timeout=ctx.config.get("timeout"))
    return {"workflow": path, "result": result}
