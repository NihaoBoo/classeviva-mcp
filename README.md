<div align="center">

# 🎓🔌 ClasseViva MCP Server (`classeviva-mcp`)

**Connettore MCP per il registro elettronico Spaggiari ClasseViva (Studenti & Famiglie) e Google Classroom.**

Incrocia dati presi direttamente dal registro voti, agenda, compiti, argomenti delle lezioni e circolari della bacheca scolastica con qualsiasi agente AI compatibile MCP.

<p align="center">
  <a href="https://github.com/NihaoBoo/classeviva-mcp/releases"><img src="https://img.shields.io/badge/release-v1.1.3-blue?style=flat-square" alt="release" height="20"></a>&nbsp;
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="python" height="20"></a>&nbsp;
  <a href="https://github.com/NihaoBoo/classeviva-mcp"><img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey?style=flat-square" alt="platform" height="20"></a>&nbsp;
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/mcp-protocol-brightgreen?style=flat-square" alt="mcp" height="20"></a>&nbsp;
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="license" height="20"></a>
</p>

<br/>

[Guida Rapida](#-guida-passo-passo) • [Google Classroom (.ics)](#-collegare-google-classroom) • [Uso da Smartphone](#-uso-da-smartphone-app-o-bot-telegramdiscord) • [Riepilogo Tool](#️-riepilogo-tool-mcp-e-limiti-anti-bloat) • [Sicurezza](#sicurezza-e-privacy) • [Crediti](#-crediti-e-riconoscimenti)

</div>

---

### ✅ Funzionalità Chiave 👀
* **Gestione credenziali**: il programma usa il **Keyring del sistema operativo** (Gestione Credenziali Windows / Portachiavi macOS / SecretService Linux). Se il Keyring non è disponibile, su Windows usa il fallback cifrato **DPAPI**; su Linux/macOS senza Keyring rifiuta di salvare in chiaro, salvo esplicito `--allow-insecure-plaintext`. La cache di sessione contiene solo `token`, `ident` e `student_id`. Dettagli in [SECURITY.md](SECURITY.md) e [PRIVACY.md](PRIVACY.md).
* **Voti & Target**: medie ponderate, materie insufficienti e calcolo matematico del voto necessario al recupero (`calculate_grade_target`).
* **Agenda & Compiti**: lettura scadenze, verifiche e interrogazioni (con supporto a Google Classroom tramite feed `.ics` iCal o OAuth2).
* **Lezioni & Argomenti**: storico degli argomenti spiegati dai docenti per pianificare il ripasso prima delle verifiche.
* **Bacheca & Circolari Intelligenti**: download e parsing `.pdf`, `.docx`, `.xlsx` (con gestione celle unite e colonne verticali per gli orari) e `.zip` con regex di filtraggio per classe (risparmio token fino al 90%), fallback Vision per scansioni e stato `🆕 NUOVA` / `📌 GIÀ VISTA` (senza mai nascondere le circolari già lette).
* **Persistenza Locale Anti-Bloat & Cambio Anno Automatico**: cache locale dell'orario e del contesto studente (sigle d'istituto, n. registro), rilevamento automatico del passaggio d'anno scolastico (es. da `3A` a `4A` tramite scheda anagrafica del registro) e tetto massimo automatico sui file salvati.

---

# ⚡ Guida Passo-Passo

### Punto 1 — Hai già un client AI compatibile MCP o un Agent Harness?

* **SÌ, e voglio che configuri tutto l'AI in automatico** (uso già Claude Code, Codex, Opencode, Cursor, Antigravity o simili):
  Copia e incolla questo prompt nella chat del tuo agent e poi **salta direttamente al Punto 3**:
  ```text
  Configura il server MCP da questa repository GitHub (https://github.com/NihaoBoo/classeviva-mcp): installalo con `uv tool install git+https://github.com/NihaoBoo/classeviva-mcp@stable` (oppure clona la repo ed esegui `uv sync`), registralo nel mio file di configurazione MCP usando il percorso assoluto dell'eseguibile, e spiegami come lanciare `classeviva-mcp login` nel terminale per salvare le credenziali nel Keyring se disponibile e verificare il backend effettivamente utilizzato.
  ```

* **SÌ, ma preferisco configurarlo a mano** 👉 **Procedi al Punto 2**.

* **NO, non ho ancora un client AI** 👉 Scarica e installa gratis uno di questi client: **[Claude Desktop](https://claude.ai/download)**, **[Opencode](https://opencode.ai)**, **[Codex](https://openai.com/codex)**, **[Google Antigravity](https://antigravity.google/download/)** oppure **[Cursor](https://cursor.com)**, poi scegli una delle due opzioni qui sopra.

### Punto 2 — Hai già `uv` installato sul PC?
* **SÌ** 👉 **Procedi al Punto 3**.
* **NO** 👉 Apri **PowerShell** (su Windows) o il **Terminale** (su Mac/Linux) e incolla questo comando:
  * **Windows**:
    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```
  * **macOS / Linux**:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

