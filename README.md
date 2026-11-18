# process-reconciler

Long-running, multi-step processes described as data and advanced by a reconciler that is a
*pure function of the milestones recorded so far*.

This is a sanitised copy of a private project, kept as a realistic workload for testing a
container platform: a scheduled CLI with a MySQL store that calls n8n webhooks and posts events
to an HTTP gateway. It ships no process definitions; the one sample under `tests/fixtures/` uses
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

The one act that is *not* idempotent is opening a cycle: a second open mints a second cycle.

## Running it

A plain CLI, meant to be run on a schedule. `bin/reconciler` is the entry point in a container:

```
bin/reconciler reconcile --process <name> [--dry-run]
bin/reconciler sweep-tasks [--dry-run]
bin/reconciler open --process <name> [--seq N] [--dry-run]
bin/reconciler migrate [--dry-run]      # apply migrations/*.sql to MySQL
bin/reconciler gateway                  # a stand-in event gateway, see below
```

`--dry-run` writes nothing and sends nothing, and prints what would have happened.

`railpack.json` builds the image with [Railpack](https://railpack.com). Its start command is a
dry run of the task sweep; each app overrides it with one of the commands above.

### On tribble

One source, several apps, each with its own start command:

| app | start command | runs as |
| :-- | :-- | :-- |
| gateway | `sh bin/reconciler gateway` | a service, internal |
| migrate | `sh bin/reconciler migrate` | scheduled, run by hand with "Run now" |
| advance | `sh bin/reconciler reconcile --process <name>` | scheduled, hourly |
| sweep | `sh bin/reconciler sweep-tasks` | scheduled, every 5 minutes |

`tribble.json` gives the service its port (8080) and health check (`GET /healthz`); scheduled
apps have neither.

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

**The gateway.** `tools/gateway_stub.py` accepts the same `POST /sensors/<app>` the real gateway
does and writes one line per event to its log. Point `VECTOR_BASE_URL` at it
(`http://<its internal address>`) to run end to end before a real one exists.

## Configuration

All of it is environment variables.

| variable | |
| :-- | :-- |
| `RECONCILER_STORE` | `mysql` or `neon` (Postgres, the default) |
| `MYSQL_DATABASE_URL` | `mysql://user:pass@host:port/db` |
| `MYSQL_SSL_CA` | path to the MySQL server's CA certificate; required with `mysql` |
| `MYSQL_SSL_CA_B64` | the same certificate, base64 on one line, where a file cannot be mounted |
| `NEON_DATABASE_URL` | the Postgres DSN, with `neon` |
| `VECTOR_BASE_URL` | the HTTP gateway milestones and task events are POSTed to |
| `GMAIL_QUERY_URL`, `GMAIL_SEARCH_LIGHT_URL`, `GMAIL_LABEL_URL`, `GMAIL_TRASH_URL` | n8n webhooks that query and change the mailbox |
| `MAIL_RELAY_URL`, `MAIL_RELAY_HEADER`, `MAIL_RELAY_TOKEN`, `MAIL_TO`, `MAIL_FROM` | the n8n webhook that sends mail |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_SENDER` | sending mail over SMTP instead |
| `LLM_RELAY_URL` | the n8n webhook that classifies a mail |
| `CRAFTMYPDF_API_KEY` | rendering the invoice PDF |
| `AWARE_USER_ID` | the user the recorded events belong to (default `demo`) |
| `DOPPLER_TOKEN` | optional: load the rest from Doppler at start |

The MySQL schema is `migrations/*.sql`, applied in order by `scripts/mysql_migrate.py`.

## Layout

| path | |
| :-- | :-- |
| `processes/` | the definitions: data, not code. Empty here; add one `*.yml` per process |
| `tests/fixtures/processes/` | a sample definition the test suite reads |
| `src/reconciler/core.py` | `Cycle`, `Milestone`, and the ports. Imports nothing |
| `src/reconciler/adapters/` | the outside world: MySQL, Postgres, Gmail, mail, LLM, PDF |
| `src/reconciler/actions/` | what an `act` stage can do |
| `src/reconciler/run.py` | the CLI |
| `bin/reconciler` | the container entry point |
| `tools/gateway_stub.py` | a stand-in event gateway |
| `migrations/` | numbered SQL, applied in order |

```bash
uv sync --extra dev
uv run pytest
```

## A note on the comments

Docstrings and comments were carried over from the original project. Some still describe its
earlier runners (Prefect, Argo Workflows, a Kubernetes operator) and files that are not part of
this copy.
