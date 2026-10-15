# Dashboard access and exposure

The October 15 paper desk refresh is running at [the local Omen console](http://127.0.0.1:8787/).
It uses horizontal navigation, a compact account line and a positions ledger. Research and
system routes remain available through their menus. Operational warnings are visible by
default; the manual stop remains prominent. Calendar and learner charts share the light palette.

The overview reads only the configured account without exposing its identifier. It distinguishes
missing inventory from a known flat account. Heartbeats must match the current executable
binding before appearing as reporting. Existing same-origin controls and gates are unchanged.

The console runs from its own pinned native checkout. Its availability is not a dependency
for the pending core handover; task and process provenance remain checked. The trading
release, configuration and original pending exit are unchanged. See the [UI handover
evidence](evidence/paper-core/dashboard-2026-10-15/README.md).

The dated access observations below remain historical.

> **Current core boundary — October 14, 2026.** The [PAPER core](paper-core.md) dashboard/exporter is optional and read-only; entry, guardian and outcome progress must continue independently. The existing local console is a legacy deployed view, not proof that the new roles have started. Their [current deployment package](evidence/paper-core/omen-deployment/README.md) records disabled tasks and pending commissioning. A page view cannot migrate, commission or activate the replacement. The access observations below retain their original dates.

The Omen dashboard is available **on the Omen** at
http://127.0.0.1:8787/ . Experiment views use /experiments and
/experiments/adaptive-sector-raw-20261006-v1; /learner shows the existing learner,
and the calendar uses the durable daily-close reader. Unknown data remains
unknown; selecting a page never trains or activates a model.

## Current access boundary

The October 6 access check found the listener bound to 127.0.0.1:8787 only.
An unauthenticated local GET returned HTTP 200. No active SSH (22) or Remote
Desktop (3389) listener was found. These are bounded observations, not a claim
about the Alienware's power, network connectivity or credentials.

Typing that loopback address on the Alienware addresses the Alienware itself,
not the Omen. There is currently no verified Alienware-accessible dashboard URL.
Do not substitute the Omen's LAN address or bind the service to 0.0.0.0 as a fix.

The console includes operator write controls as well as read-only pages. Local
same-origin checks and readiness/approval gates do not constitute remote user
authentication. A VPN alone would not make an unauthenticated dashboard safe to
expose. Do not assume a reverse proxy preserves the local-origin restrictions.

## Minimum proposed safe remote option

Keep the dashboard loopback-only and use an authenticated, explicitly authorized
remote-desktop session to view the Omen browser, or a narrowly scoped authenticated
tunnel forwarding only the loopback dashboard. First verify an existing approved
service, account/access method, allowed clients and network path. If none exists,
installation, persistent access, keys/accounts, firewall or network changes need
specific operator approval and a rollback plan; this documentation grants none.

For a tunnel, expose only the Alienware's local loopback forwarding port, not a
public or LAN-wide listener. Review which read/write console routes should be
available and their authentication/origin behavior before commissioning. Verify
from the actual authorized Alienware session, then provide its measured local
URL. Until that test passes, no remote-access success is claimed.

Repository integration never changes dashboard binding, model activation,
credentials, Windows security settings or the existing alert webhook.
