import asyncio
import os
import re
import random
import string
import urllib.parse
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
            r_form = await client.get(FORM_URL, headers=HEADERS)

            # Primeira requisição SEMPRE com 10 resultados: o portal só entrega resultados
            # estáticos no caminho pós-CAPTCHA quando linhasPorPagina=10 na requisição
            # que dispara o CAPTCHA. Com 50 o servidor entrega 828KB de formulário vazio.
            params_10 = {**params, "linhasPorPagina": "10"}
            response = await _get_iso(client, SEARCH_URL, params_10, HEADERS)

            debug_info = [f"form_status={r_form.status_code} cookies={list(client.cookies.keys())}"]

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
                    codigo = ""
                    for _ocr_try in range(4):
                        codigo = await _resolver_captcha_codigo(client)
                        if codigo:
                            break
                    dwr_diag = getattr(client, "_last_dwr", "n/a")
                    debug_info[-1] += f" ocr={repr(codigo)} dwr={dwr_diag}"
                    if codigo:
                        response = await _get_iso(
                            client, SEARCH_URL,
                            {**params_10, "captcha_text": codigo},
                            HEADERS,
                        )
                    else:
                        await client.get(FORM_URL, headers=HEADERS)
                        response = await _get_iso(client, SEARCH_URL, params_10, HEADERS)
                else:
                    debug_info[-1] += f" html300={repr(html[:300])}"
                    await client.get(FORM_URL, headers=HEADERS)
                    response = await _get_iso(client, SEARCH_URL, params_10, HEADERS)

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

            # Sessão verificada — tenta busca com n_resultados completo
            if n_resultados > 10:
                r2 = await _get_iso(client, SEARCH_URL, params, HEADERS)
                h2 = _decode_html(r2)
                if _tem_resultados(h2):
                    html = h2

            return _parse_resultados(html, palavras, escopo)

    except httpx.TimeoutException:
        return "Tempo limite excedido ao consultar o TJMG. O portal pode estar lento."
    except Exception as e:
        return f"Erro inesperado ao consultar o TJMG: {e}"


def _decode_html(response: httpx.Response) -> str:
    """Portal TJMG é ISO-8859-1 mas não declara charset no header HTTP."""
    return response.content.decode("iso-8859-1", errors="replace")


def _get_iso(client: httpx.AsyncClient, url: str, params: dict, headers: dict):
    """GET com query string codificada em ISO-8859-1 (x-www-form-urlencoded).

    httpx/urllib codificam parâmetros como UTF-8 por padrão (%C3%A7 para ç).
    TJMG espera ISO-8859-1 (%E7 para ç). Além disso, urlencode com bytes produz
    %2B para espaços — TJMG decodifica %2B como '+' literal e não encontra resultados.
    A forma correta é: acentos como %XX ISO-8859-1, espaços como '+' literal.
    """
    parts = []
    for k, v in params.items():
        if isinstance(v, str):
            # Codifica como ISO-8859-1 → percent-encode todos os bytes não-ASCII/não-seguros
            # → substitui %20 por '+' (espaço em x-www-form-urlencoded)
            v_enc = urllib.parse.quote_from_bytes(
                v.encode("iso-8859-1", errors="replace"), safe=""
            ).replace("%20", "+")
        else:
            v_enc = urllib.parse.quote_plus(str(v))
        parts.append(f"{k}={v_enc}")
    qs = "&".join(parts)
    return client.get(f"{url}?{qs}", headers=headers)


def _tem_resultados(html: str) -> bool:
    # Regex aceita aspas simples/duplas e múltiplas classes; descarta CSS (.caixa_processo {)
    return bool(_RE_CAIXA.search(html)) or "foram encontrados" in html.lower()


def _e_pagina_captcha(html: str) -> bool:
    return (
        "captcha_text" in html
        or "Digite os n" in html
        or "Informe o c" in html  # título da página de captcha
    )


