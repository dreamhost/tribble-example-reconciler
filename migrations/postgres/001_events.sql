-- The event log, and on Postgres the reconciler's whole state.
--
-- Vector is the only writer: it takes every event off Kafka and inserts it here. The reconciler
-- reads. Its own milestones arrive the same way as anybody's events, because it posts them to
-- Vector like any other producer.
--
-- The columns are the JSON fields Vector emits, by name: its Postgres sink inserts with
-- jsonb_populate_recordset, so a column the event lacks is written NULL (defaults are ignored)
-- and a renamed column silently stops being filled.

CREATE TABLE events (
    -- A uuid minted by Vector unless the producer sent one.
    --
    -- NOT a primary key, and not unique, on purpose. Kafka delivery is at-least-once and the
    -- sink has no ON CONFLICT: one duplicate would fail its whole batch, and Vector would
    -- retry that batch forever. So a redelivered event is a second row, and the readers
    -- de-duplicate (the latest milestone of a stage wins; a cycle is its first genesis).
    id           text         NOT NULL,
    name         text         NOT NULL,
    -- The `<app>` of `POST /sensors/<app>`: who produced it.
    app          text         NOT NULL,
    user_id      text         NOT NULL,
    -- 'raw' for everything today. Kept because the readers filter on it.
    event_class  text         NOT NULL,
    -- Event time, stated by the producer; `ingested_at` is when Vector received it.
    occurred_at  timestamptz  NOT NULL,
    ingested_at  timestamptz  NOT NULL,
    -- The whole event body, envelope fields included.
    message      jsonb        NOT NULL
);

-- An await, and opening cycles from events: one name from a point in time, oldest first.
CREATE INDEX events_name_time ON events (name, occurred_at);
-- A cycle's milestones: `message @> {"cycle_key": ...}`.
CREATE INDEX events_message ON events USING gin (message jsonb_path_ops);
CREATE INDEX events_id ON events (id);
