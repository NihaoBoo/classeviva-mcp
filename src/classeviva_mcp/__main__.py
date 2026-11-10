from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
from pathlib import Path

from classeviva_mcp import __version__
from classeviva_mcp.classroom_ical import validate_ical_url
from classeviva_mcp.client import ClasseVivaAuthError, ClasseVivaError, SpaggiariClient
from classeviva_mcp.credentials import CredentialStore, StoredCredentials
from classeviva_mcp.network import detected_proxy, tls_backend_label
from classeviva_mcp.server import _normalize_class_code, mcp

CLIENT_FORMATS = ("generic", "opencode", "vscode", "codex", "claude-code")


def _looks_ephemeral(path: Path) -> bool:
    """True when ``path`` lives inside uv's cache (e.g. an ``uvx`` throwaway env).

    ``uvx classeviva-mcp login`` runs from ``<uv cache>/archive-v0/<hash>/`` and
    puts that folder's ``Scripts``/``bin`` first on PATH. Pointing an MCP
    client at that executable works once, then breaks as soon as uv prunes or
    refreshes its cache (``uv cache clean``, a new release, a school PC that
    wipes AppData at logoff...).
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    parts = [p.lower() for p in resolved.parts]
    if any(p.startswith(("archive-v", "builds-v", "environments-v")) for p in parts):
        return True
    for first, second in zip(parts, parts[1:], strict=False):
        if (first, second) in {("uv", "cache"), (".cache", "uv")}:
            return True
    cache_dir = os.environ.get("UV_CACHE_DIR", "").strip()
    if cache_dir:
        try:
            resolved.relative_to(Path(cache_dir).resolve())
            return True
        except (ValueError, OSError):
            pass
    return False


def _running_from_ephemeral_env() -> bool:
    return _looks_ephemeral(Path(sys.prefix)) or _looks_ephemeral(Path(sys.executable))


def _find_uvx() -> str:
    uvx = shutil.which("uvx")
    if uvx:
        return uvx
    uv = shutil.which("uv")
    if uv:
        sibling = Path(uv).with_name("uvx.exe" if sys.platform == "win32" else "uvx")
        if sibling.exists():
            return str(sibling)
    return "uvx"


def _resolve_launch_command() -> tuple[str, list[str], str]:
    """Returns ``(command, args, kind)`` for a stable way to start the server.

    ``kind`` is ``dev`` (local checkout), ``installed`` (``uv tool install`` /
    pip, stable path) or ``uvx`` (run on demand, survives cache cleanups).
    """
    pkg_dir = Path(__file__).resolve().parent.parent.parent
    if (pkg_dir / "pyproject.toml").exists() and not _looks_ephemeral(pkg_dir):
        uv_bin = shutil.which("uv") or "uv"
        return uv_bin, ["run", "--directory", str(pkg_dir), "classeviva-mcp"], "dev"

    installed_bin = shutil.which("classeviva-mcp")
    if installed_bin and not _running_from_ephemeral_env() and not _looks_ephemeral(Path(installed_bin)):
        return str(Path(installed_bin).absolute()), [], "installed"

    return _find_uvx(), ["classeviva-mcp"], "uvx"


def _build_ready_mcp_config(client: str = "generic") -> str:
    """Builds a copy-pasteable MCP config with absolute paths for ``client``.

    GUI clients that spawn the server directly cannot resolve bare command names,
    so we always point to a concrete executable (``classeviva-mcp``, ``uv`` or
    ``uvx``) and never to a file inside uv's cache.
    """
    command, args, _kind = _resolve_launch_command()
    if client == "opencode":
        return json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "classeviva": {
                        "type": "local",
                        "command": [command, *args],
                        "enabled": True,
                    }
                },
            },
            indent=2,
        )
    if client == "vscode":
        return json.dumps(
            {"servers": {"classeviva": {"type": "stdio", "command": command, "args": args}}},
            indent=2,
        )
    if client == "codex":
        toml_args = ", ".join(json.dumps(a) for a in args)
        return f"[mcp_servers.classeviva]\ncommand = {json.dumps(command)}\nargs = [{toml_args}]"
    if client == "claude-code":
        quoted = " ".join(json.dumps(part) for part in [command, *args])
        return f"claude mcp add --scope user classeviva -- {quoted}"
    return json.dumps({"mcpServers": {"classeviva": {"command": command, "args": args}}}, indent=2)


def _launch_advice() -> list[str]:
    """Human hints about the chosen launch method (printed by login/config/doctor)."""
    _command, _args, kind = _resolve_launch_command()
    if kind == "uvx":
        return [
            "Stai usando `uvx`: il JSON avvia il server tramite uvx e non punta alla",
            "cache di uv (che viene svuotata e cambierebbe percorso). Il primo avvio",
            "dal client AI può richiedere ~30 secondi per scaricare le dipendenze.",
            "Per un avvio istantaneo e un percorso fisso installalo una volta con:",
            "    uv tool install classeviva-mcp",
            "e rigenera il JSON con `classeviva-mcp config`.",
        ]
    return []


def _mask_ident(ident: str) -> str:
    value = (ident or "").strip()
    if len(value) <= 5:
        return "***"
    return f"{value[0]}****{value[-4:]}"


def _credential_backend_label(backend: str) -> tuple[str, str]:
    """Returns a friendly name and a short description for the storage backend."""
    if backend == "keyring":
        return (
            "Keyring di sistema",
            "Gestione Credenziali Windows / Portachiavi macOS / Secret Service Linux",
        )
    if backend == "dpapi":
        return ("Archivio cifrato Windows (DPAPI)", "Protezione legata al tuo account utente")
    return ("File locale (fallback)", "Attivato esplicitamente con --allow-insecure-plaintext")


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _stored_username(store: CredentialStore) -> str:
    """Username of the credentials currently stored (env excluded), or ''."""
    try:
        creds = store.load()
    except Exception:  # noqa: BLE001 - nothing stored
        return ""
    return creds.username if creds.source != "env" else ""


def _env_overrides_active() -> bool:
    return bool(os.getenv("CLASSEVIVA_USER", "").strip() and os.getenv("CLASSEVIVA_PASSWORD", "").strip())


async def _cmd_login(args: argparse.Namespace) -> int:
    store = CredentialStore(allow_insecure_plaintext=args.allow_insecure_plaintext)
    if not sys.stdin.isatty():
        print(
            "Errore: `login` va eseguito in un terminale interattivo (PowerShell, Terminale),\n"
            "non dalla chat o dagli strumenti dell'agente AI: la password non deve passare\n"
            "dalla conversazione. In alternativa imposta CLASSEVIVA_USER e CLASSEVIVA_PASSWORD\n"
            "nell'ambiente del client MCP.",
            file=sys.stderr,
        )
        return 1
    print()
    print("=" * 64)
    print("  ClasseViva MCP - Login sicuro")
    print("=" * 64)
    print()
    print("  Inserisci la tua email personale oppure il codice Studente (S...)")
    print("  o Genitore (G...). Le credenziali restano sul tuo computer.")
    print()

    # Fail fast if there is no secure place to store the credentials: better
    # to know before typing the password than after.
    try:
        backend = store.probe_storage()
    except RuntimeError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1

    username = input("1. Email o Codice Utente ClasseViva: ").strip()
    if not username:
        print("Errore: utente vuoto.", file=sys.stderr)
        return 1
    if any(ch.isspace() for ch in username) or _has_control_chars(username):
        print("Errore: l'utente non può contenere spazi o caratteri invisibili.", file=sys.stderr)
        return 1

    password = getpass.getpass("2. Password ClasseViva (nascosta mentre scrivi): ")
    # Pasting can append a newline/CR; never strip regular spaces, they may be real.
    password = password.rstrip("\r\n")
    if not password:
        print("Errore: password vuota.", file=sys.stderr)
        return 1
    if _has_control_chars(password):
        print(
            "Errore: la password contiene caratteri di controllo (es. ^V). Succede incollando\n"
            "con Ctrl+V in alcune console Windows: incolla col tasto destro del mouse,\n"
            "usa Windows Terminal oppure digitala a mano.",
            file=sys.stderr,
        )
        return 1
    if password != password.strip():
        print(
            "Attenzione: la password inizia o finisce con uno spazio. Se non è voluto,\n"
            "rilancia `login` e ricopiala senza spazi.",
            file=sys.stderr,
        )

    school_class = input("3. La tua Classe e Sezione [es. 5AI, premi Invio per configurarla dopo]: ").strip()
    if school_class:
        try:
            school_class = _normalize_class_code(school_class)
        except ValueError as exc:
            print(f"Errore classe: {exc}", file=sys.stderr)
            return 1
    course_name = input("4. Indirizzo di studio [es. Informatica, premi Invio per saltare]: ").strip()
    ical_url = input("5. Link .ics Google Classroom [Opzionale, premi Invio per saltare]: ").strip()
    if ical_url:
        try:
            ical_url = validate_ical_url(ical_url)
        except ValueError as exc:
            print(f"Errore link iCal: {exc}", file=sys.stderr)
            return 1

    # Validate the credentials against Spaggiari BEFORE storing them: a wrong
    # password must never end up in the Keyring or in the fallback file.
    candidate = StoredCredentials(
        username=username,
        password=password,
        classroom_ical_url=ical_url,
        source="login",
    )

    probe_client = SpaggiariClient(store=store, credentials=candidate, persist_session=False)
    try:
        profile = await probe_client.get_profile()
    except ClasseVivaAuthError as exc:
        print(f"\n[ERRORE LOGIN] {exc}", file=sys.stderr)
        print("Nessuna nuova credenziale e' stata salvata.", file=sys.stderr)
        previous = _stored_username(store)
        if previous:
            print(
                f"Restano salvate le credenziali precedenti ({_mask_ident(previous)}): il server MCP\n"
                "e `doctor` continueranno a usarle. Per rimuoverle: `classeviva-mcp logout`.",
                file=sys.stderr,
            )
        return 1
    except ClasseVivaError as exc:
        print(f"\n[ERRORE CONNESSIONE] {exc}", file=sys.stderr)
        print("Nessuna credenziale e' stata salvata (password non verificata).", file=sys.stderr)
        return 1
    finally:
        await probe_client.aclose()

    try:
        backend = store.save(username=username, password=password, classroom_ical_url=ical_url)
    except RuntimeError as exc:
        print(f"Errore salvataggio credenziali: {exc}", file=sys.stderr)
        return 1

    if school_class or course_name:
        ctx = store.load_student_context()
        if school_class:
            ctx.school_class = school_class
        if course_name:
            ctx.course_name = course_name
        store.save_student_context(ctx)

    backend_label, backend_detail = _credential_backend_label(backend)
    print()
    print("=" * 64)
    print("  LOGIN COMPLETATO")
    print("=" * 64)
    print()
    print(f"  Account    : {profile.account_type.upper()} ({_mask_ident(profile.ident)})")
    print(f"  Sicurezza  : {backend_label}")
    print(f"               {backend_detail}")
    if args.verbose:
        if profile.school_name:
            print(f"  Istituto   : {profile.school_name}")
        if school_class or profile.school_class:
            print(f"  Classe     : {(school_class or profile.school_class).upper()}")
    print()
    print("-" * 64)
    print("  CONFIGURAZIONE MCP")
    print("-" * 64)
    print()
    print("  1. Copia il blocco JSON qui sotto.")
    print("  2. Incollalo nella configurazione MCP del tuo client,")
    print("     oppure nella chat del tuo agente AI chiedendo di")
    print("     configurare il server.")
    print()
    print("  Il JSON è specifico di questo computer: su un'altra")
    print("  macchina esegui di nuovo `login` per ottenerne uno valido.")
    print("  Formati per altri client: `classeviva-mcp config --client")
    print("  opencode|vscode|codex|claude-code`.")
    print()
    print("-" * 64)
    for line in _build_ready_mcp_config(getattr(args, "client", "generic")).splitlines():
        print("  " + line)
    print("-" * 64)
    for line in _launch_advice():
        print("  " + line)
    if _env_overrides_active():
        print()
        print("  ATTENZIONE: CLASSEVIVA_USER/CLASSEVIVA_PASSWORD sono impostate e hanno")
        print("  la precedenza sulle credenziali appena salvate.")
    print()
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """Prints only the MCP configuration (no credentials needed)."""
    print(_build_ready_mcp_config(args.client))
    advice = _launch_advice()
    if advice:
        print(file=sys.stderr)
        for line in advice:
            print(line, file=sys.stderr)
    return 0


async def _cmd_doctor(args: argparse.Namespace) -> int:
    """Runs a privacy-safe diagnostic check (never prints names, grades, or passwords)."""
    store = CredentialStore(allow_insecure_plaintext=args.allow_insecure_plaintext)
    print("=== ClasseViva MCP - Diagnostica (Privacy-Safe) ===")
    command, cmd_args, kind = _resolve_launch_command()
    print(f"[i]  Versione {__version__}, Python {sys.version.split()[0]}, {sys.platform}")
    print(f"[i]  Avvio consigliato  : {kind} -> {' '.join([command, *cmd_args])}")
    if _running_from_ephemeral_env():
        print("[i]  In esecuzione dalla cache di uv (uvx): non usare questo percorso nel JSON MCP.")
    print(f"[i]  Rete               : proxy {detected_proxy()}; TLS {tls_backend_label()}")
    try:
        creds = store.load()
        ident_kind = "email" if "@" in creds.username else "codice utente"
        print(f"[OK] Credenziali trovate (sorgente: {creds.source}, {ident_kind} {_mask_ident(creds.username)})")
        print(f"     Cartella dati      : {store.data_dir}")
        print(f"     Sessione in cache  : {'presente' if store.session_file.exists() else 'assente'}")
        if creds.source == "env":
            print("[i]  CLASSEVIVA_USER/CLASSEVIVA_PASSWORD hanno la precedenza sul Keyring.")
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1

    client = SpaggiariClient(store=store)
    try:
        profile = await client.get_profile()
        ctx = store.load_student_context()
        info_label = "OK"
        if args.verbose:
            info_label = ctx.school_class or profile.school_class or profile.school_name or "OK"
        print(
            f"[OK] Login Spaggiari riuscito "
            f"(tipo account: {profile.account_type}, info: {info_label})"
        )
    except ClasseVivaAuthError as exc:
        print(f"[FAIL] Login Spaggiari fallito: {exc}")
        print("       Le credenziali salvate non sono valide: esegui `classeviva-mcp login`")
        print("       (le vecchie vengono sostituite solo se la nuova password è corretta).")
        await client.aclose()
        return 1
    except Exception as exc:
        print(f"[FAIL] Login Spaggiari fallito: {exc}")
        await client.aclose()
        return 1

    try:
        grades = await client.get_grades()
        notices = await client.list_notices()
        subjects = await client.get_subjects()
        print(f"[OK] Endpoint Materie/Prof raggiungibile ({len(subjects)} materie rilevate)")
        print(f"[OK] Endpoint Voti raggiungibile ({len(grades)} voti letti)")
        print(f"[OK] Endpoint Bacheca raggiungibile ({len(notices)} circolari in elenco)")
    except Exception as exc:
        print(f"[WARN] Errore lettura endpoint: {exc}")
        return 1
    finally:
        await client.aclose()

    print("\nTUTTI I CONTROLLI SUPERATI! Il server MCP è pronto per i tuoi agent.")
    return 0


def _cmd_set_classroom_url(args: argparse.Namespace) -> int:
    url = (args.url or "").strip()
    if url:
        print(
            "Attenzione: passare il link come argomento puo' lasciarlo nella cronologia del terminale.",
            file=sys.stderr,
        )
    else:
        url = getpass.getpass(
            "Link segreto .ics di Google Classroom (input nascosto): "
        ).strip()
    if not url:
        print("Errore: URL vuoto.", file=sys.stderr)
        return 1
    try:
        clean = validate_ical_url(url)
    except ValueError as exc:
        print(f"Errore link iCal: {exc}", file=sys.stderr)
        return 1
    store = CredentialStore(allow_insecure_plaintext=args.allow_insecure_plaintext)
    try:
        store.save_classroom_ical_url(clean)
    except RuntimeError as exc:
        print(f"Errore salvataggio URL iCal: {exc}", file=sys.stderr)
        return 1
    print("[OK] URL iCal salvato nel Keyring o nell'archivio cifrato.")
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    store = CredentialStore()
    try:
        store.clear(purge_all=args.purge)
    except RuntimeError as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        return 1
    print(f"Cartella dati: {store.data_dir}")
    if args.purge:
        print("Credenziali, sessione, contesto studente e allegati rimossi.")
    else:
        print("Credenziali e sessione rimosse. Orario e note conservati.")
        print("Su un computer condiviso usa `logout --purge` per cancellare tutto.")
    print("Nota: le variabili d'ambiente non vengono modificate; rimuovile a mano se le usi.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Starts the MCP server on the requested transport."""
    if args.transport == "stdio":
        mcp.run()
        return 0

    from classeviva_mcp.http_server import is_loopback, normalize_public_url, resolve_token, run_http_server

    store = CredentialStore()
    if not is_loopback(args.host) and not args.allow_lan:
        print(
            "Errore: --host diverso da 127.0.0.1 espone HTTP non cifrato sulla rete.\n"
            "Pubblica il server con un tunnel HTTPS (Tailscale Funnel, Cloudflare Tunnel) lasciando\n"
            "--host 127.0.0.1, oppure aggiungi --allow-lan se sai cosa stai facendo (es. Docker).",
            file=sys.stderr,
        )
        return 2

    public_url: str | None = None
    if args.public_url:
        try:
            public_url = normalize_public_url(args.public_url)
        except ValueError as exc:
            print(f"Errore: {exc}", file=sys.stderr)
            return 2

    auth_mode = args.auth or ("oauth" if public_url else "token")
    if auth_mode == "oauth":
        from classeviva_mcp.oauth import LocalOAuthProvider

        if not public_url:
            print("Errore: --auth oauth richiede --public-url (l'indirizzo HTTPS del tunnel).", file=sys.stderr)
            return 2
        provider = LocalOAuthProvider(
            read_state=store.load_oauth_state,
            write_state=store.save_oauth_state,
            public_url=public_url,
            resource_url=f"{public_url}{args.mcp_path}",
        )
        if not provider.has_owner():
            print("Errore: esegui prima `classeviva-mcp remote-setup` per creare la passphrase.", file=sys.stderr)
            return 2
        run_http_server(
            mcp,
            host=args.host,
            port=args.port,
            path=args.mcp_path,
            public_url=public_url,
            oauth_provider=provider,
        )
        return 0

    token, source = resolve_token(args.token_file or None, store.data_dir, store.load_mcp_token())
    if source == "generato automaticamente":
        store.save_mcp_token(token)
        print(f"Token generato e salvato in: {store.mcp_token_file}", file=sys.stderr)
    if args.print_token:
        print(token, flush=True)
    run_http_server(mcp, host=args.host, port=args.port, token=token, path=args.mcp_path, public_url=public_url)
    return 0


