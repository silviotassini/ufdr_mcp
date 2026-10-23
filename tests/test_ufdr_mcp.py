"""Testes do UFDR MCP com um UFDR sintético mínimo."""

import json
import os
import struct
import zipfile
from pathlib import Path

import pytest

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project id="p1" name="Caso Teste" xmlns="http://pa.cellebrite.com/report/2.0">
  <sourceExtractions><extractionInfo id="0" name="Logical" /></sourceExtractions>
  <caseInformation><field name="Case number">123</field></caseInformation>
  <taggedFiles>
    <file fs="FS" path="/sdcard/foto.jpg" name="foto.jpg" size="3" id="f1" deleted="Intact">
      <accessInfo><timestamp name="ModifyTime">2024-05-01T10:00:00.000+00:00</timestamp></accessInfo>
      <metadata section="File">
        <item name="Local Path"><![CDATA[files\\Image\\foto.jpg]]></item>
        <item name="MD5"><![CDATA[900150983cd24fb0d6963f7d28e17f72]]></item>
        <item name="Tags"><![CDATA[Image]]></item>
      </metadata>
    </file>
  </taggedFiles>
  <decodedData>
    <modelType type="Chat">
      <model type="Chat" id="c1" deleted_state="Intact">
        <field name="UserMapping" type="DecodingSourceOptions"><value><![CDATA[Decoding]]></value></field>
        <field name="Source" type="String"><value type="String"><![CDATA[WhatsApp]]></value></field>
        <field name="StartTime" type="TimeStamp"><value type="TimeStamp">2024-05-01T10:00:00.000+00:00</value></field>
        <multiModelField name="Participants" type="Party">
          <model type="Party" id="p-a"><field name="Identifier"><value><![CDATA[5531999990000@s.whatsapp.net]]></value></field>
            <field name="Name"><value><![CDATA[Fulano]]></value></field></model>
        </multiModelField>
        <multiModelField name="Messages" type="InstantMessage">
          <model type="InstantMessage" id="m1" deleted_state="Intact">
            <field name="Source"><value><![CDATA[WhatsApp]]></value></field>
            <modelField name="From" type="Party">
              <model type="Party" id="p-b"><field name="Name"><value><![CDATA[Fulano]]></value></field></model>
            </modelField>
            <field name="Body"><value><![CDATA[manda o pix hoje]]></value></field>
            <field name="TimeStamp"><value>2024-05-01T10:05:00.000+00:00</value></field>
            <multiModelField name="Attachments" type="Attachment">
              <model type="Attachment" id="a1" file_id="f1">
                <field name="Filename"><value><![CDATA[foto.jpg]]></value></field>
              </model>
            </multiModelField>
          </model>
          <model type="InstantMessage" id="m2" deleted_state="Deleted">
            <field name="Body"><value><![CDATA[apagada]]></value></field>
            <field name="TimeStamp"><value>2024-05-02T08:00:00.000+00:00</value></field>
          </model>
        </multiModelField>
      </model>
    </modelType>
    <modelType type="Call">
      <model type="Call" id="call1" deleted_state="Intact">
        <field name="Direction"><value><![CDATA[Incoming]]></value></field>
        <field name="TimeStamp"><value>2024-05-03T12:00:00.000+00:00</value></field>
        <multiModelField name="Parties" type="Party">
          <model type="Party" id="p-c"><field name="Identifier"><value><![CDATA[+5531988887777]]></value></field></model>
        </multiModelField>
      </model>
    </modelType>
    <modelType type="Contact">
      <model type="Contact" id="ct1">
        <field name="Name"><value><![CDATA[Beltrano]]></value></field>
        <multiModelField name="Entries" type="ContactEntry">
          <model type="PhoneNumber" id="pn1"><field name="Value"><value><![CDATA[+5531977776666]]></value></field></model>
        </multiModelField>
      </model>
    </modelType>
  </decodedData>
