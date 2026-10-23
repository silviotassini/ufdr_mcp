"""
Servidor MCP para explorar relatórios UFDR (Cellebrite).

Independente do servidor IPED: lê o .ufdr direto (ZIP + report.xml), sem Java
e sem caso IPED. Na primeira consulta de um arquivo é criado um índice SQLite
em cache (~/.cache/ufdr-mcp); as consultas seguintes são imediatas.

O caso nunca é fixado na configuração do cliente: o usuário informa a localização na
conversa ("abra o caso D:\\casos\\2025-001") e ufdr_open_case o abre; as demais
ferramentas passam a usá-lo sem precisar de ufdr_path.

Uso:
    ufdr-mcp                                   # servidor MCP (stdio)
    ufdr-mcp --index /caminho/arquivo.ufdr     # só gerar o índice e sair
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ufdr_mcp.index import UFDRIndex, get_index

app = MCPServer(
    "ufdr-mcp",
    instructions=(
        "Ferramentas para analisar relatórios UFDR (Cellebrite): chats, mensagens, contatos, "
        "chamadas, localizações, arquivos e qualquer outro tipo de modelo do report.xml. "
        "Estas ferramentas (ufdr_*) leem o .ufdr direto; não use ferramentas de outro servidor (ex.: IPED) "
        "para um caso aberto aqui, nem tente cadastrá-lo nelas. "
        "Contagens por categoria (tipos de dados, imagens, vídeos, contatos por app...): ufdr_stats. "
        "Comece com ufdr_summary. Datas são ISO 8601 em UTC; filtros de data aceitam 'YYYY-MM-DD'. "
        "Antes de tudo, abra o caso com ufdr_open_case, usando a localização que o usuário informar "
        "(caminho do .ufdr ou pasta do caso); se ele não informou, pergunte. As demais ferramentas usam "
        "o caso aberto e dispensam ufdr_path. ufdr_list_cases lista os UFDRs de uma pasta. "
        "Os arquivos do caso são evidência: NUNCA renomeie, mova, copie para dentro da pasta ou altere "
        "nada nela. UFDR dividido (nome.z01, nome.z02, ..., nome.ufdr) é normal e lido direto pelo servidor; "
        "se uma ferramenta falhar, relate o erro ao usuário em vez de tentar consertar os arquivos."
    ),
)

# Caso aberto na conversa (ufdr_open_case)
DEFAULT_UFDR: Optional[str] = None
# Profundidade máxima ao procurar .ufdr dentro de uma pasta
SEARCH_DEPTH = 6


def _json(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


async def _index(ufdr_path: Optional[str]) -> UFDRIndex:
    return await _open_index(ufdr_path)


async def _open_index(ufdr_path: Optional[str], rebuild: bool = False) -> UFDRIndex:
    path = _native_path(ufdr_path) if ufdr_path else DEFAULT_UFDR
    if not path:
        raise ToolError("Nenhum caso aberto. Peça ao usuário a localização do caso (arquivo .ufdr ou "
                        "pasta) e use ufdr_open_case.")
    try:
        # A primeira indexação pode levar dezenas de segundos; não bloquear o loop
        return await asyncio.to_thread(get_index, path, rebuild)
    except (FileNotFoundError, ValueError) as e:
        raise ToolError(str(e)) from e


def _native_path(path: str) -> str:
    """Aceitar caminhos do Windows (G:\\pasta) também quando o servidor roda no WSL/Linux."""
    path = path.strip().strip("\"'")
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if m and os.name != "nt":
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return path


def _find_ufdrs(root: Path) -> list[Path]:
    """Arquivos .ufdr dentro de uma pasta (sem descer além de SEARCH_DEPTH níveis)."""
    found = []
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base >= SEARCH_DEPTH:
            dirnames.clear()
        # Pastas "files" de UFDR extraído têm milhares de itens e nunca contêm .ufdr
        dirnames[:] = sorted(d for d in dirnames if d.lower() != "files")
        found += [Path(dirpath) / f for f in sorted(filenames) if f.lower().endswith(".ufdr")]
    return found


def _resolve_case(case: str) -> list[Path]:
    """Caminho do .ufdr (ou report.xml) ou pasta do caso, onde o .ufdr é procurado."""
    path = Path(_native_path(case)).expanduser()
    if path.is_file():
        return [path]
    if path.is_dir():
        if (path / "report.xml").exists():
            return [path]
        return _find_ufdrs(path)
    return []


def _paged(items: list, total: int, limit: int, offset: int) -> dict:
    return {"total": total, "returned": len(items), "offset": offset, "limit": limit, "items": items}


# =====================================================================
# Visão geral
# =====================================================================

@app.tool()
async def ufdr_open_case(case: str) -> str:
    """Abrir um caso; as demais ferramentas passam a usá-lo sem precisar de ufdr_path.

    Indexa o UFDR na primeira vez (UFDRs grandes podem levar alguns minutos) e retorna o resumo.
    Se houver mais de um .ufdr compatível, retorna a lista para o usuário escolher.

    Args:
        case: Localização informada pelo usuário: caminho do .ufdr (pode ser a última parte de um
              UFDR dividido em .z01, .z02...), pasta extraída/report.xml ou pasta do caso
              (o .ufdr é procurado dentro dela).
    """
    global DEFAULT_UFDR
    matches = await asyncio.to_thread(_resolve_case, case)
    if not matches:
        raise ToolError(f"Nenhum UFDR encontrado em '{case}'. Peça ao usuário o caminho do .ufdr "
                        "ou da pasta do caso.")
    if len(matches) > 1:
        return _json({"ambiguous": True, "message": "Mais de um UFDR encontrado; peça ao usuário para escolher "
                      "e chame ufdr_open_case com o caminho completo.", "options": [str(m) for m in matches]})
    idx = await _open_index(str(matches[0]))
    DEFAULT_UFDR = str(idx.ufdr_path)
    return _json({"opened": DEFAULT_UFDR, "summary": idx.summary()})


@app.tool()
async def ufdr_list_cases(folder: Optional[str] = None) -> str:
    """Mostrar o caso aberto no momento e, se 'folder' for informado, os UFDRs dentro dessa pasta.

    Args:
        folder: Pasta informada pelo usuário onde procurar arquivos .ufdr.
    """
    root = Path(_native_path(folder)).expanduser() if folder else None
    if root and not root.is_dir():
        raise ToolError(f"Pasta não encontrada: {folder}")
    cases = [str(p) for p in await asyncio.to_thread(_find_ufdrs, root)] if root else []
    return _json({"current": DEFAULT_UFDR, "folder": str(root) if root else None, "cases": cases})


@app.tool()
async def ufdr_summary(ufdr_path: Optional[str] = None) -> str:
    """Resumo do UFDR: caso, extrações, contagem por tipo de modelo, mensagens por app,
    arquivos e período coberto. Indexa o arquivo na primeira chamada.

    Args:
        ufdr_path: Caminho do .ufdr (ou pasta extraída / report.xml).
    """
    idx = await _index(ufdr_path)
    return _json(idx.summary())


@app.tool()
async def ufdr_device_info(ufdr_path: Optional[str] = None) -> str:
    """Informações do aparelho (modelo, IMEI, SO, contas, etc.) e metadados da extração.

    Args:
        ufdr_path: Caminho do .ufdr.
    """
    idx = await _index(ufdr_path)
    return _json(idx.device_info())


@app.tool()
async def ufdr_reindex(ufdr_path: Optional[str] = None) -> str:
    """Forçar a recriação do índice em cache do UFDR.

    Args:
        ufdr_path: Caminho do .ufdr.
    """
    idx = await _open_index(ufdr_path, rebuild=True)
    return _json({"reindexed": str(idx.ufdr_path), "cache": str(idx.db_path)})


# =====================================================================
# Chats e mensagens
# =====================================================================

@app.tool()
async def ufdr_list_chats(ufdr_path: Optional[str] = None, source: Optional[str] = None,
                          participant: Optional[str] = None, limit: int = 50, offset: int = 0) -> str:
    """Listar conversas (WhatsApp, Instagram, SMS...) com participantes e nº de mensagens.

    Args:
        ufdr_path: Caminho do .ufdr.
        source: Filtrar pelo app (ex: 'WhatsApp').
        participant: Filtrar por nome/número/ID de participante (substring).
        limit: Máximo de resultados.
        offset: Paginação.
    """
    idx = await _index(ufdr_path)
    return _json(idx.list_chats(source=source, participant=participant, limit=limit, offset=offset))


@app.tool()
async def ufdr_get_chat(chat_id: str, ufdr_path: Optional[str] = None, limit: int = 200,
                        offset: int = 0) -> str:
    """Ler uma conversa: dados do chat e mensagens em ordem cronológica (com anexos).

    Args:
        chat_id: ID do chat (de ufdr_list_chats).
        ufdr_path: Caminho do .ufdr.
        limit: Máximo de mensagens.
        offset: Paginação.
    """
    idx = await _index(ufdr_path)
    chat = idx.get_chat(chat_id)
    if not chat:
        return _json({"error": f"Chat {chat_id} não encontrado."})
    msgs = idx.search_messages(chat_id=chat_id, limit=limit, offset=offset)
    chat["messages"] = _paged(msgs, idx.count_messages(chat_id=chat_id), limit, offset)
    return _json(chat)


@app.tool()
async def ufdr_search_messages(ufdr_path: Optional[str] = None, text: Optional[str] = None,
                               sender: Optional[str] = None, source: Optional[str] = None,
                               chat_id: Optional[str] = None, start_date: Optional[str] = None,
                               end_date: Optional[str] = None, deleted_only: bool = False,
                               with_attachments: bool = False, limit: int = 50, offset: int = 0,
                               order: str = "asc") -> str:
    """Buscar mensagens de chats/SMS por texto, remetente, app, período e estado.

    Args:
        ufdr_path: Caminho do .ufdr.
        text: Substring no corpo da mensagem.
        sender: Substring do nome/número/ID do remetente.
        source: App (ex: 'WhatsApp', 'Instagram', 'Native Messages').
        chat_id: Restringir a um chat.
        start_date: Data inicial (YYYY-MM-DD ou ISO).
        end_date: Data final, inclusiva.
        deleted_only: Apenas mensagens recuperadas/apagadas.
        with_attachments: Apenas mensagens com anexo.
        limit: Máximo de resultados.
        offset: Paginação.
        order: 'asc' ou 'desc' por data.
    """
    idx = await _index(ufdr_path)
    filters = dict(text=text, sender=sender, source=source, chat_id=chat_id, start_date=start_date,
                   end_date=end_date, deleted_only=deleted_only, with_attachments=with_attachments)
    items = idx.search_messages(limit=limit, offset=offset, order=order, **filters)
    return _json(_paged(items, idx.count_messages(**filters), limit, offset))


# =====================================================================
# Contatos, chamadas, localizações
# =====================================================================

@app.tool()
async def ufdr_list_contacts(ufdr_path: Optional[str] = None, query: Optional[str] = None,
                             source: Optional[str] = None, limit: int = 100, offset: int = 0) -> str:
    """Listar contatos (agenda, WhatsApp, etc.). 'query' busca em nome, números e IDs.

    Args:
        ufdr_path: Caminho do .ufdr.
        query: Substring de nome, telefone, e-mail ou user ID.
        source: App de origem.
        limit: Máximo de resultados.
        offset: Paginação.
    """
    idx = await _index(ufdr_path)
    f = dict(model_type="Contact", text=query, source=source)
    return _json(_paged(idx.query_models(**f, limit=limit, offset=offset), idx.count_models(**f), limit, offset))


@app.tool()
async def ufdr_list_calls(ufdr_path: Optional[str] = None, number: Optional[str] = None,
                          source: Optional[str] = None, start_date: Optional[str] = None,
                          end_date: Optional[str] = None, limit: int = 100, offset: int = 0,
                          order: str = "desc") -> str:
    """Listar registros de chamadas (telefonia e apps) com direção, status, partes e duração.

    Args:
        ufdr_path: Caminho do .ufdr.
        number: Substring de número/nome da outra parte.
        source: App (ex: 'WhatsApp').
        start_date: Data inicial.
        end_date: Data final.
        limit: Máximo de resultados.
        offset: Paginação.
        order: 'asc' ou 'desc'.
    """
    idx = await _index(ufdr_path)
    f = dict(model_type="Call", text=number, source=source, start_date=start_date, end_date=end_date)
    items = idx.query_models(**f, limit=limit, offset=offset, order=order)
    return _json(_paged(items, idx.count_models(**f), limit, offset))


@app.tool()
async def ufdr_list_locations(ufdr_path: Optional[str] = None, start_date: Optional[str] = None,
                              end_date: Optional[str] = None, source: Optional[str] = None,
                              limit: int = 100, offset: int = 0) -> str:
    """Listar localizações (lat,long) com data e app de origem.

    Args:
        ufdr_path: Caminho do .ufdr.
        start_date: Data inicial.
        end_date: Data final.
        source: App de origem.
        limit: Máximo de resultados.
        offset: Paginação.
    """
    idx = await _index(ufdr_path)
    f = dict(model_type="Location", source=source, start_date=start_date, end_date=end_date)
    items = idx.query_models(**f, limit=limit, offset=offset)
    return _json(_paged(items, idx.count_models(**f), limit, offset))


# =====================================================================
# Genérico (qualquer tipo de modelo)
# =====================================================================

@app.tool()
async def ufdr_query(model_type: Optional[str] = None, ufdr_path: Optional[str] = None,
                     text: Optional[str] = None, source: Optional[str] = None,
                     start_date: Optional[str] = None, end_date: Optional[str] = None,
                     deleted_state: Optional[str] = None, full: bool = False, limit: int = 50,
                     offset: int = 0, order: str = "asc") -> str:
    """Consultar qualquer tipo de modelo (ver model_counts em ufdr_summary), ex:
    InstalledApplication, VisitedPage, SearchedItem, UserAccount, Password, Notification,
    WirelessNetwork, Cookie, AppsUsageLog. Sem model_type, todos os tipos (útil como timeline).

    Args:
        model_type: Tipo(s) separados por vírgula.
        ufdr_path: Caminho do .ufdr.
        text: Substring em qualquer campo.
        source: App de origem.
        start_date: Data inicial.
        end_date: Data final.
        deleted_state: 'Intact', 'Deleted' ou 'Trash'.
        full: Incluir todos os campos do modelo (senão só o resumo).
        limit: Máximo de resultados.
        offset: Paginação.
        order: 'asc' ou 'desc' por data.
    """
    idx = await _index(ufdr_path)
    f = dict(model_type=model_type, text=text, source=source, start_date=start_date,
             end_date=end_date, deleted_state=deleted_state)
    items = idx.query_models(**f, limit=limit, offset=offset, full=full, order=order)
    return _json(_paged(items, idx.count_models(**f), limit, offset))


@app.tool()
async def ufdr_search(query: str, ufdr_path: Optional[str] = None, model_type: Optional[str] = None,
                      limit: int = 50) -> str:
    """Busca full-text em todo o relatório (todos os tipos), ordenada por relevância.
    Palavras são combinadas com AND; aceita sintaxe FTS5 (OR, "frase exata", prefixo*).

    Args:
        query: Termos de busca.
        ufdr_path: Caminho do .ufdr.
        model_type: Restringir a um tipo.
        limit: Máximo de resultados.
    """
    idx = await _index(ufdr_path)
    try:
        return _json(idx.search(query, model_type=model_type, limit=limit))
    except sqlite3.OperationalError as e:
        raise ToolError(f"Consulta FTS inválida: {e}") from e


@app.tool()
async def ufdr_get_model(model_id: str, ufdr_path: Optional[str] = None) -> str:
    """Obter todos os campos de um modelo (mensagem, contato, chamada...) pelo ID.

    Args:
        model_id: ID do modelo.
        ufdr_path: Caminho do .ufdr.
    """
    idx = await _index(ufdr_path)
    return _json(idx.get_model(model_id) or {"error": f"Modelo {model_id} não encontrado."})


# =====================================================================
# Arquivos
# =====================================================================

@app.tool()
async def ufdr_list_files(ufdr_path: Optional[str] = None, name: Optional[str] = None,
                          tag: Optional[str] = None, extension: Optional[str] = None,
                          path: Optional[str] = None, hash_value: Optional[str] = None,
                          deleted: Optional[str] = None, limit: int = 100, offset: int = 0) -> str:
    """Listar arquivos do relatório (taggedFiles) com caminho original, tamanho e hashes.
    Sem filtros, retorna também a contagem por categoria (Image, Video, Database...).

    Args:
        ufdr_path: Caminho do .ufdr.
        name: Substring do nome.
        tag: Categoria (Image, Video, Audio, Document, Database, Text, Archives...).
        extension: Extensão (ex: 'jpg').
        path: Substring do caminho no aparelho.
        hash_value: MD5 ou SHA256 exato.
        deleted: 'Intact' ou 'Deleted'.
        limit: Máximo de resultados.
        offset: Paginação.
    """
    idx = await _index(ufdr_path)
    f = dict(name=name, tag=tag, extension=extension, path=path, hash_value=hash_value, deleted=deleted)
    items = idx.list_files(**f, limit=limit, offset=offset)
    totals = idx.count_files(**f)
    result = {"total": totals["count"], "total_bytes": totals["bytes"], "returned": len(items),
              "offset": offset, "limit": limit, "items": items}
    if not any(f.values()):
        result["by_tag"] = idx.file_tags()
    return _json(result)


@app.tool()
async def ufdr_stats(model_type: Optional[str] = None, ufdr_path: Optional[str] = None) -> str:
    """Itens por categoria do caso UFDR: contagens e estatísticas, sem paginar listagens.

    Use para "liste itens por categoria", "quantos contatos/imagens/vídeos", "de onde vieram".
    Sem model_type: dados decodificados por categoria (Chat, Contact, Call, Password, Location...);
    arquivos por categoria (Image, Video, Audio, Document...) com tamanho e estado; contatos e
    mensagens por origem (app) e por estado (Intact/Deleted/Trash).
    Com model_type: totais dessa categoria por origem e por estado (ex.: 'Call', 'Contact,UserAccount').

    Args:
        model_type: Tipo(s) de modelo, separados por vírgula. Veja os tipos em ufdr_summary.
        ufdr_path: Caminho do .ufdr.
    """
    idx = await _index(ufdr_path)
    if model_type:
        return _json(idx.model_stats(model_type))
    return _json({"decoded_by_category": idx.summary()["model_counts"], "files_by_category": idx.file_stats(),
                  "contacts": idx.model_stats("Contact"), "messages": idx.model_stats("InstantMessage")})


@app.tool()
async def ufdr_extract_file(file_id: str, output_dir: str, ufdr_path: Optional[str] = None) -> str:
    """Extrair um arquivo ou anexo de dentro do UFDR para uma pasta local.

    Args:
        file_id: ID do arquivo (ufdr_list_files) ou do anexo/file_id de uma mensagem.
        output_dir: Pasta de destino.
        ufdr_path: Caminho do .ufdr.
    """
    idx = await _index(ufdr_path)
    try:
        return _json(await asyncio.to_thread(idx.extract_file, file_id, output_dir))
    except (FileNotFoundError, KeyError) as e:
        raise ToolError(str(e)) from e


# =====================================================================
# Entry point
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="UFDR MCP Server. O caso é informado na conversa (ufdr_open_case), não na configuração.")
    parser.add_argument("--index", metavar="UFDR", help="Apenas gerar o índice deste UFDR e sair")
    parser.add_argument("--rebuild", action="store_true", help="Com --index: recriar o índice")
    args = parser.parse_args()

    if args.index:
        idx = UFDRIndex(args.index, rebuild=args.rebuild)
        print(_json(idx.summary()))
        return
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
