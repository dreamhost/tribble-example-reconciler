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
5. **DNS A**, **DNS AAAA**, **DNS NS** ask Google's DNS over HTTPS for the domain's records.
6. **RDAP** looks the domain up at `rdap.org`, for its registrar and when it expires. That
   works for a registered domain (`example.com`), not for a name under one
   (`www.example.com`), which the registry answers with a 400.
7. **Certificate** reads the certificate the site serves on 443: who issued it, when it
   expires, whether it is trusted.
8. **PageSpeed** asks Google's PageSpeed Insights for a mobile performance score.
9. **Full report** puts all of it into one event.
10. **Report to Vector** posts that `site_check_reported` event, carrying the `cycle_key`
    which the cycle's last stage is waiting for.

The reconciler is answered at step 4, before the slower checks, so its stage records the
same quick result whatever they do. A check that fails is part of the report (its status or
its error), never a reason to send none.

None of these services needs an account. PageSpeed Insights without an API key shares one
daily quota with everybody else doing the same, and mostly answers 429; the report then says
so. For scores, add `&key=<your key>` to the PageSpeed node's URL.

The Certificate node is a Code node that needs Node's `tls` module, which n8n only lets code
use when `NODE_FUNCTION_ALLOW_BUILTIN=tls` is set on the workers. Without it the report's
`certificate` is an error and the rest is unaffected.

After importing:

- Create a **Header Auth** credential (name `X-Relay-Token`, value: the reconciler's
  `N8N_WEBHOOK_TOKEN`) and pick it on the Webhook node.
- Create a **Basic Auth** credential (Vector's `INGEST_USERNAME` and `INGEST_PASSWORD`) and pick
  it on the Report to Vector node.
- Set the Report to Vector node's URL to your own Vector, `https://<vector>/sensors/n8n`.
- Activate the workflow. The reconciler calls the production URL, which only an active
  workflow serves.
