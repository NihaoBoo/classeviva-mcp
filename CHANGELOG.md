# Changelog

Tutte le modifiche rilevanti sono documentate in questo file.
Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/) e il
progetto adotta [Semantic Versioning](https://semver.org/lang/it/).

## [1.1.3] - 2026-11-08

### Fixed

- **Fail-open sullo stato di lettura delle circolari.** `read_notice_pdf` con
  `allow_mark_as_read=False` poteva comunque raggiungere l'endpoint
  `noticeboard/read` se l'elenco della bacheca non era disponibile o la
  circolare non era presente in elenco: la circolare veniva segnata come letta
  sul registro senza consenso. Ora lo stato di lettura è verificato con un
  elenco **sempre aggiornato** (mai dalla cache) e in caso di dubbio si
  fallisce chiusi: solo metadati, nessuna ricevuta di lettura, nessun
  download. Il nuovo campo `notice_read_state_known` indica il caso.
- **`login` non salvava più password non verificate.** Una password sbagliata
  finiva nel Keyring prima che il login venisse validato. Ora le credenziali
  vengono prima verificate contro Spaggiari e solo dopo salvate; se il login
  fallisce non resta nulla nel Keyring né nel file di fallback.
- **`login` non chiede più la password se non esiste un archivio sicuro.**
  Viene verificata prima la disponibilità del Keyring (o del fallback DPAPI),
  così su una macchina senza keyring l'errore arriva subito.

### Changed

- Connessione HTTP riutilizzata: il client mantiene vivo il pool TLS tra una
  chiamata tool e l'altra, eliminando un handshake TLS per ogni richiesta.
- Cache breve (60 s) per gli endpoint di sola lettura (voti, agenda, lezioni,
  assenze, materie, profilo, bacheca): i workflow avanzati non interrogano più
  Spaggiari due volte per gli stessi dati. La cache viene svuotata dopo ogni
  modifica di stato remoto.
- Il prompt `notices_brief` è disponibile anche in modalità personale (non solo
  read-only): è il prompt che chiede conferma prima di segnare una circolare
  come letta.
- `probe_storage` controlla il backend disponibile senza persistere nulla.

## [1.1.2] - 2026-11-07

### Added

- Sezione [SECURITY.md](SECURITY.md#computer-pubblici-o-condivisi) e callout
  README sull'uso su computer scolastici o condivisi: il Keyring protegge dagli
  altri account del sistema, non da chi usa lo stesso account.
- Comandi `logout --purge` documentati per la pulizia su PC non personali, con
  indicazione che non richiedono privilegi di amministratore.
- `doctor` mostra la cartella dati e indica se `session_cache.json` è presente.
- Test per default `allow_mark_as_read=False`, selezione di un solo allegato e
  limite totale di byte per circolare.

### Changed

- `read_notice_pdf` usa `allow_mark_as_read=False` come default: una circolare
  nuova non viene più segnata come letta senza consenso esplicito. I prompt
  `setup` e `notices_brief` chiedono conferma allo studente.
- `attachment_index` viene applicato prima del download: viene scaricato solo
  l'allegato richiesto invece di tutti.
- Download di allegati e feed iCal in streaming, senza caricare il corpo in
  memoria prima di applicare il limite. Nuovo limite complessivo di 40 MB per
  circolare, oltre al limite di 20 MB per singolo allegato.
- `set-classroom-url` usa input nascosto (`getpass`) e avvisa se il link viene
  passato come argomento, perché resterebbe nella cronologia del terminale.
- `login` ricorda di eseguire `logout --purge` su computer condivisi; `logout`
  mostra la cartella dati e segnala che le variabili d'ambiente non vengono
  rimosse.
- `stable-tag.yml` passa i tag tramite variabili d'ambiente, li valida, verifica
  che esistano nel repository e limita il rerun manuale al proprietario.
- `LICENSE` contiene solo il testo MIT; il disclaimer è stato spostato in
  `NOTICE.md` per far riconoscere la licenza a GitHub.
- `PRIVACY.md` descrive anche il caso `GOOGLE_ACCESS_TOKEN` da variabile
  d'ambiente e la pulizia su computer condivisi.
- Classifier di sviluppo allineato a `5 - Production/Stable`.

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
