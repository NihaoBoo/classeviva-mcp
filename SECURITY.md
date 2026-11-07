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

## Security model (v1.1.0)

`classeviva-mcp` is a **single-account local connector**. It does not authenticate
MCP callers and is not a multi-user service. Do not expose it to untrusted users,
public networks, shared group chats, or the open Internet.

### Credentials

- Primary storage: the OS keyring (Windows Credential Manager, macOS Keychain,
  Linux Secret Service).
- Windows fallback: DPAPI-encrypted file bound to the current user profile.
- Linux/macOS without a keyring: saving fails closed unless the user explicitly
  passes `--allow-insecure-plaintext`.
- Files are written atomically with `0600` permissions where the platform
  supports them.
- `session_cache.json` is minimised to `token`, `ident`, `student_id`.
- The Google Classroom iCal URL is stored in the keyring/encrypted fallback and
  is migrated out of `student_context.json` on load.

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
server therefore exposes `allow_mark_as_read` (default `True`) and reports
`notice_marked_as_read` / `notice_was_unread`. With `allow_mark_as_read=False`
on an unread notice, only metadata is returned and the remote state is not
changed. `list_notices` never calls the read endpoint.

### Resource limits

| Resource | Limit |
| -------- | ----- |
| Attachment download | 20 MB |
| iCal feed | 5 MB |
| PDF pages parsed | 50 |
| DOCX `word/document.xml` | 10 MB |
| ZIP entries | 15 |
| ZIP uncompressed total | 25 MB |
| ZIP nesting depth | 1 |
| XLSX rows / columns / sheets | 500 / 50 / 20 |
| `custom_notes` | 1,500 chars |
| `seen_notice_ids` | 200 (FIFO) |
| Saved attachment files | 30 (rotation) |

### Logout

- `classeviva-mcp logout` removes credentials and the session cache, but keeps
  the timetable and notes.
- `classeviva-mcp logout --purge` also removes `student_context.json` and the
  attachment cache.

## Hardening checklist for users

- Use the tool only on a device and account you control.
- Prefer the OS keyring; avoid `--allow-insecure-plaintext`.
- Never paste the Classroom `.ics` URL into an AI chat; use
  `classeviva-mcp set-classroom-url`.
- Keep `READ_ONLY_MODE` on when the agent does not need grades or writes.
- Run `doctor` before sharing logs; it does not print names or full identifiers.
- Rotate the Classroom secret address if it may have been exposed.