<extraInfos>
<extraInfo id="x"><broken attr="tableNa
<extraInfo id="y" />
</extraInfos>
</project>
"""


@pytest.fixture
def ufdr(tmp_path, monkeypatch):
    monkeypatch.setenv("UFDR_MCP_CACHE", str(tmp_path / "cache"))
    path = tmp_path / "teste.ufdr"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("report.xml", REPORT)
        zf.writestr("files/Image/foto.jpg", b"abc")
    return path


def _split_zip(path: Path, part_size: int):
    """Dividir um ZIP no formato WinZip: nome.z01, ..., nome.ufdr (última parte com o diretório central).

    Cada entrada passa a apontar para o disco onde começa, com offset relativo a ele.
    """
    data = b"PK\x07\x08" + path.read_bytes()  # assinatura de arquivo dividido
    with zipfile.ZipFile(path) as zf:
        cd_start = zf.start_dir + 4
    chunks = [data[i:i + part_size] for i in range(0, cd_start, part_size)]
    chunks[-1] += data[len(chunks[-1]) + (len(chunks) - 1) * part_size:]
    starts = [i * part_size for i in range(len(chunks))]
    last = len(chunks) - 1

    def disk_of(off):
        return max(i for i, s in enumerate(starts) if s <= off)

    tail = bytearray(chunks[last])
    pos = cd_start - starts[last]
    while tail[pos:pos + 4] == b"PK\x01\x02":
        off = struct.unpack_from("<L", tail, pos + 42)[0] + 4
        struct.pack_into("<H", tail, pos + 34, disk_of(off))
        struct.pack_into("<L", tail, pos + 42, off - starts[disk_of(off)])
        n, m, k = struct.unpack_from("<3H", tail, pos + 28)
        pos += 46 + n + m + k
    struct.pack_into("<2H", tail, pos + 4, last, last)
    struct.pack_into("<L", tail, pos + 16, cd_start - starts[last])
    chunks[last] = bytes(tail)

    for i, chunk in enumerate(chunks[:-1], 1):
        path.with_suffix(f".z{i:02d}").write_bytes(chunk)
    path.write_bytes(chunks[-1])


@pytest.fixture
def split_ufdr(ufdr):
    with zipfile.ZipFile(ufdr, "a") as zf:
        zf.writestr("files/Video/grande.bin", bytes(range(256)) * 64, compress_type=zipfile.ZIP_STORED)
    _split_zip(ufdr, 4096)
    assert ufdr.with_suffix(".z02").exists()
    return ufdr


def test_split_ufdr(split_ufdr, tmp_path):
    from ufdr_mcp.index import UFDRIndex
    from ufdr_parser import open_zip

    with open_zip(split_ufdr) as zf:
        assert zf.read("files/Video/grande.bin") == bytes(range(256)) * 64
        assert zf.testzip() is None

    idx = UFDRIndex(split_ufdr)
    s = idx.summary()
    assert s["chats"] == 1 and s["messages"] == 2 and s["files"] == 1
    out = idx.extract_file("f1", str(tmp_path / "out"))
    assert Path(out["extracted_to"]).read_bytes() == b"abc"
    idx.close()


def test_parser_events(ufdr):
    from ufdr_parser import iter_report

    events = list(iter_report(ufdr))
    kinds = [k for k, _ in events]
    assert kinds.count("model") == 3
    assert kinds.count("file") == 1
    # extraInfos truncado vira aviso, sem perder os dados anteriores
    assert kinds[-1] == "warning"
    assert events[-1][1]["section"] == "extraInfos"

    chat = next(d for k, d in events if k == "model" and d["_type"] == "Chat")
    assert "UserMapping" not in chat
    assert chat["Source"] == "WhatsApp"
    assert chat["Messages"][0]["Body"] == "manda o pix hoje"
    assert chat["Messages"][0]["From"]["Name"] == "Fulano"
    assert chat["Messages"][0]["Attachments"][0]["_file_id"] == "f1"


def test_index_queries(ufdr, tmp_path):
    from ufdr_mcp.index import UFDRIndex

    idx = UFDRIndex(ufdr)
    s = idx.summary()
    assert s["chats"] == 1 and s["messages"] == 2 and s["files"] == 1
    assert s["case_info"] == {"Case number": "123"}
    assert s["model_counts"]["Call"] == 1

    msgs = idx.search_messages(chat_id="c1")
    assert [m["id"] for m in msgs] == ["m1", "m2"]
    assert msgs[0]["attachment_list"][0]["file_id"] == "f1"
    assert idx.count_messages(text="pix") == 1
    assert idx.count_messages(deleted_only=True) == 1
    assert idx.count_messages(start_date="2024-05-02") == 1
    assert idx.count_messages(end_date="2024-05-01") == 1

    assert idx.count_models(model_type="Contact", text="977776666") == 1
    assert "Incoming" in idx.query_models("Call")[0]["summary"]
    assert idx.search("pix")[0]["id"] == "m1"
    assert idx.list_files(hash_value="900150983CD24FB0D6963F7D28E17F72")[0]["id"] == "f1"

    # Extração por ID do arquivo e por ID do anexo
    out = idx.extract_file("a1", str(tmp_path / "out"))
    assert Path(out["extracted_to"]).read_bytes() == b"abc"
    idx.close()


def test_index_is_cached(ufdr):
    from ufdr_mcp.index import UFDRIndex

    first = UFDRIndex(ufdr)
    mtime = first.db_path.stat().st_mtime_ns
    first.close()
    second = UFDRIndex(ufdr)
    assert second.db_path.stat().st_mtime_ns == mtime
    second.close()


@pytest.mark.anyio
async def test_server_tools(ufdr):
    from mcp.server.mcpserver.exceptions import ToolError
    from ufdr_mcp import server

    data = json.loads(await server.ufdr_get_chat("c1", ufdr_path=str(ufdr)))
    assert data["messages"]["total"] == 2
    with pytest.raises(ToolError):
        await server.ufdr_summary(ufdr_path="/nao/existe.ufdr")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_open_case_in_conversation(ufdr, tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    from ufdr_mcp import server

    monkeypatch.setattr(server, "DEFAULT_UFDR", None)
    with pytest.raises(ToolError, match="ufdr_open_case"):
        await server.ufdr_summary()

    # Pelo caminho do .ufdr informado na conversa
    data = json.loads(await server.ufdr_open_case(str(ufdr)))
    assert data["opened"] == str(ufdr)
    assert data["summary"]["messages"] == 2
    # Depois de aberto, as ferramentas dispensam ufdr_path
    assert json.loads(await server.ufdr_get_chat("c1"))["messages"]["total"] == 2
    assert json.loads(await server.ufdr_list_cases())["current"] == str(ufdr)
    assert json.loads(await server.ufdr_list_cases(str(tmp_path)))["cases"] == [str(ufdr)]

    # Pela pasta do caso; com mais de um .ufdr, pede para escolher
    (tmp_path / "outro.ufdr").write_bytes(ufdr.read_bytes())
    data = json.loads(await server.ufdr_open_case(str(tmp_path)))
    assert data["ambiguous"] and len(data["options"]) == 2
    # Só um nome, sem caminho: não há pasta de casos configurada onde procurar
    with pytest.raises(ToolError):
        await server.ufdr_open_case("teste")


def test_unreadable_report_is_not_cached(tmp_path, monkeypatch):
    from ufdr_mcp.index import UFDRIndex, cache_dir

    monkeypatch.setenv("UFDR_MCP_CACHE", str(tmp_path / "cache"))
    bad = tmp_path / "ruim.ufdr"
    bad.write_bytes(b"lixo que nao e zip nem xml")
    with pytest.raises(ValueError, match="report.xml"):
        UFDRIndex(bad)
    assert not list(cache_dir().glob("*.sqlite"))


def test_cache_key_includes_split_parts(split_ufdr):
    from ufdr_mcp.index import cache_file_for

    before = cache_file_for(split_ufdr)
    part = split_ufdr.with_suffix(".z01")
    os.utime(part, ns=(part.stat().st_atime_ns, part.stat().st_mtime_ns + 10**9))
    assert cache_file_for(split_ufdr) != before


@pytest.mark.anyio
async def test_stats_and_file_totals(ufdr):
    from ufdr_mcp import server

    stats = json.loads(await server.ufdr_stats(ufdr_path=str(ufdr)))
    assert stats["decoded_by_category"]["Call"] == 1
    assert stats["contacts"]["total"] == 1
    assert stats["messages"]["by_source"] == {"WhatsApp": 1, "?": 1}
    assert stats["messages"]["by_deleted_state"] == {"Intact": 1, "Deleted": 1}
    assert stats["files_by_category"]["Image"] == {"count": 1, "bytes": 3, "by_state": {"Intact": 1}}

    calls = json.loads(await server.ufdr_stats(model_type="Call", ufdr_path=str(ufdr)))
    assert calls["total"] == 1 and calls["by_deleted_state"] == {"Intact": 1}

    files = json.loads(await server.ufdr_list_files(ufdr_path=str(ufdr), tag="Image", limit=0))
    assert files["total"] == 1 and files["total_bytes"] == 3 and files["returned"] == 0
