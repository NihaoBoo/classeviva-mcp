# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.1.x   | ✅ |
| < 1.1   | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

- Open a private GitHub Security Advisory:
  <https://github.com/NihaoBoo/classeviva-mcp/security/advisories/new>
- Include: affected version, steps to reproduce, impact, and any suggested fix.
- Do not include real credentials, student data, `.ics` secret URLs, or tokens.

You will receive an acknowledgement as soon as possible. Please allow reasonable
time for a fix before public disclosure.

## Security model (v1.2.0)

`classeviva-mcp` is a **single-account local connector**. It does not authenticate
MCP callers and is not a multi-user service. Do not expose it to untrusted users,
public networks, shared group chats, or the open Internet.

### Credentials

- Primary storage: the OS keyring (Windows Credential Manager, macOS Keychain,
  Linux Secret Service).
- Windows fallback: DPAPI-encrypted file bound to the current user profile.
- Linux/macOS without a keyring: saving fails closed unless the user explicitly
  passes `--allow-insecure-plaintext`.
- `login` verifies the password against Spaggiari **before** writing anything to
  the keyring or to disk. A wrong password is never stored.
- `login` probes the available storage backend before asking for the password,
  so a machine without a usable keyring fails immediately instead of after the
  user has typed their password.
- Files are written atomically with `0600` permissions where the platform
  supports them.
- `session_cache.json` is minimised to `token`, `ident`, `student_id`.
- The Google Classroom iCal URL is stored in the keyring/encrypted fallback and
  is migrated out of `student_context.json` on load.
- `GOOGLE_ACCESS_TOKEN`, `CLASSEVIVA_USER`, `CLASSEVIVA_PASSWORD`,
  `CLASSROOM_ICAL_URL` and `CLASSEVIVA_PROFILE` are read from the environment at
  runtime and are never written to disk by the program. `logout` does not clear
  them; remove them from the shell or client configuration manually.

### Network (SSRF)

- The Classroom iCal URL is validated against an allowlist:
  `calendar.google.com` and `classroom.google.com`.
- Only `https` on port 443 is accepted.
- Hosts resolving to private, loopback, link-local, multicast, or reserved
  addresses are rejected.
- Redirects are followed manually and revalidated (max 3).
- The response body is capped at 5 MB and `trust_env=False` avoids ambient
  proxy settings.
- Spaggiari API redirects are rejected.
- Attachment redirects are allowed only to `*.spaggiari.eu`; the signed CDN
  URL is fetched without Spaggiari auth headers and capped at 20 MB.

### Read-only mode

`CLASSEVIVA_READ_ONLY_MODE=true` (alias: `CLASSEVIVA_PUBLIC_MODE=true`):

- does not register personal or write tools;
- makes those functions raise `PermissionError`;
- minimises the profile and student context returned to the agent;
- forces `allow_mark_as_read=False` and `save_pdf=False` in `read_notice_pdf`;
- never mutates local notice state.

This is **not** an authentication or multi-tenant isolation mechanism.

### Notices

Spaggiari returns the full notice text and attachments only after the
`noticeboard/read` endpoint is called, which marks the notice as read. The
server therefore exposes `allow_mark_as_read` (**default `False`**) and reports
`notice_marked_as_read` / `notice_was_unread`. With the default on an unread
notice, only metadata is returned and the remote state is not changed; pass
`allow_mark_as_read=True` only after the user has been informed and has agreed.
The built-in `setup` and `notices_brief` prompts ask for confirmation first.
`list_notices` never calls the read endpoint.

The read state is verified against a **fresh** noticeboard listing (never from
the cache). If the listing fails or the notice is not in it, the read state is
unknown and the server **fails closed**: metadata only, no read receipt, no
attachment download. `notice_read_state_known` reports which case occurred.

### Resource limits

| Resource | Limit |
| -------- | ----- |
| Attachment download (streamed) | 20 MB per attachment |
| Total attachments per notice (streamed) | 40 MB |
| iCal feed (streamed) | 5 MB |
| PDF pages parsed | 50 |
| DOCX `word/document.xml` | 10 MB |
| ZIP entries | 15 |
| ZIP uncompressed total | 25 MB |
| ZIP nesting depth | 1 |
| XLSX rows / columns / sheets | 500 / 50 / 20 |
| `custom_notes` | 1,500 chars |
| `seen_notice_ids` | 200 (FIFO) |
| Saved attachment files | 30 (rotation) |
| Read-only response cache | 60 s TTL, dropped on any state change |

