"""
MCP TJMG v2 — Nova API pública de jurisprudência do TJMG.

Substitui o portal legado (www5.tjmg.jus.br, com CAPTCHA/OCR/DWR) pela nova API
REST pública (jurisprudencia-api.tjmg.jus.br) do site consulta-jurisprudencia.tjmg.jus.br.

Sem CAPTCHA, sem OCR, sem autenticação. Resposta em JSON estruturado.

Escopos de pesquisa (combináveis):
  marcelo   → Des. Marcelo Paulo Salgado (substituto atual na 1ª Câmara; acervo na 5ª)
  manoel    → Des. Manoel Dos Reis Morais (titular, hoje na presidência do TJ)
  camara1   → 1ª Câmara Cível (onde ambos atuam/atuaram)
  camara5   → 5ª Câmara Cível (câmara legada do Des. Marcelo)
  tjmg      → todo o TJMG, sem filtro de câmara/relator
"""

import asyncio
import os
from datetime import date

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

_RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "mcp-tjmg.onrender.com")

mcp = FastMCP(
    "TJMG Jurisprudência v2",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            _RENDER_HOST,
            f"{_RENDER_HOST}:443",
        ],
        allowed_origins=[
            "https://claude.ai",
            "https://www.claude.ai",
            f"https://{_RENDER_HOST}",
        ],
    ),
)

# ─────────────────────────── Endpoints da nova API ───────────────────────────
API_BASE   = "https://jurisprudencia-api.tjmg.jus.br"
FILTER_URL = f"{API_BASE}/jurisprudencias/filter"
DOC_URL    = f"{API_BASE}/jurisprudencias/document"
DOMINIO_URL = f"{API_BASE}/dominio"

ORIGIN = "https://consulta-jurisprudencia.tjmg.jus.br"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Content-Type": "application/json; charset=utf-8",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
}

# ─────────────────────── Nomes EXATOS dos domínios ───────────────────────────
# (validados via POST /dominio/... na própria API)
MAG_MARCELO = "Marcelo Paulo Salgado"
MAG_MANOEL  = "Manoel Dos Reis Morais"
CAMARA_1    = "1ª Câmara Cível"
CAMARA_5    = "5ª Câmara Cível"

# Família "apelação" — classes relevantes para redação de votos de apelação.
CLASSES_APELACAO = ["Apelação", "Apelação / Reexame Necessário", "Reexame Necessário"]

# Tokens de escopo → contribuição para os filtros da API.
_TOKENS_MAGISTRADO = {
    "marcelo": MAG_MARCELO, "salgado": MAG_MARCELO,
    "manoel": MAG_MANOEL, "moraes": MAG_MANOEL, "morais": MAG_MANOEL,
}
_TOKENS_CAMARA = {
    "camara1": CAMARA_1, "camara_1": CAMARA_1, "1camara": CAMARA_1,
    "primeira": CAMARA_1, "1": CAMARA_1,
    "camara5": CAMARA_5, "camara_5": CAMARA_5, "5camara": CAMARA_5,
    "quinta": CAMARA_5, "5": CAMARA_5,
}
_TOKENS_TUDO = {"tjmg", "tj", "tudo", "todos", "geral"}


def _parse_escopo(escopo: str) -> tuple[list[str], list[str], list[str]]:
    """Traduz o texto de escopo em (magistrados, orgaosJulgadores, avisos).

    Tokens separados por '+', ',', espaço, '/' ou '|'. Tokens de magistrado
    entram em magistrados[] (OR entre si); tokens de câmara em orgaosJulgadores[]
    (OR entre si). Magistrado + câmara juntos = interseção (AND) — ex.: 'marcelo+camara5'
    = votos do Marcelo apenas na 5ª Câmara. O token 'tjmg' remove todos os filtros.
    """
    import re
    magistrados: list[str] = []
    orgaos: list[str] = []
    avisos: list[str] = []
    for raw in re.split(r"[+,/|\s]+", (escopo or "").strip().lower()):
        if not raw:
            continue
        if raw in _TOKENS_TUDO:
            return [], [], []  # todo o TJ: ignora quaisquer outros filtros
        if raw in _TOKENS_MAGISTRADO:
            nome = _TOKENS_MAGISTRADO[raw]
            if nome not in magistrados:
                magistrados.append(nome)
        elif raw in _TOKENS_CAMARA:
            nome = _TOKENS_CAMARA[raw]
            if nome not in orgaos:
                orgaos.append(nome)
        else:
            avisos.append(raw)
    return magistrados, orgaos, avisos


