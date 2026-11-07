# Privacy Policy / Informativa sul trattamento dei dati

`classeviva-mcp` è un connettore locale open source. **Non gestisce un server
centrale e non invia dati a maintainer o terze parti.** Tutti i dati restano sul
dispositivo dell'utente, salvo le richieste che l'utente stesso effettua verso
Spaggiari ClasseViva e Google Classroom.

## Quali dati tratta

| Dato | Dove viene conservato | Finalità |
| ---- | --------------------- | -------- |
| Email / codice utente e password ClasseViva | Keyring di sistema o fallback cifrato DPAPI | Autenticazione a Spaggiari |
| Token di sessione Spaggiari (`token`, `ident`, `student_id`) | Keyring/fallback cifrato (`session_cache.json`) | Evitare login ripetuti |
| URL segreto `.ics` di Google Classroom | Keyring/fallback cifrato | Lettura compiti Classroom |
| Token OAuth Google (opzionale) | Keyring/fallback cifrato | Lettura API Classroom |
| Classe, indirizzo, numero di registro, note, orario | `student_context.json` locale | Contesto per l'agente |
| ID delle circolari viste | `student_context.json` locale | Etichetta `NUOVA` / `GIÀ VISTA` |
| Allegati delle circolari | cartella `attachments/` locale | Lettura PDF/DOCX/XLSX |
| Voti, assenze, lezioni, agenda | Non persistiti: letti dall'API e restituiti all'agente | Risposta alle richieste |

## Basi e finalità

Il trattamento avviene su richiesta dell'utente, per consentire all'agente AI
scelto dall'utente di leggere i dati del proprio registro elettronico. Il
progetto non ha finalità di profilazione, marketing o pubblicità.

## Destinatari

- **Spaggiari ClasseViva** (`web.spaggiari.eu`, `cdndoc.spaggiari.eu`): riceve
  le credenziali e le richieste dell'utente.
- **Google** (`calendar.google.com`, `classroom.google.com`,
  `classroom.googleapis.com`): riceve le richieste se l'utente configura il feed
  iCal o OAuth.
- **Provider AI scelto dall'utente**: riceve i dati restituiti dai tool MCP
  durante la conversazione. Il provider applica la propria informativa.

Nessun altro destinatario è previsto dal codice.

## Conservazione e cancellazione

- `classeviva-mcp logout` rimuove credenziali e sessione; conserva orario e note.
- `classeviva-mcp logout --purge` rimuove anche contesto studente e allegati.
- La cartella dati si trova in:
  - Windows: `%LOCALAPPDATA%\classeviva-mcp`
  - macOS: `~/Library/Application Support/classeviva-mcp`
  - Linux: `$XDG_DATA_HOME/classeviva-mcp` o `~/.local/share/classeviva-mcp`

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

Questa informativa si riferisce alla versione 1.1.0. Le modifiche future saranno
registrate in [CHANGELOG.md](CHANGELOG.md).
