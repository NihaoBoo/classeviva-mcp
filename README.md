<div align="center">

# 🎓🔌 ClasseViva MCP Server (`classeviva-mcp`)

**Connettore MCP per il registro elettronico Spaggiari ClasseViva (Studenti & Famiglie) e Google Classroom.**

Incrocia dati presi direttamente dal registro voti, agenda, compiti, argomenti delle lezioni e circolari della bacheca scolastica con qualsiasi agente AI compatibile MCP.

<p align="center">
  <a href="https://github.com/NihaoBoo/classeviva-mcp/releases"><img src="https://img.shields.io/badge/version-v1.0.0-blue?style=flat-square" alt="version" height="20"></a>&nbsp;
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white" alt="python" height="20"></a>&nbsp;
  <a href="https://github.com/NihaoBoo/classeviva-mcp"><img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey?style=flat-square" alt="platform" height="20"></a>&nbsp;
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/mcp-protocol-brightgreen?style=flat-square" alt="mcp" height="20"></a>&nbsp;
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="license" height="20"></a>
</p>

<br/>

[Guida Rapida](#-guida-passo-passo) • [Google Classroom (.ics)](#-collegare-google-classroom) • [Uso da Smartphone](#-uso-da-smartphone-app-o-bot-telegramdiscord) • [Riepilogo Tool](#️-riepilogo-tool-mcp-e-limiti-anti-bloat) • [Crediti](#-crediti-e-riconoscimenti)

</div>

---

### ✅ Funzionalità Chiave 👀
* **Zero Password nei File di Configurazione**: credenziali salvate nel **Keyring del sistema operativo** (Gestione Credenziali Windows / Portachiavi macOS / SecretService Linux), compatibile con login tramite **Email personale**, codice Studente (`S...`) o Genitore (`G...`) e rinnovo automatico del token.
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
  Configura il server MCP da questa repository GitHub (https://github.com/NihaoBoo/classeviva-mcp): installalo con `uv tool install git+https://github.com/NihaoBoo/classeviva-mcp` (oppure clona la repo ed esegui `uv sync`), registralo nel mio file di configurazione MCP usando il percorso assoluto dell'eseguibile, e spiegami come lanciare `classeviva-mcp login` nel terminale per salvare le credenziali nel Keyring di sistema.
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

### Punto 3 — Fai il Login Sicuro (Una sola volta)
Non serve scaricare cartelle a mano. Apri il terminale ed esegui direttamente:
```bash
uvx --from git+https://github.com/NihaoBoo/classeviva-mcp classeviva-mcp login
```
*(In alternativa, se vuoi installarlo stabilmente sul PC esegui `uv tool install git+https://github.com/NihaoBoo/classeviva-mcp` e poi `classeviva-mcp login`; se invece hai clonato la cartella in locale usa `uv run classeviva-mcp login`).*

Ti chiederà:
1. **Email o Codice ClasseViva** (`S...` / `G...`).
2. **Password** (nascosta mentre digiti, salvata nel portachiavi di sistema).
3. **Classe e Indirizzo** (es. `5A` e `Informatica`).
4. **Link `.ics` di Google Classroom** *(Opzionale: puoi premere Invio per saltarlo e aggiungerlo dopo parlando in chat con l'AI)*.

Al termine, il comando **`login` stamperà a schermo il blocco JSON già pronto con i percorsi assoluti del tuo PC** (così eviti errori `spawn uvx ENOENT` su Windows).

### Punto 4 — Incolla il JSON nel tuo Client AI e parla con l'Agent
1. Se non l'ha già fatto l'AI al Punto 1, incolla il JSON stampato dal comando `login` nel file di configurazione del tuo client:
   * **Claude Desktop (Windows)**: `%APPDATA%\Claude\claude_desktop_config.json`
   * **Cursor**: `~/.cursor/mcp.json`
   * **Codex**: `~/.codex/config.toml` (o configurazione MCP del client)
   * **Antigravity**: `mcp_config.json` nelle impostazioni MCP
   * **Opencode**: `~/.config/opencode/opencode.json`

   Esempio del JSON generato:
   ```json
   {
     "mcpServers": {
       "classeviva": {
         "command": "C:\\Users\\<TUO_UTENTE>\\.local\\bin\\uvx.exe",
         "args": [
           "--from",
           "git+https://github.com/NihaoBoo/classeviva-mcp",
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

Se hai saltato il link Classroom durante il login iniziale o hai corsi nuovi, **puoi semplicemente incollare il link in chat all'AI**:
> *"Collega questo calendario Google Classroom ai miei compiti: [LINK.ics]"*

L'AI chiamerà `update_student_context(classroom_ical_url=...)` e salverà il link per sempre senza farti aprire il terminale.

<details open>
<summary><b>▼ Come reperire il link .ics segreto da Google Calendar</b></summary>

1. Apri **[Google Calendar](https://calendar.google.com)** con il tuo account scolastico.
2. In alto a destra clicca sull'icona dell'ingranaggio ⚙️ -> **Impostazioni**.
3. Nella barra laterale sinistra scorri su **Impostazioni per i miei calendari** e clicca sul nome del tuo corso Classroom.
4. Scorri verso il basso fino alla sezione **Indirizzo segreto in formato iCal**.
5. Clicca sull'icona Copia per copiare il link che termina in `.ics` e incollalo all'AI in chat.

*(Nota: il feed `.ics` riporta tutti i compiti con data di scadenza; i post sullo Stream senza scadenza non fanno parte del calendario).*
</details>

<details>
<summary><b>▼ Alternativa per docenti / scuole aperte: OAuth2 (Clicca per espandere)</b></summary>

Se sei un docente o la tua scuola non blocca le app esterne sull'email istituzionale, puoi impostare la variabile d'ambiente `GOOGLE_ACCESS_TOKEN` nel file MCP JSON per interrogare le API REST ufficiali `classroom.googleapis.com`.
*(Negli istituti superiori la maggior parte degli admin blocca le richieste OAuth degli studenti, per cui il metodo `.ics` sopra è la scelta raccomandata).*
</details>

---

## 📱 Uso da Smartphone (App o Bot Telegram/Discord)

Ci sono due modi per usare `classeviva-mcp` fuori casa dal telefono:

### Modo 1 — Tramite l'App del tuo Agent o VPS (Senza creare bot)
Se usi un ambiente che espone i tuoi server MCP in cloud o rete privata (es. sessioni remote di **Antigravity**, **Claude**, **Opencode** o una tua istanza **LibreChat / Open-WebUI** raggiungibile via Tailscale).

### Modo 2 — Tramite un Bot su Telegram o Discord (In DM o in un gruppo)
`classeviva-mcp` fornisce i tool di lettura del registro; per usarlo dentro Telegram o Discord ti basta agganciarlo a un agente che fa da ponte con la chat (come agent menzionati precedentemente o queste altre soluzioni valide **Hermes Agent**, **OpenClaw** o automazioni **n8n**):

1. **Dove tenerlo acceso**: sul tuo PC quando è acceso, su un vecchio telefono Android con **Termux**, oppure gratis 24/7 su una piccola VPS (**Oracle Cloud Free Tier**).
2. **Quale modello AI usare (anche a costo zero)**: se non hai un abbonamento AI a pagamento, crea una chiave gratuita su **[OpenRouter](https://openrouter.ai)** e usa i modelli `:free` (come Gemini Flash o Llama/DeepSeek free) configurandoli su **Hermes Agent**, **Opencode** o tramite **CC Switch**.
3. **Se il bot è solo per te (Chat privata / DM)**: configura nel tuo bot il filtro sul tuo `OWNER_USER_ID` di Telegram o Discord, così il bot risponde esclusivamente a te.
4. **Se aggiungi il bot nel gruppo Telegram o server Discord della classe**:
   * Ognuno può avere il proprio bot personale con le proprie chiavi, ma basta che **un solo compagno** tenga attivo il bot nel server perché tutta la classe benefici dei riassunti delle circolari e dell'orario.
   * Quando il bot è in un canale/gruppo condiviso, imposta la variabile d'ambiente **`CLASSEVIVA_PUBLIC_MODE=true`** nella configurazione di `classeviva-mcp`.
   * Con `CLASSEVIVA_PUBLIC_MODE=true`, i tool personali (`get_grades`, `calculate_grade_target`, `get_absences`) vengono **disabilitati direttamente nel codice Python**: nessun compagno potrà mai leggere i tuoi voti o le tue assenze (nemmeno provando a ingannare l'AI con prompt injection), mentre chiunque nel gruppo potrà chiedere *"Fai un riassunto delle ultime circolari sulla gita per la 5A e allega il PDF"*.

<details>
<summary><b>▼ Permessi Bot Discord pre-calcolati (Clicca per espandere)</b></summary>

Su [Discord Developer Portal](https://discord.com/developers/applications):
1. Nella scheda **Bot**, attiva la spunta **Message Content Intent**.
2. Nella scheda **OAuth2 -> URL Generator**, seleziona `bot` e `applications.commands`.
3. Nei permessi spunta *Send Messages*, *Embed Links*, *Attach Files* e *Read Message History*: il permissions integer generato è **`274877975552`**.
</details>

---

## 🛠️ Riepilogo Tool MCP e Limiti Anti-Bloat

| Tool | Descrizione | Disabilitato in `PUBLIC_MODE`? |
|---|---|---|
| `get_today_context` | Data/ora (`Europe/Rome`), anno scolastico, controllo automatico cambio classe (`3A` -> `4A`), materie/professori e orario. | No |
| `update_student_context` | Salva o aggiorna classe (`5A`), indirizzo, n. registro, note e **link .ics di Google Classroom**. | No |
| `save_class_timetable` | Valida e salva in locale l'orario settimanale della classe. | No |
| `get_homework` | Compiti e consegne dei prossimi N giorni (ClasseViva + Classroom). | No |
| `get_agenda` | Verifiche, interrogazioni, compiti e note tra due date. | No |
| `get_lessons` | **Argomenti spiegati dai prof** a lezione per preparare verifiche e recuperi. | No |
| `list_notices` | Elenca tutte le circolari in bacheca senza nascondere quelle vecchie, marcandole come `🆕 NUOVA` o `📌 GIÀ VISTA`. | No |
| `read_notice_pdf` | Estrae testo da `.pdf`/`.docx`/`.xlsx`/`.zip` filtrando per classe, o renderizza PNG se scansionato. | No |
| `get_grades` | Voti, medie ponderate per materia ed elenco insufficienze (`< 6.0`). | 🔒 **Sì** |
| `calculate_grade_target` | Calcola quale voto serve nella prossima verifica per arrivare alla media obiettivo. | 🔒 **Sì** |
| `get_absences` | Assenze, ritardi e uscite anticipate. | 🔒 **Sì** |

### Limiti di Archiviazione Automatici (Zero crescita infinita su disco)
Per evitare che la cache locale (`%LOCALAPPDATA%\classeviva-mcp` su Windows o `~/.local/share/classeviva-mcp` su Linux/Mac) cresca all'infinito nel tempo:
* **Storico circolari viste (`seen_notice_ids`)**: coda FIFO limitata agli **ultimi 200 ID** (< 4 KB). Le circolari già viste non vengono mai nascoste dalla lista ma solo etichettate con `📌 GIÀ VISTA`, così se te ne dimentichi una resta sempre visibile.
* **Cartella allegati (`attachments/`)**: rotazione automatica che mantiene su disco solo gli **ultimi 30 file PDF/PNG** scaricati, rimuovendo in automatico quelli più vecchi.
* **Note contesto studente (`custom_notes`)**: limite massimo di **1.500 caratteri**.

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

Progetto open-source indipendente, **non affiliato** a Gruppo Spaggiari Parma S.p.A. né a Google LLC. Il software è fornito *"AS IS"* senza alcuna garanzia (vedi [LICENSE](LICENSE)). L'utente è l'unico responsabile del rispetto dei Termini di Servizio della propria scuola e dei provider AI scelti.