def _resolver_classes(classe: str) -> list[str]:
    c = (classe or "").strip().lower()
    if c in ("", "todas", "todos", "all"):
        return []
    if c in ("apelacao", "apelação", "apelacao/reexame", "apelação/reexame"):
        return CLASSES_APELACAO
    # valor(es) exato(s) informados pelo usuário
    return [p.strip() for p in classe.split(",") if p.strip()]


def _iso(data: str) -> str:
    """Aceita dd/MM/yyyy ou yyyy-MM-dd; devolve yyyy-MM-dd."""
    data = (data or "").strip()
    if not data:
        return ""
    if "/" in data:
        p = data.split("/")
        if len(p) == 3:
            d, m, y = p
            return f"{y}-{int(m):02d}-{int(d):02d}"
    return data


def _sort_param(ordenar: str) -> str:
    o = (ordenar or "").strip().lower()
    if o in ("recentes", "recente", "novos", "desc"):
        return "julgamento_data,desc"
    if o in ("antigos", "antigo", "asc"):
        return "julgamento_data,asc"
    return ""  # relevância (padrão da API)


def _tipo_texto(tipo: str) -> str:
    t = (tipo or "").strip().lower()
    if t in ("inteiro_teor", "inteiro teor", "integra", "íntegra", "texto"):
        return "INTEIRO_TEOR"
    return "EMENTA"


def _parse_numeros_processo(numero_processo: str) -> list[str]:
    """Aceita um ou mais números (separados por vírgula/espaço), com ou sem
    pontuação — a API quer só dígitos, na numeração interna do Tribunal
    (13 dígitos p/ processo físico de 1ª instância, ou 17 p/ 2ª instância)."""
    import re
    brutos = re.split(r"[,\s]+", (numero_processo or "").strip())
    return [re.sub(r"\D", "", n) for n in brutos if re.sub(r"\D", "", n)]


def _build_filtro(
    palavras: str, magistrados: list[str], orgaos: list[str],
    classes: list[str], tipo_texto: str,
    data_inicio: str, data_fim: str,
    numeros_processo: list[str] | None = None,
) -> dict:
    corpo: dict = {}
    if magistrados:
        corpo["magistrados"] = magistrados
    if orgaos:
        corpo["orgaosJulgadores"] = orgaos
    if classes:
        corpo["classes"] = classes
    if numeros_processo:
        corpo["numerosProcessos"] = numeros_processo
    if palavras.strip():
        corpo["texto"] = palavras.strip()
        corpo["tipoTexto"] = tipo_texto
    di, df = _iso(data_inicio), _iso(data_fim)
    if di or df:
        corpo["datasJulgamento"] = [{
            "inicio": di or "2000-01-01",
            "fim":    df or date.today().isoformat(),
        }]
    return corpo