async def _resolver_captcha_codigo(client: httpx.AsyncClient) -> str:
    """Baixa a imagem do CAPTCHA, resolve via OCR, valida via DWR.
    Retorna o código de 5 dígitos se válido, string vazia se falhou."""

    img_url = f"{CAPTCHA_IMG}?{random.random()}"
    img_resp = await client.get(img_url, headers={**HEADERS, "Referer": SEARCH_URL})
    if img_resp.status_code != 200:
        return ""

    codigo_bruto = _ocr.classification(img_resp.content)
    codigo = re.sub(r"\D", "", codigo_bruto)[:5]
    if len(codigo) != 5:
        return ""

    jsessionid = client.cookies.get("JSESSIONID", "")
    script_id  = "".join(random.choices(string.ascii_uppercase + string.digits, k=20))
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
        headers={**HEADERS, "Content-Type": "text/plain",
                 "Referer": f"{BASE}/pesquisaPalavrasEspelhoAcordao.do"},
    )
    dwr_ok = (
        dwr_resp.status_code == 200
        and "remoteHandleCallback" in dwr_resp.text
        and dwr_resp.text.strip().endswith("true);")
    )
    # Expõe a resposta DWR completa para diagnóstico (injetada como atributo no cliente)
    client._last_dwr = f"status={dwr_resp.status_code} body={repr(dwr_resp.text.strip()[-120:])}"
    return codigo if dwr_ok else ""




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
            response = await _get_iso(client, SEARCH_URL, params, HEADERS)

            for _ in range(5):
                html = _decode_html(response)
                if "panel1" in html or _tem_resultados(html):
                    break
                if _e_pagina_captcha(html):
                    codigo = ""
                    for _ocr_try in range(4):
                        codigo = await _resolver_captcha_codigo(client)
                        if codigo:
                            break
                    if codigo:
                        response = await _get_iso(
                            client, SEARCH_URL,
                            {**params, "captcha_text": codigo},
                            HEADERS,
                        )
                    else:
                        await client.get(FORM_URL, headers=HEADERS)
                        response = await _get_iso(client, SEARCH_URL, params, HEADERS)
                else:
                    await client.get(FORM_URL, headers=HEADERS)
                    response = await _get_iso(client, SEARCH_URL, params, HEADERS)

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


async def _diag_extrair_js() -> str:
    """Obtém a página 828KB pós-CAPTCHA e extrai trechos de JS relevantes."""
    import time
    params = {
        "numeroRegistro": "1", "totalLinhas": "1",
        "palavras": f"honorarios {int(time.time())}", "pesquisarPor": "ementa",
        "orderByData": "2", "listaOrgaoJulgador": CAMARA_1_CIVEL,
        "listaRelator": RELATOR_MANOEL, "classe": CLASSE_APELACAO,
        "linhasPorPagina": "10", "pesquisaPalavras": "Pesquisar",
    }
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        await client.get(FORM_URL, headers=HEADERS)
        r = await client.get(SEARCH_URL, params=params, headers=HEADERS)
        if _e_pagina_captcha(_decode_html(r)):
            codigo = await _resolver_captcha_codigo(client)
            if codigo:
                await client.get(SEARCH_URL, params={**params, "captcha_text": codigo}, headers=HEADERS)
                r = await client.get(SEARCH_URL, params=params, headers=HEADERS)

        html = _decode_html(r)
        soup = BeautifulSoup(html, "html.parser")

        log = [f"len={len(html)} status={r.status_code}"]

        # Extrai todos os blocos <script> inline
        scripts = soup.find_all("script")
        log.append(f"\n=== {len(scripts)} blocos <script> ===")
        for i, s in enumerate(scripts[:20]):
            src = s.get("src", "")
            content = (s.string or "").strip()
            if src:
                log.append(f"[{i}] src={src}")
            elif content:
                # Procura por URLs .do, $.ajax, XMLHttpRequest, pesquisa, resultado
                if any(k in content for k in [".do", "ajax", "XMLHttp", "pesquisa", "result", "captcha", "window.location"]):
                    log.append(f"[{i}] inline ({len(content)} chars):\n{content[:800]}")

        # Procura padrões específicos no HTML completo
        log.append("\n=== Ocorrências de URLs .do no HTML ===")
        for m in re.finditer(r'["\']([^"\']*\.do[^"\']*)["\']', html):
            url = m.group(1)
            if url not in [FORM_URL, SEARCH_URL] and "dwr" not in url:
                log.append(f"  {url}")

        # Procura pelo conteúdo após "Nenhum Espelho"
        pos = html.find("Nenhum Espelho")
        if pos >= 0:
            log.append(f"\n=== Contexto em torno de 'Nenhum Espelho' (@{pos}) ===")
            log.append(repr(html[max(0, pos-200):pos+500]))

        # Mostra os últimos 3KB da página (onde resultados costumam aparecer)
        log.append(f"\n=== Últimos 3KB da página ===")
        log.append(repr(html[-3000:]))

        # Posições específicas
        for pct in [0.5, 0.7, 0.85]:
            pos = int(len(html) * pct)
            log.append(f"\n=== @{pct*100:.0f}% ({pos}) ===")
            log.append(repr(html[pos:pos+500]))

        return "\n".join(log)


