import asyncio
import os
import re
import random
import string
import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

try:
    import ddddocr
    _ocr = ddddocr.DdddOcr(show_ad=False)
    OCR_DISPONIVEL = True
except Exception:
    OCR_DISPONIVEL = False

_RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "mcp-tjmg.onrender.com")

mcp = FastMCP(
    "TJMG Jurisprudência",
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

BASE          = "https://www5.tjmg.jus.br/jurisprudencia"
FORM_URL      = f"{BASE}/formEspelhoAcordao.do"
SEARCH_URL    = f"{BASE}/pesquisaPalavrasEspelhoAcordao.do"
CAPTCHA_IMG   = f"{BASE}/captcha.svl"
CAPTCHA_DWR   = f"{BASE}/dwr/call/plaincall/ValidacaoCaptchaAction.isCaptchaValid.dwr"

CAMARA_1_CIVEL  = "1-1"
RELATOR_MANOEL  = "0-19836"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": FORM_URL,
}


@mcp.tool()
async def buscar_jurisprudencia_tjmg(
    palavras: str,
    escopo: str = "camara_e_relator",
    data_inicio: str = "",
    data_fim: str = "",
    n_resultados: int = 10,
) -> str:
    """
    Busca jurisprudência no portal do TJMG. Resolve o CAPTCHA automaticamente.

    Args:
        palavras: Termos de busca. Ex: "responsabilidade civil estado omissão serviço público"
        escopo: Filtro de busca:
            "camara_e_relator" → 1ª Câmara Cível + Des. Manoel dos Reis Morais (padrão)
            "relator"          → apenas Des. Manoel dos Reis Morais (qualquer câmara)
            "camara"           → apenas 1ª Câmara Cível (qualquer relator)
            "tjmg"             → todo o TJMG sem filtro
        data_inicio: Data inicial de julgamento dd/MM/yyyy (opcional)
        data_fim:    Data final de julgamento dd/MM/yyyy (opcional)
        n_resultados: Quantidade de resultados, máximo 50 (padrão 10)

    Returns:
        Lista de decisões com número do processo, relator, data e ementa.
    """
    if not OCR_DISPONIVEL:
        return (
            "Erro de configuração: biblioteca ddddocr não instalada no servidor. "
            "Verifique os logs do Render."
        )

    n_resultados = max(1, min(n_resultados, 50))

    params = {
        "numeroRegistro":        "1",
        "totalLinhas":           "1",
        "palavras":              palavras,
        "pesquisarPor":          "ementa",
        "orderByData":           "2",
        "codigoOrgaoJulgador":   "",
        "listaOrgaoJulgador":    "",
        "codigoCompostoRelator": "",
        "listaRelator":          "",
        "classe":                "",
        "codigoAssunto":         "",
        "dataPublicacaoInicial": "",
        "dataPublicacaoFinal":   "",
        "dataJulgamentoInicial": data_inicio,
        "dataJulgamentoFinal":   data_fim,
        "siglaLegislativa":      "",
        "referenciaLegislativa": "",
        "numeroRefLegislativa":  "",
        "anoRefLegislativa":     "",
        "legislacao":            "",
        "norma":                 "",
        "descNorma":             "",
        "complemento_1":         "",
        "listaPesquisa":         "",
        "descricaoTextosLegais": "",
        "observacoes":           "",
        "linhasPorPagina":       str(n_resultados),
        "pesquisaPalavras":      "Pesquisar",
    }

    if escopo in ("camara", "camara_e_relator"):
        params["listaOrgaoJulgador"] = CAMARA_1_CIVEL
    if escopo in ("relator", "camara_e_relator"):
        params["listaRelator"] = RELATOR_MANOEL

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            # Passo 1: carrega formulário para estabelecer sessão/cookies
            await client.get(FORM_URL, headers=HEADERS)

            # Passo 2: primeira tentativa de busca
            response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

            # Passo 3: resolve CAPTCHA se necessário (até 4 tentativas)
            for tentativa in range(4):
                if not _e_pagina_captcha(response.text):
                    break
                sucesso = await _resolver_captcha(client)
                if not sucesso:
                    # CAPTCHA mal lido — recarrega sessão e tenta de novo
                    await client.get(FORM_URL, headers=HEADERS)
                response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

            if _e_pagina_captcha(response.text):
                return "Não foi possível resolver o CAPTCHA após 4 tentativas. Tente novamente em alguns segundos."

            return _parse_resultados(response.text, palavras, escopo)

    except httpx.TimeoutException:
        return "Tempo limite excedido ao consultar o TJMG. O portal pode estar lento."
    except Exception as e:
        return f"Erro inesperado ao consultar o TJMG: {e}"


def _e_pagina_captcha(html: str) -> bool:
    return "captcha_text" in html or "Digite os n" in html