def _cmd_remote_setup(args: argparse.Namespace) -> int:
    """Creates (or rotates) the owner passphrase used to approve remote connectors."""
    from classeviva_mcp.oauth import MIN_OWNER_PASSPHRASE, owner_passphrase_problem, set_owner_passphrase

    store = CredentialStore()
    try:
        existing = bool(store.load_oauth_state().get("owner"))
    except Exception:  # noqa: BLE001 - unreadable state is replaced
        existing = False
    if existing and not args.rotate:
        print("Accesso remoto già configurato. Usa `remote-setup --rotate` per una nuova passphrase")
        print("(scollega anche tutte le app già collegate).")
        return 0
    if not sys.stdin.isatty():
        print("Errore: `remote-setup` va eseguito in un terminale interattivo.", file=sys.stderr)
        return 1
    print("Scegli la passphrase del proprietario: la userai per approvare ogni app collegata.")
    print(f"Almeno {MIN_OWNER_PASSPHRASE} caratteri; una frase di 4-5 parole è facile da digitare sul telefono.")
    passphrase = getpass.getpass("Passphrase (nascosta): ")
    problem = owner_passphrase_problem(passphrase)
    if problem:
        print(f"Errore: {problem}", file=sys.stderr)
        return 1
    if getpass.getpass("Ripeti la passphrase: ") != passphrase:
        print("Errore: le due passphrase non coincidono.", file=sys.stderr)
        return 1
    set_owner_passphrase(store.load_oauth_state, store.save_oauth_state, passphrase)
    port = args.port
    print()
    print("=" * 64)
    print("  ACCESSO REMOTO (OAuth) CONFIGURATO")
    print("=" * 64)
    print()
    print("  Salva la passphrase nel tuo gestore di password: qui ne resta solo un hash.")
    print()
    print("  1. Pubblica il server in HTTPS con un tunnel gratuito, ad esempio:")
    print(f"       tailscale funnel --bg {port}")
    print("     (oppure un Cloudflare Tunnel con il tuo dominio).")
    print("  2. Avvia il server con l'URL HTTPS del tunnel:")
    print(f"       classeviva-mcp serve --transport streamable-http --port {port} \\")
    print("         --public-url https://<nome-pc>.<tailnet>.ts.net")
    print("  3. Nell'app (Claude: Impostazioni > Connettori > Aggiungi connettore")
    print("     personalizzato; ChatGPT: Connettori in modalità sviluppatore)")
    print("     incolla l'URL  https://<...>/mcp  e approva con la passphrase.")
    print()
    print("  Revoca tutte le app collegate: classeviva-mcp remote-revoke")
    print()
    return 0


