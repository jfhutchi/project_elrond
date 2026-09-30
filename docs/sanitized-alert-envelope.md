# Sanitized alert envelope prerequisite (#253)

`quantbot.operations.alert_delivery` implements a pure encoder and consumer filter.
It does not send comments, install a dispatcher, create an automation, acknowledge
an alert or change delivery status. External delivery remains NOT_CONFIGURED.

The proposed route is a comment on the private `jfhutchi/project_elrond` PR #255,
fetched through the existing authorized GitHub connector, then delivered to the
approved Elrond chat. Repository, PR, visibility and human author must be verified
from fetched GitHub metadata, not from the webhook hint or comment body.
`accept_comment` enforces these exact pins. It is a filter, not authentication.

The fixed `ELROND_ALERT_V1` payload permits only version, random UUID4 delivery ID,
event kind, reviewed alert code, severity, static subsystem/category/action and
canonical UTC first-seen/emitted timestamps. Runtime action text, account values,
holdings, symbols, orders, machine paths, logs, secrets and incident identifiers
are excluded. Unknown codes, extra/duplicate fields, tampering and malformed
inputs fail with a fixed refusal message. Calendar waits and insufficient evidence
must never be recast as an approval request; this catalog does not export them.

A future dispatcher must persist a random delivery ID and the exact encoded
envelope before transmission, reuse both on retry and validate every consumer
input. Encoding or acknowledgement never proves delivery or measured recovery.
Synthetic canary/digest aggregation, durable retry/deduplication and confirmed
delivery are still unfinished acceptance work; no canary has been sent.

Explicit approval is required before runtime data transit through GitHub, a
persistent dispatcher or chat automation is installed. No new credentials or
permissions are provisioned here. Any authentication/access expansion is a
separate decision. Consumer filtering alone cannot authenticate a sender.