### Punto 3 — Esegui il login e salva le credenziali (una sola volta)
Non serve scaricare cartelle a mano. Apri il terminale ed esegui direttamente:
```bash
uvx --from git+https://github.com/NihaoBoo/classeviva-mcp@stable classeviva-mcp login
```
*(In alternativa, se vuoi installarlo stabilmente sul PC esegui `uv tool install git+https://github.com/NihaoBoo/classeviva-mcp@stable` e poi `classeviva-mcp login`; se invece hai clonato la cartella in locale usa `uv run classeviva-mcp login`).*

> **Dove finisce l'installazione?** `uvx` non installa nulla in modo permanente: crea un ambiente temporaneo nella cache di `uv` (es. `%LOCALAPPDATA%\uv\cache` su Windows, `~/.cache/uv` su Linux/macOS) e lo riusa ai comandi successivi. Con `uv tool install` invece il comando `classeviva-mcp` viene installato stabilmente e aggiunto al `PATH`.

> `@stable` punta sempre all'ultima release stabile. Se vuoi bloccare una versione esatta, sostituisci `@stable` con `@v1.1.3`.

> ⚠️ **Computer di scuola o condivisi**
> `classeviva-mcp` è pensato per **un solo account su un dispositivo personale**. Non usarlo su un PC con account condiviso (`studente`, `lab`, `aula`...): chiunque usi quell'account può aprire il Keyring e leggere le credenziali, e due studenti che fanno `login` si sovrascrivono a vicenda. Anche con un account personale, un amministratore della macchina può accedere al profilo. Se devi usarlo su un computer non tuo, esegui **`logout --purge` prima di uscire**: non servono privilegi di admin. Dettagli in [SECURITY.md](SECURITY.md#computer-pubblici-o-condivisi).

Ti chiederà:
1. **Email o Codice ClasseViva** (`S...` / `G...`).
2. **Password** (nascosta mentre digiti; salvata nel Keyring se disponibile, altrimenti nel file JSON di fallback descritto nella sezione Sicurezza).
3. **Classe e Indirizzo** (es. `5A` e `Informatica`).
4. **Link `.ics` di Google Classroom** *(Opzionale: puoi premere Invio per saltarlo e configurarlo dopo con `classeviva-mcp set-classroom-url`)*.

Al termine, il comando **`login` stamperà a schermo il blocco JSON già pronto con i percorsi assoluti del tuo PC** (così eviti errori `spawn uvx ENOENT` su Windows).

