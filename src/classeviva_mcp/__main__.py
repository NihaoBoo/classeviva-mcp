from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import sys
from pathlib import Path

from classeviva_mcp.client import ClasseVivaError, SpaggiariClient
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.server import mcp


def _build_ready_mcp_config() -> str:
    """Builds a copy-pasteable JSON snippet using the absolute path of `classeviva-mcp` or `uv`
    so GUI clients like Claude Desktop or Cursor never fail with `spawn ENOENT`.
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
                        "git+https://github.com/NihaoBoo/classeviva-mcp",
                        "classeviva-mcp",
                    ],
                }
            }
        }
    return json.dumps(cfg, indent=2)


async def _cmd_login() -> int:
    store = CredentialStore()
    print("=== ClasseViva MCP — Login Sicuro (Keyring di Sistema) ===")
    print("Puoi inserire la tua Email personale oppure il codice Studente (S...) / Genitore (G...).\n")

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

    backend = store.save(username=username, password=password, classroom_ical_url=ical_url)
    if school_class or course_name:
        ctx = store.load_student_context()
        if school_class:
            ctx.school_class = school_class.upper()
        if course_name:
            ctx.course_name = course_name
        store.save_student_context(ctx)

    client = SpaggiariClient(store=store)
    try:
        profile = await client.get_profile()
    except ClasseVivaError as exc:
        print(f"\n[ERRORE LOGIN] {exc}", file=sys.stderr)
        return 1

    print("\n[OK] Login effettuato con successo!")
    print(f"  • Backend credenziali : {backend.upper()} (zero password nei file di config MCP)")
    print(f"  • Tipo account        : {profile.account_type.upper()} ({profile.ident})")
    if profile.school_name:
        print(f"  • Istituto rilevato   : {profile.school_name}")
    if school_class or profile.school_class:
        print(f"  • Classe attiva       : {(school_class or profile.school_class).upper()}")

    print("\n--- Incolla questo JSON in Claude Desktop / Cursor / CC Switch ---")
    print(_build_ready_mcp_config())
    return 0


async def _cmd_doctor() -> int:
    """Runs a privacy-safe diagnostic check (never prints names, grades, or passwords)."""
    store = CredentialStore()
    print("=== ClasseViva MCP — Diagnostica (Privacy-Safe) ===")
    try:
        creds = store.load()
        print(f"[OK] Credenziali trovate (sorgente: {creds.source})")
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1

    client = SpaggiariClient(store=store)
    try:
        profile = await client.get_profile()
        ctx = store.load_student_context()
        info_label = ctx.school_class or profile.school_class or profile.school_name or "OK"
        print(f"[OK] Login Spaggiari riuscito (tipo account: {profile.account_type}, info: {info_label})")
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="classeviva-mcp",
        description="Server MCP per Spaggiari ClasseViva e Google Classroom",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("login", help="Salva le credenziali nel Keyring di sistema e stampa il JSON MCP pronto")
    sub.add_parser("doctor", help="Verifica la connessione a Spaggiari senza stampare dati personali")
    sub.add_parser("logout", help="Rimuove le credenziali e la sessione salvata")
    sub.add_parser("serve", help="Avvia il server MCP su trasporto stdio (default)")

    args = parser.parse_args()

    if args.command == "login":
        sys.exit(asyncio.run(_cmd_login()))
    elif args.command == "doctor":
        sys.exit(asyncio.run(_cmd_doctor()))
    elif args.command == "logout":
        CredentialStore().clear()
        print("Credenziali e sessione rimosse dal sistema.")
        sys.exit(0)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
