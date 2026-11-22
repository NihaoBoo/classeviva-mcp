# ClasseViva MCP

Connettore indipendente per consultare Spaggiari ClasseViva e Google Classroom
dal tuo client AI. Funziona sul tuo computer, con un solo account per fonte.
Non è affiliato a Spaggiari o Google.

## Installazione

Con [uv](https://docs.astral.sh/uv/getting-started/installation/) installato:

```bash
uv tool install classeviva-mcp
classeviva-mcp login
```

Esegui il login nel terminale, non in chat. La password ClasseViva viene
verificata prima del salvataggio. `login` stampa la configurazione MCP, senza
credenziali. Per OpenCode, ad esempio:

```bash
classeviva-mcp config --client opencode
```

Incorpora il blocco `mcp.classeviva` nella configurazione del client e
riavvialo. Per VS Code, Codex, Claude Code e l'accesso da telefono vedi la
[guida ai client](docs/clients.md). `classeviva-mcp doctor` aiuta con il login
e i problemi di connessione.

## Tre richieste da provare

- **«Che compiti e verifiche ho domani?»** — il client può interrogare solo
  quel giorno. Se una fonte non risponde, il MCP segnala cosa non ha verificato.
- **«Trova la dispensa sulle reti.»** — primi cinque risultati, con fonte e data;
  chiedi altri risultati, oppure scegli un file da leggere o salvare.
- **«Mostrami solo la pagina 2 della circolare.»** — un client compatibile può
  recuperare un singolo PNG. Se la circolare è nuova, il client deve chiedere
  conferma: aprirla la segna come letta sul registro.

Puoi anche chiedere orario, lezioni, voti, assenze da giustificare, chiusure e
libri di testo. Non serve interrogare tutto il registro per una domanda mirata.

## Google Classroom

È opzionale. Se hai il JSON di un client Google OAuth **Desktop app** abilitato
per Classroom, esegui sul PC con il browser:

```bash
classeviva-mcp classroom login --client-secrets /percorso/client_secret.json
```

Google chiede accesso in sola lettura a corsi, compiti e materiali. Gli
allegati Drive restano link originali: il MCP non li apre né li scarica.
Per configurare il client Google, scollegare l'account o tenere iCal come
alternativa privata, vedi la [guida Classroom](docs/classroom.md).

## File e Discord

Il MCP distingue lettura del testo, salvataggio sul PC e preparazione di un
allegato. Per Discord serve un ponte che legga la risorsa MCP e carichi il file
come allegato nativo, **solo nella DM del proprietario**. Il pacchetto non
include un bot né invia messaggi. Vedi il [contratto per il ponte](docs/clients.md#discord).

## Privacy e logout

- Le credenziali gestite restano nel portachiavi di sistema o nel fallback Windows cifrato.
- I risultati vengono consegnati al client MCP: il tuo provider AI può riceverli.
- Chiudi server e ponti prima del logout. `logout` scollega i servizi e conserva i dati locali.
- `logout --purge` rimuove anche orario, note, cache e configurazione remota gestita.
  Copie esportate, cronologia del client e variabili d'ambiente non vengono cancellate.

Prima di cambiare account usa `logout --purge`. Per riprendere con lo stesso
account dopo un logout semplice, usa `login --keep-local-data` se richiesto.
Dettagli in [Privacy](PRIVACY.md) e [Sicurezza](SECURITY.md).

## Sviluppo

```bash
uv sync --extra dev
uv run pytest -q
```

[Changelog](CHANGELOG.md) · [Contribuire](CONTRIBUTING.md) ·
[Licenza MIT](LICENSE) · [Crediti e avvisi](NOTICE.md)