### Punto 4 — Incolla il JSON nel tuo Client AI e parla con l'Agent
1. Se non l'ha già fatto l'AI al Punto 1, incolla il JSON stampato dal comando `login` in uno di questi due modi:
   * **Nella configurazione MCP del tuo client**:
     * **Claude Desktop (Windows)**: `%APPDATA%\Claude\claude_desktop_config.json`
     * **Cursor**: `~/.cursor/mcp.json`
     * **Codex**: `~/.codex/config.toml` (o configurazione MCP del client)
     * **Antigravity**: `mcp_config.json` nelle impostazioni MCP
     * **Opencode**: `~/.config/opencode/opencode.json`
   * **Oppure nella chat del tuo agente AI**, scrivendo ad esempio: *"Configura questo server MCP per me"* e incollando il JSON.

   Esempio del JSON generato:
   ```json
   {
     "mcpServers": {
       "classeviva": {
         "command": "C:\\Users\\<TUO_UTENTE>\\.local\\bin\\uvx.exe",
         "args": [
           "--from",
           "git+https://github.com/NihaoBoo/classeviva-mcp@stable",
           "classeviva-mcp"
         ]
       }
     }
   }
   ```

   **Consiglio per chi usa più agent**: se non hai voglia di sincronizzare a mano gli stessi JSON su ogni tool, puoi usare **[CC Switch](https://github.com/farion1231/cc-switch)**, un progetto open-source su GitHub che centralizza le configurazioni MCP e la gestione dei vari agent harness.

2. Apri la chat con la tua AI e scrivi (la prima volta):
   > *"Fai `/classeviva:setup`: sono della [CLASSE] [PERCORSO_DI_STUDIO]. Controlla se in bacheca c'è l'orario delle lezioni e salva quello della mia classe."*

> 💡 **Come modificare classe, orario o preferenze in seguito**: ricordati che non serve rilanciare `/classeviva:setup` da zero per cambiare un dato: puoi anche dirglielo normalmente in chat (es. *"Ho deciso di cambiare percorso di studi / scuola..."* oppure *"Sono stato promosso"*) e l'AI aggiornerà da sola il file di contesto locale. Inoltre, a ogni nuovo anno scolastico (`get_today_context`), il server confronta la classe salvata con quella ufficiale del registro elettronico per avvisare l'AI se sei passato dalla `3A` alla `4A`.

---

## 📅 Collegare Google Classroom

Se hai saltato il link Classroom durante il login iniziale o hai corsi nuovi, **non incollarlo nella chat dell'AI**. Esegui nel terminale il comando adatto a come hai installato il server.

**Se usi `uvx` (senza installazione permanente):**
```bash
uvx --from git+https://github.com/NihaoBoo/classeviva-mcp@stable classeviva-mcp set-classroom-url
```

**Se lo hai installato stabilmente con `uv tool install`:**
```bash
classeviva-mcp set-classroom-url
```

`uvx` scarica e riusa un ambiente temporaneo nella cache di `uv`, quindi non devi sapere dove finiscono i file: il comando funziona allo stesso modo dalla prima esecuzione in poi. Con `uv tool install` il comando è già nel `PATH` e non serve anteporre `uvx`.

Il comando chiede il link con **input nascosto** e lo salva nel Keyring o nel fallback cifrato, così il segreto non passa dal provider AI. Puoi anche passarlo come argomento, ma è **sconsigliato**: resterebbe nella cronologia del terminale.

Il link iCal è accettato solo se punta a `calendar.google.com` o `classroom.google.com`, usa HTTPS sulla porta 443 e risolve verso un indirizzo pubblico. Redirect verso host diversi, IP privati/loopback/link-local e risposte oltre 5 MB vengono bloccati.

**Il link iCal è un segreto equivalente a una password.** Non inserirlo in conversazioni condivise o con agent non fidati.

<details open>
<summary><b>▼ Come reperire il link .ics segreto da Google Calendar</b></summary>

1. Apri **[Google Calendar](https://calendar.google.com)** con il tuo account scolastico.
2. In alto a destra clicca sull'icona dell'ingranaggio ⚙️ -> **Impostazioni**.
3. Nella barra laterale sinistra scorri su **Impostazioni per i miei calendari** e clicca sul nome del tuo corso Classroom.
4. Scorri verso il basso fino alla sezione **Indirizzo segreto in formato iCal**.
5. Clicca sull'icona Copia per copiare il link che termina in `.ics` e passalo a `classeviva-mcp set-classroom-url` nel terminale.

*(Nota: il feed `.ics` riporta tutti i compiti con data di scadenza; i post sullo Stream senza scadenza non fanno parte del calendario).*
</details>

<details>
<summary><b>▼ Alternativa per docenti / scuole aperte: OAuth2 (Clicca per espandere)</b></summary>

Se sei un docente o la tua scuola non blocca le app esterne sull'email istituzionale, puoi impostare la variabile d'ambiente `GOOGLE_ACCESS_TOKEN` per interrogare le API REST ufficiali `classroom.googleapis.com`. Evita di inserire il token direttamente nel JSON MCP: i valori `env` in quel file sono normalmente memorizzati in chiaro.
*(Negli istituti superiori la maggior parte degli admin blocca le richieste OAuth degli studenti, per cui il metodo `.ics` sopra è la scelta raccomandata).*
</details>

---

## 📱 Uso da Smartphone (App o Bot Telegram/Discord)

Ci sono due modi per usare `classeviva-mcp` fuori casa dal telefono:

### Modo 1 — Tramite l'App del tuo Agent o VPS (Senza creare bot)
Se usi un ambiente che espone i tuoi server MCP in cloud o rete privata (es. sessioni remote di **Antigravity**, **Claude**, **Opencode** o una tua istanza **LibreChat / Open-WebUI** raggiungibile via Tailscale).

### Modo 2 — Tramite un Bot su Telegram o Discord (solo DM privati)
`classeviva-mcp` fornisce tool di consultazione del registro e di gestione del contesto locale; per usarlo in una DM privata Telegram o Discord puoi collegarlo a un agente che fa da ponte con la chat (ad esempio **Hermes Agent**, **OpenClaw** o automazioni **n8n**):

1. **Dove tenerlo acceso**: sul tuo PC quando è acceso, su un vecchio telefono Android con **Termux**, oppure gratis 24/7 su una piccola VPS (**Oracle Cloud Free Tier**).
2. **Quale modello AI usare (anche a costo zero)**: se non hai un abbonamento AI a pagamento, crea una chiave gratuita su **[OpenRouter](https://openrouter.ai)** e usa i modelli `:free` (come Gemini Flash o Llama/DeepSeek free) configurandoli su **Hermes Agent**, **Opencode** o tramite **CC Switch**.
3. **Se il bot è solo per te (Chat privata / DM)**: configura nel tuo bot il filtro sul tuo `OWNER_USER_ID` di Telegram o Discord, così il bot risponde esclusivamente a te.
4. **Non aggiungere il bot a gruppi Telegram o server Discord condivisi.** Il server non dispone di autenticazione o autorizzazioni per singolo chiamante. `CLASSEVIVA_READ_ONLY_MODE=true` minimizza i dati e disabilita i tool personali/di scrittura, ma resta una modalità per singolo account e non isola più utenti. Usalo soltanto in DM privati con un filtro `OWNER_USER_ID` applicato dal bot, oppure tramite una rete privata con accesso riservato. Il filtro del bot è una protezione esterna: non sostituisce controlli di accesso implementati nel server MCP.

<details>
<summary><b>▼ Permessi Bot Discord pre-calcolati (Clicca per espandere)</b></summary>

Su [Discord Developer Portal](https://discord.com/developers/applications):
1. Nella scheda **Bot**, attiva la spunta **Message Content Intent**.
2. Nella scheda **OAuth2 -> URL Generator**, seleziona `bot` e `applications.commands`.
3. Nei permessi spunta *Send Messages*, *Embed Links*, *Attach Files* e *Read Message History*: il permissions integer generato è **`274877975552`**.
</details>

---

## 🛠️ Riepilogo Tool MCP e Limiti Anti-Bloat

| Tool | Descrizione | Disabilitato in `READ_ONLY_MODE`? |
|---|---|---|
| `get_today_context` | Data/ora (`Europe/Rome`), profilo minimizzato, contesto, materie/professori e orario. | No |
| `update_student_context` | Salva o aggiorna classe (`5A`), indirizzo, n. registro e note. Il link iCal non si configura da qui. | 🔒 **Sì** |
| `save_class_timetable` | Valida e salva in locale l'orario settimanale della classe. | 🔒 **Sì** |
| `get_homework` | Compiti e consegne dei prossimi N giorni (ClasseViva + Classroom). | 🔒 **Sì** |
| `get_agenda` | Verifiche, interrogazioni, compiti e note tra due date. | 🔒 **Sì** |
| `get_lessons` | **Argomenti spiegati dai prof** a lezione per preparare verifiche e recuperi. | 🔒 **Sì** |
| `list_notices` | Elenca tutte le circolari in bacheca senza nascondere quelle vecchie, marcandole come `🆕 NUOVA` o `📌 GIÀ VISTA`. | No |
| `read_notice_pdf` | Estrae testo da `.pdf`/`.docx`/`.xlsx`/`.zip` filtrando per classe, o renderizza PNG se scansionato. Non segna come letta senza consenso esplicito; in read-only non salva file. | No |
| `get_grades` | Voti, medie ponderate per materia ed elenco insufficienze (`< 6.0`). | 🔒 **Sì** |
| `calculate_grade_target` | Calcola quale voto serve nella prossima verifica per arrivare alla media obiettivo. | 🔒 **Sì** |
| `get_absences` | Assenze, ritardi e uscite anticipate. | 🔒 **Sì** |

`CLASSEVIVA_READ_ONLY_MODE=true` (alias retrocompatibile: `CLASSEVIVA_PUBLIC_MODE=true`) non registra i tool personali e di scrittura e fa fallire le funzioni corrispondenti con `PermissionError`. Non è una modalità multiutente: il server non autentica i chiamanti.

### Limiti di Archiviazione Automatici (Zero crescita infinita su disco)
Per evitare che la cache locale (`%LOCALAPPDATA%\classeviva-mcp` su Windows o `~/.local/share/classeviva-mcp` su Linux/Mac) cresca all'infinito nel tempo:
* **Storico circolari viste (`seen_notice_ids`)**: coda FIFO limitata agli **ultimi 200 ID** (< 4 KB). Le circolari già viste non vengono mai nascoste dalla lista ma solo etichettate con `📌 GIÀ VISTA`, così se te ne dimentichi una resta sempre visibile.
* **Cartella allegati (`attachments/`)**: rotazione automatica che mantiene su disco solo gli **ultimi 30 file PDF/PNG** scaricati, rimuovendo in automatico quelli più vecchi.
* **Note contesto studente (`custom_notes`)**: limite massimo di **1.500 caratteri**.

---

## Sicurezza e privacy

> **Importante:** questa sezione descrive il comportamento della v1.1.3; non rappresenta una certificazione di sicurezza. Non esporre il server a gruppi, utenti non fidati o Internet. Dettagli completi in [SECURITY.md](SECURITY.md) e [PRIVACY.md](PRIVACY.md).

- **`CLASSEVIVA_READ_ONLY_MODE` (alias `CLASSEVIVA_PUBLIC_MODE`) minimizza i dati e disabilita le scritture.** In questa modalità i tool personali e di scrittura (`get_grades`, `get_absences`, `get_homework`, `get_agenda`, `get_lessons`, `calculate_grade_target`, `update_student_context`, `save_class_timetable`) non vengono registrati e le funzioni sollevano `PermissionError`. `get_today_context` restituisce un profilo senza nome, cognome e identificativi, e un contesto senza numero di registro, note o ID delle circolari viste. Resta comunque una modalità per singolo account, **non** un sistema multiutente: il server non autentica i chiamanti.
- **Il feed Classroom è protetto contro SSRF.** Sono ammessi solo host `calendar.google.com` e `classroom.google.com`, schema HTTPS, porta 443 e risoluzione verso IP pubblici. I redirect vengono seguiti manualmente e rivalidati (massimo 3), la risposta è limitata a 5 MB e `trust_env=False` evita proxy ambientali. Il link si configura solo via CLI (`classeviva-mcp set-classroom-url`) e non viene mai restituito dai tool.
- **I segreti restano nel Keyring o in un fallback cifrato.** Su Windows il fallback usa DPAPI; su Linux/macOS senza Keyring il salvataggio fallisce in modo esplicito, salvo `--allow-insecure-plaintext`. I file sono scritti in modo atomico con permessi `0600` dove supportati. `session_cache.json` contiene solo `token`, `ident` e `student_id`. L'URL iCal viene migrato automaticamente fuori da `student_context.json`.
- **Logout esplicito.** `classeviva-mcp logout` rimuove credenziali e sessione ma conserva orario e note; `classeviva-mcp logout --purge` elimina anche `student_context.json` e la cache degli allegati. Nessuno dei due richiede privilegi di amministratore. Cancellare a mano solo le voci del Keyring **non basta**: `session_cache.json` contiene il token di sessione e resta fino a `logout`. `logout` non revoca il token lato Spaggiari: in caso di sospetto cambia la password da un altro dispositivo.
- **La lettura di una circolare può modificarne lo stato sul registro.** Spaggiari restituisce testo e allegati completi solo dopo l'endpoint `noticeboard/read`, che segna la circolare come letta. `read_notice_pdf` espone `allow_mark_as_read` con **default `False`**: su una circolare nuova restituisce solo i metadati e non modifica lo stato remoto. Passa `allow_mark_as_read=True` solo dopo aver avvisato lo studente; i prompt `setup` e `notices_brief` chiedono conferma. Se non è possibile verificare se la circolare è già stata letta, il tool **fallisce chiuso**: nessuna ricevuta di lettura, nessun download. Il tool restituisce `notice_marked_as_read` / `notice_was_unread` / `notice_read_state_known`. In `READ_ONLY_MODE` il valore è forzato a `False` e `save_pdf` è ignorato. `list_notices` non chiama mai l'endpoint di lettura.
- **Output diagnostici minimizzati.** `login` e `doctor` non stampano nome, cognome o identificativo completo; `--verbose` mostra solo scuola/classe. `doctor` mostra la cartella dati e indica se la sessione è in cache.
- **Le credenziali vengono verificate prima di essere salvate.** `login` non lascia mai nel Keyring una password non verificata: se la password è sbagliata, nel Keyring non resta nulla.
- **Limiti sulle risorse in ingresso.** Download in streaming: allegati fino a 20 MB ciascuno e 40 MB complessivi per circolare, feed iCal fino a 5 MB. Con `attachment_index` viene scaricato solo l'allegato richiesto. PDF fino a 50 pagine, XML DOCX fino a 10 MB, XLSX fino a 500 righe × 50 colonne × 20 fogli, ZIP fino a 15 file / 25 MB decompressi con profondità massima 1. I redirect degli allegati sono ammessi solo verso `*.spaggiari.eu` e il download verso il CDN avviene senza header di autenticazione.
- **Veloce nei workflow avanzati.** La connessione HTTP è riutilizzata tra le chiamate (niente handshake TLS per ogni richiesta) e gli endpoint di sola lettura hanno una cache di 60 secondi: un brief + piano di recupero + voti non interrogano Spaggiari tre volte per gli stessi dati. La cache viene svuotata dopo ogni modifica di stato.

### 🧹 Pulizia su computer condivisi

Prima di lasciare un PC che non è tuo:

```bash
# Se usi uvx (senza installazione permanente)
uvx --from git+https://github.com/NihaoBoo/classeviva-mcp@stable classeviva-mcp logout --purge

# Se lo hai installato con uv tool install
classeviva-mcp logout --purge
```

Se hai impostato variabili d'ambiente (`CLASSEVIVA_USER`, `CLASSEVIVA_PASSWORD`, `GOOGLE_ACCESS_TOKEN`, `CLASSROOM_ICAL_URL`), rimuovile anche dalla shell o dalla configurazione del client: `logout` non le tocca.

Prima di un uso condiviso servono comunque autorizzazioni per chiamante e isolamento multiutente: la modalità read-only non sostituisce questi controlli.

---

## Possibili migliorie future e integrazioni
* **Pubblicazione su PyPI**: pacchetto ufficiale per eseguirlo con `uvx classeviva-mcp` senza dover specificare l'URL GitHub.
* **Sincronizzazione multi-MCP consigliata**: guide rapide per combinare `classeviva-mcp` con `notion-mcp`, `obsidian-mcp` o `google-calendar-mcp` (es. creare automaticamente piani di studio su Notion/Obsidian partendo dalle lezioni di ClasseViva).
* **Statistiche di fine periodo / scrutinio**: confronto storico tra primo e secondo quadrimestre.

---

## 🔥 Crediti e Riconoscimenti

Questo progetto è stato reso possibile anche grazie al lavoro della community open-source italiana che ha documentato e mantenuto i wrapper per le API REST di Spaggiari ClasseViva:
* [michelangelomo/Classeviva-Official-Endpoints](https://github.com/michelangelomo/Classeviva-Official-Endpoints) — Documentazione degli endpoint REST ufficiali di ClasseViva.
* [Lioydiano/Classeviva](https://github.com/Lioydiano/Classeviva) — Python API wrapper per ClasseViva.
* [MarcoBuster/ClasseVivaAPI](https://github.com/MarcoBuster/ClasseVivaAPI) & [ALCC01/ClassevivaAPI](https://github.com/ALCC01/ClassevivaAPI) — Moduli e parser API per `web.spaggiari.eu`.
* [47PADO47/Classeviva.js](https://github.com/47PADO47/Classeviva.js) & [riccardocalligaro/registro_elettronico](https://github.com/riccardocalligaro/registro_elettronico) — Client e librerie della community.

---

## Disclaimer (MIT License)

Progetto open-source indipendente, **non affiliato** a Gruppo Spaggiari Parma S.p.A. né a Google LLC. Il software è fornito *"AS IS"* senza alcuna garanzia (vedi [LICENSE](LICENSE) e [NOTICE.md](NOTICE.md)). L'utente è l'unico responsabile del rispetto dei Termini di Servizio della propria scuola e dei provider AI scelti.
