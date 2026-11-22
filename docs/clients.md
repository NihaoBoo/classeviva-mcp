# Client MCP, telefono e Discord

## Sullo stesso computer

`classeviva-mcp config --client <formato>` stampa la configurazione per
`opencode`, `vscode`, `codex`, `claude-code` o `generic` (client con `mcpServers`,
come Claude Desktop e Cursor). Integra la voce nel file del client senza
sovrascrivere gli altri server. In modalità predefinita `stdio` il client
avvia il MCP attraverso pipe locali: non c'è alcuna porta in ascolto.

Se usi il codice del repository anziché PyPI, avvia con `uv run classeviva-mcp`.
Su una rete scolastica il MCP usa normalmente il proxy di sistema e
verifica i certificati con il trust store del sistema operativo. `doctor`
segnala i problemi più comuni. Non disattivare la verifica TLS.

## Accesso remoto da app compatibili

L'OAuth remoto autorizza un client a leggere **l'intero registro**. Non è
OAuth Google, non è multiutente, e non rende i dati adatti a chat di gruppo.
Usalo solo se ti serve da un altro dispositivo. Il server resta in ascolto
su loopback e davanti serve un tunnel HTTPS fidato:

```bash
classeviva-mcp remote-setup
# Configura il tuo tunnel HTTPS verso 127.0.0.1:8765, poi:
classeviva-mcp serve --transport streamable-http --public-url https://<host-del-tunnel>
```

Aggiungi `https://<host-del-tunnel>/mcp` nel client remoto e autorizzalo nel
browser con la passphrase del proprietario. Per script fidati sullo stesso
computer puoi usare `--auth token` (`Authorization: Bearer …`). Il valore
va nella configurazione protetta del ponte, non in chat o nei log.
`remote-revoke` scollega i client gestiti; riavvia il server per sostituire
un token bearer già caricato. Vedi [Sicurezza](../SECURITY.md#transport).

## Discord

Il pacchetto **non** include un bot. Un ponte personalizzato deve applicare
i controlli di seguito; non tutti i client supportano `resources/read`.

1. Accetta la richiesta solo in una DM con l'ID Discord del proprietario
   fissato in configurazione. Non fidarti di ID, canali o destinatari proposti
   dal modello. Rifiuta gruppo, guild e webhook pubblici.
2. Chiama `prepare_attachment` con `file_id` per Didattica o `notice_id` e
   `attachment_index` (da 0) per una circolare. `page=2` prepara **solo** il
   secondo foglio come PNG. Se il tool chiede conferma per una circolare,
   non ripetere con `allow_mark_as_read=True` finché l'utente non accetta.
3. Se il risultato è un `ResourceLink`, il ponte legge **quel** `uri` via
   `resources/read` sulla stessa istanza MCP autenticata. Non aprire URL
   esterni, non leggere file dal PC e non inoltrare blob, URI o base64 al
   modello. Il link è monouso e scade dopo 90 secondi. Leggi e valida MIME,
   nome, byte e limite prima di inviare.
4. Carica come [allegato nativo Discord](https://docs.discord.com/developers/reference#uploading-files)
   e conferma solo dopo che Discord accetta l'upload. Se il ponte non
   supporta gli allegati o il file è troppo grande, dillo. Offri
   `download_didactics_item` per salvare il file ClasseViva su una cartella
   indicata dall'utente; per Classroom mostra il link originale.

Il MCP limita il passaggio a 8 MiB per file. Il limite effettivo del ponte
può essere più basso. Anche in DM, Discord conserva il file secondo la
propria informativa: un logout dal MCP non cancella il messaggio.