# ────────────────────────────── Ferramentas MCP ──────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True, idempotentHint=True))
async def buscar_jurisprudencia_tjmg(
    palavras: str = "",
    escopo: str = "marcelo,manoel",
    tipo_texto: str = "ementa",
    classe: str = "apelacao",
    data_inicio: str = "",
    data_fim: str = "",
    n_resultados: int = 20,
    ordenar: str = "recentes",
    pagina: int = 0,
    numero_processo: str = "",
) -> str:
    """
    Busca jurisprudência na NOVA base do TJMG (consulta-jurisprudencia.tjmg.jus.br).
    Sem CAPTCHA — resposta rápida e estruturada. A ementa já vem no resultado.

    Args:
        numero_processo: número(s) de processo, na numeração interna do Tribunal
            (13 dígitos p/ 1ª instância ou 17 p/ 2ª instância — ex.: "10000250644614001"),
            com ou sem pontuação. Vários números separados por vírgula ou espaço.
            NÃO é o número CNJ completo nem o `id_documento`. Quando informado,
            localiza o(s) processo(s) diretamente — ignora `palavras` se ela vier vazia.
        palavras: Termos de busca. O campo aceita OPERADORES (motor Elasticsearch) —
            use-os para precisão:
              "frase exata"          → aspas para expressão literal. Ex.: "responsabilidade objetiva"
              termo1 + termo2        → E (AND): exige os dois. Ex.: honorários + sucumbência
              termo1 | termo2        → OU (OR): pelo menos um. Ex.: ipsemg | ipsm
              -termo                 → NÃO (NOT): exclui. Ex.: prescrição -penal
              ( ... )                → agrupa. Ex.: ( "dano moral" | "dano material" ) + município
              radical*               → curinga/truncamento. Ex.: indeniz* (indenização, indenizar…)
            Combine à vontade. Ex.: ( "improbidade administrativa" | "ato ímprobo" ) + dolo -culposo
        escopo: Um ou mais escopos, combináveis com '+' ou ',':
            "marcelo"  → Des. Marcelo Paulo Salgado (substituto atual da 1ª Câmara)
            "manoel"   → Des. Manoel Dos Reis Morais (titular, hoje na presidência)
            "camara1"  → 1ª Câmara Cível
            "camara5"  → 5ª Câmara Cível (câmara legada do Des. Marcelo)
            "tjmg"     → todo o TJMG, sem filtro
            Combinações: "marcelo,manoel" (os dois relatores), "camara1,camara5"
            (as duas câmaras), "marcelo+camara5" (só votos do Marcelo na 5ª Câmara).
            Padrão: "marcelo,manoel".
        tipo_texto: onde buscar as palavras — "ementa" (padrão) ou "inteiro_teor".
        classe: "apelacao" (padrão: Apelação + Reexame Necessário), "todas",
            ou classe(s) exata(s) separadas por vírgula (ex.: "Agravo De Instrumento").
        data_inicio: data inicial de julgamento dd/MM/yyyy ou yyyy-MM-dd (opcional).
        data_fim:    data final de julgamento (opcional).
        n_resultados: quantidade por página, máx. 50 (padrão 20).
        ordenar: "recentes" (padrão — julgamento mais novo primeiro, mais seguro
            para o estado atual da jurisprudência), "antigos" ou "relevancia"
            (score textual do Elasticsearch; sem critério jurídico).
        pagina: número da página (0 = primeira).

    Returns:
        Lista numerada com processo, câmara, relator, data, classe e ementa.
        Cada item traz `id_documento` e `publicacao` — use-os em
        obter_inteiro_teor_tjmg para ler o voto completo.
    """
    n_resultados = max(1, min(n_resultados, 50))
    pagina = max(0, pagina)

    numeros_processo = _parse_numeros_processo(numero_processo)
    # Busca por número é uma localização exata — não faz sentido restringir
    # por relator/câmara/classe default junto (o processo pode ser de
    # qualquer um deles); ignora esses filtros quando o número é informado.
    if numeros_processo:
        magistrados, orgaos, avisos = [], [], []
        classes: list[str] = []
    else:
        magistrados, orgaos, avisos = _parse_escopo(escopo)
        classes = _resolver_classes(classe)
    corpo = _build_filtro(
        palavras, magistrados, orgaos, classes,
        _tipo_texto(tipo_texto), data_inicio, data_fim,
        numeros_processo=numeros_processo,
    )

    params = {"size": str(n_resultados), "page": str(pagina)}
    sort = _sort_param(ordenar)
    if sort:
        params["sort"] = sort

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(FILTER_URL, params=params, json=corpo, headers=HEADERS)
    except httpx.TimeoutException:
        return "Tempo limite excedido ao consultar a API do TJMG."
    except Exception as e:
        return f"Erro inesperado ao consultar a API do TJMG: {e}"

    if r.status_code != 200:
        return (
            f"A API do TJMG retornou HTTP {r.status_code}. "
            f"Detalhe: {r.text[:300]}"
        )

    try:
        dados = r.json()
    except Exception:
        return f"Resposta inesperada da API do TJMG: {r.text[:300]}"

    return _formatar_resultados(dados, palavras, escopo, magistrados, orgaos, avisos, pagina, n_resultados)


