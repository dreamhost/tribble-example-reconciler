# process-reconciler

Long-running, multi-step processes described as data and advanced by a reconciler that is a
*pure function of the milestones recorded so far*.

This is a sanitised copy of a private project, kept as a realistic workload for testing a
container platform: a scheduled CLI that reads an event log in Postgres, runs n8n workflows,
and posts its own progress as events to an HTTP gateway (Vector). It ships no process definitions; the one sample under `tests/fixtures/` uses
placeholder names, addresses, rates and ids.

## The idea

A process is a YAML file. Each stage is either something the reconciler **does** (`act`) or
something it **waits for** (`await`):

```yaml
stages:
  - name: computed_lines
    kind: act
    action: lines.worked_days

  - name: invoice_approved
    kind: await
    after: [invoice_emailed]
    signal:
      source: gmail
      correlate_on: subject
      reply:
        accept: ["yes"]
```

`after` is a list, so a process is a DAG rather than a chain.

**The reconciler never remembers where it got to.** On every run it reads the milestones already
recorded, works out the frontier, and acts on it:

- **No resume tokens, no correlation IDs, no suspended-run state.**
- **Every run is safe to repeat.** Re-running advances nothing that has already advanced, so a
  missed schedule is a latency problem, never a correctness one.
- **A missed `await` is self-healing.** If the fact it waits for shows up late, the next run
  sees it.

The one act that is *not* idempotent is opening a cycle by hand: a second open mints a second
cycle. A cycle opened by an event is keyed by that event, so it can only be opened once.

## Reacting to events

```
producer --POST /sensors/<app>--> Vector --> Postgres `events`
                                    ^               |
                                    |               v
            n8n workflow <--- webhook --- reconciler (on a schedule)
```

Everything that happens is an event in one table, and the reconciler is one more producer: it
posts each milestone to the gateway and reads it back from Postgres on the next run. It never
writes to the database.

```yaml
name: site_check
cycle_key: "site_check_{event_id}"
opens:
  - {via: event, event: site_check_requested}   # one cycle per event; the event is its context
stages:
  - name: checked
    kind: act
    action: n8n.workflow          # POST <N8N_WEBHOOK_BASE_URL>/webhook/site-check
    config: {path: site-check}    # the JSON answer is recorded as the stage's `result`
  - name: reported
    kind: await
    after: [checked]
    signal: {source: events, event: site_check_reported, correlate_on: cycle_key}
```

- **`n8n.workflow`** sends `{cycle_key, process, user_id, context, input}` and records the JSON
  object the workflow answers with. A failed call records nothing, so the next run calls
  again: workflows with side effects should de-duplicate on `cycle_key`.
- **`source: events`** waits for an event of that name. A workflow reports back by posting one
  to the gateway.
- A milestone is readable only once Vector has written it. A run that starts before then
  repeats the stage, which is why every stage must be safe to repeat.

The full sample is `tests/fixtures/processes/site_check.yml`.

## Running it

A plain CLI, meant to be run on a schedule. `bin/reconciler` is the entry point in a container:

```
bin/reconciler reconcile --process <name> [--dry-run]
bin/reconciler sweep-tasks [--dry-run]
bin/reconciler open --process <name> [--seq N] [--dry-run]
bin/reconciler migrate [--dry-run]      # create or update the store's tables
bin/reconciler gateway                  # a stand-in event gateway, for running locally
```

`--dry-run` writes nothing and sends nothing, and prints what would have happened.

