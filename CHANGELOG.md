# Changelog

Tutte le modifiche rilevanti sono documentate in questo file.
Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/) e il
progetto adotta [Semantic Versioning](https://semver.org/lang/it/).

## [1.4.0] - 2026-11-09

### Added

- **Didattica / Materiali-Compiti**: `list_didactics` (cartelle, materia, docente, file, con filtri)
  e `download_didactics_item` (scarica un file con permessi `0o600` ed estrae il testo da
  PDF/DOCX/XLSX/TXT/CSV). Tollera il refuso upstream `didacticts`.
- **`get_calendar`**: giorni di vacanza, festività, patrono e ponti deliberati dalla scuola
  (giorni `HD`) nei prossimi giorni, più i periodi dell'anno scolastico.
- **`get_notes`**: note disciplinari, richiami e sanzioni (`NTTE`, `NTCL`, `NTWN`, `NTST`) con
  testo completo.
- **`get_documents`**: elenco di pagelle e documenti ufficiali di scrutinio.
- **`get_schoolbooks`**: libri adottati con ISBN, autori, editore, prezzo e obbligatorio/consigliato.
- **Prompt MCP** `recap-settimanale` e `piano-recupero-materie`.
- **`get_snapshot`** include ora prossimi giorni di vacanza/ponti e novità in Didattica.
  Le note disciplinari restano escluse di proposito.
- README: tabella tool aggiornata e guida al collaudo live sul proprio account.

### Changed

- `read_notice_pdf`: se l'allegato è bloccato perché la circolare non è mai stata aperta,
  con `allow_mark_as_read=True` la circolare viene segnata come letta, l'allegato scaricato e
  la risposta riporta `notice_marked_as_read: true`. Senza consenso esplicito non cambia nulla.
- Tutti i nuovi tool dati personali non vengono registrati in `CLASSEVIVA_READ_ONLY_MODE`.

### Tests

- 198 test (73 nuovi) con risposte Spaggiari simulate offline.

## [1.3.0] - 2026-11-09

### Fixed

- **JSON MCP non più legato alla cache di uv**: eseguendo `uvx classeviva-mcp login`
  il JSON stampato puntava a `...\uv\cache\archive-v0\<hash>\Scripts\classeviva-mcp.EXE`,
  un ambiente temporaneo che uv elimina o rigenera (pulizia cache, nuova versione,
  profili scolastici ripuliti al logoff). Ora l'ambiente temporaneo viene riconosciuto
  e il JSON usa `uvx classeviva-mcp`; con `uv tool install` usa lo shim stabile.
- **Reti scolastiche**: i client HTTP ignoravano il proxy di sistema
  (`trust_env=False`) e verificavano solo contro `certifi`, quindi dietro un proxy
  o un filtro HTTPS con certificato della scuola ogni richiesta falliva. Ora si usano
  proxy e certificati di sistema (`truststore`), con opt-out `CLASSEVIVA_PROXY=off`
  e `CLASSEVIVA_TLS=certifi`.
- **Errori di login comprensibili**: credenziali rifiutate (es. codice `251`),
  pagina HTML di un filtro web, errori TLS/proxy/timeout e 5xx producono messaggi
  distinti con suggerimenti (codice `S...` al posto dell'email, attesa dopo troppi
  tentativi, accesso solo Google/SPID).
- **Niente più blocco dell'account per tentativi ripetuti**: dopo una password
  rifiutata il server sospende per 10 minuti i nuovi login con le stesse credenziali.
- **`get_snapshot`** non segnala più "Classroom non disponibile" quando Classroom
  non è configurato.

### Added

- `classeviva-mcp config [--client generic|opencode|vscode|codex|claude-code]`
  stampa la configurazione MCP senza rifare il login; `login --client` idem.
- `doctor` mostra versione, metodo di avvio consigliato, proxy/TLS in uso, tipo di
  identificativo (email o codice) e se le variabili d'ambiente hanno la precedenza.
- `login` rifiuta l'esecuzione non interattiva (da agente AI), rileva caratteri di
  controllo incollati nella password (`^V`) e ricorda che le credenziali precedenti
  restano attive se la nuova password è errata.

## [1.2.1] - 2026-11-08

### Fixed

- **Filtro PDF per classe, non per pagina**: i tabelloni orario impilano più
  classi nella stessa pagina (es. 5AI in alto e 5AL in basso). Il filtro
  `filter_keyword` teneva l'intera pagina, restituendo anche i docenti e le aule
  delle altre classi. Ora ritaglia la sola griglia della classe richiesta
  (riconoscendo l'intestazione `5 AI - Settimana …`): su un tabellone reale il
  testo passa da 5.347 a 1.131 caratteri, con **92% di risparmio** e nessun dato
  estraneo.
- **Materia dell'agenda presa dal nome del docente**: quando Spaggiari non
  invia `subjectDesc`, il client usava `authorName` come materia, duplicando il
  docente (già presente in `author`) e inducendo l'agente a leggere un nome di
  persona come materia. Ora `subject` resta vuoto e il docente vive solo in
  `author`.
- **Google Classroom non fallisce più in silenzio**: se il feed `.ics` o
  l'accesso OAuth non rispondono, `get_homework` restituisce i risultati parziali
  e un `warnings` esplicito (link scaduto, HTTP 404/410, feed non valido) invece
  di un elenco vuoto indistinguibile da "nessun compito".
- **Elenco materie non più inghiottito**: se l'API materie fallisce,
  `get_today_context` mantiene il resto del contesto e aggiunge un warning, così
  un elenco vuoto non viene scambiato per "nessuna materia".
- **`saved_pdf_path` punta solo a un PDF**: se la circolare contiene anche
  immagini o altri allegati, il campo non restituisce più il primo file
  qualsiasi.

### Added

- **Risposte strutturate per modelli deboli**: `get_homework` espone
  `breakdown` e `summary`; `get_absences` espone `justified_count`,
  `unjustified_count`, `breakdown_by_kind` e `summary`; `get_grades` espone
  `average_method` e `summary`; `calculate_grade_target` espone `found`,
  `achievable_with_one_exam` e `max_grade`.
- **`get_snapshot` propaga i warning** e segnala la sovrapposizione tra
  `homework` e `agenda` (`also_in_agenda`, `includes_homework`), evitando che
  l'agente conti due volte gli stessi impegni.
- **`list_notices` distingue tre stati**: `🆕 NUOVA`, `👀 GIÀ MOSTRATA ALL'AI`
  (vista in una lista precedente, non letta sul registro) e
  `📌 GIÀ LETTA SUL REGISTRO`. Aggiunge `total_available`, `truncated`,
  `unread_available` e `summary`: `count` non viene più confuso con il totale.
- **`read_notice_pdf` espone `notice_title` e `attachments_total`**, così
  l'agente sa quante risorse esistono anche quando ne legge una sola.

### Changed

- **Validazione degli input locali**: `update_student_context` normalizza la
  classe (`5 AI` → `5AI`) e limita le note a 1.500 caratteri;
  `save_class_timetable` rifiuta orari non `HH:MM`, ore duplicate nello stesso
  giorno, classi diverse da quella salvata e caratteri CJK/cinesi inventati
  dall'AI. L'orario resta **solo in locale** e la risposta lo dichiara con
  `storage: "local_only"`.
- **`get_agenda` e `get_lessons`** validano le date (`YYYY-MM-DD`) con fallback
  e warning invece di restituire un HTTP 404 grezzo; `kind` accetta anche i
  sinonimi italiani (`verifiche`, `compiti`, `interrogazioni`, `note`).
- **`get_grades`**: `overall_average` vale `null` quando non ci sono voti (non
  più `0.0`, che sembrava una media reale).
- **`calculate_grade_target`**: se la materia non ha voti, `found` è `false` e
  `required_next_grade` è `null`, non un voto inventato.
- **Istruzioni agente aggiornate**: l'orario e le note sono descritti come dati
  privati locali, le assenze da giustificare sono `unjustified_count` (non
  `total_events`) e i `warnings` vanno riportati allo studente.

## [1.2.0] - 2026-11-08

### Added

- **Tool `get_snapshot`**: profilo, contesto, materie, voti con medie, assenze
  da giustificare, compiti e agenda in **una sola chiamata**. Le cinque
  richieste di rete partono in parallelo: un `daily_brief` + `recovery_plan`
  passano da 6-8 round-trip a 1.
- **Risorse MCP**: dataset in sola lettura indirizzati da URI
  (`classeviva://profile`, `//subjects`, `//notices`, `//grades`, `//absences`,
  `//homework/{days}`, `//agenda/{start}/{end}`, `//lessons/{start}/{end}`), che
  il client può scaricare una volta e riusare senza ripetere la chiamata.
- **Trasporto `streamable-http` protetto da bearer token** per uso remoto
  (VPS, telefono, bot): `classeviva-mcp serve --transport streamable-http`.
  Token sempre obbligatorio, confronto in tempo costante, middleware a livello
  ASGI (non interferisce con lo streaming SSE), binding su `127.0.0.1` di
  default con avviso esplicito su indirizzi raggiungibili.

### Changed

- Il token del trasporto HTTP viene risolto da `--token-file`, poi dalla
  variabile `CLASSEVIVA_MCP_TOKEN`, poi generato e salvato in `mcp_token.json`
  (cifrato con DPAPI su Windows, `0600` su Linux/macOS).
- README: sezione dedicata a stdio vs HTTP, tabella delle risorse e del tool
  `get_snapshot`.

### Infra

- Ruleset `main-integrity`: divieto di force-push e di cancellazione di `main`,
  cronologia lineare obbligatoria.
- Ruleset `release-tag-creation` / `release-tag-immutability`: i tag `v*` non
  possono essere creati da altri e, una volta pubblicati, non possono essere
  spostati o cancellati. Il tag `stable` resta mobile per il workflow.
- Dependabot Security Updates abilitati.
- Firma SSH attiva per i commit e i tag locali.

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

## [1.1.3] - 2026-11-08

### Added

- Workflow `publish-pypi.yml` per la pubblicazione su PyPI tramite Trusted
  Publishing (OIDC, nessun token a lungo termine salvato in GitHub) con
  verifica dei metadati tramite `twine check`.
- URL di progetto (`Homepage`, `Repository`, `Issues`, `Changelog`, `Security`)
  nei metadati del pacchetto, così la pagina PyPI porta ai link giusti.

### Changed

- Action aggiornate: `actions/checkout@v7`, `actions/setup-python@v7`,
  `astral-sh/setup-uv@v10.3.3`, `github/codeql-action@v4` (CodeQL v3 va in
  deprecazione a dicembre 2026).
- README: sezione dedicata all'installazione da PyPI e badge del pacchetto.
- `classeviva-mcp` pubblicato su PyPI: da ora `uvx classeviva-mcp login`
  funziona senza `git` sul PC.

### Fixed

- Il workflow `publish-pypi.yml` resta manuale (`workflow_dispatch`) finché il
  Trusted Publisher non è registrato su PyPI, così una release non si
  espone a un upload fallito.

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
