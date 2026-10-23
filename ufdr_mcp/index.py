"""
Índice SQLite de um relatório UFDR.

Na primeira consulta o report.xml é lido uma única vez (streaming) e gravado
num SQLite em cache; as consultas seguintes usam só o índice. O cache é
invalidado quando o arquivo UFDR muda (caminho, tamanho ou mtime).

Local do cache: $UFDR_MCP_CACHE ou ~/.cache/ufdr-mcp/
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Iterable, Optional

from ufdr_parser import iter_report, open_zip, split_parts

SCHEMA_VERSION = 2

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")

# Campos de data preferidos, em ordem, para o "timestamp" de cada modelo
TS_FIELDS = (
    "TimeStamp", "StartTime", "LastActivity", "LastVisited", "Date", "DateSent",
    "TimeCreated", "CreationTime", "StartDate", "DateCreated", "LastUsed",
    "LastConnection", "PurchaseDate", "EndTime",
)

# Campos usados para montar o resumo legível de cada modelo
SUMMARY_FIELDS = (
    "Name", "Title", "Subject", "Body", "Value", "Url", "Identifier", "Username",
    "Address", "Description", "Key", "Path", "Filename",
)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE models (
    id TEXT PRIMARY KEY, type TEXT, source TEXT, deleted_state TEXT,
    ts TEXT, chat_id TEXT, summary TEXT, text TEXT, data TEXT
);
CREATE TABLE chats (
    id TEXT PRIMARY KEY, source TEXT, name TEXT, participants TEXT,
    start_time TEXT, last_activity TEXT, message_count INTEGER, deleted_state TEXT
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY, chat_id TEXT, source TEXT, ts TEXT, sender TEXT,
    sender_is_owner INTEGER, body TEXT, deleted_state TEXT, attachments INTEGER
);
CREATE TABLE attachments (
    id TEXT, message_id TEXT, chat_id TEXT, filename TEXT, content_type TEXT,
    file_id TEXT, local_path TEXT, url TEXT
);
CREATE TABLE files (
    id TEXT PRIMARY KEY, name TEXT, path TEXT, fs TEXT, size INTEGER,
    local_path TEXT, md5 TEXT, sha256 TEXT, tags TEXT, deleted TEXT,
    modify_time TEXT, access_time TEXT, create_time TEXT, data TEXT
);
CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, type UNINDEXED, text);
"""

INDEXES = """
CREATE INDEX idx_models_type ON models(type, ts);
CREATE INDEX idx_models_ts ON models(ts);
CREATE INDEX idx_messages_chat ON messages(chat_id, ts);
CREATE INDEX idx_messages_ts ON messages(ts);
CREATE INDEX idx_att_msg ON attachments(message_id);
CREATE INDEX idx_files_name ON files(name);
CREATE INDEX idx_files_md5 ON files(md5);
CREATE INDEX idx_files_sha256 ON files(sha256);
"""


def _log(msg: str):
    # stdout é o canal do protocolo MCP; log vai para stderr
    print(f"[ufdr-mcp] {msg}", file=sys.stderr, flush=True)


# =====================================================================
# Helpers de conversão
# =====================================================================

def party_label(party: Optional[dict]) -> Optional[str]:
    """'Nome <identificador>' de um modelo Party."""
    if not isinstance(party, dict):
        return None
    name, ident = party.get("Name"), party.get("Identifier")
    if name and ident and name != ident:
        return f"{name} <{ident}>"
    return name or ident


def model_ts(data: dict) -> Optional[str]:
    """Data principal de um modelo."""
    for f in TS_FIELDS:
        v = data.get(f)
        if isinstance(v, str) and ISO_RE.match(v):
            return v
    for k, v in data.items():
        if not k.startswith("_") and isinstance(v, str) and ISO_RE.match(v):
            return v
    return None


def flatten_text(data, out: list[str]):
    """Coletar todos os valores texto (recursivo) para busca."""
    if isinstance(data, dict):
        for k, v in data.items():
            if not k.startswith("_"):
                flatten_text(v, out)
    elif isinstance(data, list):
        for v in data:
            flatten_text(v, out)
    elif isinstance(data, str) and data not in ("False", "True", "0", "Unknown", "Intact"):
        out.append(data)


