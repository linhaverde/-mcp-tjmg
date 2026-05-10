import os
import re
import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("TJMG Jurisprudência")

BASE = "https://www5.tjmg.jus.br/jurisprudencia"
FORM_URL = f"{BASE}/formEspelhoAcordao.do"
SEARCH_URL = f"{BASE}/pesquisaPalavrasEspelhoAcordao.do"

# Códigos fixos do gabinete
CAMARA_1_CIVEL = "1-1"
RELATOR_MANOEL = "0-19836"

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
    Busca jurisprudência no portal do TJMG.

    Args:
        palavras: Termos de busca. Ex: "responsabilidade civil estado omissão serviço público"
        escopo: Onde buscar:
            "camara_e_relator" → 1ª Câmara Cível + Des. Manoel dos Reis Morais (padrão)
            "relator"          → apenas decisões do Des. Manoel dos Reis Morais
            "camara"           → apenas 1ª Câmara Cível (qualquer relator)
            "tjmg"             → todo o TJMG sem filtro de câmara ou relator
        data_inicio: Data inicial de julgamento no formato dd/MM/yyyy (opcional)
        data_fim:    Data final de julgamento no formato dd/MM/yyyy (opcional)
        n_resultados: Quantidade de resultados desejados (máximo 50, padrão 10)

    Returns:
        Lista de decisões com número do processo, relator, data e ementa.
    """
    n_resultados = max(1, min(n_resultados, 50))

    params = {
        "numeroRegistro": "1",
        "totalLinhas": "1",
        "palavras": palavras,
        "pesquisarPor": "ementa",
        "orderByData": "2",
        "codigoOrgaoJulgador": "",
        "listaOrgaoJulgador": "",
        "codigoCompostoRelator": "",
        "listaRelator": "",
        "classe": "",
        "codigoAssunto": "",
        "dataPublicacaoInicial": "",
        "dataPublicacaoFinal": "",
        "dataJulgamentoInicial": data_inicio,
        "dataJulgamentoFinal": data_fim,
        "siglaLegislativa": "",
        "referenciaLegislativa": "",
        "numeroRefLegislativa": "",
        "anoRefLegislativa": "",
        "legislacao": "",
        "norma": "",
        "descNorma": "",
        "complemento_1": "",
        "listaPesquisa": "",
        "descricaoTextosLegais": "",
        "observacoes": "",
        "linhasPorPagina": str(n_resultados),
        "pesquisaPalavras": "Pesquisar",
    }

    if escopo in ("camara", "camara_e_relator"):
        params["listaOrgaoJulgador"] = CAMARA_1_CIVEL

    if escopo in ("relator", "camara_e_relator"):
        params["listaRelator"] = RELATOR_MANOEL

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Carrega a página do formulário para estabelecer sessão/cookies
            await client.get(FORM_URL, headers=HEADERS)
            # Executa a busca com os cookies de sessão ativos
            response = await client.get(SEARCH_URL, params=params, headers=HEADERS)

        if response.status_code != 200:
            return f"Erro HTTP {response.status_code} ao consultar o TJMG. Tente novamente."

        return _parse_results(response.text, palavras, escopo)

    except httpx.TimeoutException:
        return "Tempo limite excedido ao consultar o TJMG. O portal pode estar lento."
    except Exception as e:
        return f"Erro inesperado: {e}"


def _parse_results(html: str, palavras: str, escopo: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    header = f'Jurisprudência TJMG — "{palavras}" [{escopo}]\n{"=" * 60}\n'

    # Tenta extrair decisões de tabelas de resultado
    decisoes = _extrair_de_tabelas(soup)

    if not decisoes:
        # Fallback: extrai texto corrido e tenta identificar blocos de decisão
        decisoes = _extrair_texto_corrido(soup)

    if not decisoes:
        return header + "Nenhum resultado encontrado para os termos pesquisados."

    return header + "\n\n---\n\n".join(decisoes)


def _extrair_de_tabelas(soup: BeautifulSoup) -> list[str]:
    decisoes = []
    # Procura células com texto de ementa (padrão TJMG)
    celulas = soup.find_all("td")
    bloco_atual = []

    for cel in celulas:
        texto = cel.get_text(separator=" ", strip=True)
        if not texto or len(texto) < 20:
            continue

        # Padrão: linha com número de processo TJMG (ex: 1.0000.23.000000-0/001)
        if re.search(r"\d+\.\d{4}\.\d{2}\.\d{6}-\d/\d{3}", texto):
            if bloco_atual:
                decisoes.append("\n".join(bloco_atual))
                bloco_atual = []
            bloco_atual.append(f"**Processo:** {texto}")

        elif any(p in texto.upper() for p in ["RELATOR", "RELATORA"]):
            bloco_atual.append(f"**{texto}**")

        elif any(p in texto.upper() for p in ["EMENTA", "EMENT"]):
            bloco_atual.append(f"**Ementa:** {texto}")

        elif bloco_atual and len(texto) > 50:
            bloco_atual.append(texto)

    if bloco_atual:
        decisoes.append("\n".join(bloco_atual))

    return decisoes


def _extrair_texto_corrido(soup: BeautifulSoup) -> list[str]:
    texto_completo = soup.get_text(separator="\n", strip=True)
    linhas = [l.strip() for l in texto_completo.splitlines() if len(l.strip()) > 15]

    blocos = []
    bloco = []
    for linha in linhas:
        if re.search(r"\d+\.\d{4}\.\d{2}\.\d{6}-\d/\d{3}", linha):
            if bloco:
                blocos.append("\n".join(bloco))
                bloco = []
        bloco.append(linha)

    if bloco:
        blocos.append("\n".join(bloco))

    # Limita a 500 linhas no total para não estourar o contexto
    return blocos[:20] if blocos else ["\n".join(linhas[:80])]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