def _formatar_resultados(dados, palavras, escopo, magistrados, orgaos, avisos, pagina, n_resultados) -> str:
    itens = dados.get("jurisprudencias", []) or []
    total = dados.get("totalRecords", 0)

    escopo_desc = escopo
    if not magistrados and not orgaos:
        escopo_desc = "todo o TJMG"

    cab = [
        f'Jurisprudência TJMG (nova base) — "{palavras}"  [{escopo_desc}]',
        f"Total encontrado: {total}  |  Página {pagina + 1} (exibindo {len(itens)})",
        "=" * 66,
    ]
    if avisos:
        cab.insert(0, f"⚠️ Tokens de escopo não reconhecidos e ignorados: {', '.join(avisos)}")
    if not itens:
        return "\n".join(cab) + "\n\nNenhum resultado encontrado."

    blocos = []
    for i, j in enumerate(itens, start=1 + pagina * n_resultados):
        # highlight tem o trecho com os termos destacados; cai para ementa completa
        ementa = ""
        hl = (j.get("highlights") or {}).get("ementa")
        if hl:
            ementa = " ".join(hl)
        else:
            ementa = j.get("ementa", "")
        ementa = ementa.replace("<b>", "").replace("</b>", "").replace("\n", " ").replace("\t", " ").strip()
        if len(ementa) > 900:
            ementa = ementa[:900] + "…"

        linhas = [
            f"**[{i}] {j.get('classe','')} nº {j.get('numeroProcessoCnj') or j.get('numeroProcessoTj','')}**",
            f"Órgão: {j.get('orgaoJulgador','')}  |  Relator(a): {j.get('magistrado','')}",
            f"Julgamento: {j.get('julgamentoData','')}  |  Publicação: {j.get('publicacaoData','')}"
            f"  |  Tipo: {j.get('tipoDocumento','')}",
        ]
        assuntos = j.get("assuntos")
        if assuntos:
            linhas.append(f"Assuntos: {', '.join(assuntos)}")
        linhas.append(f"Ementa: {ementa}")
        linhas.append(
            f"» id_documento: {j.get('documentoId','')}  |  publicacao: {j.get('publicacaoData','')}"
        )
        blocos.append("\n".join(linhas))

    rodape = ""
    if total > (pagina + 1) * n_resultados:
        rodape = f"\n\n(Há mais resultados — chame novamente com pagina={pagina + 1}.)"

    return "\n".join(cab) + "\n\n" + "\n\n---\n\n".join(blocos) + rodape


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True, idempotentHint=True))
async def obter_inteiro_teor_tjmg(documento_id: str, publicacao_data: str = "") -> str:
    """
    Obtém o inteiro teor (voto completo) de um acórdão do TJMG na nova base.
    Use os campos `id_documento` e `publicacao` retornados por buscar_jurisprudencia_tjmg.

    Args:
        documento_id: valor de `id_documento` do resultado desejado.
        publicacao_data: valor de `publicacao` (dd/MM/yyyy) do mesmo resultado.

    Returns:
        Texto completo do acórdão: ementa, relatório, voto e dispositivo.
    """
    if not documento_id:
        return "Informe o documento_id (campo `id_documento` do resultado da busca)."

    corpo: dict = {"documentoId": documento_id}
    iso = _iso(publicacao_data)
    if iso:
        corpo["datasPublicacao"] = [{"inicio": iso, "fim": iso}]

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(DOC_URL, json=corpo, headers=HEADERS)
    except httpx.TimeoutException:
        return "Tempo limite excedido ao obter o inteiro teor no TJMG."
    except Exception as e:
        return f"Erro ao obter inteiro teor: {e}"

    if r.status_code != 200:
        return (
            f"A API do TJMG retornou HTTP {r.status_code} para o inteiro teor. "
            f"Confira id_documento e publicacao_data. Detalhe: {r.text[:200]}"
        )

    return _html_para_texto(r.text, documento_id)


