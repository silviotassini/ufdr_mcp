# UFDR MCP

Servidor [MCP (Model Context Protocol)](https://modelcontextprotocol.io) para analisar relatórios
**UFDR da Cellebrite** em linguagem natural, a partir do Claude Code, Claude Desktop, Hermes Agent ou
qualquer outro cliente MCP.

Lê o `.ufdr` direto: não precisa de Java, IPED, Physical Analyzer nem de extrair o arquivo.

```
Você:   abra o caso D:\casos\2025-001
Agente: Caso aberto. Android, 120 mil mensagens (WhatsApp, SMS, Telegram), 2.300 contatos,
        18.000 imagens e 400 vídeos, de 2019-03-02 a 2025-08-20.
Você:   mostre as mensagens apagadas que falam de "pix" em julho de 2025
```

## Recursos

- **Caso informado na conversa**: você diz ao agente onde está o `.ufdr` (o arquivo ou a pasta do caso).
  Nenhuma pasta de caso vai na configuração do agente.
- **UFDR dividido em partes** (`nome.z01`, `nome.z02`, ..., `nome.ufdr`) lido direto, sem juntar as partes.
- **Índice SQLite em cache**: o `report.xml` é lido uma única vez, e as consultas seguintes são imediatas.
- **Chats, mensagens, contatos, chamadas, localizações, arquivos** e qualquer outro tipo de dado decodificado
  (apps instalados, senhas, páginas visitadas, notificações...), com filtros por texto, app, remetente,
  período e estado (intacto/apagado).
- **Estatísticas por categoria**: contatos por app de origem, imagens e vídeos por estado, tamanhos.
- **Busca full-text** (SQLite FTS5) em todo o relatório.
- **Extração de arquivos e anexos** de dentro do UFDR, sem alterá-lo.
- **Somente leitura**: o servidor nunca grava na pasta do caso.

---

## 1. Instalação

Requisitos: Python 3.11+ e [`uv`](https://github.com/astral-sh/uv).

A leitura do UFDR fica no pacote [`ufdr_parser`](https://github.com/silviotassini/ufdr_parser),
compartilhado com outros projetos. Ele é instalado a partir da pasta vizinha `../ufdr_parser`
(veja `[tool.uv.sources]` no `pyproject.toml`), então clone os dois lado a lado:

```bash
git clone https://github.com/silviotassini/ufdr_parser.git
git clone https://github.com/silviotassini/ufdr_mcp.git
cd ufdr_mcp
uv sync
```

Teste rápido:

```bash
uv run ufdr-mcp --index /caminho/arquivo.ufdr      # indexa e imprime o resumo do caso
```

---

## 2. Uso

```bash
uv run ufdr-mcp
```

O servidor conversa por stdio; quem o inicia normalmente é o cliente MCP (seção 4). Ele não recebe
nenhum caso na linha de comando: o caso é sempre informado na conversa.

| Opção | Variável de ambiente | Descrição |
|-------|----------------------|-----------|
| `--index ARQUIVO` | | Só gera o índice, imprime o resumo e sai (`--rebuild` força recriar) |
| | `UFDR_MCP_CACHE` | Pasta dos índices (padrão: `~/.cache/ufdr-mcp`) |

### Abrindo um caso

Diga ao agente **onde está o caso**:

- *"Abra o caso D:\casos\2025-001\EXTRACTION_FFS\aparelho.ufdr"*: pelo caminho do `.ufdr`.
- *"Abra o caso D:\casos\2025-001"*: pela pasta (o `.ufdr` é procurado dentro dela, até 6 níveis).
- *"Quais UFDRs há em D:\casos?"*: lista os `.ufdr` de uma pasta (`ufdr_list_cases`).

Se você pedir um caso sem dizer onde ele está, o agente pergunta a localização. Se a pasta tiver mais de
um `.ufdr`, o agente mostra as opções para você escolher. Depois de aberto, todas as
ferramentas usam o caso; `ufdr_path` continua aceito para consultar outro arquivo pontualmente.
Caminhos do Windows (`G:\...`) são convertidos para `/mnt/g/...` quando o servidor roda no WSL.

### UFDR dividido em partes

Extrações grandes são gravadas pelo UFED como um ZIP dividido:

```
aparelho.z01  aparelho.z02  ...  aparelho.z15  aparelho.ufdr
```

O `aparelho.ufdr` é a **última parte**. Basta apontar para ele, com as partes `.zNN` na mesma pasta: as
partes são lidas como um único arquivo, sem extrair nem copiar nada. Não renomeie as partes, e não
trate o `.ufdr` como XML corrompido: sozinho, ele não abre em ferramentas de ZIP comuns.

### Índice em cache

Na primeira consulta de um UFDR, o `report.xml` é lido uma vez e gravado num índice SQLite. Isso leva de
segundos a alguns minutos, conforme o tamanho (cerca de 3 min para um `report.xml` de 780 MB).
As consultas seguintes são imediatas, e o índice é refeito sozinho se o `.ufdr` ou alguma parte mudar.

Para não esperar na primeira pergunta, gere o índice antes:

```bash
uv run ufdr-mcp --index /caminho/arquivo.ufdr            # imprime o resumo
uv run ufdr-mcp --index /caminho/arquivo.ufdr --rebuild  # força recriar
```

> **Atenção:** o índice contém o conteúdo do relatório (mensagens, contatos...) em texto puro. Guarde
> `UFDR_MCP_CACHE` num local com o mesmo nível de proteção das evidências e apague os índices ao encerrar o caso.

---

## 3. Ferramentas

As datas são ISO 8601 UTC, e os filtros de data aceitam `YYYY-MM-DD`. As listagens retornam
`total`/`returned` e paginam com `limit`/`offset`.

| Ferramenta | Descrição |
|------------|-----------|
| `ufdr_open_case(case)` | Abre o caso informado pelo usuário (caminho do `.ufdr` ou pasta) e retorna o resumo |
| `ufdr_list_cases(folder)` | Mostra o caso aberto e lista os `.ufdr` de uma pasta |
| `ufdr_summary` | Caso, extrações, contagem por tipo de dado, mensagens por app, período, avisos |
| `ufdr_stats(model_type)` | Itens por categoria: dados decodificados por tipo, arquivos por categoria (imagens, vídeos...) com tamanho e estado, contatos e mensagens por app e estado; ou um tipo qualquer por origem/estado |
| `ufdr_device_info` | Modelo, IMEI, SO, nome do aparelho, contas |
| `ufdr_list_chats(source, participant)` | Conversas com participantes e nº de mensagens |
| `ufdr_get_chat(chat_id)` | Mensagens de uma conversa em ordem cronológica, com anexos |
| `ufdr_search_messages(text, sender, source, chat_id, start_date, end_date, deleted_only, with_attachments)` | Busca em mensagens |
| `ufdr_list_contacts(query, source)` | Contatos (busca em nome, telefones, IDs) |
| `ufdr_list_calls(number, source, start_date, end_date)` | Registro de chamadas |
| `ufdr_list_locations(start_date, end_date, source)` | Localizações |
| `ufdr_query(model_type, text, source, start_date, end_date, deleted_state, full)` | Qualquer tipo de dado (InstalledApplication, VisitedPage, Password, Notification...) ou linha do tempo geral |
| `ufdr_search(query, model_type)` | Busca full-text em todo o relatório |
| `ufdr_get_model(model_id)` | Todos os campos de um item |
| `ufdr_list_files(name, tag, extension, path, hash_value, deleted)` | Arquivos do relatório com hashes MD5/SHA256; retorna total e tamanho do que foi filtrado |
| `ufdr_extract_file(file_id, output_dir)` | Copia um arquivo/anexo de dentro do UFDR para uma pasta |
| `ufdr_reindex()` | Recria o índice em cache |

Exemplos de perguntas:

- *"Abra o caso D:\casos\2025-001"* → `ufdr_open_case`
- *"Resuma o caso"* → `ufdr_summary`
- *"Liste os itens por categoria"* / *"Quantos contatos há e de onde vieram?"* → `ufdr_stats`
- *"Quais chats de WhatsApp existem?"* → `ufdr_list_chats`
- *"Mostre mensagens com 'pix' em maio de 2024"* → `ufdr_search_messages`
- *"Quais apps estão instalados?"* → `ufdr_query` com `model_type=InstalledApplication`
- *"Extraia o anexo da mensagem X para D:\saida"* → `ufdr_extract_file`

---

## 4. Integração com clientes MCP

A configuração só diz **como iniciar o servidor**; nenhuma pasta de caso entra nela. O caso é
informado depois, na conversa (*"abra o caso D:\casos\2025-001"*).

Nos exemplos, troque `/caminho/ufdr_mcp` pela pasta do projeto. No Windows, use barras normais
(`D:/workspace/ufdr_mcp`) ou barras invertidas duplicadas em JSON/YAML (`D:\\workspace\\ufdr_mcp`).

### Claude Code

```bash
claude mcp add ufdr -s user -- uv run --directory /caminho/ufdr_mcp ufdr-mcp

# Windows (PowerShell ou cmd)
claude mcp add ufdr -s user -- uv run --directory D:/workspace/ufdr_mcp ufdr-mcp
```

`-s user` deixa o servidor disponível em todos os projetos; use `-s project` para gravá-lo no
`.mcp.json` do projeto atual. Confira com `claude mcp list` (deve mostrar `ufdr ... ✓ Connected`)
ou com `/mcp` dentro do Claude Code. Para remover: `claude mcp remove ufdr -s user`.

Como a primeira indexação de um UFDR grande leva minutos, aumente o tempo limite das ferramentas
se a chamada for interrompida: `MCP_TOOL_TIMEOUT=600000` (ms) no ambiente ou em `env` do
`~/.claude/settings.json` — ou gere o índice antes com `--index`.

### Claude Desktop e outros clientes com `mcpServers`

No `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`;
macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`) ou no `.mcp.json`:

```json
{
  "mcpServers": {
    "ufdr": {
      "command": "uv",
      "args": ["run", "--directory", "/caminho/ufdr_mcp", "ufdr-mcp"]
    }
  }
}
```

O mesmo bloco está em [`mcp_config.json`](mcp_config.json). Se o `uv` não estiver no `PATH` do
aplicativo, use o caminho completo em `"command"`.

### Hermes Agent

Na seção `mcp_servers` do `config.yaml` do Hermes:

- Windows (instalação nativa): `%LOCALAPPDATA%\hermes\config.yaml`
- Linux/macOS/WSL: `~/.hermes/config.yaml` (no WSL, os discos do Windows ficam em `/mnt/<letra>`)

```yaml
mcp_servers:
  ufdr:
    command: "uv"
    args: ["run", "--directory", "/caminho/ufdr_mcp", "ufdr-mcp"]
    timeout: 600          # a 1ª indexação de um UFDR grande leva minutos
    connect_timeout: 120
```

No Windows nativo, por exemplo:

```yaml
mcp_servers:
  ufdr:
    command: uv
    args: ["run", "--directory", "D:/workspace/ufdr_mcp", "ufdr-mcp"]
    timeout: 600
    connect_timeout: 120
```

O caminho em `--directory` tem de ser exatamente o da pasta do projeto (`ufdr_mcp`, com sublinhado).
Reinicie o Hermes e peça *"abra o caso D:\casos\2025-001"*. As ferramentas aparecem como `mcp_ufdr_ufdr_*`.

> Se o agente também tiver o MCP do IPED, ele pode confundir os servidores em pedidos como
> "liste por categoria". Desative o outro servidor (`enabled: false`) ou diga "usando o ufdr, ...".

### Cuidados com agentes

Agentes com acesso ao terminal podem tentar "consertar" arquivos que não entendem, por exemplo
renomear o `.ufdr` de um UFDR dividido. O servidor instrui o agente a não alterar a pasta do caso,
mas isso é só uma orientação. Para garantir, deixe as evidências **somente leitura**
(permissão NTFS de leitura, `attrib +R` ou montagem read-only) ou restrinja o acesso do agente ao terminal.

---

## 5. Notas sobre o formato UFDR

- O `.ufdr` é um ZIP com `report.xml` (namespace `http://pa.cellebrite.com/report/2.0`) e os arquivos em `files/<Categoria>/`.
- Também é possível apontar para a pasta extraída (que contém `report.xml`) ou para o próprio `report.xml`.
- O `report.xml` é lido em streaming (`iterparse`): relatórios de centenas de MB não são carregados inteiros na memória.
- Alguns relatórios vêm com a seção final `<extraInfos>` truncada ou malformada. O parser registra um aviso
  (visível em `ufdr_summary.warnings`) e mantém todos os dados decodificados, que vêm antes dela.
- Em algumas versões do relatório os arquivos não têm o atributo `name`; nesses casos o nome é tirado do caminho.

---

## 6. Como biblioteca Python

```python
from ufdr_mcp import UFDRIndex

idx = UFDRIndex("/caminho/arquivo.ufdr")
print(idx.summary()["messages_by_source"])
for m in idx.search_messages(text="pix", source="WhatsApp"):
    print(m["ts"], m["sender"], m["body"])
```

Há também um cliente MCP de linha de comando, útil para testar o servidor. Com `--ufdr`, ele abre o
caso com `ufdr_open_case` antes do comando, como um agente faria:

```bash
uv run python ufdr_mcp_client.py --ufdr arquivo.ufdr list-tools
uv run python ufdr_mcp_client.py --ufdr arquivo.ufdr call ufdr_search_messages '{"text": "pix"}'
```

> No PowerShell, o JSON entre aspas simples precisa de aspas internas escapadas
> (`'{\"text\": \"pix\"}'`). Prefira o Git Bash, ou use a biblioteca Python.

---

## 7. Estrutura

```
ufdr_mcp/
├── pyproject.toml      # Projeto uv (entry point ufdr-mcp)
├── ufdr_mcp/
│   ├── __init__.py     # Exporta UFDRIndex, get_index (e iter_report do ufdr_parser)
│   ├── server.py       # Servidor MCP e ferramentas
│   └── index.py        # Índice SQLite em cache + consultas (lê o UFDR via ufdr_parser)
├── ufdr_mcp_client.py  # Cliente MCP de linha de comando / biblioteca
├── mcp_config.json     # Exemplo de configuração para clientes MCP
└── tests/              # Testes com UFDRs sintéticos (inclusive dividido)
```

## 8. Desenvolvimento

```bash
uv run pytest tests/ -v
uv run ruff check ufdr_mcp tests
```

Os testes usam apenas UFDRs sintéticos gerados na hora. Nunca adicione material de casos reais ao
repositório; o `.gitignore` já bloqueia `*.ufdr`, `*.zNN`, `report.xml` e índices `*.sqlite`.
