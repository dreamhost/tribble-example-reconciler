"""`ProcessDefinition` — a long-running process expressed as data, not code (ADR 0012).

The YAML-on-disk schema (`processes/<name>.yml`) the reconciler loads. Deliberately the
same shape of thing as `inference.runtime.definition.EventDefinition`, one tier up: a
generic runner plus definitions-as-data, so process N+1 is a file rather than a component.

Every stage is one of exactly two kinds — `act` (the reconciler does it and records the
milestone itself) or `await` (it watches for a fact and records when it appears). Resisting
a third kind is a live design constraint, not an accident: see ADR 0012 trip-wires 1 and 7.

Like `engine_config` in the inference tier, a stage's `signal` block is **opaque to the
core** — the finder that resolves it parses its own config, and nothing here knows what a
Gmail matcher looks like.
"""

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

logger = logging.getLogger(__name__)

# Milestones the reconciler does not own as stages. `cycle_opened` is the genesis fact —
# a cycle exists because it exists, so the reconciler is handed cycles, it never creates
# them. `cycle_voided` is terminal: correction is re-running, never amending (ADR 0012).
GENESIS_STAGE = "cycle_opened"
VOID_STAGE = "cycle_voided"
RESERVED_STAGES = frozenset({GENESIS_STAGE, VOID_STAGE})


class Opens(BaseModel):
    """One way a cycle of this process comes into existence.

    A list rather than a single `schedule:` field because a cycle is opened by an *event*,
    and a schedule is only one producer of that event. The Acme invoice is opened
    monthly on a cron *and* by hand for an ad-hoc bonus, which has no cadence at all — had
    the schedule been the definition of a cycle, manual invoices would have needed a
    parallel entry point.

    The discriminator is `via`, **not** `on`: YAML 1.1 resolves a bare `on` key to the
    boolean `True` (the same trap GitHub Actions workflows carry), so `{on: manual}` parses
    as `{True: "manual"}` and the field silently goes missing. Quoting it in every process
    file would work and would be forgotten exactly once; renaming it cannot be.
    """

    model_config = ConfigDict(extra="forbid")

    via: Literal["schedule", "manual", "event"]
    cron: str | None = None
    # `via: event` — every recorded event of this name opens one cycle, whose context is the
    # event. The cycle key must then carry `{event_id}`, which is what makes opening idempotent:
    # the same event can only ever mint the same key.
    event: str | None = None
    lookback_seconds: int = 7 * 24 * 3600

    @model_validator(mode="after")
    def _cron_iff_schedule(self):
        if self.via == "schedule" and not self.cron:
            raise ValueError("opens.via=schedule requires a `cron`")
        if self.via != "schedule" and self.cron:
            raise ValueError(f"opens.via={self.via} must not carry a `cron`")
        if self.via == "event" and not self.event:
            raise ValueError("opens.via=event requires an `event` name")
        if self.via != "event" and self.event:
            raise ValueError(f"opens.via={self.via} must not carry an `event`")
        return self


class Stage(BaseModel):
    """One step of a process.

    `after` is a list, so a process is a DAG rather than a chain — parallel branches cost
    nothing. It must reference *earlier* stages (enforced below), which is what makes the
    reconciler's single linear scan a valid topological walk.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["act", "await"]
    after: list[str] = []
    action: str | None = None   # `act` only — resolved against the action registry
    signal: dict = {}           # `await` only — opaque here; the finder parses it
    config: dict = {}           # opaque here too; the ACTION parses its own, as engines do

    @model_validator(mode="after")
    def _kind_matches_payload(self):
        if self.kind == "act":
            if not self.action:
                raise ValueError(f"stage '{self.name}': kind=act requires an `action`")
            if self.signal:
                raise ValueError(f"stage '{self.name}': kind=act must not carry a `signal`")
        else:
            if not self.signal:
                raise ValueError(f"stage '{self.name}': kind=await requires a `signal`")
            if self.action:
                raise ValueError(f"stage '{self.name}': kind=await must not carry an `action`")
        return self


class ProcessDefinition(BaseModel):
    """A single process, loaded from `processes/<name>.yml`."""

    model_config = ConfigDict(extra="forbid")

    name: str                    # identity — snake_case; prefixes every emitted event name
    enabled: bool = True         # skip-load toggle, matching EventDefinition
    cycle_key: str               # template, e.g. "acme_invoice_{year}_{seq:03d}"
    opens: list[Opens] = []
    stages: list[Stage]

    @model_validator(mode="after")
    def _stages_form_an_ordered_dag(self):
        seen: set[str] = set()
        for stage in self.stages:
            if stage.name in RESERVED_STAGES:
                raise ValueError(f"stage '{stage.name}' uses a reserved milestone name")
            if stage.name in seen:
                raise ValueError(f"duplicate stage '{stage.name}'")
            for dep in stage.after:
                # Forward references would break the single-pass scan in `core.reconcile`,
                # and a cycle would make the process unreachable — both caught here rather
                # than at run time, where the symptom is a process that silently stalls.
                if dep not in seen:
                    raise ValueError(
                        f"stage '{stage.name}' depends on '{dep}', which is not an earlier stage"
                    )
            seen.add(stage.name)
        if not self.stages:
            raise ValueError("a process needs at least one stage")
        # A milestone is itself an event, named `<process>_<stage>`. An outside event under
        # that prefix which carries a `cycle_key` would be read back as a milestone, and a
        # stage would look recorded when nobody recorded it.
        listened = [o.event for o in self.opens if o.event]
        listened += [s.signal["event"] for s in self.stages
                     if s.signal.get("source") == "events" and s.signal.get("event")]
        for event in listened:
            if event.startswith(f"{self.name}_"):
                raise ValueError(
                    f"event '{event}' starts with this process's own name; milestones are "
                    f"events named '{self.name}_<stage>', so rename the process or the event")
        if any(o.via == "event" for o in self.opens) and "{event_id}" not in self.cycle_key:
            raise ValueError("a process opened by an event needs `{event_id}` in its "
                             "`cycle_key`, or two events would mint the same cycle")
        return self

    def event_name(self, stage_name: str) -> str:
        """The event `name` a milestone is emitted under.

        Prefixed with the process name **structurally**, not by convention, because an
        unprefixed milestone could collide with a definition's `input_event_names()` and be
        routed into an engine that was not expecting it — the one thing that turns process
        events (which nothing consumes) from a no-op into a bug. See ADR 0012.
        """
        return f"{self.name}_{stage_name}"


def load_definitions(processes_dir: Path) -> list[ProcessDefinition]:
    """Load every `*.yml` under `processes_dir` into a validated `ProcessDefinition`.

    Best-effort and isolated, exactly as `inference.runtime.definition.load_definitions`
    is: one malformed process is logged and skipped rather than stopping the others.
    """
    definitions: list[ProcessDefinition] = []
    for path in sorted(processes_dir.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            definition = ProcessDefinition.model_validate(raw)
        except (ValidationError, yaml.YAMLError) as e:
            logger.error("Skipping invalid process definition %s: %s", path.name, e)
            continue
        if not definition.enabled:
            logger.info("Skipping disabled process definition %s", definition.name)
            continue
        definitions.append(definition)
    logger.info("Loaded %d process definition(s): %s",
                len(definitions), [d.name for d in definitions])
    return definitions