def _html_para_texto(html: str, documento_id: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    texto = soup.get_text(separator="\n", strip=True)
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    limpo = "\n".join(linhas)
    cab = f"Inteiro teor — documento {documento_id}\n{'=' * 66}\n\n"
    if len(limpo) < 100:
        return cab + "Não foi possível extrair o texto do acórdão."
    return cab + limpo


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True, idempotentHint=True))
async def listar_valores_filtro(campo: str, contem: str = "") -> str:
    """
    Lista valores válidos de um filtro na nova base do TJMG (autocomplete).
    Útil para descobrir nomes exatos de magistrados, câmaras, classes, assuntos e
    comarcas — permitindo filtrar com precisão e economizar tokens na pesquisa.

    Args:
        campo: um de "magistrados", "orgaosJulgadores" (câmaras), "classes",
            "assuntos" ou "comarcas".
        contem: filtra localmente os valores que contêm este texto (opcional).

    Returns:
        Lista de valores com a quantidade REAL de documentos de cada um.
    """
    validos = {"magistrados", "orgaosJulgadores", "classes", "assuntos", "comarcas"}
    if campo not in validos:
        return f'Campo inválido. Use um de: {", ".join(sorted(validos))}.'

    # Corpo vazio → a API devolve o domínio completo com a contagem real de cada
    # valor. NÃO enviar `texto` aqui: `texto` é busca de ementa/inteiro teor e
    # distorceria a contagem (limitaria aos documentos que contêm o termo). O
    # filtro por `contem` é aplicado localmente sobre os nomes retornados.
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(f"{DOMINIO_URL}/{campo}", json={}, headers=HEADERS)
    except Exception as e:
        return f"Erro ao consultar domínio '{campo}': {e}"

    if r.status_code != 200:
        return f"A API retornou HTTP {r.status_code} para o domínio '{campo}'."

    try:
        dominios = r.json().get("dominios", [])
    except Exception:
        return f"Resposta inesperada: {r.text[:200]}"

    alvo = contem.strip().lower()
    linhas = [
        f"  {d['dominio']}  ({d.get('quantidade','?')})"
        for d in dominios
        if not alvo or alvo in d["dominio"].lower()
    ]
    if not linhas:
        return f"Nenhum valor de '{campo}' contém \"{contem}\"."
    return f"Valores de '{campo}'" + (f' contendo "{contem}"' if contem else "") + ":\n" + "\n".join(linhas[:100])


# ─────────────────── Infra: /health, keep-alive, host patch ───────────────────

async def _keep_alive():
    """Ping /health a cada 10 min para evitar o spin-down do Render free tier."""
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        return
    ping_url = f"{base_url}/health"
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await client.get(ping_url)
            except Exception:
                pass
            await asyncio.sleep(600)


class _HealthASGI:
    """Middleware ASGI: responde /health e corrige o Host header para o FastMCP.

    O MCP SDK rejeita hosts externos com 421 (proteção anti-DNS-rebinding).
    Trocamos o Host para 'localhost' antes de entregar ao FastMCP.
    """

    def __init__(self, app):
        self.app = app
        self._health_body = b'{"status":"ok","server":"TJMG Jurisprud\xc3\xaancia v2"}'

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        if path in ("/", "/health"):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(self._health_body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": self._health_body})
            return

        if path == "/diag":
            result = await buscar_jurisprudencia_tjmg("responsabilidade civil estado", n_resultados=3)
            body = result.encode("utf-8", errors="replace")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        headers = [
            (b"host", b"localhost") if name.lower() == b"host" else (name, value)
            for name, value in scope.get("headers", [])
        ]
        await self.app({**scope, "headers": headers}, receive, send)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    mcp_app = mcp.streamable_http_app()
    app = _HealthASGI(mcp_app)

    async def _run():
        config = uvicorn.Config(app, host="0.0.0.0", port=port)
        server = uvicorn.Server(config)
        keep_alive_task = asyncio.create_task(_keep_alive())
        await server.serve()
        keep_alive_task.cancel()

    asyncio.run(_run())
