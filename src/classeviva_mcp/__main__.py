from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import sys
from pathlib import Path

from classeviva_mcp import __version__
from classeviva_mcp.classroom_ical import validate_ical_url
from classeviva_mcp.client import ClasseVivaError, SpaggiariClient
from classeviva_mcp.credentials import CredentialStore, StoredCredentials
from classeviva_mcp.server import mcp


def _build_ready_mcp_config() -> str:
    """Builds a copy-pasteable MCP JSON snippet with absolute paths.

    GUI clients that spawn the server directly cannot resolve bare command names,
    so we always point to the concrete executable (or to `uv`) for this machine.
    """
    installed_bin = shutil.which("classeviva-mcp")
    pkg_dir = Path(__file__).resolve().parent.parent.parent
    if (pkg_dir / "pyproject.toml").exists():
        uv_bin = shutil.which("uv") or "uv"
        cfg = {
            "mcpServers": {
                "classeviva": {
                    "command": uv_bin,
                    "args": [
                        "run",
                        "--directory",
                        str(pkg_dir),
                        "classeviva-mcp",
                    ],
                }
            }
        }
    elif installed_bin:
        cfg = {
            "mcpServers": {
                "classeviva": {
                    "command": installed_bin,
                    "args": [],
                }
            }
        }
    else:
        uvx_bin = shutil.which("uvx") or "uvx"
        cfg = {
            "mcpServers": {
                "classeviva": {
                    "command": uvx_bin,
                    "args": [
                        "--from",
                        "git+https://github.com/NihaoBoo/classeviva-mcp@stable",
                        "classeviva-mcp",
                    ],
                }
            }
        }
    return json.dumps(cfg, indent=2)


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


async def _cmd_login(args: argparse.Namespace) -> int:
    store = CredentialStore(allow_insecure_plaintext=args.allow_insecure_plaintext)
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

    password = getpass.getpass("2. Password ClasseViva (nascosta mentre scrivi): ")
    if not password:
        print("Errore: password vuota.", file=sys.stderr)
        return 1

    school_class = input("3. La tua Classe e Sezione [es. 5AI, premi Invio per configurarla dopo]: ").strip()
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
    except ClasseVivaError as exc:
        print(f"\n[ERRORE LOGIN] {exc}", file=sys.stderr)
        print("Nessuna credenziale e' stata salvata: la password non era corretta.", file=sys.stderr)
        return 1

    try:
        backend = store.save(username=username, password=password, classroom_ical_url=ical_url)
    except RuntimeError as exc:
        print(f"Errore salvataggio credenziali: {exc}", file=sys.stderr)
        return 1

    if school_class or course_name:
        ctx = store.load_student_context()
        if school_class:
            ctx.school_class = school_class.upper()
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
    print("  PROSSIMO PASSO: collega il server al tuo client MCP")
    print("-" * 64)
    print()
    print("  1. Copia il blocco JSON qui sotto.")
    print()
    print("  2. Incollalo in uno di questi modi:")
    print("     - Nella configurazione MCP del tuo client (Claude, Cursor,")
    print("       Codex, Antigravity, Opencode, ...).")
    print("     - Oppure nella chat del tuo agente AI, scrivendo ad esempio:")
    print('       "Configura questo server MCP per me."')
    print()
    print("  Il JSON contiene il percorso assoluto di questa installazione:")
    print("  su un altro computer sarà diverso, quindi non riusarlo altrove.")
    print()
    print("-" * 64)
    for line in _build_ready_mcp_config().splitlines():
        print("  " + line)
    print("-" * 64)
    print()
    print("  Dopo aver salvato la configurazione, riavvia il client e chiedi")
    print("  all'agente qualcosa inerente al registro, come leggere voti o")
    print("  agenda, per verificare che funzioni.")
    print()
    print("  Computer condiviso o di scuola? Esegui `logout --purge` prima di uscire.")
    print()
    return 0


async def _cmd_doctor(args: argparse.Namespace) -> int:
    """Runs a privacy-safe diagnostic check (never prints names, grades, or passwords)."""
    store = CredentialStore(allow_insecure_plaintext=args.allow_insecure_plaintext)
    print("=== ClasseViva MCP — Diagnostica (Privacy-Safe) ===")
    try:
        creds = store.load()
        print(f"[OK] Credenziali trovate (sorgente: {creds.source})")
        print(f"     Cartella dati      : {store.data_dir}")
        print(f"     Sessione in cache  : {'presente' if store.session_file.exists() else 'assente'}")
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
    except Exception as exc:
        print(f"[FAIL] Login Spaggiari fallito: {exc}")
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


def _cmd_serve(args: argparse.Namespace) -> None:
    """Starts the MCP server on the requested transport."""
    if args.transport == "stdio":
        mcp.run()
        return

    from classeviva_mcp.http_server import is_loopback, resolve_token, run_http_server

    store = CredentialStore()
    token, source = resolve_token(args.token_file or None, store.data_dir)

    if source == "generato automaticamente":
        store.save_mcp_token(token)
        print(f"Token generato e salvato in: {store.mcp_token_file}", file=sys.stderr)

    if args.print_token:
        print(token, flush=True)

    if not is_loopback(args.host):
        print(
            "ATTENZIONE: ascolto su un indirizzo raggiungibile dalla rete.\n"
            "Il traffico HTTP non e' cifrato: il token viaggia in chiaro.\n"
            "Usalo solo su rete privata fidata o dietro un tunnel (Tailscale, SSH).",
            file=sys.stderr,
        )

    run_http_server(
        mcp,
        host=args.host,
        port=args.port,
        token=token,
        path=args.mcp_path,
    )


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

    args = parser.parse_args()

    if args.command == "login":
        sys.exit(asyncio.run(_cmd_login(args)))
    elif args.command == "doctor":
        sys.exit(asyncio.run(_cmd_doctor(args)))
    elif args.command == "set-classroom-url":
        sys.exit(_cmd_set_classroom_url(args))
    elif args.command == "logout":
        sys.exit(_cmd_logout(args))
    elif args.command == "serve":
        _cmd_serve(args)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