async def _resolver_captcha(client: httpx.AsyncClient) -> bool:
    """Baixa a imagem do CAPTCHA, resolve via OCR e valida via DWR."""

    # Baixa imagem (cache-buster para garantir imagem fresca)
    img_url = f"{CAPTCHA_IMG}?{random.random()}"
    img_resp = await client.get(
        img_url,
        headers={**HEADERS, "Referer": SEARCH_URL},
    )
    if img_resp.status_code != 200:
        return False

    # OCR — extrai só os dígitos
    codigo_bruto = _ocr.classification(img_resp.content)
    codigo = re.sub(r"\D", "", codigo_bruto)[:5]

    if len(codigo) != 5:
        return False

    # Valida via DWR (Direct Web Remoting)
    jsessionid  = client.cookies.get("JSESSIONID", "")
    script_id   = "".join(random.choices(string.ascii_uppercase + string.digits, k=20))

    dwr_body = (
        f"callCount=1\n"
        f"page=/jurisprudencia/pesquisaPalavrasEspelhoAcordao.do\n"
        f"httpSessionId={jsessionid}\n"
        f"scriptSessionId={script_id}\n"
        f"c0-scriptName=ValidacaoCaptchaAction\n"
        f"c0-methodName=isCaptchaValid\n"
        f"c0-id=0\n"
        f"c0-param0=string:{codigo}\n"
        f"batchId=0\n"
    )

    dwr_resp = await client.post(
        CAPTCHA_DWR,
        content=dwr_body.encode("utf-8"),
        headers={
            **HEADERS,
            "Content-Type": "text/plain",
            "Referer":      f"{BASE}/pesquisaPalavrasEspelhoAcordao.do",
        },
    )

    # Resposta DWR: ...dwr.engine._remoteHandleCallback('0','0',true);
    return (
        dwr_resp.status_code == 200
        and "remoteHandleCallback" in dwr_resp.text
        and dwr_resp.text.strip().endswith("true);")
    )


def _parse_resultados(html: str, palavras: str, escopo: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    cabecalho = f'Jurisprudência TJMG — "{palavras}" [{escopo}]\n{"=" * 60}\n'

    decisoes = _extrair_decisoes(soup)
    if not decisoes:
        texto_limpo = soup.get_text(separator="\n", strip=True)
        return cabecalho + texto_limpo[:3000] or "Nenhum resultado encontrado."

    return cabecalho + "\n\n---\n\n".join(decisoes)


def _extrair_decisoes(soup: BeautifulSoup) -> list[str]:
    decisoes: list[str] = []
    bloco: list[str] = []

    padrao_processo = re.compile(r"\d+\.\d{4}\.\d{2,3}\.\d{6}-\d/\d{3}")

    for cel in soup.find_all("td"):
        texto = cel.get_text(separator=" ", strip=True)
        if not texto or len(texto) < 10:
            continue

        if padrao_processo.search(texto):
            if bloco:
                decisoes.append("\n".join(bloco))
            bloco = [f"**Processo:** {texto}"]

        elif bloco and any(k in texto.upper() for k in ("RELATOR", "RELATORA")):
            bloco.append(f"**{texto}**")

        elif bloco and len(texto) > 40:
            bloco.append(texto)

    if bloco:
        decisoes.append("\n".join(bloco))

    # Fallback: extrai por blocos de texto com número de processo
    if not decisoes:
        texto_total = soup.get_text(separator="\n", strip=True)
        linhas = [l.strip() for l in texto_total.splitlines() if len(l.strip()) > 10]
        bloco_fb: list[str] = []
        for linha in linhas:
            if padrao_processo.search(linha):
                if bloco_fb:
                    decisoes.append("\n".join(bloco_fb))
                bloco_fb = [linha]
            elif bloco_fb:
                bloco_fb.append(linha)
        if bloco_fb:
            decisoes.append("\n".join(bloco_fb))

    return decisoes[:30]  # máximo 30 decisões por busca


async def _keep_alive():
    """Pings /health a cada 10 min para evitar o spin-down do Render free tier."""
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        return
    ping_url = f"{base_url}/health"
    await asyncio.sleep(60)  # aguarda 1 min antes do primeiro ping
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await client.get(ping_url)
            except Exception:
                pass
            await asyncio.sleep(600)  # 10 minutos


class _HealthASGI:
    """Middleware ASGI puro — responde /health e corrige Host header para o FastMCP.

    O MCP SDK rejeita hosts externos com 421 (proteção anti-DNS-rebinding).
    Patchamos o Host para 'localhost' antes de entregar ao FastMCP — a validação
    de segurança passa e o streaming SSE nunca é bufferizado.
    """

    def __init__(self, app):
        self.app = app
        self._health_body = b'{"status":"ok","server":"TJMG Jurisprud\xc3\xaancia MCP"}'

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

        # Patcha o Host para localhost antes de chegar na validação do FastMCP
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
