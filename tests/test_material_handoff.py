from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest
from mcp.types import ResourceLink, TextContent

import classeviva_mcp.server as server
from classeviva_mcp import attachments, classroom_ical
from classeviva_mcp.credentials import CredentialStore
from classeviva_mcp.models import DidacticsFile, DidacticsFolder


class Materials:
    def __init__(self) -> None:
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((20, 30), "Pagina esclusa")
            pdf.new_page().insert_text((20, 30), "Seconda pagina sintetica")
            self.pdf = pdf.tobytes()
        self.downloads = 0
        self.notice_attempts: list[bool] = []

    async def get_didactics(self) -> list[DidacticsFolder]:
        return [DidacticsFolder(folder_id=1, folder_name="Dispense", files=[
            DidacticsFile(content_id=i, content_name=f"Reti {i}.pdf", share_date=f"2026-11-{i:02d}")
            for i in range(1, 8)
        ] + [DidacticsFile(content_id=10, content_name="Storia.pdf")])]

    async def download_didactics_file(self, file_id: int | str) -> tuple[bytes, str, str]:
        self.downloads += 1
        return self.pdf, "Reti.pdf", "application/pdf"

    async def read_notice_content(self, **kwargs: Any) -> dict[str, Any]:
        self.notice_attempts.append(kwargs["allow_mark_as_read"])
        if not kwargs["allow_mark_as_read"]:
            return {"skipped": True, "read_state_known": True}
        return {"attachments": [{"bytes": self.pdf, "file_name": "Avviso.pdf"}]}


@pytest.fixture
def materials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Materials:
    client = Materials()
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_store", CredentialStore(tmp_path))
    monkeypatch.setattr(server, "READ_ONLY_MODE", False)
    return client


@pytest.mark.asyncio
async def test_search_matches_filename_in_generic_folder_and_paginates(materials: Materials) -> None:
    page = await server.list_didactics(subject="reti")
    assert [item["id"] for item in page["items"]] == [7, 6, 5, 4, 3]
    assert page["items"][0]["source"] == "classeviva"
    assert page["items"][0]["date"] == "2026-11-07"
    assert "folders" not in page and "warnings" not in page
    more = await server.list_didactics(subject="reti", cursor=page["next_cursor"])
    assert [item["id"] for item in more["items"]] == [2, 1]
    assert "next_cursor" not in more
    assert materials.downloads == 0


@pytest.mark.asyncio
async def test_search_combines_classroom_drive_metadata_with_classeviva_without_fetching_files(
    materials: Materials, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "classroom.googleapis.com"
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json={"courses": [{"id": "3", "name": "Informatica"}]})
        if request.url.path.endswith("/courseWorkMaterials"):
            return httpx.Response(200, json={"courseWorkMaterial": [{
                "id": "8", "title": "Dispensa", "creationTime": "2026-11-18T10:00:00Z",
                "materials": [{"driveFile": {"driveFile": {
                    "id": "file-8", "title": "Reti locali.pdf", "alternateLink": "https://drive.google.com/file/d/file-8/view",
                }}}],
            }]})
        assert request.url.path.endswith("/courseWork")
        return httpx.Response(200, json={})

    async def access_token(store: CredentialStore) -> str:
        return "synthetic"

    monkeypatch.setattr(server._classroom_tokens, "access_token", access_token)
    monkeypatch.setattr(classroom_ical, "build_async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(respond), **kw
    ))
    result = await server.search_materials("reti")
    assert result["items"][0] == {
        "id": "classroom:3:material:8:0", "title": "Reti locali.pdf", "source": "classroom",
        "date": "2026-11-18", "subject": "Informatica", "url": "https://drive.google.com/file/d/file-8/view",
    }
    assert result["items"][1]["source"] == "classeviva"
    assert "warnings" not in result
    assert materials.downloads == 0


@pytest.mark.asyncio
async def test_save_without_text_and_read_without_copy(materials: Materials) -> None:
    result = await server.download_didactics_item(1)
    assert "text" not in result
    assert Path(result["saved_path"]).read_bytes() == materials.pdf
    saved = list(server._store.didactics_dir.iterdir())
    reading = await server.read_didactics_item(2)
    assert "Seconda pagina sintetica" in reading["text"]
    assert "saved_path" not in reading
    assert list(server._store.didactics_dir.iterdir()) == saved


@pytest.mark.asyncio
async def test_mcp_attachment_roundtrip_pdf_then_single_png(materials: Materials) -> None:
    for args, mime, signature in (
        ({"file_id": 1}, "application/pdf", b"%PDF"),
        ({"file_id": 1, "page": 2}, "image/png", b"\x89PNG\r\n\x1a\n"),
    ):
        response = await server.mcp.call_tool("prepare_attachment", args)
        assert len(response.content) == 1 and isinstance(response.content[0], ResourceLink)
        link = response.content[0]
        assert "Seconda pagina" not in str(link)
        assert link.mime_type == mime
        [resource] = await server.mcp.read_resource(link.uri)
        assert resource.mime_type == mime and resource.content.startswith(signature)
        if mime == "application/pdf":
            assert resource.content == materials.pdf
        else:
            with pymupdf.open(stream=resource.content, filetype="png") as png:
                assert len(png) == 1
            assert link.name == "Reti-pagina-2.png"
        with pytest.raises(Exception, match="scaduto|Error|expired"):
            await server.mcp.read_resource(link.uri)
    assert not server._store.didactics_dir.exists()


@pytest.mark.asyncio
async def test_notice_handoff_requires_consent_and_cannot_be_redeemed_after_logout(materials: Materials) -> None:
    [message] = await server.prepare_attachment(notice_id="1", page=2)
    assert isinstance(message, TextContent) and "Procedo?" in message.text
    [link] = await server.prepare_attachment(notice_id="1", page=2, allow_mark_as_read=True)
    assert isinstance(link, ResourceLink)
    assert materials.notice_attempts == [False, True]
    server._store._invalidate_session()
    with pytest.raises(Exception, match="scaduto|Error|expired"):
        await server.mcp.read_resource(link.uri)


@pytest.mark.asyncio
async def test_handoff_refuses_large_and_expired_files(materials: Materials, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(attachments, "time", type("Clock", (), {"monotonic": staticmethod(lambda: now)}))
    [link] = await server.prepare_attachment(file_id=1)
    assert isinstance(link, ResourceLink)
    now += 91
    with pytest.raises(Exception, match="scaduto|Error|expired"):
        await server.mcp.read_resource(link.uri)

    materials.pdf = b"x" * (8 * 1024 * 1024 + 1)
    [message] = await server.prepare_attachment(file_id=1)
    assert isinstance(message, TextContent) and "salvataggio locale" in message.text
