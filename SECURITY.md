# Security policy

## Local trust boundary

The desktop UI calls `ToolService` in-process.  The optional Web compatibility
server binds only to `localhost` or the IPv4 loopback range (`127/8`); IPv6 binding is
intentionally unsupported in this release.  Each process creates a random API
token and prints it only in the launch URL fragment.  The browser moves that
token to session storage, removes the fragment, and sends the token in the
`X-ISF-Token` header.  API requests without it are rejected.  Requests also
require a literal loopback `Host`; when a browser sends an `Origin`, writes
require it to match the exact HTTP listener.  The token is a local
cross-process safety boundary, not permission to expose the server through an
untrusted proxy.

The normal UI executes real Cadence child processes or explicitly configured
custom adapters. Clicking the clearly labelled environment-check/start/resume
action grants authority for that request, without a separate Live selector or
authorization checkbox. Authority is deliberately non-persistent: loading,
saving or binding a configuration never grants execution permission. The
backend still rejects unauthorized execution. Read-only inspection does not
source a Cadence environment script or execute configured probes; the explicit
execution action authorizes those short probes immediately before execution.
Keep workspaces, profiles, credentials, license paths, and simulator artifacts
outside a public checkout.

Normal new-run start and explicit old-job resume have no second confirmation
dialog. Clicking resume authorizes that request with the job's frozen
configuration; backend ownership and execution-contract checks still apply.
Jobs are never automatically resumed, and reusable execution authorization is
never saved.

## Reporting

Please report vulnerabilities privately through the repository's security
advisory feature rather than in a public issue.  Do not include PDK, OA, model,
netlist, RDB, PSF, license or internal path data in any report; send a minimal
redacted description.
