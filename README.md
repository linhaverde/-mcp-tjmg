# MCP TJMG v2 — Nova base de jurisprudência

Conector MCP para a **nova base unificada de jurisprudência do TJMG**
(`consulta-jurisprudencia.tjmg.jus.br`, no ar desde jun/2026), que expõe uma
**API REST pública, sem autenticação e sem CAPTCHA**:
`https://jurisprudencia-api.tjmg.jus.br`.

Substitui o conector legado (`../mcp-tjmg`, que raspava o portal antigo
`www5.tjmg.jus.br` com OCR de CAPTCHA). **O conector antigo continua intacto** —
este é uma implementação nova e paralela.

## Por que migrar

| | Legado (`mcp-tjmg`) | v2 (este) |
|---|---|---|
| CAPTCHA | Sim (OCR `ddddocr` + DWR) | **Não** |
| Resposta | HTML raspado (frágil) | **JSON estruturado** |
| Encoding | Hack ISO-8859-1 | UTF-8 |
| Ementa na busca | Não (2ª chamada) | **Sim, embutida** |
| Filtros | 1 câmara + 1 relator (IDs fixos) | **Múltiplas câmaras/relatores, assunto, comarca, data, classe** |
| Cold start Render | Lento (baixa `ddddocr`) | Rápido (3 libs leves) |

## Ferramentas MCP

### `buscar_jurisprudencia_tjmg`
Parâmetros principais:
- `palavras` — termos de busca. **Aceita operadores** (Elasticsearch): `"frase exata"`,
  `a + b` (E), `a | b` (OU), `-termo` (NÃO), `( ... )` (grupos), `radical*` (curinga).
  Ex.: `( "dano moral" | "dano material" ) + município -penal`.
- `escopo` — um ou mais, combináveis com `+` ou `,`:
  - `marcelo` → Des. **Marcelo Paulo Salgado** (substituto atual na 1ª Câmara)
  - `manoel` → Des. **Manoel Dos Reis Morais** (titular, hoje na presidência do TJ)
  - `camara1` → **1ª Câmara Cível**
  - `camara5` → **5ª Câmara Cível** (câmara legada do Des. Marcelo)
  - `tjmg` → todo o TJMG, sem filtro
  - Ex.: `marcelo,manoel` · `camara1,camara5` · `marcelo+camara5` (Marcelo só na 5ª).
- `tipo_texto` — `ementa` (padrão) ou `inteiro_teor`.
- `classe` — `apelacao` (padrão: Apelação + Reexame Necessário), `todas`, ou string exata.
- `data_inicio` / `data_fim` — `dd/MM/yyyy` ou `yyyy-MM-dd` (julgamento).
- `n_resultados` (≤50), `ordenar` (`relevancia`/`recentes`/`antigos`), `pagina`.

Cada resultado traz `id_documento` e `publicacao` para o inteiro teor.

### `obter_inteiro_teor_tjmg(documento_id, publicacao_data)`
Voto completo (ementa, relatório, voto, dispositivo). Use os campos `id_documento`
e `publicacao` retornados pela busca — **não precisa refazer a pesquisa** (era assim no legado).

### `listar_valores_filtro(campo, contem)`
Autocomplete de `magistrados`, `orgaosJulgadores`, `classes`, `assuntos`, `comarcas`.
Serve para achar nomes exatos e filtrar com precisão (menos ruído, menos tokens).

## Contrato da API (referência)

- **Busca**: `POST /jurisprudencias/filter?size=N&page=P&sort=julgamento_data,desc`
  Corpo: `{orgaosJulgadores[], magistrados[], classes[], assuntos[], comarcas[],
  datasJulgamento:[{inicio,fim}], datasPublicacao:[], texto, tipoTexto:"EMENTA"|"INTEIRO_TEOR"}`.
  Filtros são arrays (OR dentro do array; AND entre tipos). Valores = string exata do domínio.
  `sort` = `campo,ordem` snake_case: `julgamento_data`, `publicacao_data`, `relevancia`.
- **Inteiro teor**: `POST /jurisprudencias/document`
  Corpo: `{documentoId, datasPublicacao:[{inicio,fim}]}` (data = `publicacaoData` em ISO). Retorna HTML.
- **Domínio**: `POST /dominio/{campo}` com `{}` (lista tudo) ou `{texto,tipoTexto}` (filtra).
- Tipos de documento na base: Acórdão (3,3 mi), Decisão Monocrática (452 mil),
  Decisão Turma Recursal (528 mil), Decisão Vice-Presidência (225 mil).

## Deploy (Render)

Serviço web separado do legado. `render.yaml` e `Procfile` inclusos.
Defina `RENDER_EXTERNAL_HOSTNAME` (ex.: `mcp-tjmg-v2.onrender.com`) — usado no
allowlist anti-DNS-rebinding e no keep-alive. Health check em `/health`;
diagnóstico rápido em `/diag`.

No Claude.ai, adicione como conector MCP a URL do serviço v2 (endpoint streamable HTTP).