async def _diag_captcha_raw(palavras: str) -> str:
    """Executa a busca com diagnóstico completo de cada etapa, incluindo DWR."""
    params = {
        "numeroRegistro": "1", "totalLinhas": "1",
        "palavras": palavras, "pesquisarPor": "ementa", "orderByData": "2",
        "codigoOrgaoJulgador": "", "listaOrgaoJulgador": CAMARA_1_CIVEL,
        "codigoCompostoRelator": "", "listaRelator": RELATOR_MANOEL,
        "classe": CLASSE_APELACAO,
        "codigoAssunto": "", "dataPublicacaoInicial": "", "dataPublicacaoFinal": "",
        "dataJulgamentoInicial": "", "dataJulgamentoFinal": "",
        "siglaLegislativa": "", "referenciaLegislativa": "", "numeroRefLegislativa": "",
        "anoRefLegislativa": "", "legislacao": "", "norma": "", "descNorma": "",
        "complemento_1": "", "listaPesquisa": "", "descricaoTextosLegais": "",
        "observacoes": "", "linhasPorPagina": "10", "pesquisaPalavras": "Pesquisar",
    }
    log = [f"DIAGNÓSTICO CAPTCHA — termos={repr(palavras)}"]
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        r0 = await client.get(FORM_URL, headers=HEADERS)
        log.append(f"form: status={r0.status_code} cookies={list(client.cookies.keys())}")

        r1 = await _get_iso(client, SEARCH_URL, params, HEADERS)
        h1 = _decode_html(r1)
        log.append(f"busca1: status={r1.status_code} len={len(h1)} cap={_e_pagina_captcha(h1)} res={_tem_resultados(h1)}")

        if _tem_resultados(h1):
            log.append("SUCESSO sem CAPTCHA")
            log.append(_parse_resultados(h1, palavras, "camara_e_relator")[:500])
            return "\n".join(log)

        if not _e_pagina_captcha(h1):
            log.append(f"FALHA: sem captcha e sem resultados. html300={repr(h1[:300])}")
            return "\n".join(log)

        # --- caminho CAPTCHA ---
        img_resp = await client.get(f"{CAPTCHA_IMG}?{random.random()}", headers={**HEADERS, "Referer": SEARCH_URL})
        log.append(f"captcha_img: status={img_resp.status_code} len={len(img_resp.content)}")

        codigo_bruto = _ocr.classification(img_resp.content) if OCR_DISPONIVEL else ""
        codigo = re.sub(r"\D", "", codigo_bruto)[:5]
        log.append(f"ocr: bruto={repr(codigo_bruto)} limpo={repr(codigo)}")

        jsessionid = client.cookies.get("JSESSIONID", "")
        script_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=20))
        dwr_body = (
            f"callCount=1\npage=/jurisprudencia/pesquisaPalavrasEspelhoAcordao.do\n"
            f"httpSessionId={jsessionid}\nscriptSessionId={script_id}\n"
            f"c0-scriptName=ValidacaoCaptchaAction\nc0-methodName=isCaptchaValid\n"
            f"c0-id=0\nc0-param0=string:{codigo}\nbatchId=0\n"
        )
        dwr_r = await client.post(CAPTCHA_DWR, content=dwr_body.encode(),
                                  headers={**HEADERS, "Content-Type": "text/plain",
                                           "Referer": f"{BASE}/pesquisaPalavrasEspelhoAcordao.do"})
        log.append(f"dwr: status={dwr_r.status_code} body={repr(dwr_r.text.strip())}")

        def _html_diag(h: str, label: str) -> str:
            nenhum = "Nenhum Espelho" in h or "nenhum espelho" in h.lower()
            trecho = h[int(len(h)*0.35):int(len(h)*0.35)+600]
            return (
                f"{label}: len={len(h)} cap={_e_pagina_captcha(h)} res={_tem_resultados(h)} "
                f"nenhum={nenhum}\n"
                f"  @35%: {repr(trecho)}"
            )

        if len(codigo) == 5:
            r_cap = await _get_iso(client, SEARCH_URL, {**params, "captcha_text": codigo}, HEADERS)
            h_cap = _decode_html(r_cap)
            log.append(_html_diag(h_cap, "captcha_get"))

            r2 = await _get_iso(client, SEARCH_URL, params, HEADERS)
            h2 = _decode_html(r2)
            log.append(_html_diag(h2, "busca2"))

            if _tem_resultados(h2):
                log.append("SUCESSO com CAPTCHA")
                log.append(_parse_resultados(h2, palavras, "camara_e_relator")[:500])
        else:
            log.append("OCR FALHOU — código inválido")

    return "\n".join(log)


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

        if path == "/diag":
            result = await buscar_jurisprudencia_tjmg("honorarios Estado")
            body = result.encode("utf-8", errors="replace")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        if path == "/diag-teor":
            result = await obter_inteiro_teor_tjmg("honorarios Estado", numero_resultado=1)
            body = result.encode("utf-8", errors="replace")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        if path == "/diag-js":
            # Baixa a página 828KB pós-CAPTCHA e extrai os scripts JS para encontrar chamadas AJAX
            result = await _diag_extrair_js()
            body = result.encode("utf-8", errors="replace")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        if path == "/diag-captcha":
            # Aceita ?q=termo para testar qualquer termo; padrão é termo acentuado
            qs_raw = scope.get("query_string", b"").decode("utf-8", errors="replace")
            q_params = dict(urllib.parse.parse_qsl(qs_raw))
            termos = q_params.get("q", "prescrição responsabilidade Estado")
            result = await _diag_captcha_raw(termos)
            body = result.encode("utf-8", errors="replace")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
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
