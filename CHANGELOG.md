# Changelog

Tutte le modifiche rilevanti sono documentate in questo file.
Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/) e il
progetto adotta [Semantic Versioning](https://semver.org/lang/it/).

## [1.1.1] - 2026-11-07

### Added

- Workflow `stable-tag.yml` che sposta il tag mobile `stable` sull'ultima
  release non prerelease.

### Changed

- `login` mostra un output a sezioni con istruzioni generiche per collegare il
  server al client MCP o all'agente AI, senza elencare singoli client.
- Il JSON MCP generato usa il canale `@stable` e spiega che il percorso è
  specifico della macchina.
- README: installazione e comandi Classroom distinguono `uvx` (senza
  installazione permanente) da `uv tool install`, con indicazioni su dove
  finiscono i file.

## [1.1.0] - 2026-11-07

### Added

- Modalità `CLASSEVIVA_READ_ONLY_MODE` con alias retrocompatibile
  `CLASSEVIVA_PUBLIC_MODE`.
- Comando `classeviva-mcp set-classroom-url` per salvare il link `.ics` senza
  passarlo dalla chat dell'agente.
- Opzione `classeviva-mcp logout --purge` per rimuovere anche contesto studente
  e cache allegati.
- Opzioni `--version`, `--verbose` e `--allow-insecure-plaintext` alla CLI.
- Test di sicurezza per SSRF, privacy/read-only, credenziali, risorse e
  circolari.
- Workflow GitHub Actions per test, lint, type-check e CodeQL.
- Documenti `SECURITY.md`, `PRIVACY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`.
- Configurazione `ruff`, `mypy`, `pytest`, coverage e `py.typed`.

### Changed

- `READ_ONLY_MODE` non registra più i tool personali e di scrittura e fa
  sollevare `PermissionError` alle funzioni corrispondenti.
- In read-only il profilo e il contesto studente sono minimizzati (niente nome,
  cognome, identificativi, numero di registro, note o ID circolari).
- `read_notice_pdf` espone `allow_mark_as_read` e riporta
  `notice_marked_as_read` / `notice_was_unread`; in read-only forza
  `allow_mark_as_read=False` e `save_pdf=False`.
- Il link iCal non è più accettato da `update_student_context` e non è più
  presente in `StudentContext`.
- `session_cache.json` contiene solo `token`, `ident`, `student_id`.
- Il fallback credenziali su Windows usa DPAPI; su Linux/macOS senza Keyring il
  salvataggio fallisce in modo esplicito salvo opt-in.
- Scrittura atomica dei file con permessi `0600` dove supportati.
- Redirect HTTP disabilitati per l'API Spaggiari; redirect degli allegati
  consentiti solo verso `*.spaggiari.eu`.
- Il download dal CDN Spaggiari avviene senza header di autenticazione.
- `logout` conserva orario e note per default; `--purge` cancella tutto.
- `login` e `doctor` non stampano più nome, cognome o identificativo completo.

### Fixed

- SSRF sul feed iCal: allowlist Google, solo HTTPS/443, blocco IP
  privati/loopback/link-local, redirect rivalidati, cap 5 MB,
  `trust_env=False`.
- Segreti rimossi da `student_context.json` con migrazione automatica nel
  Keyring.
- Limiti risorse: allegati 20 MB, PDF 50 pagine, XML DOCX 10 MB, XLSX
  500×50×20, ZIP 15 file / 25 MB / profondità 1.
- Errori di cancellazione dal Keyring ora segnalati da `logout`.
- Gestione robusta di `attachNum` non numerici.
- `.gitignore` non esclude più `uv.lock` e non ignora più allegati generici
  (`*.pdf`, `*.docx`, `*.xlsx`, `*.png`) che potrebbero essere fixture.

### Removed

- Cartella duplicata `classeviva-mcp/` dal repository.
- Dipendenza implicita da URL iCal nel contesto in chiaro.

## [1.0.0] - 2026-11-07

- Prima release pubblica del connettore MCP (`Release v1.0 classeviva-mcp`).
