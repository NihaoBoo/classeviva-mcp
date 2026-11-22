# Privacy Policy / Informativa sul trattamento dei dati

`classeviva-mcp` è un connettore locale open source. **Non gestisce un server
centrale e non invia dati ai maintainer.** Si collega a Spaggiari e,
se configurato, a Google. I risultati vanno al client MCP scelto dall'utente,
che può inoltrarli a un provider AI o, se configurato, a un ponte esterno.

## Quali dati tratta

| Dato | Dove viene conservato | Finalità |
| ---- | --------------------- | -------- |
| Email / codice utente e password ClasseViva | Keyring di sistema o fallback cifrato DPAPI | Autenticazione a Spaggiari |
| Token di sessione Spaggiari, identificativi e generazione della sessione | RAM; `session_cache.json` solo con DPAPI o fallback legacy esplicito, mai per l'accesso da ambiente | Evitare login ripetuti |
| URL segreto `.ics` di Google Classroom | Keyring/fallback cifrato | Lettura compiti Classroom |
| Grant OAuth Google (opzionale) | Refresh token e configurazione necessaria in Keyring o DPAPI; access token solo in RAM | Lettura API Classroom |
| Classe, indirizzo, numero di registro, note, orario | `student_context.json` locale | Contesto per l'agente |
| ID delle circolari viste | `student_context.json` locale | Etichetta `NUOVA` / `GIÀ VISTA` |
| Allegati esportati nella cache | Cartelle locali `attachments/` e `didactics/` | Copie salvate su richiesta |
| Allegato in consegna | Risorsa MCP temporanea in RAM per un client autenticato | Consegna nativa tramite un ponte supportato |
| Voti, assenze, lezioni, agenda | Non persistiti: letti dall'API e restituiti all'agente | Risposta alle richieste |

Il token OAuth Google può essere fornito anche tramite la variabile d'ambiente
`GOOGLE_ACCESS_TOKEN`: in quel caso il programma lo legge a runtime e **non lo
salva** nei propri file. La variabile resta però nella shell o nella
configurazione del client, dove va rimossa manualmente. Lo stesso vale per
`CLASSEVIVA_USER`, `CLASSEVIVA_PASSWORD`, `CLASSROOM_ICAL_URL` e
`CLASSEVIVA_PROFILE`.

## Basi e finalità

Il trattamento avviene su richiesta dell'utente, per consentire all'agente AI
scelto dall'utente di leggere i dati del proprio registro elettronico. Il
progetto non ha finalità di profilazione, marketing o pubblicità.

## Destinatari

- **Spaggiari ClasseViva** (`web.spaggiari.eu`, `cdndoc.spaggiari.eu`): riceve
  le credenziali e le richieste dell'utente.
- **Google** (`accounts.google.com`, `oauth2.googleapis.com`,
  `calendar.google.com`, `classroom.google.com`, `classroom.googleapis.com`):
  riceve, secondo la configurazione, consenso, rinnovo/revoca o letture.
- **Provider AI scelto dall'utente**: riceve i dati restituiti dai tool MCP
  durante la conversazione. Il provider applica la propria informativa.

- **Discord o altro ponte configurato dall'utente**: l'allegato è inviato
  solo se il ponte esterno lo carica. Il MCP non contatta Discord direttamente.

## Conservazione e cancellazione

- Chiudi prima il client, il server MCP e i ponti collegati.
- `classeviva-mcp classroom logout` rimuove le fonti Google salvate senza scollegare Spaggiari.
- `classeviva-mcp logout` rimuove credenziali, sessioni e autorizzazioni remote
  gestite; conserva impostazioni locali, cache file e passphrase remota.
- `classeviva-mcp logout --purge` rimuove anche contesto, cache `attachments/`
  e `didactics/`, e configurazione remota gestita. Non richiede privilegi
  amministrativi. Conserva le copie esportate altrove; non tocca i file di
  configurazione esterni, le chat o i messaggi Discord.
- Entrambi i logout Google tentano la revoca lato Google e segnalano se
  non è confermata. I file tecnici di generazione del login non contengono
  identità, credenziali o dati scolastici.
- La cartella dati si trova in:
  - Windows: `%LOCALAPPDATA%\classeviva-mcp`
  - macOS: `~/Library/Application Support/classeviva-mcp`
  - Linux: `$XDG_DATA_HOME/classeviva-mcp` o `~/.local/share/classeviva-mcp`
- **Su un computer pubblico o condiviso**, esegui `logout --purge` prima di
  uscire. Cancellare a mano solo le voci del Keyring non basta:
  `session_cache.json` contiene il token di sessione e resta fino a `logout`.
  Il Keyring protegge i dati dagli altri account del sistema operativo, non da
  chi usa lo stesso account. Dettagli in
  [SECURITY.md](SECURITY.md#computer-pubblici-o-condivisi).
- `logout` non revoca il token lato Spaggiari: in caso di sospetto, cambia la
  password ClasseViva da un altro dispositivo.

## Sicurezza del trattamento

Vedi [SECURITY.md](SECURITY.md) per keyring, DPAPI, fail-closed, limiti di rete
e protezione SSRF.

## Minori

Il progetto è pensato per studenti e famiglie. L'utente (o chi ne esercita la
responsabilità genitoriale) è responsabile dell'uso del connettore e della
configurazione del provider AI scelto.

## Diritti dell'interessato

Poiché i dati sono conservati localmente, l'utente può esercitare accesso,
rettifica e cancellazione direttamente sui file indicati sopra o tramite i
comandi `logout` / `logout --purge`. Per i dati presso Spaggiari o Google vale
la loro informativa.

## Modifiche

Questa informativa descrive il comportamento da 1.6.0. Le modifiche sono
registrate in [CHANGELOG.md](CHANGELOG.md).