`railpack.json` builds the image with [Railpack](https://railpack.com). Its start command is a
dry run of the task sweep; each app overrides it with one of the commands above.

### On tribble

One source, several apps, each with its own start command:

| app | start command | runs as |
| :-- | :-- | :-- |
| migrate | `sh bin/reconciler migrate` | scheduled, run by hand with "Run now" |
| advance | `sh bin/reconciler reconcile --process <name>` | scheduled, every minute or so |
| sweep | `sh bin/reconciler sweep-tasks` | scheduled, every 5 minutes |

Beside them run Postgres (from the template), Vector
([vector-tribble](https://github.com/rodis/vector-tribble)) and n8n
([n8n-tribble](https://github.com/rodis/n8n-tribble)). Each scheduled app needs
`RECONCILER_STORE=postgres`, `DATABASE_URL` as a reference to the Postgres app's, and
`VECTOR_BASE_URL=http://<Vector's internal address>` with its credentials.

### With MySQL instead

`RECONCILER_STORE=mysql` keeps cycles and milestones in MySQL tables the reconciler writes
itself (`migrations/*.sql`). It has no event log there, so `via: event` and `source: events`
need Postgres.

**The MySQL CA.** The MySQL adapter verifies the server against a CA certificate file and has no
mode without one. tribble cannot mount a file into an app, and a variable cannot hold a newline,
so the certificate goes in base64-encoded and `bin/reconciler` writes it to a file at start:

```
# in the database's container (tribble ssh <mysql app>):
base64 -w0 /var/lib/mysql/data/ca.pem
# then set the output as MYSQL_SSL_CA_B64 on every app that talks to MySQL
```

The certificate is public, not a secret. It is generated per database instance, so a re-created
database needs it read again.

**The stand-in gateway.** `tools/gateway_stub.py` accepts the same `POST /sensors/<app>` Vector
does and writes one line per event to its log, for running without Vector.

## Configuration

All of it is environment variables.

| variable | |
| :-- | :-- |
| `RECONCILER_STORE` | `postgres`, `mysql`, or `neon` (the default: Postgres, configured with `NEON_DATABASE_URL`) |
| `DATABASE_URL` | the Postgres DSN, with `postgres` |
| `MYSQL_DATABASE_URL` | `mysql://user:pass@host:port/db` |
| `MYSQL_SSL_CA` | path to the MySQL server's CA certificate; required with `mysql` |
| `MYSQL_SSL_CA_B64` | the same certificate, base64 on one line, where a file cannot be mounted |
| `NEON_DATABASE_URL` | the Postgres DSN, with `neon` |
| `VECTOR_BASE_URL` | the HTTP gateway milestones and task events are POSTed to |
| `VECTOR_USERNAME`, `VECTOR_PASSWORD` | HTTP basic credentials for the gateway, if it asks |
| `N8N_WEBHOOK_BASE_URL` | the n8n that `n8n.workflow` stages run on, e.g. `https://n8n.example.com` |
| `N8N_WEBHOOK_TOKEN`, `N8N_WEBHOOK_HEADER` | optional token sent to every workflow (header defaults to `X-Relay-Token`) |
| `GMAIL_QUERY_URL`, `GMAIL_SEARCH_LIGHT_URL`, `GMAIL_LABEL_URL`, `GMAIL_TRASH_URL` | n8n webhooks that query and change the mailbox |
| `MAIL_RELAY_URL`, `MAIL_RELAY_HEADER`, `MAIL_RELAY_TOKEN`, `MAIL_TO`, `MAIL_FROM` | the n8n webhook that sends mail |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_SENDER` | sending mail over SMTP instead |
| `LLM_RELAY_URL` | the n8n webhook that classifies a mail |
| `CRAFTMYPDF_API_KEY` | rendering the invoice PDF |
| `AWARE_USER_ID` | the user the recorded events belong to (default `demo`) |
| `DOPPLER_TOKEN` | optional: load the rest from Doppler at start |

The Postgres schema is `migrations/postgres/*.sql` and the MySQL one `migrations/*.sql`;
`bin/reconciler migrate` applies whichever the store needs.

## Layout

| path | |
| :-- | :-- |
| `processes/` | the definitions: data, not code. Empty here; add one `*.yml` per process |
| `tests/fixtures/processes/` | a sample definition the test suite reads |
| `src/reconciler/core.py` | `Cycle`, `Milestone`, and the ports. Imports nothing |
| `src/reconciler/adapters/` | the outside world: Postgres, MySQL, n8n, Gmail, mail, LLM, PDF |
| `src/reconciler/actions/` | what an `act` stage can do |
| `src/reconciler/run.py` | the CLI |
| `bin/reconciler` | the container entry point |
| `tools/gateway_stub.py` | a stand-in event gateway |
| `migrations/` | numbered SQL, applied in order (`postgres/` for Postgres) |

```bash
uv sync --extra dev
uv run pytest
```

## A note on the comments

Docstrings and comments were carried over from the original project. Some still describe its
earlier runners (Prefect, Argo Workflows, a Kubernetes operator) and files that are not part of
this copy.