def _cmd_remote_revoke(args: argparse.Namespace) -> int:
    from classeviva_mcp.oauth import revoke_all

    store = CredentialStore()
    count = revoke_all(store.load_oauth_state, store.save_oauth_state)
    store.mcp_token_file.unlink(missing_ok=True)
    print(f"Revocate {count} sessioni OAuth e il token bearer salvato. Le app dovranno ricollegarsi.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="classeviva-mcp",
        description="Server MCP per Spaggiari ClasseViva e Google Classroom",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser(
        "login", help="Salva le credenziali nel Keyring di sistema e stampa il JSON MCP pronto"
    )
    login.add_argument(
        "--allow-insecure-plaintext",
        action="store_true",
        help="Consenti il fallback in chiaro se Keyring e DPAPI non sono disponibili",
    )
    login.add_argument("--verbose", action="store_true", help="Mostra scuola e classe in output")
    login.add_argument(
        "--client",
        choices=CLIENT_FORMATS,
        default="generic",
        help="Formato della configurazione MCP stampata (default: generic / mcpServers)",
    )

    config = sub.add_parser("config", help="Stampa la configurazione MCP per questo computer (nessun login richiesto)")
    config.add_argument(
        "--client",
        choices=CLIENT_FORMATS,
        default="generic",
        help="generic (Claude Desktop, Cursor, Antigravity...), opencode, vscode, codex, claude-code",
    )

    doctor = sub.add_parser(
        "doctor", help="Verifica la connessione a Spaggiari senza stampare dati personali"
    )
    doctor.add_argument(
        "--allow-insecure-plaintext",
        action="store_true",
        help="Consenti la lettura del fallback in chiaro",
    )
    doctor.add_argument("--verbose", action="store_true", help="Mostra scuola e classe in output")

    set_url = sub.add_parser(
        "set-classroom-url", help="Salva il link segreto .ics di Google Classroom senza passare dalla chat"
    )
    set_url.add_argument(
        "url",
        nargs="?",
        help="URL .ics (sconsigliato: resta nella cronologia; se omesso viene chiesto con input nascosto)",
    )
    set_url.add_argument(
        "--allow-insecure-plaintext",
        action="store_true",
        help="Consenti il fallback in chiaro se Keyring e DPAPI non sono disponibili",
    )

    logout = sub.add_parser("logout", help="Rimuove le credenziali e la sessione salvata")
    logout.add_argument(
        "--purge",
        action="store_true",
        help="Rimuovi anche student_context.json e la cache degli allegati",
    )

    serve = sub.add_parser(
        "serve", help="Avvia il server MCP su trasporto stdio (default) o streamable-http"
    )
    serve.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="Trasporto MCP: stdio (default, locale) oppure streamable-http (richiede un token)",
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Indirizzo di ascolto per streamable-http (default: 127.0.0.1, solo locale)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Porta di ascolto per streamable-http (default: 8765)",
    )
    serve.add_argument(
        "--mcp-path",
        default="/mcp",
        help="Percorso HTTP del server MCP (default: /mcp)",
    )
    serve.add_argument(
        "--token-file",
        default="",
        help="File con il bearer token (altrimenti CLASSEVIVA_MCP_TOKEN o token generato)",
    )
    serve.add_argument(
        "--print-token",
        action="store_true",
        help="Stampa il token bearer usato per streamable-http",
    )
    serve.add_argument(
        "--public-url",
        default="",
        help="Indirizzo HTTPS pubblico del tunnel (es. https://pc.tail1234.ts.net); attiva OAuth",
    )
    serve.add_argument(
        "--auth",
        choices=("oauth", "token"),
        default=None,
        help="oauth (default con --public-url: app Claude/ChatGPT) oppure token (header Bearer)",
    )
    serve.add_argument(
        "--allow-lan",
        action="store_true",
        help="Consenti --host diverso da 127.0.0.1 (HTTP non cifrato: solo reti fidate o Docker)",
    )

    remote_setup = sub.add_parser(
        "remote-setup", help="Configura l'accesso remoto OAuth (app mobile) e mostra la passphrase"
    )
    remote_setup.add_argument("--rotate", action="store_true", help="Genera una nuova passphrase e scollega tutto")
    remote_setup.add_argument("--port", type=int, default=8765, help="Porta usata negli esempi (default 8765)")

    sub.add_parser("remote-revoke", help="Scollega tutte le app remote (OAuth e token bearer)")

    args = parser.parse_args()

    if args.command not in (None, "serve"):
        # Windows consoles/pipes may use a legacy code page: never crash on output.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass

    if args.command == "login":
        sys.exit(asyncio.run(_cmd_login(args)))
    elif args.command == "config":
        sys.exit(_cmd_config(args))
    elif args.command == "doctor":
        sys.exit(asyncio.run(_cmd_doctor(args)))
    elif args.command == "set-classroom-url":
        sys.exit(_cmd_set_classroom_url(args))
    elif args.command == "logout":
        sys.exit(_cmd_logout(args))
    elif args.command == "serve":
        sys.exit(_cmd_serve(args))
    elif args.command == "remote-setup":
        sys.exit(_cmd_remote_setup(args))
    elif args.command == "remote-revoke":
        sys.exit(_cmd_remote_revoke(args))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
