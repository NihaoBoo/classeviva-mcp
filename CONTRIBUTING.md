# Contributing

Grazie per l'interesse! Questo progetto è un connettore locale per il registro
elettronico Spaggiari ClasseViva.

## Requisiti

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone https://github.com/NihaoBoo/classeviva-mcp
cd classeviva-mcp
uv sync
```

## Test e controlli

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv run pip-audit --skip-editable
```

La suite usa `httpx.MockTransport` e monkeypatch del Keyring: non tocca
credenziali reali né servizi esterni.

## Linee guida

- Non inserire mai credenziali, token, URL `.ics` reali o dati di studenti in
  codice, test, issue o commit.
- Mantieni la compatibilità con Python 3.11 e con i client MCP basati su stdio.
- Aggiungi test per ogni correzione di sicurezza o bug.
- Segui lo stile esistente (`ruff`, line-length 120).
- Non loggare segreti o dati personali.
- Per modifiche alla sicurezza, vedi [SECURITY.md](SECURITY.md).

## Pull request

1. Crea un branch descrittivo (`fix/ssrf-ical`, `feat/read-only-mode`).
2. Assicurati che test, lint e type-check passino.
3. Descrivi il problema, la soluzione e i test aggiunti.
4. Non includere file generati, cache o allegati reali.

## Licenza

Contribuendo accetti che il tuo contributo sia distribuito con licenza MIT.
