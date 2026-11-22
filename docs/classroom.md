# Collegare Google Classroom

Il login Google è separato sia dalla password Spaggiari sia da `remote-setup`,
che autorizza i client a collegarsi al MCP. Il MCP non chiede la password Google.

1. Nel tuo progetto [Google Cloud](https://console.cloud.google.com/) abilita la
   **Google Classroom API** e configura il consenso OAuth. Se il progetto è in
   modalità Testing, aggiungi il tuo account agli utenti di test. Google può
   far scadere i refresh token dei progetti esterni in Testing dopo sette giorni.
2. Crea un client OAuth di tipo **Desktop app** e scarica il JSON. Non
   incollarlo in chat e non aggiungerlo al repository.
3. Sul computer che esegue il MCP e ha accesso al browser, esegui:

   ```bash
   classeviva-mcp classroom login --client-secrets /percorso/client_secret.json
   ```

4. Seleziona l'account scolastico e concedi i permessi. Il listener si apre per
   il solo login su `127.0.0.1`, con porta casuale. Il codice torna al programma
   nel browser; non devi copiarlo. Il programma non ha più bisogno del JSON
   originale dopo il login: conserva nella memoria sicura solo i dati
   necessari al rinnovo.

Il flusso usa **Authorization Code con PKCE S256 e `state`**, browser di
sistema, e tre scope: `classroom.courses.readonly`,
`classroom.coursework.me.readonly` e `classroom.courseworkmaterials.readonly`.
Gli access token sono effimeri e non vengono dati all'agente. Se la scuola
blocca l'app, chiedi all'amministratore di approvare il **client OAuth**;
non pubblicare il calendario e non tentare di aggirare il blocco.

`get_homework` usa le scadenze dei corsi attivi; `search_materials` cerca
metadati di allegati dei compiti e dei post Materiali. Le ricerche seguono le
pagine Google fino ai limiti operativi; se una parte non si può verificare,
lo dicono in `warnings`. I file Drive si aprono con il link originale,
secondo i permessi del tuo account. Il MCP non usa scope Drive, Google Picker
né esportazione PDF da Google Docs. Se il download è consentito, puoi
fornire un singolo file scaricato da te al client.

## Scollegare Classroom

```bash
classeviva-mcp classroom logout
```

Rimuove il collegamento Google gestito, prova a revocare il grant, e rimuove
anche i vecchi token e l'eventuale iCal salvato. Non scollega ClasseViva. Se
la revoca Google non è confermata, il comando lo segnala e indica
[le connessioni del tuo account Google](https://myaccount.google.com/permissions).
Revocare un grant può coinvolgere gli altri client dello stesso progetto
Google Cloud. Il comando non effettua logout dal browser.

## Alternativa iCal privata

Se la scuola rende disponibile l'indirizzo iCal **segreto**, configuralo con
`classeviva-mcp set-classroom-url` e incollalo solo nel terminale, quando viene
chiesto con input nascosto. Non usare un calendario pubblico. iCal contiene
solo scadenze: viene usato se l'API non è configurata o risponde in modo
incompleto. Le vecchie variabili `CLASSROOM_ICAL_URL` e `GOOGLE_ACCESS_TOKEN`
sono ancora lette a runtime; rimuovile manualmente al logout.

Riferimenti: [OAuth desktop Google](https://developers.google.com/identity/protocols/oauth2/native-app) ·
[Controlli Workspace per l'accesso delle app](https://support.google.com/a/answer/7281227).
