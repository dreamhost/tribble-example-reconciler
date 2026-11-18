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

A plain CLI, meant to be run on a schedule:

```
python -m reconciler.run reconcile --process <name> [--dry-run]
python -m reconciler.run sweep-tasks [--dry-run]
python -m reconciler.run open --process <name> [--seq N] [--dry-run]
```

`--dry-run` writes nothing and sends nothing, and prints what would have happened.

`railpack.json` builds it with [Railpack](https://railpack.com); its start command is a dry run
of the task sweep, to be overridden per schedule.

## Configuration

All of it is environment variables.

| variable | |
| :-- | :-- |
| `RECONCILER_STORE` | `mysql` or `neon` (Postgres, the default) |
| `MYSQL_DATABASE_URL` | `mysql://user:pass@host:port/db` |
| `MYSQL_SSL_CA` | path to the MySQL server's CA certificate; required with `mysql` |
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
| `migrations/` | numbered SQL, applied in order |

```bash
uv sync --extra dev
uv run pytest
```

## A note on the comments

Docstrings and comments were carried over from the original project. Some still describe its
earlier runners (Prefect, Argo Workflows, a Kubernetes operator) and files that are not part of
this copy.