def _entries(data: dict) -> list[str]:
    """Valores de Entries (telefones, e-mails, user IDs) de contatos/contas."""
    return [e["Value"] for e in data.get("Entries", []) if isinstance(e, dict) and e.get("Value")]


def model_summary(data: dict) -> str:
    """Resumo de uma linha de um modelo, de acordo com o tipo."""
    t = data.get("_type")
    if t in ("Contact", "UserAccount"):
        parts = [data.get("Name") or data.get("Username") or ""] + _entries(data)
        return " | ".join(p for p in parts if p)
    if t == "Call":
        parties = ", ".join(filter(None, (party_label(p) for p in data.get("Parties", []))))
        return " ".join(filter(None, (data.get("Direction"), data.get("Status"), parties,
                                      data.get("Duration") and f"dur={data['Duration']}")))
    if t == "Location" or "Position" in data:
        pos = data.get("Position") or {}
        coords = f"{pos.get('Latitude')},{pos.get('Longitude')}" if pos.get("Latitude") else ""
        return " ".join(filter(None, (coords, data.get("Name"), data.get("Description"))))
    if t == "InstantMessage":
        return " ".join(filter(None, (party_label(data.get("From")), ":", data.get("Body") or "")))
    parts = []
    for f in SUMMARY_FIELDS:
        v = data.get(f)
        if isinstance(v, str) and v not in parts:
            parts.append(v)
    if not parts:
        parts = _entries(data)
    if not parts:
        flatten_text(data, parts)
        parts = parts[:4]
    return " | ".join(parts)[:500]


def _attachment_rows(msg: dict, chat_id: Optional[str]) -> list[tuple]:
    rows = []
    atts = list(msg.get("Attachments", []))
    if isinstance(msg.get("Attachment"), dict):
        atts.append(msg["Attachment"])
    for a in atts:
        rows.append((a.get("_id"), msg.get("_id"), chat_id, a.get("Filename"), a.get("ContentType"),
                     a.get("_file_id"), a.get("attachment_extracted_path"), a.get("URL")))
    return rows


# =====================================================================
# Índice
# =====================================================================

def cache_dir() -> Path:
    base = os.environ.get("UFDR_MCP_CACHE")
    return Path(base) if base else Path.home() / ".cache" / "ufdr-mcp"


def cache_file_for(ufdr_path: Path) -> Path:
    if ufdr_path.is_dir():
        files = [ufdr_path / "report.xml"]
    else:
        files = split_parts(ufdr_path)  # UFDR dividido: qualquer parte trocada invalida o índice
    stamp = "|".join(f"{f.stat().st_size}:{f.stat().st_mtime_ns}" for f in files)
    key = f"{ufdr_path.resolve()}|{stamp}|{SCHEMA_VERSION}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return cache_dir() / f"{ufdr_path.stem}-{digest}.sqlite"


