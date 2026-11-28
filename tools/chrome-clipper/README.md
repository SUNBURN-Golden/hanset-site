# HANSET 자료 담기 (Chrome Manifest V3)

Load this directory as an unpacked extension in Chrome developer mode. Copy the
installed extension origin into the worker's external `extension_origins` registry
and run the [Linux worker](../../crawler/README.md). No key or credential is used.
The intake binds only `127.0.0.1:8766`, validates exact Host/extension Origin, JSON
type, fixed endpoints and a 64 KB size limit, and never enables wildcard CORS.

`이 자료 담기` requests the optional loopback permission, stores a sanitized URL
intent, and requests a worker reservation and begin approval **before reading the
body**. With no worker or no budget, no extraction occurs. It never reloads,
navigates, clicks page controls or fetches source URLs. After approval it reads
only visible `main`/`article` text and a short title, excludes form/private areas,
filters PII/injection locally, and stores only sanitized excerpts in
`chrome.storage.local`. The worker filters and validates them again before ACK.
The worker may extract fact fields using approved rules; the extension cannot
declare facts official, verify, or publish them.
The worker can accept sanitized public-tab clips from origins absent from its
network approval registry. They remain clues; clipping grants no unattended
access permission. Public membership restrictions are retained as facility facts,
while member-only conversations remain excluded.

`저장된 자료 재전송`, browser startup, and optional `alarms` replay saved payloads
with their original reservations. Replay requires no document read or body charge.
The queue removes each payload after the collector returns a matching committed
ACK or confirms that the reservation expired, was revoked, or was superseded.
Unconfirmed delivery or failed cleanup leaves the payload available for replay.
If service-worker exit makes extraction status ambiguous, resume can revoke the
old reservation and keeps its checkpoint without beginning or reading again.

`중단된 담기 새로 시작` first checks the active tab for a saved payload. If one
exists, the action returns its delivery outcome. Otherwise it starts a new
reservation only after the collector confirms that every prior reservation for
the tab has drained and the old checkpoints are removed. The document ID and URL
must match for the new extraction.

Required permissions are exactly `activeTab`, `scripting`, `storage`. There are no
required host grants, `webRequest`, `debugger`, or all-sites permissions. Optional
host grants are exact origins listed in `manifest.json`; the selector is derived
from that list. To add a reviewed venue origin, update the exact manifest entry,
reload, and separately approve it in the worker's local registry. The shipped
scientific API origins are permission options, not automatic access approval.

`선택 출처 권한 허용` requests that origin on a user gesture and records the grant
with the worker only after checking both Chrome permission and worker approval.
`현재 URL 작업기 등록` submits a public URL for the Linux runner, never navigates
the user's tab. After submit/resume a short-lived consent responder answers fresh
worker challenges with the current Chrome host permission before start and each
network request. Offline or terminated responders fail closed. Revoked permissions
also revoke associated worker authorization on reconciliation; reconnect and use
resume after an offline revocation. Chrome
permissions are consent gates, not a request firewall. Network isolation and all
unattended browser requests are controlled by the Linux runner; they cannot be
retroactively applied to the user's existing tab traffic.

Run `node --test tools/chrome-clipper/tests/*.test.mjs` from the repository root.
The mandatory real-Chrome lifecycle/network tests and Linux prerequisites are in
the [collector README](../../crawler/README.md#reports-and-tests).
