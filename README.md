# 🎓 classeviva-mcp

Connettore **MCP (Model Context Protocol)** per il registro elettronico **Spaggiari ClasseViva** (Studenti e Famiglie) e **Google Classroom**.

* **Zero password nei file di configurazione**: salva le credenziali nel **Keyring del sistema operativo** (Gestione Credenziali Windows / Portachiavi macOS).
* **Funziona con Email personale, Codice Studente (`S...`) o Genitore (`G...`)**: rinnova il token in automatico quando scade (`HTTP 401`).
* **Ottimizzato contro lo spreco di token**: legge allegati `.pdf`, `.docx`, `.xlsx` (anche con celle unite e colonne verticali per gli orari) e `.zip`, filtrando solo le pagine/colonne della tua classe e salvando l'orario in locale.

---

## ⚡ Guida Passo-Passo (Per chi parte da ZERO)

> **Hai già un AI agent?** Copia e incolla questo prompt al tuo agent (Claude Code, Codex, Opencode, Cursor, Antigravity o simili) e poi **salta direttamente al Punto 3**:
> ```text
> Configura il server MCP da questa repository GitHub (https://github.com/NihaoBoo/classeviva-mcp): installalo con `uv tool install git+https://github.com/NihaoBoo/classeviva-mcp` (oppure clona la repo ed esegui `uv sync`), registralo nel mio file di configurazione MCP usando il percorso assoluto dell'eseguibile, e spiegami come lanciare `classeviva-mcp login` nel terminale per salvare le credenziali nel Keyring di sistema.
> ```

### Punto 1 — Hai già un client AI compatibile MCP o un Agent Harness?
* **SÌ** (uso già Claude Desktop/Code, Codex, Opencode, Cursor, Antigravity o simili) 👉 **Procedi al Punto 2**.
* **NO** 👉 Scarica e installa gratis **[Claude Desktop](https://claude.ai/download)**, **[Opencode](https://opencode.ai)** oppure **[Cursor](https://cursor.com)**.

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
3. **Classe e Indirizzo** (es. `5AI` e `Informatica`).
4. **Link `.ics` di Google Classroom** *(Opzionale, premi Invio per saltare)*.

Al termine, il comando **`login` stamperà a schermo il blocco JSON già pronto con i percorsi assoluti del tuo PC** (così eviti errori `spawn uvx ENOENT` su Windows).

### Punto 4 — Incolla il JSON nel tuo Client AI e usa `/setup`
1. Copia il JSON stampato dal comando `login` e incollalo nel file di configurazione del tuo client:
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

2. Apri la chat con la tua AI e scrivi:
   > *"Fai `/setup`: sono della 5AI (nella mia scuola I=Informatica, AC=Chimica, AM=Meccanica, AL=Logistica). Controlla se in bacheca c'è l'orario delle lezioni e salva quello della 5AI."*

L'AI salverà in locale le sigle del tuo istituto e l'orario settimanale, senza dover rileggere PDF di 50 pagine ogni giorno.

---

## 📚 Opzionale: Collegare Google Classroom (OAuth2 vs `.ics`)

### Metodo A — Google Classroom OAuth2 (`GOOGLE_ACCESS_TOKEN`)
Se sei un docente o la tua scuola non blocca le app OAuth esterne sull'email di istituto, puoi impostare la variabile d'ambiente `GOOGLE_ACCESS_TOKEN` per interrogare direttamente le API REST `classroom.googleapis.com`.

*(Nota: nella maggior parte degli istituti superiori italiani gli admin Google Workspace bloccano l'OAuth agli studenti; usa il **Metodo B** se ricevi "Accesso bloccato").*

### Metodo B — Link `.ics` iCal (Se l'OAuth è bloccato dalla scuola)
1. Apri **[Google Calendar](https://calendar.google.com)** con l'account scolastico -> clicca l'ingranaggio ⚙️ in alto a destra -> **Impostazioni**.
2. Nella colonna a sinistra clicca sul calendario del tuo corso Classroom.
3. Scorri fino a **Indirizzo segreto in formato iCal**, copia il link che finisce in `.ics` e incollalo durante `classeviva-mcp login`.

*(Nota: il feed `.ics` riporta tutti i compiti con data di scadenza; i post sullo Stream senza scadenza non fanno parte del calendario).*

---

## 📱 Opzionale: Usarlo da Smartphone su Discord e Telegram (DM e Gruppi Classe)

Cosa ti serve per interrogare il registro dal telefono:
* **Un dispositivo sempre attivo**: può essere il tuo PC di casa, un vecchio telefono Android con **Termux**, oppure una VPS gratuita come **Oracle Cloud Free Tier**.
* **Un Provider AI o accesso remoto al tuo harness**: puoi usare l'accesso remoto del tuo agent (es. Claude Cloud, Antigravity remoto) oppure, se non hai abbonamenti AI a pagamento, creare una chiave gratuita su **[OpenRouter](https://openrouter.ai)** e usare i modelli `:free` tramite **Opencode**, **Hermes Agent**, o facendo rerouting con **CC Switch**.
* **Regola "1 Bot = 1 Proprietario"**: collega `classeviva-mcp` al tuo agente/bot personale (es. Hermes Agent, OpenClaw, n8n) filtrando i comandi sul tuo `OWNER_USER_ID` così risponde solo a te.
* **Modalità Pubblica Sicura (`CLASSEVIVA_PUBLIC_MODE=true`)**: se aggiungi il bot in un server Discord o gruppo Telegram condiviso con la classe, imposta la variabile d'ambiente `CLASSEVIVA_PUBLIC_MODE=true`. I tool dei voti e delle assenze vengono **disabilitati a livello di codice Python** (impossibile esporre i tuoi voti anche con prompt injection), mentre tutta la classe può leggere i riassunti delle circolari e scaricare i PDF allegati in chat.
* **Permessi Bot Discord pre-calcolati**: su [Discord Developer Portal](https://discord.com/developers/applications) attiva *Message Content Intent* nella scheda *Bot*, poi in *OAuth2 -> URL Generator* spunta `bot` + `applications.commands` con Permissions Integer **`274877975552`** (include invio messaggi, embed e allegati PDF).

---

## 🛠️ Riepilogo Tool MCP

*(Imposta `CLASSEVIVA_PUBLIC_MODE=true` nelle variabili d'ambiente del server quando lo usi in gruppi o server condivisi con altre persone).*

| Tool | Descrizione | Disabilitato in `PUBLIC_MODE`? |
|---|---|---|
| `get_today_context` | Data/ora (`Europe/Rome`), profilo, materie/professori, contesto classe e orario salvato. | No |
| `update_student_context` | Salva classe (`5AI`), indirizzo, n. sul registro e note sulle sigle dell'istituto. | No |
| `save_class_timetable` | Valida e salva in locale l'orario settimanale della classe. | No |
| `get_homework` | Compiti e consegne dei prossimi N giorni (ClasseViva + Classroom). | No |
| `get_agenda` | Verifiche, interrogazioni, compiti e note tra due date. | No |
| `get_lessons` | **Argomenti spiegati dai prof** a lezione per preparare verifiche e recuperi. | No |
| `list_notices` | Cerca circolari in bacheca per parola chiave senza segnarle lette. | No |
| `read_notice_pdf` | Estrae testo da `.pdf`/`.docx`/`.xlsx`/`.zip` filtrando per classe, o renderizza PNG se scansionato. | No |
| `get_grades` | Voti, medie ponderate per materia ed elenco insufficienze (`< 6.0`). | 🔒 **Sì** |
| `calculate_grade_target` | Calcola quale voto serve nella prossima verifica per arrivare alla media obiettivo. | 🔒 **Sì** |
| `get_absences` | Assenze, ritardi e uscite anticipate. | 🔒 **Sì** |

---

## ⚠️ Disclaimer (MIT License)

Progetto open-source indipendente, **non affiliato** a Gruppo Spaggiari Parma S.p.A. né a Google LLC. Il software è fornito *"AS IS"* senza alcuna garanzia (vedi [LICENSE](LICENSE)). L'utente è l'unico responsabile del rispetto dei Termini di Servizio della propria scuola e dei provider AI scelti.
