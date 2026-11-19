# n8n workflows

Importable workflows the processes in `processes/` run. In n8n: **Create workflow**, then
**Import from file**.

## site-check

Run by `processes/check_site.yml`.

1. **Webhook** takes the reconciler's `POST /webhook/site-check`.
2. **Check site** fetches `https://<domain>`, the domain of the event that opened the cycle.
3. **Result** keeps the status code and whether it was a success. An unreachable site is a
   result (`status: 0` and an `error`), not a failure of the workflow.
4. **Answer the reconciler** returns that result, which the reconciler records.
5. **Report to Vector** posts a `site_check_reported` event carrying the `cycle_key`, which
   the cycle's last stage is waiting for.

After importing:

- Create a **Header Auth** credential (name `X-Relay-Token`, value: the reconciler's
  `N8N_WEBHOOK_TOKEN`) and pick it on the Webhook node.
- Create a **Basic Auth** credential (Vector's `INGEST_USERNAME` and `INGEST_PASSWORD`) and pick
  it on the Report to Vector node.
- Set the Report to Vector node's URL to your own Vector, `https://<vector>/sensors/n8n`.
- Activate the workflow. The reconciler calls the production URL, which only an active
  workflow serves.