class UFDRIndex:
    """Índice consultável de um UFDR."""

    def __init__(self, ufdr_path: str | Path, rebuild: bool = False):
        self.ufdr_path = Path(ufdr_path).expanduser()
        if not self.ufdr_path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {self.ufdr_path}")
        self.db_path = cache_file_for(self.ufdr_path)
        if rebuild and self.db_path.exists():
            self.db_path.unlink()
        if not self.db_path.exists():
            self._build()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Construção
    # ------------------------------------------------------------------

    def _build(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_suffix(".building")
        if tmp.exists():
            tmp.unlink()
        _log(f"Indexando {self.ufdr_path} -> {self.db_path}")
        conn = sqlite3.connect(tmp)
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)

        buf: dict[str, list[tuple]] = {k: [] for k in ("models", "chats", "messages", "attachments", "files", "fts")}
        sql = {
            "models": "INSERT OR REPLACE INTO models VALUES (?,?,?,?,?,?,?,?,?)",
            "chats": "INSERT OR REPLACE INTO chats VALUES (?,?,?,?,?,?,?,?)",
            "messages": "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            "attachments": "INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?)",
            "files": "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            "fts": "INSERT INTO fts VALUES (?,?,?)",
        }

        def flush(force=False):
            for k, rows in buf.items():
                if rows and (force or len(rows) >= 5000):
                    conn.executemany(sql[k], rows)
                    rows.clear()

        meta = {"extractions": [], "metadata": [], "warnings": []}
        count = 0

        def add_model(data: dict, chat_id: Optional[str] = None):
            texts: list[str] = []
            flatten_text(data, texts)
            text = " ".join(texts)
            mid = data.get("_id")
            buf["models"].append((
                mid, data.get("_type"), data.get("Source") or data.get("SourceApplication"),
                data.get("_deleted_state"), model_ts(data), chat_id, model_summary(data), text,
                json.dumps(data, ensure_ascii=False),
            ))
            buf["fts"].append((mid, data.get("_type"), text))

        def add_message(msg: dict, chat_id: Optional[str]):
            sender = msg.get("From") if isinstance(msg.get("From"), dict) else {}
            atts = _attachment_rows(msg, chat_id)
            buf["messages"].append((
                msg.get("_id"), chat_id, msg.get("Source") or msg.get("SourceApplication"),
                model_ts(msg), party_label(sender), 1 if sender.get("IsPhoneOwner") == "True" else 0,
                msg.get("Body"), msg.get("_deleted_state"), len(atts),
            ))
            buf["attachments"].extend(atts)
            add_model(msg, chat_id)

        for kind, data in iter_report(self.ufdr_path):
            if kind == "model":
                if data["_type"] == "Chat":
                    messages = data.pop("Messages", [])
                    data["MessageCount"] = len(messages)
                    participants = [party_label(p) for p in data.get("Participants", [])]
                    buf["chats"].append((
                        data["_id"], data.get("Source"), data.get("Name"),
                        json.dumps([p for p in participants if p], ensure_ascii=False),
                        data.get("StartTime"), data.get("LastActivity"), len(messages),
                        data.get("_deleted_state"),
                    ))
                    for m in messages:
                        add_message(m, data["_id"])
                    add_model(data)
                elif data["_type"] == "InstantMessage":
                    add_message(data, None)
                else:
                    add_model(data)
                count += 1
                if count % 20000 == 0:
                    _log(f"  {count} modelos...")
            elif kind == "file":
                buf["files"].append((
                    data.get("id"), data.get("name"), data.get("path"), data.get("fs"),
                    int(data["size"]) if str(data.get("size", "")).isdigit() else None,
                    data.get("Local Path"), data.get("MD5"), data.get("SHA256"), data.get("Tags"),
                    data.get("deleted"), data.get("ModifyTime"), data.get("AccessTime"),
                    data.get("CreationTime"), json.dumps(data, ensure_ascii=False),
                ))
            elif kind == "project":
                meta["project"] = data
            elif kind == "case_info":
                meta["case_info"] = data
            elif kind == "extraction":
                meta["extractions"].append(data)
            elif kind == "metadata":
                meta["metadata"].append(data)
            elif kind == "warning":
                _log(f"  AVISO: {data}")
                meta["warnings"].append(data)
            flush()

        if "project" not in meta and meta["warnings"]:
            # Nada foi lido: não gravar em cache um índice vazio de um arquivo ilegível
            conn.close()
            tmp.unlink()
            raise ValueError(f"Não foi possível ler o report.xml de {self.ufdr_path}: "
                             f"{meta['warnings'][0]['error']}")
        flush(force=True)
        meta["ufdr_path"] = str(self.ufdr_path.resolve())
        conn.executemany("INSERT INTO meta VALUES (?,?)",
                         [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        conn.executescript(INDEXES)
        conn.commit()
        conn.close()
        tmp.replace(self.db_path)
        _log(f"Indexação concluída: {count} modelos")

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------

    def _rows(self, sql: str, params: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, list(params))]

    def meta(self, key: str):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def summary(self) -> dict:
        types = self._rows("SELECT type, COUNT(*) AS n FROM models GROUP BY type ORDER BY n DESC")
        by_source = self._rows(
            "SELECT COALESCE(source,'?') AS source, COUNT(*) AS n FROM messages GROUP BY source ORDER BY n DESC")
        span = self.conn.execute("SELECT MIN(ts), MAX(ts) FROM models WHERE ts IS NOT NULL").fetchone()
        return {
            "ufdr_path": str(self.ufdr_path),
            "project": self.meta("project"),
            "case_info": self.meta("case_info"),
            "extractions": self.meta("extractions"),
            "warnings": self.meta("warnings"),
            "model_counts": {r["type"]: r["n"] for r in types},
            "chats": self.conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
            "messages": self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "messages_by_source": {r["source"]: r["n"] for r in by_source},
            "files": self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "time_span": {"first": span[0], "last": span[1]},
        }

    def device_info(self) -> dict:
        rows = self._rows("SELECT data FROM models WHERE type='DeviceInfoEntry'")
        entries = {}
        for r in rows:
            d = json.loads(r["data"])
            name, value = d.get("EntryName") or d.get("Name"), d.get("EntryValue") or d.get("Value")
            if name and value:
                entries.setdefault(name, [])
                if value not in entries[name]:
                    entries[name].append(value)
        return {
            "device_info": {k: v[0] if len(v) == 1 else v for k, v in entries.items()},
            "metadata": self.meta("metadata"),
        }

    def list_chats(self, source=None, participant=None, limit=50, offset=0) -> list[dict]:
        where, params = [], []
        if source:
            where.append("source LIKE ?"); params.append(f"%{source}%")
        if participant:
            where.append("(participants LIKE ? OR name LIKE ?)"); params += [f"%{participant}%"] * 2
        sql = "SELECT * FROM chats" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY last_activity DESC LIMIT ? OFFSET ?"
        rows = self._rows(sql, params + [limit, offset])
        for r in rows:
            r["participants"] = json.loads(r["participants"] or "[]")
        return rows

    def get_chat(self, chat_id: str) -> Optional[dict]:
        rows = self._rows("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not rows:
            return None
        chat = rows[0]
        chat["participants"] = json.loads(chat["participants"] or "[]")
        return chat

    @staticmethod
    def _message_where(text=None, chat_id=None, source=None, sender=None, start_date=None,
                       end_date=None, deleted_only=False, with_attachments=False):
        where, params = [], []
        if text:
            where.append("body LIKE ?"); params.append(f"%{text}%")
        if chat_id:
            where.append("chat_id = ?"); params.append(chat_id)
        if source:
            where.append("source LIKE ?"); params.append(f"%{source}%")
        if sender:
            where.append("sender LIKE ?"); params.append(f"%{sender}%")
        if start_date:
            where.append("ts >= ?"); params.append(start_date)
        if end_date:
            where.append("ts <= ?"); params.append(_end_of_day(end_date))
        if deleted_only:
            where.append("deleted_state = 'Deleted'")
        if with_attachments:
            where.append("attachments > 0")
        return (" WHERE " + " AND ".join(where) if where else ""), params

    def search_messages(self, limit=50, offset=0, order="asc", **filters) -> list[dict]:
        where, params = self._message_where(**filters)
        sql = f"SELECT * FROM messages{where} ORDER BY ts {'DESC' if order == 'desc' else 'ASC'} LIMIT ? OFFSET ?"
        rows = self._rows(sql, params + [limit, offset])
        for r in rows:
            if r["attachments"]:
                r["attachment_list"] = self._rows(
                    "SELECT id, filename, content_type, file_id, local_path FROM attachments WHERE message_id=?",
                    (r["id"],))
        return rows

    def count_messages(self, **filters) -> int:
        where, params = self._message_where(**filters)
        return self.conn.execute(f"SELECT COUNT(*) FROM messages{where}", params).fetchone()[0]

    def count_models(self, model_type=None, text=None, source=None, start_date=None, end_date=None,
                     deleted_state=None) -> int:
        where, params = self._model_where(model_type, text, source, start_date, end_date, deleted_state)
        return self.conn.execute(f"SELECT COUNT(*) FROM models{where}", params).fetchone()[0]

    @staticmethod
    def _model_where(model_type=None, text=None, source=None, start_date=None, end_date=None,
                     deleted_state=None):
        where, params = [], []
        if model_type:
            types = [t.strip() for t in model_type.split(",") if t.strip()]
            where.append(f"type IN ({','.join('?' * len(types))})"); params += types
        if text:
            where.append("text LIKE ?"); params.append(f"%{text}%")
        if source:
            where.append("source LIKE ?"); params.append(f"%{source}%")
        if start_date:
            where.append("ts >= ?"); params.append(start_date)
        if end_date:
            where.append("ts <= ?"); params.append(_end_of_day(end_date))
        if deleted_state:
            where.append("deleted_state = ?"); params.append(deleted_state)
        return (" WHERE " + " AND ".join(where) if where else ""), params

    def query_models(self, model_type=None, text=None, source=None, start_date=None, end_date=None,
                     deleted_state=None, limit=50, offset=0, full=False, order="asc") -> list[dict]:
        where, params = self._model_where(model_type, text, source, start_date, end_date, deleted_state)
        cols = "id, type, source, deleted_state, ts, chat_id, summary" + (", data" if full else "")
        sql = f"SELECT {cols} FROM models{where}"
        sql += f" ORDER BY ts IS NULL, ts {'DESC' if order == 'desc' else 'ASC'} LIMIT ? OFFSET ?"
        rows = self._rows(sql, params + [limit, offset])
        if full:
            for r in rows:
                r["data"] = json.loads(r["data"])
        return rows

    def search(self, query: str, model_type=None, limit=50) -> list[dict]:
        """Busca full-text (FTS5) em todos os modelos."""
        fts_query = query if any(c in query for c in '"*:()') or " OR " in query \
            else " ".join(f'"{w}"' for w in query.split())
        where, params = ["fts MATCH ?"], [fts_query]
        if model_type:
            where.append("fts.type = ?"); params.append(model_type)
        sql = ("SELECT m.id, m.type, m.source, m.deleted_state, m.ts, m.chat_id, m.summary, "
               "snippet(fts, 2, '[', ']', '…', 12) AS snippet FROM fts JOIN models m ON m.id = fts.id "
               f"WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?")
        return self._rows(sql, params + [limit])

    def get_model(self, model_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT data FROM models WHERE id=?", (model_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _file_where(name=None, tag=None, extension=None, path=None, hash_value=None, deleted=None):
        where, params = [], []
        # Alguns relatórios (ex.: 7.48) não trazem o atributo name; usar o fim do path
        if name:
            where.append("COALESCE(name, path) LIKE ?"); params.append(f"%{name}%")
        if tag:
            where.append("tags LIKE ?"); params.append(f"%{tag}%")
        if extension:
            where.append("COALESCE(name, path) LIKE ?"); params.append(f"%.{extension.lstrip('.')}")
        if path:
            where.append("path LIKE ?"); params.append(f"%{path}%")
        if hash_value:
            where.append("(md5 = ? OR sha256 = ?)"); params += [hash_value.lower()] * 2
        if deleted:
            where.append("deleted = ?"); params.append(deleted)
        return (" WHERE " + " AND ".join(where) if where else ""), params

    def list_files(self, limit=50, offset=0, **filters) -> list[dict]:
        where, params = self._file_where(**filters)
        sql = ("SELECT id, name, path, fs, size, local_path, md5, sha256, tags, deleted, modify_time "
               f"FROM files{where} ORDER BY COALESCE(name, path) LIMIT ? OFFSET ?")
        rows = self._rows(sql, params + [limit, offset])
        for r in rows:
            if not r["name"] and r["path"]:
                r["name"] = r["path"].replace("\\", "/").rsplit("/", 1)[-1]
        return rows

    def count_files(self, **filters) -> dict:
        where, params = self._file_where(**filters)
        n, size = self.conn.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files{where}", params).fetchone()
        return {"count": n, "bytes": size}

    def file_tags(self) -> dict:
        rows = self._rows("SELECT COALESCE(tags,'?') AS tag, COUNT(*) AS n, SUM(size) AS bytes "
                          "FROM files GROUP BY tags ORDER BY n DESC")
        return {r["tag"]: {"count": r["n"], "bytes": r["bytes"]} for r in rows}

    def file_stats(self) -> dict:
        """Arquivos por categoria, com tamanho e divisão por estado (Intact/Deleted)."""
        out: dict[str, dict] = {}
        for r in self._rows("SELECT COALESCE(tags,'?') AS tag, COALESCE(deleted,'?') AS state, "
                            "COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files GROUP BY 1, 2"):
            t = out.setdefault(r["tag"], {"count": 0, "bytes": 0, "by_state": {}})
            t["count"] += r["n"]
            t["bytes"] += r["bytes"]
            t["by_state"][r["state"]] = r["n"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["count"]))

    def model_stats(self, model_type: str) -> dict:
        """Total de um tipo de modelo (ou vários, separados por vírgula) por origem e por estado."""
        where, params = self._model_where(model_type)
        stats = {"model_type": model_type, "total": self.count_models(model_type=model_type)}
        for key, col in (("by_source", "source"), ("by_deleted_state", "deleted_state")):
            rows = self._rows(f"SELECT COALESCE({col},'?') AS k, COUNT(*) AS n FROM models{where} "
                              "GROUP BY 1 ORDER BY n DESC", params)
            stats[key] = {r["k"]: r["n"] for r in rows}
        return stats

    def resolve_local_path(self, file_or_attachment_id: str) -> Optional[str]:
        """Caminho do arquivo dentro do UFDR a partir do ID de um arquivo ou anexo."""
        row = self.conn.execute("SELECT local_path FROM files WHERE id=?", (file_or_attachment_id,)).fetchone()
        if row and row[0]:
            return row[0]
        row = self.conn.execute(
            "SELECT a.local_path, f.local_path FROM attachments a LEFT JOIN files f ON f.id = a.file_id "
            "WHERE a.id=? OR a.file_id=?", (file_or_attachment_id, file_or_attachment_id)).fetchone()
        if row:
            return row[0] or row[1]
        return None

    def extract_file(self, file_id: str, output_dir: str) -> dict:
        """Copiar um arquivo de dentro do UFDR para output_dir."""
        local = self.resolve_local_path(file_id)
        if not local:
            raise FileNotFoundError(f"Nenhum arquivo/anexo com ID {file_id} tem caminho no UFDR.")
        member = local.replace("\\", "/")
        out_dir = Path(output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / Path(member).name  # só o nome: evita path traversal
        if self.ufdr_path.is_dir():
            shutil.copyfile(self.ufdr_path / member, dest)
        else:
            with open_zip(self.ufdr_path) as zf, zf.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return {"file_id": file_id, "ufdr_member": member, "extracted_to": str(dest), "size": dest.stat().st_size}

    def close(self):
        self.conn.close()


def _end_of_day(date: str) -> str:
    """'2024-05-18' -> '2024-05-18T23:59:59.999~' para comparação inclusiva."""
    return date + "T23:59:59.999~" if len(date) == 10 else date


# =====================================================================
# Cache de índices abertos (um por arquivo)
# =====================================================================

_open: dict[str, UFDRIndex] = {}
_lock = threading.Lock()


def get_index(ufdr_path: str, rebuild: bool = False) -> UFDRIndex:
    key = str(Path(ufdr_path).expanduser().resolve())
    with _lock:
        idx = _open.get(key)
        if idx is None or rebuild or not idx.db_path.exists() or idx.db_path != cache_file_for(Path(key)):
            if idx:
                idx.close()
            idx = UFDRIndex(key, rebuild=rebuild)
            _open[key] = idx
        return idx
