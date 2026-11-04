# Resend VPS routing — deployment verified, provider activation pending

No VPS deployment, Nginx edit, secret change, webhook registration, provider
request, mail delivery or production migration is authorized by this document.

## Historical inspection baseline

Read-only inspection on 2026-11-04 found srv1577410 serving
growup.securecodeusa.com through Cloudflare and Nginx, with a private-preview
Basic Authentication gate at server scope. Nginx proxies to 127.0.0.1:8010.
The active release was 20261101T171548Z_91591ba. Its running environment named
growup_prod, growup_app, DEMO_MODE=yes and MVP_WORKFLOW_ENABLED=0. Backend health
and readiness returned 200. The release lacked the Resend implementation.
This baseline was superseded by the owner-authorized live deployment on 2026-11-04.
The first verified new activation was 4d1000e; consult the current RELEASE-MANIFEST.txt
for subsequent exact revisions. Reconfirm the current state before another change.

## Independent configuration

SERVER_MODE=DEV_SERVER selects https://securecodeusa.local:8443 and private
development integration behavior. SERVER_MODE=LIVE_SERVER selects
https://growup.securecodeusa.com and its exact callback URL. These are Grow Up
service destinations; official third-party provider endpoints are not rewritten.
Changing this setting does not deploy, transfer databases, redirect credentials,
enable inference/outreach or fix public routing. Browser-relative requests stay
on the server actually serving the page. Open the selected origin explicitly.

DEMO_MODE controls the existing external-action simulation policy. Synthetic
dossiers use the dedicated growup_handoff_demo connector on either server; the
operational database remains growup_dev or growup_prod. DEMO_MODE=yes alone is
not a synthetic-database selector. Any future global synthetic-only request mode
must be implemented explicitly rather than silently relabeling real records.

The new server/database readiness guard refuses a known cross-server mismatch.
DEV_SERVER intentionally reports public callbacks unavailable. Development
offline tests and separately authorized receive-only polling remain available.
LIVE_SERVER activation requires successful code, schema, credential and routing
checks; a mode label cannot remove a genuine blocker or override authentication.

## Owner-controlled deployment checkpoint

1. Pin the exact reviewed GitHub commit. Compare VPS HEAD/schema with the new
   release, inventory pending migrations, record the active symlink, service
   definition and Nginx file, and prepare a protected database backup/restore
   checkpoint. No production migration follows merely from a mode change.
2. Prepare a separate release on the VPS using Git/GitHub and the reviewed
   installation procedure. Do not reuse deploy-dev.ps1 against myvps; it is
   deliberately pinned to ubuntu@securecode-dev. Avoid switching current until
   the proposed production schema/configuration and rollback are approved.
3. In the VPS's private service environment set SERVER_MODE=LIVE_SERVER, retain
   the intended growup_prod credentials, and keep RESEND_TRACKING_ENABLED=0,
   RESEND_POLL_ENABLED=0 and all provider/spending approvals disabled. Remove the
   retired placeholder RESEND_PUBLIC_CALLBACK_URL or replace it with exactly
   https://growup.securecodeusa.com/integrations/resend/webhook. Preserve the
   existing action mode and workflow enablement unless separately approved.
4. Run the focused server/readiness/signature checks in the disposable gate
   database, never against growup_prod. Inspect python -B -m app.server_target
   and python -B -m app.resend_readiness under the service environment without
   displaying secrets. Run backend health/readiness on 127.0.0.1:8010 and verify
   the POST webhook route exists in the candidate's OpenAPI specification.

## Proposed Nginx exception — exact path only

After code acceptance, add this location inside the existing HTTPS server block.
Keep its server-level auth_basic, certificate, ACME and ordinary location / intact.
Do not expose enrollment, Wondrue commands, docs or the rest of /integrations/.

```nginx
location = /integrations/resend/webhook {
    auth_basic off;
    limit_except POST { deny all; }
    client_max_body_size 32k;
    proxy_pass http://127.0.0.1:8010;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_connect_timeout 5s;
    proxy_read_timeout 60s;
}
```

Nginx's auth_basic off cancels inherited Basic Authentication for that location:
https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html . Application-level
raw-body Svix signature, timestamp, bounded body, event identity, enrollment,
deduplication and correlation checks remain mandatory:
https://resend.com/docs/webhooks/verify-webhooks-requests .

5. Validate nginx -t before reload. Check through public Cloudflare HTTPS:
   ordinary pages and private enrollment still require preview authentication;
   GET on the exact callback is refused; an unsigned POST reaches the application
   and returns its disabled-integration JSON while tracking remains off. Confirm
   no Cloudflare login/challenge intercepts this exact machine endpoint. Any edge
   exception must be limited to this route, with signatures still enforced.
6. STOP before registering a Resend callback. Obtain the separate activation
   checkpoint. Later, install the real endpoint-specific signing secret privately,
   enable tracking deliberately, and require unsigned/invalid signatures to
   return 401 before dispatch. Do not print secrets or use synthetic events to
   mutate production campaigns. A correct callback URL is not reachability proof.
7. Only after activation approval, register the exact HTTPS callback, select
   supported event types, and conduct an independently bounded owner-mailbox test.
   Receiving domain, per-message enrollment and sender matching remain required.
   This does not implement the still-missing outbound sender or Solomon transport.
8. Rollback on failure: disable tracking, restore the reviewed Nginx file and
   validate/reload it, restore the prior release/service configuration and verify
   health. Never erase receipts/events, reverse migrations casually or assume
   symlink rollback reverses a schema migration. Use the reviewed backup plan.

## Completion evidence required later

Exact deployed commit, schema/migration disposition, nonsecret server/data/action
profile, health/readiness, private-page protection, public endpoint rejection
tests, correct signature tests, retained event replay evidence, rollback point,
and a statement of which provider calls or deliveries were actually authorized.


## Verified deployment checkpoint (2026-11-04)

The new GitHub-based live launcher successfully installed the reviewed application
on srv1577410, configured LIVE_SERVER, checked an empty pending-migration list
without production SQL writes, preserved a custom-format growup_prod backup with
a verified SHA256 and retained its previous release/environment rollback point.
Health and readiness passed on 127.0.0.1:8010. The exact Nginx callback exception
was installed after nginx -t validation; no other site's routing was modified.

Public HTTPS verification through Cloudflare found: / and /docs return Basic
Authentication 401; /integrations/resend/enroll and /wondrue/command still return
Basic Authentication 401; GET on the exact callback returns method-restricted 403;
POST on the exact callback reaches application JSON resend_tracking_disabled
(404, no Basic Authentication challenge). A trailing-slash callback remains
protected. Thus routing is established, but event processing remains disabled.

Resend registration, actual signing secret, actual receiving domain, enabled event
tracking and bounded end-to-end provider proof are still pending. No outbound
sender or Solomon live-mail transport was added by this deployment task.