### Logout

- `classeviva-mcp logout` removes credentials and the session cache, but keeps
  the timetable and notes.
- `classeviva-mcp logout --purge` also removes `student_context.json` and the
  attachment cache.
- Neither command requires administrator privileges.
- `logout` removes local data only: it does not revoke the Spaggiari session
  token server-side. If the token may have been copied, change the ClasseViva
  password from another device.
- Environment variables are not cleared by `logout`.

### Computer pubblici o condivisi

`classeviva-mcp` è un connettore locale per **un solo account**. Il Keyring
protegge i dati dagli altri utenti del sistema operativo, **non** da chi usa lo
stesso account.

- Su un PC con **account condiviso** (es. `studente`, `lab`, `aula`), chiunque
  usi quell'account può aprire il Keyring e leggere le credenziali. Due
  studenti che eseguono `login` si sovrascrivono a vicenda, perché il server è
  single-account.
- Su un PC con **account personale** gli altri studenti non possono leggere il
  Keyring. Un amministratore della macchina può comunque accedere al profilo o
  reimpostare l'account: il Keyring non è una protezione contro l'admin.
- Non usare `--allow-insecure-plaintext` su un computer pubblico: credenziali e
  sessione finirebbero in chiaro su disco.
- `CLASSEVIVA_READ_ONLY_MODE=true` riduce i dati e disabilita le scritture, ma
  non protegge le credenziali e non isola più utenti.

**Prima di lasciare un computer non tuo, esegui `logout --purge`.** Il comando
non richiede privilegi di amministratore e rimuove le voci del Keyring,
`session_cache.json`, `student_context.json` e la cartella `attachments/`.
Cancellare a mano solo le voci del Keyring **non basta**: `session_cache.json`
contiene il token di sessione e resta fino a `logout`. Su Windows è cifrato con
DPAPI; su Linux/macOS con Keyring non viene creato; con
`--allow-insecure-plaintext` è in chiaro.

Se usi variabili d'ambiente, rimuovile anche dalla shell o dalla configurazione
del client, perché `logout` non le tocca.

### Transport

The default transport is **stdio**: the MCP client spawns the server as a child
process and talks to it over pipes. No port is opened and no network listener
exists, so the server is reachable only by whoever can run a process as that
user.

The optional `streamable-http` transport opens a TCP port and is therefore
**always protected by a bearer token**:

- the token is required in the `Authorization: Bearer <token>` header; without
  it the server answers `401 Unauthorized` and runs nothing;
- the comparison is constant-time (`hmac.compare_digest`);
- the middleware is implemented at the ASGI level, so it never interferes with
  the SSE streaming of the transport;
- the token is resolved from `--token-file`, then `CLASSEVIVA_MCP_TOKEN`, then
  generated once and stored as `mcp_token.json` in the data directory
  (DPAPI-encrypted on Windows, `0600` on Linux/macOS);
- the server binds to `127.0.0.1` by default and prints a loud warning when it
  is asked to bind to a routable address.

The transport is **single-tenant**: whoever holds the token can read the whole
registry. The traffic is **not encrypted** — over anything but loopback, use a
trusted private network or a tunnel (Tailscale, SSH). This is not a
multi-user system and does not replace per-caller authorization.

### Resources

MCP resources are read-only documents addressed by URI
(`classeviva://profile`, `classeviva://grades`, ...). They expose exactly the
same data as the corresponding tools, follow the same `READ_ONLY_MODE`
rules (the personal ones are not registered in read-only mode), and never
contain secrets such as the iCal URL or the OAuth token.

## Hardening checklist for users

- Use the tool only on a device and account you control. Avoid school or shared
  computers with a common account; if you must, run `logout --purge` before
  leaving (see [Computer pubblici o condivisi](#computer-pubblici-o-condivisi)).
- Prefer the OS keyring; avoid `--allow-insecure-plaintext`.
- Never paste the Classroom `.ics` URL into an AI chat; use
  `classeviva-mcp set-classroom-url`.
- Keep `READ_ONLY_MODE` on when the agent does not need grades or writes.
- Run `doctor` before sharing logs; it does not print names or full identifiers.
- Rotate the Classroom secret address if it may have been exposed.
