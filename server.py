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

CAMARA_1_CIVEL    = "1-1"
RELATOR_MANOEL    = "0-19836"
CLASSE_APELACAO   = "8"   # Apelação Cível

_RE_CAIXA = re.compile(r'class=["\'][^"\']*caixa_processo')

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
    n_resultados: int = 50,
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
        n_resultados: Quantidade de resultados, máximo 100 (padrão 50)

    Returns:
        Lista numerada de decisões (ex: [1], [2]...) com processo, relator e ementa.
        Use obter_inteiro_teor_tjmg com os mesmos parâmetros e numero_resultado=N
        para ler o voto completo de um resultado específico.
    """
    if not OCR_DISPONIVEL:
        return (
            "Erro de configuração: biblioteca ddddocr não instalada no servidor. "
            "Verifique os logs do Render."
        )

    n_resultados = max(1, min(n_resultados, 100))

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
    params["classe"] = CLASSE_APELACAO

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            # Passo 1: carrega formulário para estabelecer sessão/cookies
            r_form = await client.get(FORM_URL, headers=HEADERS)

            # Passo 2: primeira tentativa de busca
            response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

            # Passo 3: obtém resultados — lida com CAPTCHA e redirect para formulário
            debug_info = [
                f"form_status={r_form.status_code} "
                f"cookies={list(client.cookies.keys())}"
            ]
            for tentativa in range(5):
                html = _decode_html(response)
                tem = _tem_resultados(html)
                cap = _e_pagina_captcha(html)
                debug_info.append(
                    f"[t{tentativa+1}] status={response.status_code} "
                    f"len={len(html)} resultados={tem} captcha={cap}"
                )
                if tem:
                    break
                if cap:
                    sucesso = await _resolver_captcha(client)
                    debug_info[-1] += f" ocr_ok={sucesso}"
                    if not sucesso:
                        await client.get(FORM_URL, headers=HEADERS)
                else:
                    # Página grande sem CAPTCHA: é o estado pós-CAPTCHA "formulário pré-preenchido"
                    # — a sessão já está verificada, basta repetir a busca SEM resetar a sessão.
                    # Página pequena: formulário vazio / erro real — aí sim recarrega a sessão.
                    if len(html) > 50000:
                        debug_info[-1] += " (post-captcha-form: retry-same-session)"
                    else:
                        debug_info[-1] += f" html300={repr(html[:300])}"
                        await client.get(FORM_URL, headers=HEADERS)
                response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

            html = _decode_html(response)
            if not _tem_resultados(html) and _e_pagina_captcha(html):
                diag = "\n".join(debug_info)
                return f"Não foi possível resolver o CAPTCHA.\n{diag}"
            if not _tem_resultados(html):
                diag = "\n".join(debug_info)
                return (
                    f'[DIAGNÓSTICO — "{palavras}" / {escopo}]\n'
                    f"{diag}\n"
                    f"HTML final (400 chars): {repr(html[:400])}"
                )

            return _parse_resultados(html, palavras, escopo)

    except httpx.TimeoutException:
        return "Tempo limite excedido ao consultar o TJMG. O portal pode estar lento."
    except Exception as e:
        return f"Erro inesperado ao consultar o TJMG: {e}"


def _decode_html(response: httpx.Response) -> str:
    """Portal TJMG é ISO-8859-1 mas não declara charset no header HTTP."""
    return response.content.decode("iso-8859-1", errors="replace")


def _tem_resultados(html: str) -> bool:
    # Regex aceita aspas simples/duplas e múltiplas classes; descarta CSS (.caixa_processo {)
    return bool(_RE_CAIXA.search(html)) or "foram encontrados" in html.lower()


def _e_pagina_captcha(html: str) -> bool:
    return (
        "captcha_text" in html
        or "Digite os n" in html
        or "Informe o c" in html  # título da página de captcha
    )


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


@mcp.tool()
async def obter_inteiro_teor_tjmg(
    palavras: str,
    numero_resultado: int = 1,
    escopo: str = "camara_e_relator",
    data_inicio: str = "",
    data_fim: str = "",
) -> str:
    """
    Obtém o inteiro teor (voto completo) de um acórdão específico do TJMG.
    Use após buscar_jurisprudencia_tjmg: passe os mesmos termos e o número do resultado desejado.

    Args:
        palavras: Mesmos termos usados na busca anterior.
        numero_resultado: Posição do resultado (1 = primeiro, 2 = segundo, etc.)
        escopo: Mesmo filtro da busca:
            "camara_e_relator" → 1ª Câmara Cível + Des. Manoel dos Reis Morais (padrão)
            "relator"          → apenas Des. Manoel dos Reis Morais
            "camara"           → apenas 1ª Câmara Cível
            "tjmg"             → todo o TJMG
        data_inicio: Data inicial dd/MM/yyyy (opcional)
        data_fim:    Data final dd/MM/yyyy (opcional)

    Returns:
        Texto completo do acórdão: ementa, relatório, voto e dispositivo.
    """
    if not OCR_DISPONIVEL:
        return "Erro de configuração: biblioteca ddddocr não instalada."

    numero_resultado = max(1, min(numero_resultado, 200))

    params = {
        "numeroRegistro":        str(numero_resultado),
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
        "linhasPorPagina":       "1",
        "paginaNumero":          str(numero_resultado),
        "pesquisaPalavras":      "Pesquisar",
    }

    if escopo in ("camara", "camara_e_relator"):
        params["listaOrgaoJulgador"] = CAMARA_1_CIVEL
    if escopo in ("relator", "camara_e_relator"):
        params["listaRelator"] = RELATOR_MANOEL
    params["classe"] = CLASSE_APELACAO

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            await client.get(FORM_URL, headers=HEADERS)
            response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

            for _ in range(5):
                html = _decode_html(response)
                if "panel1" in html or _tem_resultados(html):
                    break
                if _e_pagina_captcha(html):
                    sucesso = await _resolver_captcha(client)
                    if not sucesso:
                        await client.get(FORM_URL, headers=HEADERS)
                else:
                    await client.get(FORM_URL, headers=HEADERS)
                response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

            html = _decode_html(response)
            if _e_pagina_captcha(html):
                return "Não foi possível resolver o CAPTCHA. Tente novamente em alguns segundos."

            return _parse_inteiro_teor(html, palavras, numero_resultado)

    except httpx.TimeoutException:
        return "Tempo limite excedido ao consultar o TJMG."
    except Exception as e:
        return f"Erro ao obter inteiro teor: {e}"


def _parse_inteiro_teor(html: str, palavras: str, numero: int) -> str:
    soup = BeautifulSoup(html, "html.parser")

    cabecalho = f'Inteiro teor — "{palavras}" [resultado #{numero}]\n{"=" * 60}\n\n'

    # Estratégia 1: div#panel1 → filho direto com text-align:justify
    panel = soup.find("div", id="panel1")
    if panel:
        content_div = panel.find(
            "div", style=lambda s: s and "justify" in s.lower()
        )
        if content_div:
            return cabecalho + content_div.get_text(separator="\n", strip=True)
        texto_panel = panel.get_text(separator="\n", strip=True)
        if len(texto_panel) > 300:
            return cabecalho + texto_panel

    # Estratégia 2: qualquer div com text-align:justify
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    justify_divs = soup.find_all(
        "div", style=lambda s: s and "justify" in s.lower()
    )
    blocos = [d.get_text(separator="\n", strip=True) for d in justify_divs]
    blocos = [b for b in blocos if len(b) > 200]
    if blocos:
        return cabecalho + "\n\n---\n\n".join(blocos[:5])

    # Estratégia 3: texto completo da página
    texto = soup.get_text(separator="\n", strip=True)
    linhas = [l.strip() for l in texto.splitlines() if len(l.strip()) > 5]
    texto_limpo = "\n".join(linhas)
    if len(texto_limpo) > 200:
        return cabecalho + texto_limpo[:10000]
    return cabecalho + "Nenhum conteúdo encontrado."


def _extrair_decisoes(soup: BeautifulSoup) -> list[str]:
    decisoes: list[str] = []

    caixas = soup.find_all("div", class_="caixa_processo")
    for i, caixa in enumerate(caixas[:100]):
        num = i + 1

        # número do processo: div float:left dentro do link
        num_div = caixa.find("div", style=lambda s: s and "float: left" in s)
        numero = num_div.get_text(strip=True) if num_div else ""

        # tipo (Apelação Cível, Agravo etc.): texto do link antes do número
        link = caixa.find("a")
        tipo = ""
        if link:
            link_text = link.get_text(separator=" ", strip=True)
            m = re.search(r"Processo:\s+([\w\s\-/]+?)(?:\s+[\d.]+|$)", link_text)
            if m:
                tipo = m.group(1).strip()

        partes = [f"**[{num}] {numero}**" + (f" — {tipo}" if tipo else "")]

        # percorre irmãos após a caixa: tabela (relator), divs (data, ementa)
        cur = caixa.find_next_sibling()
        passos = 0
        while cur is not None and passos < 10:
            if hasattr(cur, "get") and cur.get("class") and "caixa_processo" in cur.get("class", []):
                break  # início do próximo resultado

            if hasattr(cur, "name"):
                if cur.name == "table":
                    td = cur.find("td")
                    if td and "Relator" in td.get_text():
                        partes.append(f"**{td.get_text(strip=True)}**")

                elif cur.name == "div":
                    texto = cur.get_text(separator=" ", strip=True)
                    style = cur.get("style", "")
                    classes = cur.get("class") or []

                    if "Data de Julgamento" in texto:
                        data = re.sub(r".*Data de Julgamento:\s*", "", texto).strip()
                        partes.append(f"Data: {data}")

                    elif "justify" in style and "corpo" in classes and len(texto) > 40:
                        ementa = re.sub(r"^\s*Ementa:\s*", "", texto).strip()
                        # remove highlight HTML artefacts já limpados pelo get_text
                        partes.append(f"Ementa: {ementa[:800]}")
                        break  # ementa é o último elemento do bloco

            cur = cur.find_next_sibling()
            passos += 1

        if numero:
            decisoes.append("\n".join(partes))

    return decisoes


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
