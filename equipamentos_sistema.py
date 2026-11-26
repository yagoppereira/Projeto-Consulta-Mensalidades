# ============================================================================
# ABA "EQUIPAMENTOS" — parque de bombas de um cliente do SISTEMA CTA
#
# Diferente das abas Cliente/Grupo (que partem do código CIGAM, ótica de
# faturamento), aqui a busca é pelo cliente_id do sistema operacional
# (o cliente_id de silver.cadastro_bombas) — é ele o dono do posto/tanque/bomba e
# dos abastecimentos. Mostra, a partir do DW:
#   - todas as bombas que o cliente tem ou já teve abastecendo nos postos
#     dele, e TODOS os seriais (equipamentos) que passaram por cada uma;
#   - o volume abastecido por bomba ao longo do tempo (dia/semana/mês),
#     com uma estrela em cada troca de serial;
#   - os períodos de uso de cada bomba, um segmento por serial;
#   - cada equipamento (serial): em quais bombas esteve, quando e quanto
#     abasteceu.
#
# Lê só da SILVER do DW, duas tabelas mantidas no dbt-cta:
#   - silver.abastecimento_bomba_serial_dia: litros e nº de abastecimentos por
#     base × dia × posto × bomba × SERIAL, já com a limpeza canônica de
#     silver.abastecimentos_validos. O serial é o gravado em cada abastecimento
#     (histórico desde 2020) — o mesmo "Número Serial da automação" do
#     relatório "Auditoria de abastecimentos" do sistema. Conferido contra
#     esse relatório (16 bombas, jan–mar/2026): os seriais batem, e as trocas
#     caem nas datas de atualização do cadastro.
#   - silver.cadastro_bombas: estado ATUAL de cada bomba (posto, cliente,
#     equipamento) — é dele que sai o "serial instalado hoje" e o diretório
#     de clientes da busca.
# Antes a aba lia o bronze (sistema_<base>__*), incluindo a tabela de
# abastecimento, a mais pesada do DW; o fato diário existe para isso não
# acontecer (pedido do responsável pelo DW, 21/10/2026).
# ============================================================================

import concurrent.futures
import re
import unicodedata

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.api_core.exceptions import BadRequest, Forbidden, NotFound
from google.cloud import bigquery

# bases do sistema CTA com histórico de uso no DW. O Link2Pump (l2p) está no
# cadastro, mas não no fato diário (que segue silver.abastecimentos_validos,
# só cta + ipc) — buscar um cliente de lá mostraria bombas sem nenhum uso.
BASES_SISTEMA = {"cta": "CTA", "ipc": "Ipiranga Connect"}

# tabelas que a aba lê — é a lista que a service account do app precisa
# enxergar (roles/bigquery.dataViewer em nível de tabela)
TABELA_USO = "silver.abastecimento_bomba_serial_dia"
TABELA_CADASTRO = "silver.cadastro_bombas"
TABELAS_LIDAS = [TABELA_USO, TABELA_CADASTRO]
# alocação CIGAM (quem paga cada bomba HOJE): as mesmas tabelas que a aba
# Cliente já lê. Sem acesso, a linha do tempo só perde a cor por pagante.
TABELA_ALOCACAO = "gold.bombas_alocadas"
TABELA_EMPRESAS_CIGAM = "bronze.cigam__empresas"

# Paleta categórica (passo escuro), validada contra o fundo do gráfico
# (#26263A): CVD adjacente ΔE >= 8.4, visão normal >= 19.3. O verde (slot 6)
# fica abaixo de 3:1 de contraste — a tabela de bombas logo abaixo do
# gráfico é a leitura alternativa. A ORDEM é o que garante a separação
# pra daltonismo: não reordenar nem acrescentar cor — passou de 8 bombas,
# o gráfico limita a seleção.
CORES_BOMBAS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
MAX_BOMBAS_GRAFICO = len(CORES_BOMBAS)

COR_FUNDO_GRAFICO = "#26263A"  # secondaryBackgroundColor do tema
COR_TEXTO_GRAFICO = "#F1F1F1"  # textColor do tema
COR_GRADE = "#3A3A52"
COR_SERIAL_ATUAL = "#3987e5"
COR_SERIAL_ANTERIOR = "#8A95A8"
COR_SEM_SERIAL = "#5B6275"
COR_SEM_ALOCACAO = "#C9CED8"
# pagante com conta própria no sistema: contorno + ⚠. O vermelho do contorno
# não entra na paleta dos pagantes (slot 7 fica de fora) pra não confundir
COR_ALERTA = "#FF4B4B"
# seta de serial que mudou de bomba: clara e pontilhada, pra não competir
# com as barras
COR_MOVIMENTO = "rgba(241, 241, 241, 0.7)"
# colchete que une os canais de um equipamento duplo: cheio, lilás claro —
# fora da paleta dos pagantes
COR_DUPLO = "#B8A9FF"
CORES_PAGANTES = CORES_BOMBAS[:6]
COR_OUTROS_PAGANTES = CORES_BOMBAS[6]

# timeline: mais de N dias sem abastecer quebra o período de uso
DIAS_SEM_USO_QUEBRA_PERIODO = 30
# equipamento instalado no cadastro só conta como ATIVO (sinalizando) se
# abasteceu nos últimos N dias — o cadastro sozinho mantém equipamento parado
# como "instalado" indefinidamente
DIAS_SINAL_ATIVO = 30
# acima disso a timeline mostra só as N bombas de maior volume do filtro
# (cliente grande chega a 200+ bombas, ~6000px de gráfico) — o filtro de
# pagante/bomba no topo da aba é o caminho pra ver as outras
LIMITE_TIMELINE_COMPLETA = 40

GRANULARIDADES = {"Dia": "D", "Semana": "W", "Mês": "M"}


def _tabela(project_id: str, nome: str) -> str:
    # nome vem sempre das constantes acima (nunca do usuário) — seguro no f-string
    assert nome in TABELAS_LIDAS + [TABELA_ALOCACAO, TABELA_EMPRESAS_CIGAM]
    return f"`{project_id}.{nome}`"


def _normalizar_texto(texto) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper().strip()


UFS_BRASIL = frozenset("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())
_MINUSCULAS_NO_NOME = {"de", "da", "do", "das", "dos", "e"}
# o campo cidade do posto às vezes traz o ESTADO por extenso: vira a sigla
# (Rio de Janeiro e São Paulo ficam de fora: também são cidades)
_ESTADOS_POR_EXTENSO = {
    "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA", "CEARA": "CE",
    "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA", "MATO GROSSO": "MT",
    "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG", "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR",
    "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO GRANDE DO NORTE": "RN", "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO",
    "RORAIMA": "RR", "SANTA CATARINA": "SC", "SERGIPE": "SE", "TOCANTINS": "TO"}
# "cidades" que não dizem onde fica: descartadas (a UF, se houver, continua)
_CIDADE_SEM_LOCAL = frozenset({"BRASIL", "MATRIZ", "FILIAL", "SEDE", "CENTRO"})
# sinais de que o campo cidade recebeu um ENDEREÇO (quadra, lote, rua...)
_PADRAO_ENDERECO = re.compile(r"\b(QD|QUADRA|LT|LOTE|CJ|CONJ|RUA|AV|AVENIDA|ROD|RODOVIA|KM|N[OoºÂ°]|BR|SP|GO)\s*\d", re.I)
# palavras que muitas cidades dividem: sozinhas não provam que o nome já
# traz a cidade ("Fazenda Santa Marta" não é "Santa Maria das Barreiras")
_PALAVRAS_COMUNS_CIDADE = frozenset({
    "SANTA", "SANTO", "SANTOS", "NOVA", "NOVO", "PORTO", "MONTE", "CAMPO", "CAMPOS", "VILA", "SERRA", "BELO", "BELA",
    "ALTO", "ALTA", "BARRA", "PONTE", "LAGOA", "PEDRA", "CIDADE", "JARDIM", "VISTA", "BOM", "BOA", "GRANDE",
    "MATRIZ", "FILIAL", "POSTO", "TANQUE", "COMBOIO", "FAZENDA", "OBRA", "BASE"})


def ler_cidade_do_posto(cidade) -> tuple:
    """O campo cidade do posto é texto livre; em vez de descartar o que vem
    sujo, aproveita o que diz ONDE fica o aparelho. Devolve (cidade, uf_do_texto):
    - "Rio Verde/MT" → ("Rio Verde", "MT") — a UF colada vira UF;
    - "GOIATUBA 2" → ("Goiatuba", None); "2Campos" → ("Campos", None) — número sai;
    - "ALAGOAS" → ("", "AL") — estado por extenso vira só a UF;
    - "brasil", "MATRIZ", endereço ("SCIA QD 8 CJ 16...") → ("", None).
    Tudo maiúsculo ou tudo minúsculo vira "Porto Feliz"."""
    c = re.sub(r"\s+", " ", cidade.strip()) if isinstance(cidade, str) else ""
    uf_txt = None
    m = re.search(r"\s*[/-]\s*([A-Za-z]{2})$", c)
    if m and m.group(1).upper() in UFS_BRASIL:
        uf_txt, c = m.group(1).upper(), c[:m.start()].strip()
    if _PADRAO_ENDERECO.search(c):
        return "", uf_txt
    c = re.sub(r"\s+", " ", re.sub(r"\d+", " ", c)).strip(" -/.,")
    norm = _normalizar_texto(c)
    if norm in _ESTADOS_POR_EXTENSO:
        return "", uf_txt or _ESTADOS_POR_EXTENSO[norm]
    if norm in _CIDADE_SEM_LOCAL or len(re.sub(r"[^A-Z]", "", norm)) < 3:
        return "", uf_txt
    if c.isupper() or c.islower():
        c = " ".join(p.lower() if i and p.lower() in _MINUSCULAS_NO_NOME else p.capitalize()
                     for i, p in enumerate(c.split(" ")))
    return c, uf_txt


def _cidade_legivel(cidade) -> str:
    return ler_cidade_do_posto(cidade)[0]


def _so_letras(texto) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", _normalizar_texto(texto)).strip()


def _nome_ja_tem_local(nome: str, cidade: str, uf: str) -> bool:
    """O nome da bomba já diz onde ela está? Sim quando traz: "/UF" de
    qualquer estado ("... - Ijuí/RS"); " - UF" do próprio posto; a cidade
    inteira, mesmo sem acento/pontuação ("Varzea/Grande"); uma palavra
    própria da cidade ("VIA CAXIAS" ~ Duque de Caxias); ou a cidade com erro
    de digitação ("Monte Carmelo" ~ "Monte Caramelo")."""
    texto = str(nome or "")
    if re.search(r"/\s*(" + "|".join(sorted(UFS_BRASIL)) + r")\b", texto.upper()):
        return True
    if uf and re.search(rf"(?:^|[\-\s]){uf}(?:$|[\s/\-.,)])", texto.upper()):
        return True
    n, c = _so_letras(texto), _so_letras(cidade)
    if not c:
        return False
    if f" {c} " in f" {n} ":
        return True
    palavras_nome = set(n.split())
    if any(len(w) >= 4 and w not in _PALAVRAS_COMUNS_CIDADE and w in palavras_nome for w in c.split()):
        return True
    import difflib
    pal, k = n.split(), len(c.split())
    janelas = {" ".join(pal[i:i + t]) for t in (k - 1, k, k + 1) if t > 0 for i in range(len(pal) - t + 1)}
    return any(difflib.SequenceMatcher(None, c, j).ratio() >= 0.85 for j in janelas)


def sufixo_local(nome, cidade, uf) -> str:
    """" - Cidade/UF" para o nome da bomba, no modelo "Posto - Cidade/UF" das
    planilhas do financeiro. Sai da CIDADE e da UF do POSTO no cadastro do
    sistema CTA (silver.cadastro_bombas), não do CIGAM — limpos por
    ler_cidade_do_posto. Sem cidade aproveitável, fica só a UF (" - SP"): pra
    revisão, saber o estado já ajuda. Se a UF escrita no campo cidade
    diverge da UF do posto, as duas aparecem. Vazio quando não há nem cidade
    nem UF, ou quando o nome já diz onde a bomba está (_nome_ja_tem_local) —
    evita "Força Ambiental - Saquarema/RJ - Saquarema/RJ"."""
    cid, uf_txt = ler_cidade_do_posto(cidade)
    uf_posto = uf.strip().upper() if isinstance(uf, str) else ""
    uf_posto = uf_posto if uf_posto in UFS_BRASIL else ""
    u = uf_txt or uf_posto
    if not (cid or u) or _nome_ja_tem_local(nome, cid, u):
        return ""
    conflito = f" (UF do posto: {uf_posto})" if uf_txt and uf_posto and uf_txt != uf_posto else ""
    return " - " + "/".join(x for x in (cid, u) if x) + conflito


def _norm_busca(texto) -> str:
    """Nome pra comparar na busca: sem acento, maiúsculo, pontuação vira
    espaço — "FORCA AMBIENTAL LTDA." (CIGAM) e "Força Ambiental" (sistema)
    ficam comparáveis."""
    if texto is None or (isinstance(texto, float) and pd.isna(texto)):
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", _normalizar_texto(texto)).strip()


# --- 1. Dados (BigQuery) ---------------------------------------------------
# CUSTO NO DW (aviso de consumo, 03/11/2026). Na cobrança sob demanda o
# BigQuery cobra pelo volume das COLUNAS lidas, com mínimo de 10 MB por
# tabela referenciada. Por isso a aba faz só três leituras COMPARTILHADAS
# (st.cache_data vale para todos os usuários do app) e o resto em memória:
#   - cadastro de bombas inteiro (silver.cadastro_bombas, ~8 MB, 1 h):
#     diretório, ponte de busca, postos e cadastro de cada cliente, contas
#     para o alerta de pagante — antes eram 2 consultas por cliente aberto;
#   - empresas do CIGAM (bronze.cigam__empresas, ~7 MB, 1 h);
#   - passagens serial × bomba agregadas do fato (~20 mil linhas, 12 h — o
#     fato só atualiza 1x por dia) — antes o fato INTEIRO (~280 MB das
#     colunas usadas) era lido a cada cliente aberto, porque filtrar por
#     serial não aproveita o cluster (base, posto, bomba).
# A alocação CIGAM vem da carga horária que o app.py já faz de
# gold.bombas_alocadas (passada em `bombas_alocadas`): antes a aba consultava
# a view de novo a cada cliente, e ela referencia ~13 tabelas do bronze
# (≥ 130 MB cobrados por consulta só pelo mínimo por tabela).
# Por cliente aberto sobra UMA consulta: o uso diário dos postos dele, que o
# cluster por posto mantém pequena.
@st.cache_data(ttl=3600, show_spinner="Carregando o cadastro de bombas (direto do BigQuery)...")
def carregar_cadastro_bombas(_client, project_id: str) -> pd.DataFrame:
    """silver.cadastro_bombas inteiro das bases da aba, só as colunas usadas."""
    sql = f"""
    SELECT
      base, bomba_id, bomba_nome, canal, bomba_ativa,
      bomba_ultima_atualizacao AS bomba_atualizada_em,
      equipamento_id, CAST(serial_equipamento AS STRING) AS serial_equipamento, equipamento_descricao,
      tanque_nome, tanque_mobile AS tanque_movel,
      CAST(posto_id AS STRING) AS posto_id, posto_nome, posto_cidade, posto_uf,
      cliente_id, cliente_nome, REGEXP_REPLACE(IFNULL(cliente_cnpj, ''), r'\\D', '') AS cliente_cnpj,
      trr_nome, IFNULL(trr_sistema_interno, FALSE) AS trr_interno
    FROM {_tabela(project_id, TABELA_CADASTRO)}
    WHERE base IN UNNEST(@bases)
    """
    return _query(_client, sql, [bigquery.ArrayQueryParameter("bases", "STRING", list(BASES_SISTEMA))])


@st.cache_data(ttl=3600, show_spinner=False)
def carregar_empresas_cigam(_client, project_id: str) -> pd.DataFrame:
    """Código, nome, fantasia e CNPJ das empresas do CIGAM (~7 mil linhas)."""
    sql = f"""
    SELECT CAST(codigo AS STRING) AS codigo, ANY_VALUE(nomeCompleto) AS nome, ANY_VALUE(fantasia) AS fantasia,
           ANY_VALUE(REGEXP_REPLACE(CAST(cnpjCpf AS STRING), r'\\D', '')) AS cnpj
    FROM {_tabela(project_id, TABELA_EMPRESAS_CIGAM)} GROUP BY 1
    """
    return _query(_client, sql, [])


@st.cache_data(ttl=12 * 3600, show_spinner="Carregando por onde cada serial já passou (direto do BigQuery)...")
def carregar_passagens_seriais(_client, project_id: str) -> pd.DataFrame:
    """Uma linha por (base, bomba, serial) do fato diário INTEIRO, com o
    primeiro e o último dia de uso e os litros — ~20 mil linhas (03/11/2026).
    Lida no máximo a cada 12 h e compartilhada entre todas as buscas."""
    sql = f"""
    SELECT base, CAST(bomba_id AS STRING) AS bomba_id, CAST(serial_equipamento AS STRING) AS serial,
           MIN(data) AS inicio, MAX(data) AS fim, COUNT(*) AS dias, SUM(litros) AS litros
    FROM {_tabela(project_id, TABELA_USO)}
    WHERE serial_equipamento IS NOT NULL AND bomba_id IS NOT NULL
    GROUP BY 1, 2, 3
    """
    df = _query(_client, sql, [])
    df["inicio"] = pd.to_datetime(df["inicio"])
    df["fim"] = pd.to_datetime(df["fim"])
    df["litros"] = pd.to_numeric(df["litros"], errors="coerce").astype(float)
    return df


def carregar_clientes_sistema(_client, project_id: str) -> pd.DataFrame:
    """Diretório de clientes do sistema, com a contagem de bombas cadastradas
    hoje nos postos de cada um — só pra resolver a busca. Sai do cadastro de
    bombas (carga compartilhada), então cliente sem nenhuma bomba cadastrada
    não aparece (e não teria abastecimento por bomba pra mostrar)."""
    cad = carregar_cadastro_bombas(_client, project_id)
    cad = cad[cad["cliente_id"].notna()]
    df = cad.groupby(["base", "cliente_id"], as_index=False).agg(
        cliente_nome=("cliente_nome", "first"), cnpj=("cliente_cnpj", "first"), trr_nome=("trr_nome", "first"),
        trr_interno=("trr_interno", "any"), qtd_bombas=("bomba_id", "size"),
        qtd_bombas_com_equipamento=("equipamento_id", "count"))
    df["cnpj"] = df["cnpj"].fillna("")
    df["_nome_norm"] = df["cliente_nome"].map(_norm_busca)
    return df


def montar_chaves_busca(cadastro: pd.DataFrame, empresas: pd.DataFrame, bombas_alocadas: pd.DataFrame = None) -> pd.DataFrame:
    """
    Tudo que leva a um cliente do sistema além do nome/id/CNPJ dele, uma
    linha por (base, cliente_id, tipo, chave) — é a ponte que deixa o
    financeiro buscar com o que ele tem na mão. Montada em memória a partir
    das cargas compartilhadas (sem consulta própria):
      - 'cigam': empresa do CIGAM que PAGA ou é LOCAL de bomba do cliente hoje
        (alocação de bombas), com código, nome, fantasia e CNPJ do CIGAM;
      - 'cigam_cnpj': empresa do CIGAM com o MESMO CNPJ do cliente no
        sistema — liga mesmo sem alocação;
      - 'bomba' e 'serial': bombas cadastradas hoje no cliente e o serial
        do equipamento instalado nelas;
      - 'id_op': ID operacional da alocação — é o "ID" com que o financeiro e
        o controle de alocação identificam a bomba nas planilhas ("ID
        932169744 - São João da Barra").
    """
    colunas = ["base", "cliente_id", "tipo", "chave", "papel", "bombas", "nome", "fantasia", "cnpj"]
    partes = []
    cad = cadastro[cadastro["cliente_id"].notna()]
    emp = empresas.drop_duplicates("codigo") if empresas is not None and not empresas.empty else \
        pd.DataFrame(columns=["codigo", "nome", "fantasia", "cnpj"])
    if bombas_alocadas is not None and not bombas_alocadas.empty:
        a = bombas_alocadas[bombas_alocadas["base"].isin(list(BASES_SISTEMA))]
        for papel, col in (("pagante", "cliente_cigam_pagante"), ("local", "cliente_cigam_local")):
            g = a.dropna(subset=[col]).assign(codigo=lambda d: d[col].map(lambda c: f"{int(c):06d}"))
            g = g.groupby(["base", "cliente_id_app", "codigo"], as_index=False).size()
            g = g.rename(columns={"cliente_id_app": "cliente_id", "size": "bombas", "codigo": "chave"})
            partes.append(g.assign(tipo="cigam", papel=papel).merge(
                emp.rename(columns={"codigo": "chave"}), on="chave", how="left"))
        ids = a.dropna(subset=["id_op_operacional"])[["base", "cliente_id_app", "id_op_operacional"]].drop_duplicates()
        partes.append(ids.rename(columns={"cliente_id_app": "cliente_id", "id_op_operacional": "chave"}).assign(tipo="id_op"))
    cnpjs = cad[(cad["cliente_cnpj"].str.len() == 14) & ~cad["cliente_cnpj"].str.startswith("00000000")]
    cnpjs = cnpjs[["base", "cliente_id", "cliente_cnpj"]].drop_duplicates()
    partes.append(cnpjs.merge(emp, left_on="cliente_cnpj", right_on="cnpj").rename(columns={"codigo": "chave"})
                  .assign(tipo="cigam_cnpj", papel="mesmo CNPJ").drop(columns=["cliente_cnpj"]))
    partes.append(cad[["base", "cliente_id", "bomba_id"]].rename(columns={"bomba_id": "chave"}).assign(tipo="bomba"))
    partes.append(cad.dropna(subset=["serial_equipamento"])[["base", "cliente_id", "serial_equipamento"]]
                  .drop_duplicates().rename(columns={"serial_equipamento": "chave"}).assign(tipo="serial"))
    df = pd.concat([p.reindex(columns=colunas) for p in partes], ignore_index=True)
    df["cliente_id"] = df["cliente_id"].astype(str)
    df["chave"] = df["chave"].astype(str)
    df["_nome_norm"] = df["nome"].map(_norm_busca)
    df["_fantasia_norm"] = df["fantasia"].map(_norm_busca)
    df["_codigo_num"] = pd.to_numeric(df["chave"].where(df["tipo"].isin(["cigam", "cigam_cnpj"])),
                                      errors="coerce").astype("Int64")
    return df


def _mostrar_falta_de_permissao(project_id: str, erro: Exception):
    """Erro 403 do BigQuery: a service account do app não lê as tabelas da
    aba. Lista exatamente o que precisa ser liberado."""
    st.error(
        "**Sem permissão de leitura nas tabelas desta aba.** Quem administra o GCP precisa conceder "
        "**BigQuery Data Viewer** (`roles/bigquery.dataViewer`) à service account do app nas tabelas abaixo "
        "(só nelas — a aba não lê o bronze):",
        icon="🔒",
    )
    st.markdown("\n".join(f"- {_tabela(project_id, nome)}" for nome in TABELAS_LIDAS))
    st.caption("Erro do BigQuery: " + str(getattr(erro, "message", erro))[:600])


def _mostrar_tabela_inexistente(erro: Exception):
    """Erro 404: a tabela ainda não foi construída no DW (modelo novo antes
    do primeiro `dbt build` de produção)."""
    st.warning(
        "**As tabelas desta aba ainda não existem no DW.** O modelo "
        f"`{TABELA_USO}` é criado pelo `dbt build` de produção do dbt-cta; "
        "a aba passa a funcionar depois da primeira execução.",
        icon="⏳",
    )
    st.caption("Erro do BigQuery: " + str(getattr(erro, "message", erro))[:600])


def _query(client, sql: str, params: list) -> pd.DataFrame:
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return client.query(sql, job_config=job_config).to_dataframe(create_bqstorage_client=False)


@st.cache_data(ttl=1800, show_spinner="Buscando os abastecimentos do cliente (direto do BigQuery)...")
def carregar_parque_cliente(_client, project_id: str, base: str, cliente_id: str):
    """
    Retorna (cadastro, uso) de um cliente do sistema:
      - uso: litros e nº de abastecimentos por bomba × dia × SERIAL, em
        todos os postos do cliente (inclusive de bomba que hoje está em
        outro cliente ou sem equipamento), do fato diário da silver — que
        já aplica a limpeza canônica de silver.abastecimentos_validos.
        bomba_id e serial nulos vêm mantidos, pra soma de litros fechar
        com a origem. É a ÚNICA consulta por cliente: o fato é clusterizado
        por base, posto_id, bomba_id, e filtrar pelos postos do cliente é o
        que mantém a leitura pequena.
      - cadastro: estado ATUAL de cada bomba (do cliente hoje + as que
        apareceram nos abastecimentos), com o serial instalado hoje — sai do
        cadastro compartilhado, sem consulta.
    """
    cad = carregar_cadastro_bombas(_client, project_id)
    cad_base = cad[cad["base"] == base]
    # postos do cliente hoje — pelo cadastro de bombas, então posto sem
    # nenhuma bomba cadastrada hoje fica de fora (e o uso dele também)
    ids_postos = sorted({int(p) for p in cad_base.loc[cad_base["cliente_id"] == str(cliente_id), "posto_id"].dropna()})

    if ids_postos:
        # o grão do fato inclui o posto: bomba que mudou de posto do mesmo
        # cliente no dia soma as duas linhas
        uso = _query(_client, f"""
        SELECT CAST(bomba_id AS STRING) AS bomba_id, data, CAST(serial_equipamento AS STRING) AS serial,
               SUM(litros) AS litros, SUM(qtd_abastecimentos) AS qtd_abastecimentos
        FROM {_tabela(project_id, TABELA_USO)}
        WHERE base = @base AND posto_id IN UNNEST(@postos)
        GROUP BY 1, 2, 3
        """, [bigquery.ScalarQueryParameter("base", "STRING", base),
              bigquery.ArrayQueryParameter("postos", "INT64", ids_postos)])
    else:
        uso = pd.DataFrame(columns=["bomba_id", "data", "serial", "litros", "qtd_abastecimentos"])

    ids_bombas = set(uso["bomba_id"].dropna().astype(str))
    cadastro = cad_base[(cad_base["cliente_id"] == str(cliente_id)) | cad_base["bomba_id"].isin(ids_bombas)]
    cadastro = cadastro.rename(columns={"cliente_id": "cliente_id_atual"})[[
        "bomba_id", "bomba_nome", "canal", "bomba_ativa", "bomba_atualizada_em", "equipamento_id",
        "serial_equipamento", "equipamento_descricao", "tanque_nome", "tanque_movel", "posto_nome",
        "posto_cidade", "posto_uf", "cliente_id_atual"]].reset_index(drop=True)

    uso["data"] = pd.to_datetime(uso["data"])
    uso["litros"] = pd.to_numeric(uso["litros"], errors="coerce").astype(float)
    return cadastro, uso


def alocacao_do_cliente(bombas_alocadas: pd.DataFrame, cadastro: pd.DataFrame, base: str, cliente_id: str,
                        bombas) -> pd.DataFrame:
    """
    Quem paga cada bomba HOJE (alocação de bombas, estado atual — não serve
    pra atribuir período passado), com nome/CNPJ do CIGAM, e se o pagante
    tem CONTA PRÓPRIA no sistema: outro cliente_id da mesma base cujo CNPJ
    tem a mesma raiz (8 dígitos) do pagante. Pagante assim, com raiz
    diferente da do próprio cliente, é sinal de bomba de outra operação no
    parque do cliente (ex.: Petrovera pagando 2 bombas nos postos da Força
    Ambiental, 22/10/2026). Não sinaliza:
      - pagante sem conta própria (ex.: União Norte, que só existe no
        CIGAM) — é o caso comum de empresa do grupo pagando;
      - conta de TRR interno (Removidos, testes): no Connect, uma conta
        removida com a raiz da Ipiranga marcaria as 255 bombas que a
        Ipiranga paga;
      - cliente sem CNPJ no sistema (vem vazio em boa parte do CTA): sem a
        raiz dele não dá pra dizer se o pagante é do grupo (Construtora Luiz
        Costa pagando as próprias 78 bombas sairia marcada por causa de uma
        conta "Desativado").
    Medido em 22/10/2026: 42 bombas de 15 clientes no CTA, quase todas pagas
    por distribuidora com conta própria (Combustran, Petrovera, Comercial
    Cordeiro, São Leopoldo TRR); nenhuma no Connect.
    Tudo em memória: `bombas_alocadas` é a carga horária de
    gold.bombas_alocadas que o app.py já faz; `cadastro`, a compartilhada.
    """
    colunas = ["bomba_id", "pagante_codigo", "pagante_nome", "pagante_cnpj", "local_codigo", "local_nome",
               "id_op", "pagante_outras_contas", "cliente_raiz"]
    if bombas_alocadas is None or bombas_alocadas.empty or "bomba_id" not in bombas_alocadas:
        return pd.DataFrame(columns=colunas)
    bombas = {str(b) for b in bombas}
    a = bombas_alocadas[(bombas_alocadas["base"] == base) & bombas_alocadas["bomba_id"].astype(str).isin(bombas)]
    codigo = lambda c: f"{int(c):06d}" if pd.notna(c) else None  # noqa: E731
    aloc = pd.DataFrame({
        "bomba_id": a["bomba_id"].astype(str),
        "pagante_codigo": a["cliente_cigam_pagante"].map(codigo),
        "pagante_nome": a["pagante_nome"],
        "pagante_cnpj": a["pagante_cnpj"].fillna("").astype(str).str.replace(r"\D", "", regex=True),
        "local_codigo": a["cliente_cigam_local"].map(codigo),
        "local_nome": a["local_nome"],
        "id_op": a["id_op_operacional"].where(a["id_op_operacional"].notna(), None),
    })
    contas = cadastro[(cadastro["base"] == base) & cadastro["cliente_id"].notna() & ~cadastro["trr_interno"].astype(bool)
                      & (cadastro["cliente_cnpj"].str.len() == 14)]
    contas = contas.groupby("cliente_id", as_index=False).agg(conta_nome=("cliente_nome", "first"),
                                                              cnpj=("cliente_cnpj", "first"))
    contas["raiz"] = contas["cnpj"].str[:8]
    raiz_cliente = contas.loc[contas["cliente_id"] == str(cliente_id), "raiz"]
    outras_por_raiz = (contas[(contas["cliente_id"] != str(cliente_id)) & (contas["raiz"] != "00000000")]
                       .sort_values("cliente_id").groupby("raiz")
                       .apply(lambda g: ", ".join(f"{n} (id {c})" for n, c in zip(g["conta_nome"][:3], g["cliente_id"][:3])),
                              include_groups=False).to_dict())
    raiz_pagante = aloc["pagante_cnpj"].where(aloc["pagante_cnpj"].str.len() == 14, "").str[:8]
    aloc["pagante_outras_contas"] = raiz_pagante.map(outras_por_raiz)
    aloc["cliente_raiz"] = raiz_cliente.iloc[0] if not raiz_cliente.empty else None
    return aloc.reset_index(drop=True)[colunas]


def percurso_dos_seriais(passagens: pd.DataFrame, cadastro: pd.DataFrame, seriais) -> pd.DataFrame:
    """Por onde cada serial do cliente já passou, em QUALQUER bomba, cliente
    ou base do fato diário (cta + ipc), com o cadastro atual da bomba (nome,
    posto, cliente) — filtrado em memória das passagens compartilhadas."""
    seriais = {str(s) for s in seriais}
    p = passagens[passagens["serial"].isin(seriais)]
    info = cadastro[["base", "bomba_id", "bomba_nome", "posto_nome", "cliente_id", "cliente_nome"]]
    return p.merge(info, on=["base", "bomba_id"], how="left").reset_index(drop=True)


# passagem de menos que isso num lugar é teste/configuração, não instalação
# (ex.: 6 L num dia) — some do percurso pra não inventar transferência
LITROS_MIN_PASSAGEM = 50.0
# dois trechos do mesmo serial no mesmo cliente que se sobrepõem por pelo
# menos isso são a MESMA instalação (canais 1 e 2 de um equipamento duplo).
# Trecho que só encosta no anterior é transferência: Queimados → Belford Roxo
# Novo (serial 16994) trocou no mesmo dia, 24/04/2025
DIAS_MIN_SOBREPOSICAO_CANAIS = 7
TIPO_DENTRO = "Entre bombas do cliente"
TIPO_ENTROU = "Entrou de outro cliente"
TIPO_SAIU = "Saiu para outro cliente"


def _rotulo_local(linha, base_cliente: str, cliente_id: str) -> str:
    """Bomba do próprio cliente: só a bomba. Fora dele: bomba · cliente
    (a base vai em coluna própria)."""
    nome = (_truncar(linha.get("bomba_nome"), 40) or "bomba fora do cadastro") + sufixo_local(
        linha.get("bomba_nome"), linha.get("posto_cidade"), linha.get("posto_uf"))
    if linha["base"] == base_cliente and str(linha.get("cliente_id")) == str(cliente_id):
        return f"{nome} (#{linha['bomba_id']})"
    cliente = _truncar(linha.get("cliente_nome"), 30) or "cliente desconhecido"
    return f"{nome} (#{linha['bomba_id']}) · {cliente}"


def _tipo_por_canais(n: int) -> str:
    """Nº de bombas (canais) atendidas ao mesmo tempo → tipo do equipamento,
    a mesma régua de silver.cadastro_bombas.bombas_no_equipamento."""
    return {1: "simples", 2: "duplo", 3: "triplo"}.get(int(n), "quádruplo")


def agrupar_instalacoes(passagens: pd.DataFrame) -> list:
    """
    Passagens de UM serial (colunas base, cliente_id, bomba_id, inicio, fim,
    litros) → instalações, na ordem do tempo. Passagens no mesmo (base,
    cliente) que se sobrepõem por DIAS_MIN_SOBREPOSICAO_CANAIS dias ou mais
    são a MESMA instalação: os canais de um equipamento duplo/triplo. Cada
    instalação traz `canais` (nº de bombas distintas) — 2 = duplo.
    É a régua única de "duplo" da aba: linha do tempo, tabela de
    equipamentos e transferências usam esta função.
    """
    instalacoes = []
    for _, linha in passagens.sort_values("inicio").iterrows():
        chave = (linha["base"], linha["cliente_id"])
        atual = instalacoes[-1] if instalacoes else None
        sobreposicao = (min(linha["fim"], atual["fim"]) - max(linha["inicio"], atual["inicio"])).days \
            if atual else -1
        if atual and atual["chave"] == chave and sobreposicao >= DIAS_MIN_SOBREPOSICAO_CANAIS:
            atual["linhas"].append(linha)
            atual["fim"] = max(atual["fim"], linha["fim"])
        else:
            instalacoes.append({"chave": chave, "inicio": linha["inicio"], "fim": linha["fim"], "linhas": [linha]})
    for inst in instalacoes:
        inst["canais"] = len({r["bomba_id"] for r in inst["linhas"]})
    return instalacoes


def canais_do_cliente(por_serial: pd.DataFrame) -> pd.DataFrame:
    """Equipamentos que funcionaram como DUPLO (ou triplo) nas bombas do
    cliente, inclusive os já retirados: uma linha por (serial, conjunto de
    bombas) com o período em que os canais abasteceram juntos."""
    colunas = ["serial", "bombas", "canais", "inicio", "fim"]
    if por_serial is None or por_serial.empty:
        return pd.DataFrame(columns=colunas)
    p = por_serial.rename(columns={"primeiro": "inicio", "ultimo": "fim"}).assign(base="", cliente_id="")
    linhas = []
    for serial, g in p.groupby("serial"):
        for inst in agrupar_instalacoes(g):
            if inst["canais"] > 1:
                bombas = tuple(r["bomba_id"] for r in sorted(inst["linhas"], key=lambda r: str(r["bomba_id"])))
                linhas.append({"serial": serial, "bombas": bombas, "canais": inst["canais"],
                               "inicio": inst["inicio"], "fim": inst["fim"]})
    return pd.DataFrame(linhas, columns=colunas)


def derivar_transferencias(percurso: pd.DataFrame, base: str, cliente_id: str) -> tuple:
    """
    Transforma o percurso dos seriais em INSTALAÇÕES e TRANSFERÊNCIAS:
      - instalação = trechos do serial no mesmo (base, cliente) que se
        sobrepõem por DIAS_MIN_SOBREPOSICAO_CANAIS dias ou mais (canais do
        mesmo equipamento contam como um só);
      - transferência = a passagem de uma instalação para a seguinte.
    Devolve (eventos, percursos): `eventos` só com as transferências que
    entram, saem ou circulam DENTRO do cliente — o PERÍODO da mudança
    (último abastecimento na origem → primeiro no destino), o tipo, as duas
    pontas e a base de cada uma; `percursos` com o caminho completo de cada
    serial em texto.
    """
    colunas = ["serial", "data", "saida", "periodo", "tipo", "de", "base_de", "para", "base_para", "entre_bases"]
    if percurso is None or percurso.empty:
        return pd.DataFrame(columns=colunas), {}
    p = percurso[percurso["litros"] >= LITROS_MIN_PASSAGEM].copy()
    p["cliente_id"] = p["cliente_id"].astype(object).where(p["cliente_id"].notna(), None)
    eventos, percursos = [], {}
    for serial, g in p.sort_values(["serial", "inicio"]).groupby("serial"):
        instalacoes = agrupar_instalacoes(g)
        do_cliente = [inst["chave"] == (base, str(cliente_id)) for inst in instalacoes]
        if not any(do_cliente):
            continue
        def _onde(inst, do_proprio):
            # no próprio cliente o que importa é a BOMBA; fora dele, o cliente
            if do_proprio:
                bombas = " + ".join(f"{_truncar(r.get('bomba_nome'), 30) or 'bomba'}"
                                    f"{sufixo_local(r.get('bomba_nome'), r.get('posto_cidade'), r.get('posto_uf'))}"
                                    f" (#{r['bomba_id']})"
                                    for r in inst["linhas"])
                return bombas + (f" [{_tipo_por_canais(inst['canais'])}]" if inst["canais"] > 1 else "")
            outra_base = "" if inst["chave"][0] == base else \
                f" [{BASES_SISTEMA.get(inst['chave'][0], inst['chave'][0])}]"
            return f"{_truncar(inst['linhas'][0].get('cliente_nome'), 28) or 'cliente desconhecido'}{outra_base}"

        percursos[serial] = " → ".join(
            f"{_onde(inst, proprio)} ({inst['inicio']:%m/%y}–{inst['fim']:%m/%y})"
            for inst, proprio in zip(instalacoes, do_cliente))
        for i in range(1, len(instalacoes)):
            a, b = instalacoes[i - 1], instalacoes[i]
            if not (do_cliente[i - 1] or do_cliente[i]):
                continue
            # ponta "principal" do destino (a bomba que mais abasteceu) — é
            # nela que a estrela de troca da linha do tempo mostra a origem
            lb = max(b["linhas"], key=lambda r: r["litros"])
            tipo = TIPO_DENTRO if do_cliente[i - 1] and do_cliente[i] else (TIPO_ENTROU if do_cliente[i] else TIPO_SAIU)
            dias_parado = (b["inicio"] - a["fim"]).days
            if dias_parado < 0:
                # o serial abasteceu nos dois lugares no mesmo período (ex.:
                # 1 dia em Belford Roxo 1 no meio do uso em Nova Iguaçu): a
                # "saída" vem depois da chegada, então mostra só a chegada
                periodo = (f"{b['inicio']:%d/%m/%Y} (uso simultâneo — seguiu abastecendo na origem "
                           f"até {a['fim']:%d/%m/%Y})")
            else:
                parado = "no mesmo dia" if dias_parado == 0 else \
                    f"{dias_parado} {'dia' if dias_parado == 1 else 'dias'} parado"
                periodo = f"{a['fim']:%d/%m/%Y} → {b['inicio']:%d/%m/%Y} ({parado})"
            eventos.append({
                "serial": serial, "data": b["inicio"], "saida": a["fim"],
                "periodo": periodo,
                "tipo": tipo,
                "de": " + ".join(_rotulo_local(r, base, cliente_id) for r in a["linhas"])
                      + (f" [{_tipo_por_canais(a['canais'])}]" if a["canais"] > 1 else ""),
                "base_de": BASES_SISTEMA.get(a["chave"][0], a["chave"][0]),
                "para": " + ".join(_rotulo_local(r, base, cliente_id) for r in b["linhas"])
                        + (f" [{_tipo_por_canais(b['canais'])}]" if b["canais"] > 1 else ""),
                "base_para": BASES_SISTEMA.get(b["chave"][0], b["chave"][0]),
                "entre_bases": a["chave"][0] != b["chave"][0],
                "_bomba_destino": lb["bomba_id"],
                # pontas da seta da linha do tempo: a bomba que parou por
                # último na origem e a que começou primeiro no destino
                "_bomba_saida": max(a["linhas"], key=lambda r: r["fim"])["bomba_id"],
                "_bomba_chegada": min(b["linhas"], key=lambda r: r["inicio"])["bomba_id"],
            })
    ev = pd.DataFrame(eventos)
    if ev.empty:
        return pd.DataFrame(columns=colunas), percursos
    return ev.sort_values(["data", "serial"], ascending=[False, True]).reset_index(drop=True), percursos


def renderizar_transferencias(transferencias: pd.DataFrame, aviso: str = None):
    """Seção de transferências: QUANDO cada serial saiu de uma bomba e voltou a
    abastecer noutra — no cliente, vindo de outro cliente ou indo pra outro —
    e em que base. É o rastro das transferências que o processo não documenta."""
    st.markdown("**Transferências de equipamento** · quando um serial sai de uma bomba e volta a abastecer em outra")
    if aviso:
        st.caption(aviso)
    if transferencias is None or transferencias.empty:
        st.caption("Nenhum serial deste cliente passou por outra bomba, cliente ou base.")
        return
    t = transferencias
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entre bombas do cliente", int((t["tipo"] == TIPO_DENTRO).sum()))
    c2.metric("Entraram de outro cliente", int((t["tipo"] == TIPO_ENTROU).sum()))
    c3.metric("Saíram para outro cliente", int((t["tipo"] == TIPO_SAIU).sum()))
    c4.metric("Entre bases", int(t["entre_bases"].sum()))
    # base numa coluna só, logo depois do movimento: "CTA" quando não muda,
    # "CTA → Ipiranga Connect" quando muda (com a origem e o destino a tabela
    # fica larga e a base de destino ia pra fora da tela)
    bases = [o if o == d else f"{o} → {d}" for o, d in zip(t["base_de"], t["base_para"])]
    st.dataframe(
        t.assign(bases=bases)[["periodo", "serial", "tipo", "bases", "de", "para"]].rename(columns={
            "periodo": "Período da mudança (saída → reinstalação)", "serial": "Serial", "tipo": "Movimento",
            "bases": "Base", "de": "Saiu de", "para": "Foi para"}),
        use_container_width=True, hide_index=True,
    )
    st.caption("Saída = último abastecimento do serial na origem; reinstalação = primeiro abastecimento no destino. "
               "Passagem de menos de 50 L num lugar é tratada como teste e fica de fora. O percurso completo de cada "
               "serial, inclusive por outras bases antes de chegar ao cliente, está na tabela de equipamentos acima. "
               "Link2Pump não está no fato diário — passagem por lá não aparece.")


def contar_trocas_por_equipamento(trocas: pd.DataFrame) -> int:
    """Trocas de serial contadas uma vez por EQUIPAMENTO: num duplo os dois
    canais recebem o mesmo serial novo no mesmo período — é uma troca só."""
    if trocas is None or trocas.empty:
        return 0
    return int(trocas.drop_duplicates(["serial_anterior", "serial_novo"]).shape[0])


def alertas_duplo(resumo: pd.DataFrame) -> list:
    """Equipamento duplo de hoje com pagantes diferentes nos canais: é UM
    equipamento físico (uma licença de pedestal duplo) cobrado de dois
    pagantes — vale conferir na planilha do financeiro."""
    if "pagante_codigo" not in resumo:
        return []
    r = resumo[resumo["situacao"].isin(SITUACOES_ATIVAS)].dropna(subset=["equipamento_id"])
    linhas = []
    for _, g in r.groupby("equipamento_id"):
        pagantes = set(g["pagante_rotulo"].dropna())
        if len(g) > 1 and len(pagantes) > 1:
            linhas.append(f"**Equipamento {_tipo_por_canais(len(g))} (serial {g['serial_equipamento'].iloc[0]}) com "
                          f"pagantes diferentes nos canais:** "
                          + "; ".join(f"{rb} → {pg}" for rb, pg in zip(g["rotulo"], g["pagante_rotulo"])) + ".")
    return linhas


def alertas_id_operacional(resumo: pd.DataFrame) -> list:
    """ID operacional da alocação repetido em bombas de EQUIPAMENTOS
    diferentes (canais do mesmo equipamento dividem o ID, isso é normal) —
    ex.: São João de Meriti e Mangaratiba com o mesmo 1106920037 (23/10/2026)."""
    if "id_op" not in resumo:
        return []
    r = resumo.dropna(subset=["id_op"])
    linhas = []
    for id_op, g in r.groupby("id_op"):
        if g["equipamento_id"].nunique() > 1:
            linhas.append(f"**ID operacional {id_op} está em {len(g)} bombas de equipamentos diferentes:** "
                          + ", ".join(sorted(g["rotulo"])) + ". Na planilha só uma delas casa com esse ID.")
    return linhas


def anexar_alocacao(resumo: pd.DataFrame, alocacao: pd.DataFrame) -> pd.DataFrame:
    """Põe no resumo de bombas o pagante de hoje e o alerta de pagante de
    outra conta. Só vale para bomba instalada HOJE no cliente: a alocação é
    estado atual, e bomba retirada ou em outro cliente não tem o que mostrar."""
    resumo = resumo.drop(columns=[c for c in ("pagante_codigo", "pagante_nome", "pagante_outras_contas",
                                              "pagante_fora_do_grupo", "pagante_rotulo", "local_rotulo",
                                              "id_op") if c in resumo])
    ativa = resumo["situacao"].isin(["Instalada", "Bomba inativa no cadastro"])
    if alocacao is None or alocacao.empty:
        resumo["pagante_codigo"] = None
        resumo["pagante_nome"] = None
        resumo["pagante_outras_contas"] = None
        resumo["pagante_fora_do_grupo"] = False
        resumo["local_rotulo"] = None
        resumo["id_op"] = None
    else:
        aloc = alocacao.drop_duplicates("bomba_id").copy()
        raiz_pagante = aloc["pagante_cnpj"].fillna("").astype(str).str[:8]
        cliente_raiz = aloc["cliente_raiz"].fillna("").astype(str)
        aloc["pagante_fora_do_grupo"] = (aloc["pagante_outras_contas"].notna() & (cliente_raiz.str.len() == 8)
                                         & (raiz_pagante != cliente_raiz))
        for col in ("local_codigo", "local_nome", "id_op"):
            if col not in aloc:
                aloc[col] = None
        aloc["local_rotulo"] = [
            f"{str(n or 'Sem nome no CIGAM').strip()} (CIGAM {c})" if isinstance(c, str) and c else None
            for c, n in zip(aloc["local_codigo"], aloc["local_nome"])]
        resumo = resumo.merge(aloc[["bomba_id", "pagante_codigo", "pagante_nome", "pagante_outras_contas",
                                    "pagante_fora_do_grupo", "local_rotulo", "id_op"]], on="bomba_id", how="left")
        resumo.loc[~ativa.to_numpy(), ["pagante_codigo", "pagante_nome", "pagante_outras_contas",
                                       "local_rotulo", "id_op"]] = None
        resumo["pagante_fora_do_grupo"] = (resumo["pagante_fora_do_grupo"].fillna(False).astype(bool)
                                           & ativa.to_numpy())
    resumo["pagante_rotulo"] = [
        (f"{str(n or 'Sem nome no CIGAM').strip()} (CIGAM {c})" if isinstance(c, str) and c else
         ("Sem alocação CIGAM" if a else None))
        for c, n, a in zip(resumo["pagante_codigo"], resumo["pagante_nome"], ativa.to_numpy())
    ]
    return resumo


def gerar_dados_demo():
    """Diretório + parque fictícios no MESMO formato das consultas, pra
    testar a aba sem credenciais. Cobre: equipamento duplo (2 bombas no
    mesmo serial), bomba em comboio, troca de equipamento no meio do
    período, bomba retirada (sem equipamento) cujo serial é reinstalado
    noutra bomba do cliente (Filial → Comboio 02) e bomba nova."""
    import numpy as np

    diretorio = pd.DataFrame([
        {"base": "cta", "cliente_id": "900101", "cliente_nome": "TRANSPORTE DEMO LTDA", "cnpj": "12345678000100",
         "trr_nome": "TRR DEMO", "trr_interno": False, "qtd_bombas": 6, "qtd_bombas_com_equipamento": 5},
        {"base": "ipc", "cliente_id": "900102", "cliente_nome": "MINERAÇÃO DEMO S.A.", "cnpj": "98765432000155",
         "trr_nome": "TRR DEMO", "trr_interno": False, "qtd_bombas": 1, "qtd_bombas_com_equipamento": 0},
    ])
    diretorio["_nome_norm"] = diretorio["cliente_nome"].map(_norm_busca)

    base_bomba = {"bomba_ativa": True, "bomba_atualizada_em": pd.Timestamp("2025-03-11"), "posto_cidade": "Cidade Exemplo",
                  "posto_uf": "PR", "cliente_id_atual": "900101", "equipamento_descricao": None}
    cadastro = pd.DataFrame([
        {**base_bomba, "bomba_id": "5001", "bomba_nome": "Pedestal S10", "canal": 1, "equipamento_id": "71",
         "serial_equipamento": "4410071", "tanque_nome": "Tanque S10", "tanque_movel": False, "posto_nome": "Garagem Matriz"},
        {**base_bomba, "bomba_id": "5002", "bomba_nome": "Pedestal S500", "canal": 2, "equipamento_id": "71",
         "serial_equipamento": "4410071", "tanque_nome": "Tanque S500", "tanque_movel": False, "posto_nome": "Garagem Matriz"},
        {**base_bomba, "bomba_id": "5003", "bomba_nome": "Comboio 01", "canal": 1, "equipamento_id": "88",
         "serial_equipamento": "4410188", "tanque_nome": "Comboio 01", "tanque_movel": True, "posto_nome": "Obra Norte"},
        {**base_bomba, "bomba_id": "5004", "bomba_nome": "Pedestal Filial", "canal": 1, "equipamento_id": None,
         "serial_equipamento": None, "tanque_nome": "Tanque Filial", "tanque_movel": False, "posto_nome": "Filial Sul"},
        {**base_bomba, "bomba_id": "5005", "bomba_nome": "Comboio 02", "canal": 1, "equipamento_id": "93",
         "serial_equipamento": "4410293", "tanque_nome": "Comboio 02", "tanque_movel": True, "posto_nome": "Obra Norte"},
        {**base_bomba, "bomba_id": "5006", "bomba_nome": "Pedestal Arla", "canal": 1, "equipamento_id": "97",
         "serial_equipamento": "4410297", "tanque_nome": "Tanque Arla", "tanque_movel": False, "posto_nome": "Garagem Matriz",
         "bomba_ativa": False},
    ])

    rng = np.random.default_rng(42)
    # (bomba, início, fim, litros/dia médio, pausa opcional)
    perfis = [
        ("5001", "2022-03-01", "2026-09-30", 900, None),
        ("5002", "2022-03-01", "2026-09-30", 350, None),
        ("5003", "2023-06-10", "2026-09-30", 1400, ("2024-12-15", "2025-02-20")),
        ("5004", "2022-03-01", "2024-08-31", 500, None),
        ("5005", "2025-04-01", "2026-09-30", 1100, None),
        ("5006", "2024-01-15", "2025-11-30", 80, None),
        (None, "2022-03-01", "2026-09-30", 40, None),
    ]
    # (bomba, a partir de, serial) — 5001/5002 são o mesmo equipamento duplo
    # e trocam juntos; 5003 troca depois da pausa; 5004 foi retirada
    historico_serial = {
        "5001": [("2022-03-01", "4410055"), ("2024-05-22", "4410071")],
        "5002": [("2022-03-01", "4410055"), ("2024-05-22", "4410071")],
        "5003": [("2023-06-10", "4410099"), ("2025-03-10", "4410188")],
        "5004": [("2022-03-01", "4410040")],
        "5005": [("2025-04-01", "4410040"), ("2025-10-01", "4410293")],
        "5006": [("2024-01-15", "4410297")],
    }

    def _serial_no_dia(bomba, dia):
        atual = None
        for desde, serial in historico_serial.get(bomba, []):
            if dia >= pd.Timestamp(desde):
                atual = serial
        return atual

    linhas = []
    for bomba, ini, fim, media, pausa in perfis:
        dias = pd.date_range(ini, fim, freq="D")
        sazonal = 1 + 0.25 * np.sin(2 * np.pi * dias.dayofyear / 365)
        fds = np.where(dias.dayofweek >= 5, 0.35, 1.0)
        litros = rng.gamma(4, media / 4, len(dias)) * sazonal * fds
        ativo = rng.random(len(dias)) > 0.08
        if pausa:
            ativo &= ~((dias >= pausa[0]) & (dias <= pausa[1]))
        for d, l, a in zip(dias, litros, ativo):
            if a:
                linhas.append({"bomba_id": bomba, "data": d, "serial": _serial_no_dia(bomba, d),
                               "litros": round(float(l), 3), "qtd_abastecimentos": int(max(1, l // 120))})
    uso = pd.DataFrame(linhas)
    return diretorio, (cadastro, uso)


def gerar_chaves_demo() -> pd.DataFrame:
    """Ponte CIGAM ↔ sistema fictícia, no formato de montar_chaves_busca."""
    linhas = [
        ("cta", "900101", "cigam", "000101", "pagante", 3, "TRANSPORTE DEMO LTDA", "TRANSDEMO", "12345678000100"),
        ("cta", "900101", "cigam", "000101", "local", 5, "TRANSPORTE DEMO LTDA", "TRANSDEMO", "12345678000100"),
        ("cta", "900101", "cigam", "000202", "pagante", 1, "LOGÍSTICA DEMO S.A.", None, "22333444000155"),
        ("cta", "900101", "cigam", "000303", "pagante", 1, "DISTRIBUIDORA DEMO DE PETRÓLEO LTDA", None, "55666777000188"),
        ("ipc", "900102", "cigam_cnpj", "000404", "mesmo CNPJ", None, "MINERACAO DEMO S/A", None, "98765432000155"),
    ]
    linhas += [("cta", "900101", "bomba", b, None, None, None, None, None) for b in ["5001", "5002", "5003", "5004", "5005", "5006"]]
    linhas += [("cta", "900101", "serial", s, None, None, None, None, None) for s in ["4410071", "4410188", "4410293", "4410297"]]
    df = pd.DataFrame(linhas, columns=["base", "cliente_id", "tipo", "chave", "papel", "bombas", "nome", "fantasia", "cnpj"])
    df["_nome_norm"] = df["nome"].map(_norm_busca)
    df["_fantasia_norm"] = df["fantasia"].map(_norm_busca)
    df["_codigo_num"] = pd.to_numeric(df["chave"].where(df["tipo"].isin(["cigam", "cigam_cnpj"])),
                                      errors="coerce").astype("Int64")
    return df


def gerar_percurso_demo(cadastro: pd.DataFrame, uso: pd.DataFrame, alocacao) -> pd.DataFrame:
    """Percurso fictício dos seriais, no formato de percurso_dos_seriais:
    as passagens pelo próprio cliente (do uso demo) + de onde vieram e pra
    onde foram — inclusive um serial que vai pra outra base."""
    u = uso.dropna(subset=["bomba_id", "serial"])
    p = u.groupby(["bomba_id", "serial"], as_index=False).agg(
        inicio=("data", "min"), fim=("data", "max"), dias=("data", "nunique"), litros=("litros", "sum"))
    p["base"] = "cta"
    p = p.merge(cadastro[["bomba_id", "bomba_nome", "posto_nome"]], on="bomba_id", how="left")
    p["cliente_id"], p["cliente_nome"] = "900101", "TRANSPORTE DEMO LTDA"
    aloc = alocacao[["bomba_id", "pagante_codigo", "pagante_nome", "id_op"]] if alocacao is not None and "id_op" in alocacao \
        else pd.DataFrame(columns=["bomba_id", "pagante_codigo", "pagante_nome", "id_op"])
    p = p.merge(aloc, on="bomba_id", how="left")
    fora = pd.DataFrame([
        # de onde veio o serial do Comboio 01: outra construtora, outro pagante
        {"base": "cta", "bomba_id": "7001", "serial": "4410188", "inicio": "2023-01-10", "fim": "2025-02-20",
         "bomba_nome": "Construtora Exemplo - Comboio", "cliente_id": "700001", "cliente_nome": "CONSTRUTORA EXEMPLO",
         "pagante_codigo": "000777", "pagante_nome": "CONSTRUTORA EXEMPLO LTDA"},
        # pra onde foi o serial antigo do Comboio 01
        {"base": "cta", "bomba_id": "7002", "serial": "4410099", "inicio": "2025-04-01", "fim": "2026-09-30",
         "bomba_nome": "Construtora Exemplo - Pedestal", "cliente_id": "700001", "cliente_nome": "CONSTRUTORA EXEMPLO",
         "pagante_codigo": "000777", "pagante_nome": "CONSTRUTORA EXEMPLO LTDA"},
        # o serial antigo do equipamento duplo foi parar em OUTRA BASE
        {"base": "ipc", "bomba_id": "8001", "serial": "4410055", "inicio": "2024-07-01", "fim": "2026-09-30",
         "bomba_nome": "Posto Exemplo - Bomba 1", "cliente_id": "800001", "cliente_nome": "MINERAÇÃO EXEMPLO",
         "pagante_codigo": "000490", "pagante_nome": "IPIRANGA PROD DE PETROLEO S/A"},
    ])
    for col in ("inicio", "fim"):
        fora[col] = pd.to_datetime(fora[col])
    fora["dias"], fora["litros"], fora["posto_nome"], fora["id_op"] = 300, 90000.0, None, None
    return pd.concat([p, fora], ignore_index=True)


def gerar_alocacao_demo() -> pd.DataFrame:
    """Alocação CIGAM fictícia, no formato de alocacao_do_cliente: o
    próprio cliente paga a maior parte, uma empresa do grupo sem conta no
    sistema paga o comboio 01, e uma distribuidora com conta própria paga o
    comboio 02 (o caso que a aba sinaliza). Os comboios 01 e 02 dividem o
    mesmo ID operacional sem serem o mesmo equipamento (o outro alerta)."""
    proprio = ("000101", "TRANSPORTE DEMO LTDA", "12345678000100", None)
    ids_op = {"5001": "1000001", "5002": "1000001", "5006": "1000006", "5003": "1000003", "5005": "1000003"}
    return pd.DataFrame([
        {"bomba_id": b, "pagante_codigo": c, "pagante_nome": n, "pagante_cnpj": j, "pagante_outras_contas": o,
         "local_codigo": "000101", "local_nome": "TRANSPORTE DEMO LTDA", "cliente_raiz": "12345678",
         "id_op": ids_op[b]}
        for b, (c, n, j, o) in {
            "5001": proprio, "5002": proprio, "5006": proprio,
            "5003": ("000202", "LOGÍSTICA DEMO S.A.", "22333444000155", None),
            "5005": ("000303", "DISTRIBUIDORA DEMO DE PETRÓLEO LTDA", "55666777000188", "Distribuidora Demo (id 900303)"),
        }.items()
    ])


# --- 2. Busca --------------------------------------------------------------
def _casa_inicio_de_palavra(serie: pd.Series, termo_norm: str) -> pd.Series:
    """O termo casa só no INÍCIO de uma palavra: "ORIZON" acha "ORIZON
    SOROCABA", mas não "HORIZONTE" (mesma regra da busca de grupo)."""
    return serie.str.contains(r"(?<![A-Z0-9])" + re.escape(termo_norm), regex=True, na=False)


def _fmt_cigam(linha) -> str:
    nome = linha["nome"] if isinstance(linha["nome"], str) and linha["nome"] else "sem nome no CIGAM"
    return f"CIGAM {linha['chave']} {nome}"


def _motivo_chave(linha) -> tuple:
    """(prioridade, texto) do porquê de uma chave da ponte ter casado —
    prioridade menor aparece primeiro na lista de candidatos."""
    tipo = linha["tipo"]
    if tipo == "bomba":
        return 0, f"bomba #{linha['chave']}"
    if tipo == "id_op":
        return 0, f"ID operacional {linha['chave']}"
    if tipo == "serial":
        return 0, f"serial {linha['chave']} instalado aqui"
    if tipo == "cigam_cnpj":
        return 1, f"{_fmt_cigam(linha)} (mesmo CNPJ)"
    papel = "paga" if linha["papel"] == "pagante" else "é local de"
    return (2 if linha["papel"] == "pagante" else 3), f"{_fmt_cigam(linha)} {papel} {int(linha['bombas'])} bomba(s)"


def buscar_clientes_sistema(diretorio: pd.DataFrame, termo: str, chaves: pd.DataFrame = None) -> pd.DataFrame:
    """
    Acha o cliente do SISTEMA a partir do que o financeiro tiver na mão:
      - nome no CIGAM ou no sistema (início de palavra, sem acento/caixa/pontuação);
      - código CIGAM ("580" ou "000580") de quem paga ou é local das bombas;
      - CNPJ/CPF completo do cliente no sistema ou da empresa no CIGAM, ou só
        a raiz do CNPJ (8 dígitos);
      - cliente_id do sistema, nº da bomba, serial do equipamento ou ID
        operacional da alocação (o "ID" das planilhas do financeiro).
    Devolve TODOS os candidatos, com a coluna `_motivo` dizendo por que cada
    um apareceu — quem escolhe entre eles é o usuário, nunca a busca. Um
    número pode ser várias coisas ao mesmo tempo (id, bomba, serial,
    código CIGAM): entram todas.
    """
    termo = str(termo).strip()
    digitos = "".join(ch for ch in termo if ch.isdigit())
    so_numero = bool(digitos) and re.fullmatch(r"[\d.\-/\s]+", termo) is not None
    termo_norm = _norm_busca(termo)
    motivos = {}  # (base, cliente_id) -> [(prioridade, texto)]
    cigam_casados = {}  # (base, cliente_id) -> {código CIGAM que paga/é local de bomba dele}

    def _marcar(df, prioridade, texto):
        for base, cid, txt in zip(df["base"], df["cliente_id"], texto if not isinstance(texto, str) else [texto] * len(df)):
            motivos.setdefault((base, cid), []).append((prioridade, txt))

    if so_numero:
        _marcar(diretorio[diretorio["cliente_id"] == digitos], 0, "cliente_id do sistema")
        if len(digitos) in (11, 14):
            _marcar(diretorio[diretorio["cnpj"] == digitos], 0, "CNPJ do cliente no sistema")
        if len(digitos) == 8:
            _marcar(diretorio[diretorio["cnpj"].str[:8] == digitos], 1, "raiz do CNPJ no sistema")
    elif termo_norm:
        _marcar(diretorio[_casa_inicio_de_palavra(diretorio["_nome_norm"], termo_norm)], 0, "nome no sistema")

    if chaves is not None and not chaves.empty:
        if so_numero:
            casou = (chaves["tipo"].isin(["bomba", "serial", "id_op"]) & (chaves["chave"] == digitos))
            if len(digitos) <= 6:
                casou |= chaves["_codigo_num"] == int(digitos)
            if len(digitos) in (11, 14):
                casou |= chaves["tipo"].isin(["cigam", "cigam_cnpj"]) & (chaves["cnpj"] == digitos)
            if len(digitos) == 8:
                casou |= chaves["tipo"].isin(["cigam", "cigam_cnpj"]) & (chaves["cnpj"].str[:8] == digitos)
        elif termo_norm:
            casou = chaves["tipo"].isin(["cigam", "cigam_cnpj"]) & (
                _casa_inicio_de_palavra(chaves["_nome_norm"], termo_norm)
                | _casa_inicio_de_palavra(chaves["_fantasia_norm"], termo_norm))
        else:
            casou = pd.Series(False, index=chaves.index)
        achadas = chaves[casou.fillna(False)]
        for (base, cid), g in achadas.groupby(["base", "cliente_id"]):
            for _, linha in g.iterrows():
                motivos.setdefault((base, cid), []).append(_motivo_chave(linha))
                if linha["tipo"] == "cigam":
                    cigam_casados.setdefault((base, cid), set()).add(str(linha["chave"]))

    if not motivos:
        return diretorio.iloc[0:0].assign(_motivo="", _prioridade=0)
    chave = list(zip(diretorio["base"], diretorio["cliente_id"]))
    achados = diretorio[[k in motivos for k in chave]].copy()
    resumo_motivos = {}
    for k, lista in motivos.items():
        lista = sorted(set(lista))
        textos = [t for _, t in lista]
        resumo_motivos[k] = (lista[0][0], "; ".join(textos[:3]) + (f" (+{len(textos) - 3})" if len(textos) > 3 else ""))
    pares = list(zip(achados["base"], achados["cliente_id"]))
    achados["_prioridade"] = [resumo_motivos[k][0] for k in pares]
    achados["_motivo"] = [resumo_motivos[k][1] for k in pares]
    achados["_cigam"] = [tuple(sorted(cigam_casados.get(k, ()))) for k in pares]
    return achados.sort_values(["_prioridade", "qtd_bombas", "cliente_nome"], ascending=[True, False, True])


def _motivo_curto(motivo: str) -> str:
    """Motivo da busca sem o nome da empresa do CIGAM, que é o que a pessoa
    acabou de digitar: "CIGAM 000580 PETROVERA ... paga 2 bomba(s)" →
    "CIGAM 000580 paga 2 bomba(s)"."""
    texto = re.sub(r"(CIGAM \d+) [^;]*? (paga|é local de|\(mesmo CNPJ\))", r"\1 \2", str(motivo or ""))
    texto = re.sub(r"(CIGAM \d+) \(mesmo CNPJ\)", r"mesmo CNPJ do \1", texto)
    return texto.replace("nome no sistema", "nome").replace(" bomba(s)", "")


MAX_POSTOS_POR_CLIENTE_NA_BUSCA = 15
TODOS_OS_POSTOS = "— todos os postos —"


def _resumo_cidades(locais: list) -> str:
    """Cidades dos postos de um cliente num texto curto: "São Gonçalo,
    Paciência, Saquarema +6 (RJ)" quando é tudo da mesma UF, senão
    "Macaé/RJ, Itajubá/MG +2". Posto só com UF não entra (já está no fim)."""
    pares = list(dict.fromkeys((c, u) for c, u in locais if c or u))
    cidades = [(c, u) for c, u in pares if c]
    ufs = {u for _, u in pares if u}
    if not cidades:
        return ", ".join(sorted(ufs))
    if len(ufs) <= 1:
        nomes = list(dict.fromkeys(c for c, _ in cidades))
        txt = ", ".join(nomes[:3]) + (f" +{len(nomes) - 3}" if len(nomes) > 3 else "")
        return txt + (f" ({next(iter(ufs))})" if ufs else "")
    nomes = list(dict.fromkeys(f"{c}/{u}" if u else c for c, u in cidades))
    return ", ".join(nomes[:3]) + (f" +{len(nomes) - 3}" if len(nomes) > 3 else "")


def _fmt_equip(ativos: int, instalados: int) -> str:
    """"1 de 2" — equipamentos ativos (sinalizando) de instalados, a mesma
    régua do card "Equipamentos ativos hoje" e do filtro Posto."""
    return f"{ativos} de {instalados}" if instalados else "nenhum instalado"


def postos_dos_candidatos(candidatos: pd.DataFrame, cadastro: pd.DataFrame,
                          bombas_alocadas: pd.DataFrame = None, ultimo_uso: dict = None,
                          referencia=None) -> pd.DataFrame:
    """Resultado da busca POR POSTO: por cliente achado, uma linha "todos os
    N postos" e uma por posto, com cidade/UF e nº de bombas. Cliente de um
    posto só tem uma linha. Quando a busca casou por empresa do CIGAM (quem
    paga/é local), o posto mostra quantas bombas DELE essa empresa paga e
    esses postos vêm primeiro — é onde está o que a pessoa procurou.
    Cidade/UF fica em branco quando o nome do posto já diz o local.
    "Equipamentos ativos" = equipamentos (seriais distintos — um duplo conta
    uma vez) instalados no cadastro, e quantos deles abasteceram nos últimos
    DIAS_SINAL_ATIVO dias (`ultimo_uso`: (base, bomba_id) → último dia de
    uso) — a mesma conta do card da aba, pra não haver dois números."""
    ultimo_uso = ultimo_uso or {}
    limite = (pd.Timestamp(referencia).normalize() - pd.Timedelta(days=DIAS_SINAL_ATIVO)) if referencia is not None else None

    def _contar(base, g):
        inst = g["equipamento_id"].nunique()
        if limite is None:
            return 0, inst
        ativos = {e for e, b in zip(g["equipamento_id"], g["bomba_id"])
                  if (u := ultimo_uso.get((base, b))) is not None and u >= limite}
        return len(ativos), inst
    cad = cadastro.copy() if cadastro is not None else pd.DataFrame()
    if not cad.empty:
        cad = cad[cad["equipamento_id"].notna()].copy()
        cad["cliente_id"] = cad["cliente_id"].astype(str)
        cad["bomba_id"] = cad["bomba_id"].astype(str)
        cad["_posto"] = [rotulo_posto(n, c, u) for n, c, u in zip(cad["posto_nome"], cad["posto_cidade"], cad["posto_uf"])]
        cad["_cid"] = cad["posto_cidade"].map(_cidade_legivel)
        cad["_uf"] = cad["posto_uf"].map(lambda u: u.strip().upper() if isinstance(u, str) and u.strip().upper()
                                         in UFS_BRASIL else "")
    aloc = {}
    if bombas_alocadas is not None and not bombas_alocadas.empty:
        for base, b, pg, lc in zip(bombas_alocadas["base"], bombas_alocadas["bomba_id"].astype(str),
                                   bombas_alocadas["cliente_cigam_pagante"], bombas_alocadas["cliente_cigam_local"]):
            aloc[(base, b)] = tuple(f"{int(c):06d}" if pd.notna(c) and str(c).strip().isdigit() else None for c in (pg, lc))
    linhas = []
    for i, (_, cand) in enumerate(candidatos.iterrows()):
        empresa = f"{cand['cliente_nome']} · {BASES_SISTEMA.get(cand['base'], cand['base'])}"
        motivo = _motivo_curto(cand.get("_motivo"))
        codigos = set(cand.get("_cigam") or ())
        quem = ("CIGAM " + "/".join(sorted(codigos))) if codigos else ""
        g = cad[(cad["base"] == cand["base"]) & (cad["cliente_id"] == str(cand["cliente_id"]))] if not cad.empty else cad
        postos = []
        for (rot, nome), gp in (g.groupby(["_posto", "posto_nome"], dropna=False) if not g.empty else []):
            pagas = locais = 0
            for b in gp["bomba_id"].unique():
                pg, lc = aloc.get((cand["base"], b), (None, None))
                pagas += pg in codigos
                locais += lc in codigos and pg not in codigos
            cid, uf = gp["_cid"].iloc[0], gp["_uf"].iloc[0]
            nome_curto = _sem_prefixo_do_cliente(nome, cand["cliente_nome"]) or rot
            ativos, inst = _contar(cand["base"], gp)
            postos.append({"rot": rot, "nome": nome_curto, "cid": cid, "uf": uf, "bombas": inst,
                           "equip": _fmt_equip(ativos, inst),
                           "hits": pagas + locais,
                           "local": "" if _nome_ja_tem_local(nome_curto, cid, uf) else "/".join(x for x in (cid, uf) if x),
                           "liga": "; ".join(x for x in (
                               f"{quem} paga {pagas} aqui" if pagas else "", f"{quem} é local de {locais} aqui" if locais else "")
                               if x)})
        postos.sort(key=lambda p: (-p["hits"], -p["bombas"], p["nome"]))
        equip_cliente = _fmt_equip(*_contar(cand["base"], g)) if not g.empty else _fmt_equip(0, 0)
        if len(postos) == 1:
            # um posto só: o cabeçalho já mostra o posto; a linha do posto vem
            # embaixo (aparece só quando o cliente é aberto nos botões)
            p = postos[0]
            linhas.append({"_i": i, "_posto": None, "Empresa (cliente no sistema)": empresa, "Posto": p["nome"],
                           "Cidade/UF": p["local"], "Equipamentos ativos": p["equip"],
                           "Por que apareceu": motivo})
            linhas.append({"_i": i, "_posto": p["rot"], "Empresa (cliente no sistema)": "", "Posto": "   " + p["nome"],
                           "Cidade/UF": p["local"], "Equipamentos ativos": p["equip"], "Por que apareceu": p["liga"]})
            continue
        linhas.append({"_i": i, "_posto": None, "Empresa (cliente no sistema)": empresa,
                       "Posto": f"Todos os {len(postos)} postos" if postos else "Sem bomba instalada",
                       "Cidade/UF": _resumo_cidades([(p["cid"], p["uf"]) for p in postos]),
                       "Equipamentos ativos": equip_cliente, "Por que apareceu": motivo})
        for p in postos[:MAX_POSTOS_POR_CLIENTE_NA_BUSCA]:
            linhas.append({"_i": i, "_posto": p["rot"], "Empresa (cliente no sistema)": "", "Posto": "   " + p["nome"],
                           "Cidade/UF": p["local"], "Equipamentos ativos": p["equip"], "Por que apareceu": p["liga"]})
        if len(postos) > MAX_POSTOS_POR_CLIENTE_NA_BUSCA:
            linhas.append({"_i": i, "_posto": None, "Empresa (cliente no sistema)": "",
                           "Posto": f"   … mais {len(postos) - MAX_POSTOS_POR_CLIENTE_NA_BUSCA} postos (abra 'Todos' e "
                                    "use o filtro Posto)", "Cidade/UF": "", "Equipamentos ativos": "",
                           "Por que apareceu": ""})
    return pd.DataFrame(linhas, columns=["_i", "_posto", "Empresa (cliente no sistema)", "Posto", "Cidade/UF",
                                         "Equipamentos ativos", "Por que apareceu"])


# --- 3. Processamento ------------------------------------------------------
def _rotulo_bomba(bomba_id, nome) -> str:
    nome = str(nome).strip() if nome and str(nome).strip() and str(nome) != "None" else "Bomba"
    return f"{nome} (#{bomba_id})"


def _situacao(linha, cliente_id: str) -> str:
    if pd.isna(linha.get("cliente_id_atual")) and pd.isna(linha.get("bomba_nome")):
        return "Fora do cadastro atual"
    if pd.isna(linha.get("equipamento_id")):
        return "Sem equipamento (retirado)"
    if str(linha.get("cliente_id_atual")) != str(cliente_id):
        return "Hoje em outro cliente"
    ativa = linha.get("bomba_ativa")
    if not pd.isna(ativa) and not bool(ativa):
        return "Bomba inativa no cadastro"
    return "Instalada"


def seriais_por_bomba(uso_b: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por (bomba, serial): primeiro e último dia em que esse
    serial abasteceu na bomba, dias com uso e litros. Abastecimento sem
    serial não entra."""
    com_serial = uso_b.dropna(subset=["serial"])
    if com_serial.empty:
        return pd.DataFrame(columns=["bomba_id", "serial", "primeiro", "ultimo", "dias", "litros"])
    return com_serial.groupby(["bomba_id", "serial"], as_index=False).agg(
        primeiro=("data", "min"), ultimo=("data", "max"), dias=("data", "nunique"), litros=("litros", "sum"),
    ).sort_values(["bomba_id", "primeiro"]).reset_index(drop=True)


def derivar_trocas(por_serial: pd.DataFrame) -> pd.DataFrame:
    """Troca de serial = primeiro dia em que um serial NOVO aparece na bomba
    (o primeiro serial de cada bomba não é troca). É a mesma regra do
    gráfico "Monitoramento de Serial" do relatório de abastecimentos: um
    pino na primeira aparição de cada serial. Serial que volta pra uma
    bomba onde já esteve não gera pino de novo."""
    linhas = []
    for bomba, g in por_serial.groupby("bomba_id"):
        g = g.sort_values("primeiro")
        anteriores = g["serial"].tolist()
        for i in range(1, len(g)):
            linhas.append({"bomba_id": bomba, "data": g["primeiro"].iloc[i],
                           "serial_anterior": anteriores[i - 1], "serial_novo": anteriores[i]})
    return pd.DataFrame(linhas, columns=["bomba_id", "data", "serial_anterior", "serial_novo"])


# o que no nome do tanque/bomba indica comboio: a palavra, caminhão, "CB"/"CMB",
# "móvel" ou uma placa de veículo ("NVN9D01", "AZJ-5J83", "PLACA: JVR-4G96")
_NOME_DIZ_MOVEL = re.compile(r"COMBOIO|CAMINHAO|\bCB\b|\bCMB|MOVEL|\b[A-Z]{3}[ -]?[0-9][A-Z0-9][0-9]{2}\b")
_NOME_DIZ_FIXO = re.compile(r"FIXO|FIXA|PEDESTAL")


def instalacao_com_adendo(movel, tanque_nome, bomba_nome, posto_nome) -> str:
    """Instalação como está no CADASTRO do sistema (móvel = tanque registrado
    como comboio), com um adendo quando o nome do tanque/bomba aponta o
    contrário — o app não corrige a marca, só avisa que vale conferir:
      - móvel, mas o nome diz fixo/pedestal ou o tanque tem o nome do posto
        e nada de comboio/placa ("PIRECAL Guarani/GO") → "nome sugere fixo";
      - fixa, mas o nome fala em comboio, caminhão ou placa → "nome sugere comboio"."""
    if movel is None or (isinstance(movel, float) and pd.isna(movel)):
        return "—"
    nomes = _normalizar_texto(f"{tanque_nome or ''} {bomba_nome or ''}")
    diz_movel = bool(_NOME_DIZ_MOVEL.search(nomes))
    if bool(movel):
        tanque_e_posto = (isinstance(tanque_nome, str) and isinstance(posto_nome, str) and tanque_nome.strip()
                          and re.findall(r"[A-Z0-9]+", _normalizar_texto(tanque_nome))
                          == re.findall(r"[A-Z0-9]+", _normalizar_texto(posto_nome)))
        if not diz_movel and (_NOME_DIZ_FIXO.search(nomes) or tanque_e_posto):
            return "Móvel (cadastro) · nome sugere fixo"
        return "Móvel (comboio)"
    if diz_movel and not _NOME_DIZ_FIXO.search(nomes):
        return "Fixa (cadastro) · nome sugere comboio"
    return "Fixa (pedestal)"


def _fmt_mes_curto(d) -> str:
    return f"{d:%m/%y}"


def montar_resumo_bombas(cadastro: pd.DataFrame, uso: pd.DataFrame, por_serial: pd.DataFrame,
                         trocas: pd.DataFrame, cliente_id: str) -> pd.DataFrame:
    """1 linha por bomba: cadastro atual + período e volume de uso + seriais
    que passaram por ela."""
    uso_b = uso.dropna(subset=["bomba_id"])
    agg = uso_b.groupby("bomba_id").agg(
        primeiro_abastecimento=("data", "min"),
        ultimo_abastecimento=("data", "max"),
        dias_com_uso=("data", "nunique"),
        litros_total=("litros", "sum"),
        qtd_abastecimentos=("qtd_abastecimentos", "sum"),
    ).reset_index()
    tr = trocas.groupby("bomba_id").size().rename("trocas_serial").reset_index()
    historico = por_serial.groupby("bomba_id").apply(
        lambda g: " → ".join(f"{s} ({_fmt_mes_curto(a)}–{_fmt_mes_curto(b)})"
                             for s, a, b in zip(g["serial"], g["primeiro"], g["ultimo"])),
        include_groups=False,
    ).rename("historico_seriais").reset_index() if not por_serial.empty else \
        pd.DataFrame(columns=["bomba_id", "historico_seriais"])

    resumo = (cadastro.merge(agg, on="bomba_id", how="outer")
              .merge(tr, on="bomba_id", how="left").merge(historico, on="bomba_id", how="left"))
    resumo["trocas_serial"] = resumo["trocas_serial"].fillna(0).astype(int)
    resumo["litros_total"] = resumo["litros_total"].fillna(0.0)
    resumo["dias_com_uso"] = resumo["dias_com_uso"].fillna(0).astype(int)
    resumo["media_por_dia_de_uso"] = (resumo["litros_total"] / resumo["dias_com_uso"].where(resumo["dias_com_uso"] > 0)).round(1)
    # "Nome - Cidade/UF (#id)": cidade e UF do posto no cadastro do sistema
    # (ver sufixo_local) — o mesmo modelo "Posto - Cidade/UF" do financeiro
    vazio = pd.Series(None, index=resumo.index, dtype=object)
    resumo["rotulo"] = [
        _rotulo_bomba(b, (n.strip() + sufixo_local(n, c, u)) if isinstance(n, str) and n.strip() else n)
        for b, n, c, u in zip(resumo["bomba_id"], resumo["bomba_nome"],
                              resumo.get("posto_cidade", vazio), resumo.get("posto_uf", vazio))]
    resumo["situacao"] = resumo.apply(lambda l: _situacao(l, cliente_id), axis=1)
    resumo["instalacao"] = [instalacao_com_adendo(m, t, n, p) for m, t, n, p in zip(
        resumo["tanque_movel"], resumo.get("tanque_nome", pd.Series(None, index=resumo.index, dtype=object)),
        resumo["bomba_nome"], resumo.get("posto_nome", pd.Series(None, index=resumo.index, dtype=object)))]
    # tipo do equipamento instalado HOJE: nº de bombas ativas do cliente com o
    # mesmo equipamento_id (2 = duplo), e o canal desta bomba nele
    ativa = resumo["situacao"].isin(["Instalada", "Bomba inativa no cadastro"]) & resumo["equipamento_id"].notna()
    n_canais = resumo[ativa].groupby("equipamento_id")["bomba_id"].transform("nunique")
    resumo["equipamento_tipo"] = None
    resumo.loc[ativa, "equipamento_tipo"] = [
        _tipo_por_canais(n).capitalize() + (f" · canal {int(c)}" if n > 1 and pd.notna(c) else "")
        for n, c in zip(n_canais, resumo.loc[ativa, "canal"])]
    return resumo.sort_values(["litros_total", "rotulo"], ascending=[False, True]).reset_index(drop=True)


def montar_resumo_equipamentos(por_serial: pd.DataFrame, resumo: pd.DataFrame) -> pd.DataFrame:
    """1 linha por SERIAL (equipamento): tipo (simples/duplo pela régua de
    agrupar_instalacoes), por onde passou no cliente — duplo aparece como UMA
    instalação com os canais juntos, mudança de bomba aparece com "→" —,
    primeiro e último uso, volume, e se ainda está instalado hoje numa bomba
    do cliente (pelo cadastro atual)."""
    if por_serial.empty:
        return pd.DataFrame()
    rotulos = resumo.set_index("bomba_id")["rotulo"].to_dict()
    instalado = resumo[resumo["situacao"].isin(["Instalada", "Bomba inativa no cadastro"])]
    bombas_hoje = instalado.dropna(subset=["serial_equipamento"]).groupby("serial_equipamento")["rotulo"].apply(
        lambda s: ", ".join(sorted(s))).to_dict()
    p = por_serial.rename(columns={"primeiro": "inicio", "ultimo": "fim"}).assign(base="", cliente_id="")
    linhas = []
    for serial, g in p.groupby("serial"):
        insts = agrupar_instalacoes(g)
        onde = " → ".join(
            (f"{_tipo_por_canais(i['canais'])}: " if i["canais"] > 1 else "")
            + " + ".join(rotulos.get(r["bomba_id"], r["bomba_id"]) for r in i["linhas"])
            + f" ({i['inicio']:%m/%y}–{i['fim']:%m/%y})"
            for i in insts)
        # tipo = como o equipamento está/esteve na ÚLTIMA instalação; se já
        # foi diferente antes (duplo que virou simples), diz entre parênteses
        atual, maximo = insts[-1]["canais"], max(i["canais"] for i in insts)
        tipo = _tipo_por_canais(atual).capitalize() + \
            (f" (foi {_tipo_por_canais(maximo)} antes)" if maximo != atual else "")
        linhas.append({"serial": serial, "tipo": tipo, "onde": onde,
                       "primeiro": g["inicio"].min(), "ultimo": g["fim"].max(), "litros": g["litros"].sum()})
    eq = pd.DataFrame(linhas)
    eq["situacao"] = eq["serial"].map(
        lambda s: f"Instalado hoje em {bombas_hoje[s]}" if s in bombas_hoje else "Não está mais nas bombas do cliente")
    return eq.sort_values(["ultimo", "litros"], ascending=[False, False]).reset_index(drop=True)


def _agregar(uso_b: pd.DataFrame, granularidade: str) -> pd.DataFrame:
    """Soma por bomba × período e preenche com 0 os períodos SEM
    abastecimento entre o primeiro e o último uso da bomba — fora desse
    intervalo a bomba não existe pro cliente, então fica sem ponto."""
    freq = GRANULARIDADES[granularidade]
    df = uso_b.copy()
    df["periodo"] = df["data"].dt.to_period(freq).dt.start_time
    df = df.groupby(["bomba_id", "periodo"], as_index=False)["litros"].sum()
    preenchidos = []
    for bomba, g in df.groupby("bomba_id"):
        faixa = pd.period_range(g["periodo"].min(), g["periodo"].max(), freq=freq).start_time
        g = g.set_index("periodo")["litros"].reindex(faixa, fill_value=0.0).rename_axis("periodo").reset_index()
        g["bomba_id"] = bomba
        preenchidos.append(g)
    return pd.concat(preenchidos, ignore_index=True) if preenchidos else df


def _periodos_de_uso(uso_b: pd.DataFrame, resumo: pd.DataFrame) -> pd.DataFrame:
    """Faixas contínuas de uso por bomba E serial pra timeline: cada serial
    vira um segmento, quebrado quando a bomba fica mais de
    DIAS_SEM_USO_QUEBRA_PERIODO dias sem abastecer com ele."""
    info = resumo.set_index("bomba_id")
    df = uso_b.copy()
    df["serial"] = df["serial"].fillna("")
    linhas = []
    for (bomba, serial), g in df.groupby(["bomba_id", "serial"]):
        dias = g.groupby("data")["litros"].sum().sort_index()
        novo = (dias.index.to_series().diff().dt.days > DIAS_SEM_USO_QUEBRA_PERIODO).to_numpy()
        serial_hoje = info.at[bomba, "serial_equipamento"] if bomba in info.index else None
        situacao = info.at[bomba, "situacao"] if bomba in info.index else "Fora do cadastro atual"
        if not serial:
            categoria = "Sem serial no abastecimento"
        elif serial == serial_hoje and situacao in ("Instalada", "Bomba inativa no cadastro"):
            categoria = "Serial instalado hoje"
        else:
            categoria = "Serial anterior"
        for _, seg in dias.groupby(novo.cumsum()):
            inicio, fim = seg.index.min(), seg.index.max()
            linhas.append({
                "bomba_id": bomba, "serial": serial, "inicio": inicio, "fim": fim + pd.Timedelta(days=1),
                "fim_real": fim, "litros": seg.sum(), "dias": len(seg), "categoria": categoria,
            })
    return pd.DataFrame(linhas)


# --- 4. Gráficos -----------------------------------------------------------
def _layout_base(fig, altura: int):
    fig.update_layout(
        template="plotly_dark", height=altura,
        paper_bgcolor=COR_FUNDO_GRAFICO, plot_bgcolor=COR_FUNDO_GRAFICO,
        font=dict(color=COR_TEXTO_GRAFICO, family="system-ui, -apple-system, Segoe UI, sans-serif"),
        hoverlabel=dict(bgcolor="#1E1E2C", font=dict(color=COR_TEXTO_GRAFICO)),
    )
    fig.update_xaxes(gridcolor=COR_GRADE, linecolor=COR_GRADE, zeroline=False)
    fig.update_yaxes(gridcolor=COR_GRADE, linecolor=COR_GRADE, zeroline=False)


def plotar_volume(uso_b, trocas, resumo, selecionadas, cores, granularidade, ano):
    """Volume por bomba ao longo do tempo (linha) + estrela em cada troca de
    serial — versão DW do gráfico 'Variação Diária e Monitoramento de
    Serial'."""
    serie = _agregar(uso_b[uso_b["bomba_id"].isin(selecionadas)], granularidade)
    rotulos = resumo.set_index("bomba_id")["rotulo"].to_dict()
    seriais = resumo.set_index("bomba_id")["serial_equipamento"].to_dict()
    freq = GRANULARIDADES[granularidade]
    unidade = {"Dia": "dia", "Semana": "semana", "Mês": "mês"}[granularidade]
    fmt_x = "%d/%m/%Y" if granularidade != "Mês" else "%m/%Y"

    fig = go.Figure()
    for bomba in selecionadas:
        s = serie[serie["bomba_id"] == bomba].sort_values("periodo")
        cor = cores[bomba]
        rotulo = rotulos.get(bomba, bomba)
        serial = seriais.get(bomba)
        serial_txt = serial if isinstance(serial, str) and serial else "sem equipamento hoje"
        fig.add_trace(go.Scatter(
            x=s["periodo"], y=s["litros"], name=rotulo, legendgroup=bomba,
            mode="lines+markers" if granularidade != "Dia" else "lines",
            line=dict(width=2, color=cor), marker=dict(size=6),
            customdata=[serial_txt] * len(s),
            hovertemplate=f"<b>{rotulo}</b> · %{{y:,.0f}} L<extra></extra>",
        ))

        tb = trocas[trocas["bomba_id"] == bomba]
        if not tb.empty:
            periodo_troca = tb["data"].dt.to_period(freq).dt.start_time
            y = periodo_troca.map(s.set_index("periodo")["litros"]).fillna(0.0)
            textos = [
                f"<b>Novo serial</b><br>{rotulo}<br>{d:%d/%m/%Y}<br>Serial: {ant} → <b>{novo}</b>"
                for d, ant, novo in zip(tb["data"], tb["serial_anterior"], tb["serial_novo"])
            ]
            fig.add_trace(go.Scatter(
                x=periodo_troca, y=y, name=f"Trocas de serial — {rotulo}", legendgroup=bomba, showlegend=False,
                mode="markers", hovertext=textos, hoverinfo="text",
                marker=dict(symbol="star", size=16, color=COR_FUNDO_GRAFICO, line=dict(width=2, color=cor)),
            ))

    _layout_base(fig, 560)
    fig.update_layout(
        hovermode="x unified",
        margin=dict(l=60, r=30, t=70, b=40),
        legend=dict(orientation="h", yanchor="top", y=-0.32, xanchor="center", x=0.5, title_text=""),
        xaxis=dict(
            hoverformat=fmt_x,
            rangeslider=dict(visible=True, thickness=0.06, bgcolor="#1E1E2C"),
            rangeselector=dict(
                buttons=[
                    dict(count=7, label="7 dias", step="day", stepmode="backward"),
                    dict(count=1, label="1 mês", step="month", stepmode="backward"),
                    dict(count=6, label="6 meses", step="month", stepmode="backward"),
                    dict(count=1, label="1 ano", step="year", stepmode="backward"),
                    dict(step="all", label="Tudo"),
                ],
                bgcolor="#33334A", activecolor="#4C4C6A", font=dict(color=COR_TEXTO_GRAFICO),
                x=0, y=1.08,
            ),
        ),
        yaxis=dict(ticksuffix=" L", title=f"Volume por {unidade}", rangemode="tozero"),
    )
    if ano is not None:
        fig.update_xaxes(range=[f"{ano}-01-01", f"{ano}-12-31"])
    return fig


SITUACOES_ATIVAS = ("Instalada", "Bomba inativa no cadastro")
GRUPO_OUTRO_CLIENTE = "Hoje em outro cliente"
GRUPO_FORA_CADASTRO = "Fora do cadastro atual"


def _truncar(texto, limite: int) -> str:
    texto = "" if texto is None or (isinstance(texto, float) and pd.isna(texto)) else str(texto).strip()
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


GRUPO_INSTALADAS = "Instaladas hoje"
GRUPO_RETIRADAS = "Retiradas (sem equipamento hoje)"
SEM_ALOCACAO = "Sem alocação CIGAM"


def rotulo_pagante_curto(rotulo, limite: int = 34) -> str:
    """"NOME LONGO LTDA. (CIGAM 003435)" → corta o NOME, nunca o código —
    é o código que o financeiro procura."""
    rotulo = str(rotulo or "")
    if " (CIGAM " in rotulo and rotulo.endswith(")"):
        nome, codigo = rotulo.rsplit(" (CIGAM ", 1)
        return f"{_truncar(nome, limite)} (CIGAM {codigo}"
    return _truncar(rotulo, limite + 14)


_SEPARADORES = " -–—:/|·,."


def _texto_alinhado(texto: str) -> str:
    """Versão sem acento e maiúscula do texto com o MESMO comprimento (1
    caractere por caractere), pra achar um trecho ignorando acento/caixa e
    cortar a mesma posição no texto original."""
    return "".join((unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode().upper() or "?")[:1]
                   for c in texto)


def _sem_trecho(texto, trecho, so_no_inicio: bool = False) -> str:
    """Tira `trecho` de `texto` ignorando acento, caixa, espaço e pontuação
    entre as palavras ("PIRECAL- Vila Propício/GO" casa com "Pirecal - Vila
    Propicio/GO") e limpa os separadores que sobram. Só palavra inteira."""
    texto = "" if texto is None or (isinstance(texto, float) and pd.isna(texto)) else str(texto).strip()
    palavras = re.findall(r"[A-Z0-9]+", _normalizar_texto(trecho))
    if not texto or not palavras:
        return texto
    padrao = r"(?<![A-Z0-9])" + r"[^A-Z0-9]*".join(map(re.escape, palavras)) + r"(?![A-Z0-9])"
    m = re.search(("^[" + re.escape(_SEPARADORES) + "]*" if so_no_inicio else "") + padrao, _texto_alinhado(texto))
    if not m:
        return texto
    resto = texto[:m.start()] + " " + texto[m.end():]
    resto = re.sub(r"\s+", " ", resto)
    resto = re.sub(r"(^|\s)/\s*[A-Za-z]{2}\b", r"\1", resto)       # "/SP" que sobrou da cidade tirada
    resto = re.sub(r"\s*[-–—]\s*(?:[-–—]\s*)+", " - ", resto)   # "A -  - B" → "A - B"
    return resto.strip(_SEPARADORES + " ")


_SUFIXOS_SOCIETARIOS = frozenset({"LTDA", "SA", "S", "A", "EIRELI", "ME", "EPP", "CIA", "E", "DE", "DO", "DA"})


def _palavras_do_cliente(nome_cliente) -> frozenset:
    return frozenset(re.findall(r"[A-Z0-9]+", _normalizar_texto(nome_cliente))) | _SUFIXOS_SOCIETARIOS


def _sem_prefixo_do_cliente(nome, nome_cliente) -> str:
    """Tira do começo do nome da bomba/posto as palavras que são do nome do
    cliente ("RODOZINHO TRANSPORTES - Rio Negro/SC" → "Rio Negro/SC";
    "PIRECAL Guarani/GO" → "Guarani/GO"), ignorando acento, caixa e
    pontuação. Só corta se a primeira palavra é do cliente e ao menos uma
    das cortadas o identifica (não é "LTDA", "DE"...). Se não sobra nada,
    devolve o nome como estava."""
    nome = "" if nome is None or (isinstance(nome, float) and pd.isna(nome)) else str(nome).strip()
    if not nome or not isinstance(nome_cliente, str) or not nome_cliente.strip():
        return nome
    palavras = _palavras_do_cliente(nome_cliente)
    fim_corte, identifica = None, False
    for m in re.finditer(r"[A-Z0-9]+", _texto_alinhado(nome)):
        if m.group() not in palavras:
            break
        fim_corte = m.end()
        identifica |= m.group() not in _SUFIXOS_SOCIETARIOS and len(m.group()) >= 3
    if fim_corte is None or not identifica:
        return nome
    resto = nome[fim_corte:].strip(_SEPARADORES + " ")
    return resto or nome


def nome_curto_bomba(nome, nome_cliente, nome_posto=None) -> str:
    """Nome da bomba sem o que a tela já mostra ao lado: o cliente (título da
    aba) e, quando informado, o posto (rótulo do bloco), em qualquer ponto do
    nome — "CL1875 - Pirecal Taguatinga/TO" no posto "PIRECAL Taguatinga/TO"
    vira "CL1875"; "Tietê/SP - Comboio S10" no posto "Tietê/SP - Matriz",
    "Comboio S10". Pode voltar vazio: a bomba se chama como o posto."""
    def _palavras(t):
        return re.findall(r"[A-Z0-9]+", _normalizar_texto(t))

    curto = _sem_prefixo_do_cliente(nome, nome_cliente)
    if isinstance(nome_posto, str) and nome_posto.strip():
        posto_curto = _sem_prefixo_do_cliente(nome_posto, nome_cliente)
        if _palavras(curto) == _palavras(posto_curto):
            return ""
        # o posto inteiro, e depois cada pedaço dele ("Tietê/SP", "Matriz"),
        # inclusive sem o "/UF" ("São Gonçalo 1" no posto "São Gonçalo/RJ")
        pedacos = [posto_curto] + [x.strip() for x in re.split(r"\s+[-–—]\s+", posto_curto)]
        pedacos += [re.sub(r"\s*/\s*[A-Za-z]{2}$", "", x) for x in pedacos]
        for pedaco in dict.fromkeys(pedacos):
            if len(re.sub(r"[^A-Za-z]", "", pedaco)) >= 3:
                curto = _sem_trecho(curto, pedaco)
        if not curto:
            return ""
    # pedaço (entre " - ") que só tem palavras do cliente: "CL1875 - Pirecal" → "CL1875"
    palavras_cli = _palavras_do_cliente(nome_cliente)
    pedacos = re.split(r"\s+[-–—]\s+", curto)
    mantidos = [x for x in pedacos if not (_palavras(x) and set(_palavras(x)) <= palavras_cli)]
    return " - ".join(mantidos) if mantidos else curto


def rotulo_bloco_posto(nome_posto, nome_cliente, cidade=None, uf=None) -> str:
    """Rótulo do bloco de um posto na linha do tempo: o nome sem o cliente e,
    quando o nome não diz onde fica, " - Cidade/UF" do cadastro do posto."""
    curto = _sem_prefixo_do_cliente(nome_posto, nome_cliente)
    return curto + sufixo_local(nome_posto, cidade, uf)


def grupos_das_bombas(resumo: pd.DataFrame) -> dict:
    """bomba_id → (grupo, posição do grupo). Grupo = PAGANTE de hoje (com ⚠
    se sinalizado) para bomba instalada; "Retiradas", "Hoje em outro cliente"
    ou "Fora do cadastro atual" para as demais. É a mesma régua no filtro de
    pagante e na linha do tempo."""
    info = resumo.set_index("bomba_id")
    tem_alocacao = "pagante_rotulo" in resumo
    ordem_pagantes = {p: i for i, p in enumerate(cores_pagantes(resumo))} if tem_alocacao else {}
    grupos = {}
    for bomba in info.index:
        situacao = info.at[bomba, "situacao"]
        if situacao in SITUACOES_ATIVAS:
            pagante = (info.at[bomba, "pagante_rotulo"] or SEM_ALOCACAO) if tem_alocacao else GRUPO_INSTALADAS
            rank = ordem_pagantes.get(pagante, len(ordem_pagantes))
            fora = bool(info.at[bomba, "pagante_fora_do_grupo"]) if tem_alocacao else False
            grupo = ("⚠ " if fora else "") + (rotulo_pagante_curto(pagante) if tem_alocacao else pagante)
        elif situacao in (GRUPO_OUTRO_CLIENTE, GRUPO_FORA_CADASTRO):
            grupo, rank = situacao, 1000 + (situacao == GRUPO_FORA_CADASTRO)
        else:
            grupo, rank = GRUPO_RETIRADAS, 999
        grupos[bomba] = (grupo, rank)
    return grupos


def rotulo_de_busca(linha) -> str:
    """Rótulo da bomba no filtro: nome, nº da bomba, serial instalado hoje e
    ID operacional — o filtro do Streamlit busca no texto do rótulo, então é
    isso que deixa achar a bomba pelo serial ou pelo ID da planilha."""
    partes = [linha["rotulo"]]
    serial = linha.get("serial_equipamento")
    partes.append(f"serial {serial}" if isinstance(serial, str) and serial else "sem equipamento hoje")
    id_op = linha.get("id_op")
    if isinstance(id_op, str) and id_op:
        partes.append(f"ID {id_op}")
    return " · ".join(partes)


def marcar_sinal(resumo: pd.DataFrame, referencia) -> pd.DataFrame:
    """Sinal de cada bomba HOJE, pelo último abastecimento: `sinalizando`
    (instalada e abasteceu nos últimos DIAS_SINAL_ATIVO dias até
    `referencia`), `dias_sem_sinal` e o texto `sinal` da tabela. Bomba que não
    está instalada hoje fica com "—"."""
    r = resumo.copy()
    ref = pd.Timestamp(referencia).normalize()
    ultimo = pd.to_datetime(r["ultimo_abastecimento"])
    r["dias_sem_sinal"] = (ref - ultimo).dt.days
    instalada = r["situacao"].isin(SITUACOES_ATIVAS)
    r["sinalizando"] = instalada & r["dias_sem_sinal"].le(DIAS_SINAL_ATIVO)
    textos = []
    for ok, inst, dias, ult in zip(r["sinalizando"], instalada, r["dias_sem_sinal"], ultimo):
        if not inst:
            textos.append("—")
        elif pd.isna(ult):
            textos.append("Nunca abasteceu")
        elif ok:
            textos.append("Sinalizando")
        else:
            textos.append(f"Sem sinal há {int(dias)} dias")
    r["sinal"] = textos
    return r


def _chave_posto(nome, tem_ativa: bool) -> tuple:
    """Ordem dos postos: os que têm bomba instalada hoje primeiro, depois por
    nome; bomba fora do cadastro (sem posto) no fim."""
    n = _normalizar_texto(nome) if isinstance(nome, str) and nome.strip() else ""
    return (n == "", not tem_ativa, n)


def rotulo_posto(nome, cidade, uf) -> str:
    """"Posto · Cidade/UF" (só o que existir); sem posto, o aviso."""
    if not (isinstance(nome, str) and nome.strip()):
        return "Sem posto (bomba fora do cadastro)"
    local = "/".join(v.strip() for v in (cidade, uf) if isinstance(v, str) and v.strip())
    return nome.strip() + (f" · {local}" if local else "")


def postos_do_resumo(resumo: pd.DataFrame) -> dict:
    """bomba_id → (rótulo do posto, posição do posto na ordem da aba)."""
    rotulos = {r["bomba_id"]: rotulo_posto(r.get("posto_nome"), r.get("posto_cidade"), r.get("posto_uf"))
               for _, r in resumo.iterrows()}
    nomes = dict(zip(resumo["bomba_id"], resumo["posto_nome"]))
    ativos = {rotulos[b] for b, sit in zip(resumo["bomba_id"], resumo["situacao"]) if sit in SITUACOES_ATIVAS}
    chaves = {}
    for b, rot in rotulos.items():
        chaves.setdefault(rot, _chave_posto(nomes.get(b), rot in ativos))
    ordem = {rot: i for i, rot in enumerate(sorted(chaves, key=lambda r: chaves[r]))}
    return {b: (rot, ordem[rot]) for b, rot in rotulos.items()}


def ordem_timeline(periodos: pd.DataFrame, resumo: pd.DataFrame, canais: pd.DataFrame = None,
                   agrupar: str = "pagante", nome_cliente: str = None) -> pd.DataFrame:
    """Uma linha por bomba da timeline, na ordem de exibição.
    agrupar="posto": um bloco por POSTO (postos com bomba instalada hoje
    primeiro, depois por nome; bomba fora do cadastro no fim) e, dentro do
    posto, pagante → equipamento — os equipamentos do mesmo posto ficam
    juntos, como um pré-filtro visual.
    agrupar="pagante": agrupada pelo PAGANTE de hoje (alocação CIGAM): pagantes com mais bombas primeiro (é a
    mesma ordem das cores), "sem alocação" por último entre as instaladas;
    depois as bombas retiradas, as que hoje estão em outro cliente e as fora
    do cadastro. Dentro do grupo, pela data do primeiro uso. Sem alocação
    carregada, as instaladas ficam num grupo só. Canais do mesmo equipamento
    (duplo de hoje pelo equipamento_id; duplo já retirado por `canais`)
    ficam lado a lado."""
    info = resumo.set_index("bomba_id")
    equip_de = {}
    if canais is not None and not canais.empty:
        for _, c in canais.sort_values("inicio").iterrows():
            for b in c["bombas"]:
                equip_de[b] = f"serial {c['serial']}"
    for b, e in info["equipamento_id"].items():
        if isinstance(e, str) and e:
            equip_de[b] = f"equip {e}"
    grupos = grupos_das_bombas(resumo)
    postos = postos_do_resumo(resumo)
    sem_posto = rotulo_posto(None, None, None)
    linhas = []
    for bomba, g in periodos.groupby("bomba_id"):
        pagante, rank_pagante = grupos.get(bomba, (GRUPO_FORA_CADASTRO, 1001))
        posto, rank_posto = postos.get(bomba, (sem_posto, 10 ** 6))
        if agrupar == "posto":
            # no eixo, só o nome do posto (cidade/UF ficam no filtro)
            nome_posto = info.at[bomba, "posto_nome"] if bomba in info.index else None
            # "OURO NEGRO TRANSPORTES E TURISMO - Rio das ostras/RJ" → "Rio das ostras/RJ":
            # o cliente já está no título da aba
            grupo = (_truncar(rotulo_bloco_posto(nome_posto, nome_cliente, info.at[bomba, "posto_cidade"]
                                                 if "posto_cidade" in info else None,
                                                 info.at[bomba, "posto_uf"] if "posto_uf" in info else None), 44)
                     if isinstance(nome_posto, str) and nome_posto.strip() else posto)
            linhas.append({"bomba_id": bomba, "grupo": grupo, "rank": rank_posto, "sub": rank_pagante,
                           "inicio": g["inicio"].min()})
        else:
            linhas.append({"bomba_id": bomba, "grupo": pagante, "rank": rank_pagante, "sub": rank_posto,
                           "inicio": g["inicio"].min()})
    df = pd.DataFrame(linhas)
    if df.empty:
        return df
    df["equip"] = [equip_de.get(b, f"bomba {b}") for b in df["bomba_id"]]
    # canais de um equipamento seguem juntos dentro do bloco
    df["sub"] = df.groupby(["grupo", "equip"])["sub"].transform("min")
    df["inicio_equip"] = df.groupby(["grupo", "equip"])["inicio"].transform("min")
    return df.sort_values(["rank", "grupo", "sub", "inicio_equip", "equip", "inicio"]).reset_index(drop=True)


def alertas_de_pagante(resumo: pd.DataFrame) -> list:
    """Uma frase por pagante com conta própria no sistema, com as bombas que
    ele paga no parque do cliente — a versão em texto do contorno vermelho."""
    if "pagante_fora_do_grupo" not in resumo:
        return []
    fora = resumo[resumo["pagante_fora_do_grupo"].fillna(False).astype(bool)]
    linhas = []
    for pagante, g in fora.groupby("pagante_rotulo"):
        bombas = ", ".join(sorted(g["rotulo"]))
        linhas.append(
            f"**{len(g)} bomba(s) pagas por {pagante}**, fora do grupo deste cliente pela raiz do CNPJ e "
            f"com conta própria no sistema: {g['pagante_outras_contas'].iloc[0]}. "
            f"Pode ser bomba de outra operação: {bombas}.")
    return linhas


def _clarear(cor_hex: str, fracao: float, intensidade: float = 0.6) -> str:
    """Tom mais claro da mesma cor (fracao 0 = cor original). Só clareia:
    contra o fundo escuro do gráfico, escurecer apagaria a linha."""
    cor = cor_hex.lstrip("#")
    r, g, b = (int(cor[i:i + 2], 16) for i in (0, 2, 4))
    d = max(0.0, min(1.0, fracao)) * intensidade
    return "#" + "".join(f"{round(c + (255 - c) * d):02x}" for c in (r, g, b))


def cores_por_bomba(resumo: pd.DataFrame, bombas) -> dict:
    """Cor de cada bomba no PADRÃO DA ABA: a cor do pagante de hoje (a mesma
    da linha do tempo, cores_pagantes) com um tom por bomba; bomba sem
    pagante hoje (retirada, em outro cliente, fora do cadastro) em tons de
    cinza, como o "serial anterior" da linha do tempo. Usada no gráfico de
    volume e no quadradinho da tabela de bombas."""
    base = cores_pagantes(resumo)
    info = resumo.set_index("bomba_id")
    tem_alocacao = "pagante_rotulo" in info
    familias = {}
    for b in bombas:
        situacao = info.at[b, "situacao"] if b in info.index else GRUPO_FORA_CADASTRO
        if situacao in SITUACOES_ATIVAS:
            if tem_alocacao:
                pagante = info.at[b, "pagante_rotulo"] or SEM_ALOCACAO
                cor = base.get(pagante, COR_OUTROS_PAGANTES)
            else:
                cor = COR_SERIAL_ATUAL
        else:
            cor = COR_SERIAL_ANTERIOR
        familias.setdefault(cor, []).append(b)
    cores = {}
    for cor, membros in familias.items():
        for i, b in enumerate(membros):
            cores[b] = _clarear(cor, i / (len(membros) - 1) if len(membros) > 1 else 0.0)
    return cores


def cores_pagantes(resumo: pd.DataFrame) -> dict:
    """Cor por pagante de hoje, na ordem de nº de bombas instaladas (o
    pagante principal fica com a 1ª cor). Passou de 6 pagantes, o resto
    divide a 7ª cor; bomba instalada sem alocação tem cor própria."""
    if "pagante_rotulo" not in resumo:
        return {}
    ativos = resumo[resumo["situacao"].isin(SITUACOES_ATIVAS)]
    contagem = ativos["pagante_rotulo"].dropna().value_counts()
    sinalizados = set(ativos.loc[ativos["pagante_fora_do_grupo"].fillna(False).astype(bool), "pagante_rotulo"])
    # pagantes do grupo primeiro e os sinalizados (⚠) por último; empate no nº
    # de bombas vai por ordem alfabética, pra cor não trocar entre buscas
    pagantes = sorted((p for p in contagem.index if p != SEM_ALOCACAO),
                      key=lambda p: (p in sinalizados, -contagem[p], p))
    cores = {p: (CORES_PAGANTES[i] if i < len(CORES_PAGANTES) else COR_OUTROS_PAGANTES)
             for i, p in enumerate(pagantes)}
    cores[SEM_ALOCACAO] = COR_SEM_ALOCACAO
    return cores


def plotar_periodos(periodos, trocas, resumo, ano, nome_cliente=None, origens=None, movimentos=None, canais=None,
                    agrupar: str = "pagante"):
    """Timeline: quando cada bomba abasteceu, um segmento por serial,
    quebrado em pausas longas, agrupada pelo pagante de hoje (o posto vai no
    hover). O trecho do serial instalado HOJE tem a cor do pagante de hoje
    (alocação CIGAM); pagante com conta própria no sistema ganha contorno
    vermelho e ⚠ no nome."""
    ordem = ordem_timeline(periodos, resumo, canais, agrupar, nome_cliente)
    info = resumo.set_index("bomba_id")
    tem_alocacao = "pagante_rotulo" in resumo
    alerta = info["pagante_fora_do_grupo"].to_dict() if tem_alocacao else {}
    # o nome da bomba repete o do cliente em boa parte da base — tira esse
    # prefixo e corta o nome, nunca o "(#id)", que é o que torna o rótulo único
    # canais de um duplo ganham a marca no nome, além do colchete no gráfico
    tipo_canal = {}
    if canais is not None and not canais.empty:
        for _, c in canais.iterrows():
            for b in c["bombas"]:
                tipo_canal[b] = int(c["canais"])
    # com mais de um posto, o nome do posto abre o rótulo (as bombas já vêm
    # ordenadas posto a posto) — a não ser que o nome da bomba já o contenha
    postos = info["posto_nome"].to_dict()
    cidades = info["posto_cidade"].to_dict() if "posto_cidade" in info else {}
    ufs = info["posto_uf"].to_dict() if "posto_uf" in info else {}
    varios_postos = info.loc[info.index.isin(ordem["bomba_id"]), "posto_nome"].dropna().nunique() > 1

    def _prefixo_posto(b, nome_bomba):
        posto = postos.get(b)
        if agrupar == "posto" or not varios_postos or not isinstance(posto, str) or not posto.strip():
            return ""
        if _normalizar_texto(posto) in _normalizar_texto(nome_bomba):
            return ""
        return f"{_truncar(_sem_prefixo_do_cliente(posto, nome_cliente), 24)} › "

    sem_sinal = {b: int(d) for b, ok, inst, d in zip(
        info.index, info.get("sinalizando", pd.Series(True, index=info.index)),
        info["situacao"].isin(SITUACOES_ATIVAS), info.get("dias_sem_sinal", pd.Series(0, index=info.index)))
        if inst and not ok and pd.notna(d)}
    def _nome_na_linha(b, n):
        """Nome da bomba sem o que já está escrito ao lado (ver
        nome_curto_bomba): o cliente e, agrupando por posto, o posto do bloco.
        Vazio = a bomba se chama como o posto."""
        return _truncar(nome_curto_bomba(n, nome_cliente, postos.get(b) if agrupar == "posto" else None), 38)

    def _rotulo_linha(b, nome):
        # nome vazio (= o posto do bloco) fica só o ID: "Bomba (#id)" não diz nada
        return f"{nome} (#{b})" if nome else f"#{b}"

    rotulos = {b: ("⚠ " if alerta.get(b) else "") + _prefixo_posto(b, n)
               + _rotulo_linha(b, (lambda nl: nl + (sufixo_local(n, cidades.get(b), ufs.get(b))
                                                   if agrupar != "posto" else "") if nl else "")(
                   _nome_na_linha(b, n) if isinstance(n, str) and n.strip() else ""))
               # bombas do mesmo equipamento (duplo/triplo/quádruplo): #2/#3/#4
               + (f" · #{tipo_canal[b]}" if b in tipo_canal else "")
               + (f" · sem sinal há {sem_sinal[b]}d" if b in sem_sinal else "")
               for b, n in info["bomba_nome"].items()}
    ids_op = info["id_op"].to_dict() if "id_op" in info else {}
    moveis = info["tanque_movel"].to_dict() if "tanque_movel" in info else {}
    grupos = dict(zip(ordem["bomba_id"], ordem["grupo"]))

    def eixo_y(bombas):
        return [[grupos.get(b, GRUPO_FORA_CADASTRO) for b in bombas], [rotulos.get(b, b) for b in bombas]]

    # trecho de 1 dia some na escala de anos — largura mínima só no desenho
    # (o hover mostra as datas reais)
    if ano is not None:
        janela_ms = 365 * 86_400_000
    else:
        janela_ms = max((periodos["fim"].max() - periodos["inicio"].min()).total_seconds() * 1000, 86_400_000)
    largura_min = janela_ms * 0.008

    fig = go.Figure()
    # 1º trace invisível com TODAS as bombas na ordem certa: no eixo de 2
    # níveis (pagante, bomba) a ordem das categorias é a da primeira aparição
    fig.add_trace(go.Scatter(x=[periodos["inicio"].min()] * len(ordem), y=eixo_y(ordem["bomba_id"]),
                             mode="markers", marker=dict(opacity=0), showlegend=False, hoverinfo="skip"))

    def _hover(p, extra):
        return [
            f"<b>{rotulos.get(b, b)}</b><br>Posto: {postos.get(b) or '—'}"
            + (" · comboio: local = posto de cadastro" if moveis.get(b) is True else "")
            + (f" · ID operacional {ids_op[b]}" if isinstance(ids_op.get(b), str) else "")
            + f"<br>Serial {s or '—'} · {extra(b)}<br>"
            f"{i:%d/%m/%Y} → {f:%d/%m/%Y}<br>"
            + f"{d} dia(s) com abastecimento · {l:,.0f} L".replace(",", ".")
            for b, s, i, f, d, l in zip(p["bomba_id"], p["serial"], p["inicio"], p["fim_real"], p["dias"], p["litros"])
        ]

    def _barras(p, nome, cor, hachura="", contorno=None, legenda=True, extra=None):
        largura = ((p["fim"] - p["inicio"]).dt.total_seconds() * 1000).clip(lower=largura_min)
        fig.add_trace(go.Bar(
            y=eixo_y(p["bomba_id"]), base=p["inicio"], x=largura, orientation="h", name=nome,
            legendgroup=nome, showlegend=legenda,
            marker=dict(color=cor, line=dict(width=3 if contorno else 0, color=contorno or cor),
                        pattern=dict(shape=hachura, fgcolor=COR_FUNDO_GRAFICO, size=6, solidity=0.25)),
            hovertext=_hover(p, extra or (lambda b: nome)), hoverinfo="text", width=0.62,
        ))

    atuais = periodos[periodos["categoria"] == "Serial instalado hoje"]
    if tem_alocacao and not atuais.empty:
        cores = cores_pagantes(resumo)
        pagante_de = info["pagante_rotulo"].to_dict()

        def _extra(b):
            txt = f"pago hoje por {pagante_de.get(b) or 'Sem alocação CIGAM'}"
            if alerta.get(b):
                txt += f"<br>⚠ pagante tem conta própria no sistema: {info.at[b, 'pagante_outras_contas']}"
            return txt

        for pagante, cor in cores.items():
            p = atuais[atuais["bomba_id"].map(lambda b: (pagante_de.get(b) or "Sem alocação CIGAM") == pagante)]
            if p.empty:
                continue
            fora = p["bomba_id"].map(lambda b: bool(alerta.get(b)))
            nome = rotulo_pagante_curto(pagante, 40)
            if (~fora).any():
                _barras(p[~fora], nome, cor, hachura="." if pagante == "Sem alocação CIGAM" else "", extra=_extra)
            if fora.any():
                _barras(p[fora], nome, cor, contorno=COR_ALERTA, legenda=not (~fora).any(), extra=_extra)
        if any(alerta.get(b) for b in atuais["bomba_id"]):
            # entrada de legenda só pro contorno
            fig.add_trace(go.Scatter(x=[None], y=[[None], [None]], mode="markers",
                                     name="⚠ Pagante com conta própria no sistema", hoverinfo="skip",
                                     marker=dict(symbol="square", size=12, color=COR_FUNDO_GRAFICO,
                                                 line=dict(width=3, color=COR_ALERTA))))
    elif not atuais.empty:
        _barras(atuais, "Serial instalado hoje", COR_SERIAL_ATUAL)

    for categoria, cor, hachura in [("Serial anterior", COR_SERIAL_ANTERIOR, ""),
                                    ("Sem serial no abastecimento", COR_SEM_SERIAL, "/")]:
        p = periodos[periodos["categoria"] == categoria]
        if not p.empty:
            _barras(p, categoria, cor, hachura=hachura)

    # colchete do equipamento duplo: linha cheia ligando os canais no início
    # do período em que abasteceram juntos (a seta de mudança é pontilhada)
    if canais is not None and not canais.empty:
        primeira = True
        for _, c in canais.iterrows():
            visiveis_c = [b for b in ordem["bomba_id"] if b in c["bombas"]]
            if len(visiveis_c) < 2:
                continue
            texto = (f"<b>Equipamento {_tipo_por_canais(c['canais'])} · serial {c['serial']}</b><br>"
                     f"Canais: {', '.join(rotulos.get(b, b) for b in visiveis_c)}<br>"
                     f"{c['inicio']:%d/%m/%Y} → {c['fim']:%d/%m/%Y}")
            fig.add_trace(go.Scatter(
                x=[c["inicio"]] * len(visiveis_c), y=eixo_y(visiveis_c), mode="lines+markers",
                name="Mesmo equipamento (#2 duplo, #3 triplo…)", legendgroup="duplo", showlegend=primeira,
                hovertext=[texto] * len(visiveis_c), hoverinfo="text",
                line=dict(color=COR_DUPLO, width=2.5),
                marker=dict(symbol="line-ew", size=10, line=dict(width=2.5, color=COR_DUPLO)),
            ))
            primeira = False

    # seta discreta ligando o fim do serial numa bomba ao começo dele na
    # bomba seguinte — só quando as duas bombas estão na linha do tempo
    # (transferência dentro do cliente; de/para outro cliente fica no hover
    # da estrela e na tabela de transferências)
    if movimentos is not None and not movimentos.empty:
        visiveis = set(grupos)
        m = movimentos[(movimentos["tipo"] == TIPO_DENTRO)
                       & movimentos["_bomba_saida"].isin(visiveis) & movimentos["_bomba_chegada"].isin(visiveis)
                       & (movimentos["_bomba_saida"] != movimentos["_bomba_chegada"])]
        for i, (_, mv) in enumerate(m.iterrows()):
            # uso simultâneo: a "saída" vem depois da chegada — a seta fica
            # vertical, na data da chegada
            x0 = min(mv["saida"], mv["data"])
            texto = (f"<b>Serial {mv['serial']} mudou de bomba</b><br>{rotulos.get(mv['_bomba_saida'])} → "
                     f"{rotulos.get(mv['_bomba_chegada'])}<br>{mv['periodo']}")
            fig.add_trace(go.Scatter(
                x=[x0, mv["data"]], y=eixo_y([mv["_bomba_saida"], mv["_bomba_chegada"]]),
                mode="lines+markers", name="Serial mudou de bomba", legendgroup="movimento",
                showlegend=i == 0, hovertext=[texto, texto], hoverinfo="text",
                line=dict(color=COR_MOVIMENTO, width=1.3, dash="dot"),
                marker=dict(symbol=["circle", "arrow"], size=[4, 11], angleref="previous",
                            color=COR_MOVIMENTO, line=dict(width=0)),
            ))

    t = trocas[trocas["bomba_id"].isin(periodos["bomba_id"])]
    if not t.empty:
        fig.add_trace(go.Scatter(
            x=t["data"], y=eixo_y(t["bomba_id"]), mode="markers",
            name="Troca de serial",
            marker=dict(symbol="star", size=10, color=COR_FUNDO_GRAFICO, line=dict(width=1.5, color=COR_TEXTO_GRAFICO)),
            hovertext=[f"<b>Troca de serial</b><br>{rotulos.get(b, b)}<br>{d:%d/%m/%Y}<br>{a} → <b>{n}</b>"
                       + (f"<br>↳ {_truncar((origens or {})[(n, b)], 140)}" if (n, b) in (origens or {}) else "")
                       for b, d, a, n in zip(t["bomba_id"], t["data"], t["serial_anterior"], t["serial_novo"])],
            hoverinfo="text",
        ))

    # a legenda fica ACIMA do gráfico e precisa de espaço próprio: com poucas
    # bombas (gráfico baixo) e vários pagantes ela não cabia na margem e o
    # Plotly a empurrava pra cima das barras. A margem de cima cresce com o nº
    # de linhas que a legenda ocupa (estimado pela largura dos nomes numa área
    # de ~900 px, pra sobrar folga em telas estreitas)
    nomes_legenda = [tr.name for tr in fig.data if tr.name and tr.showlegend is not False]
    linhas_legenda, largura_linha = 1, 0
    for nome in nomes_legenda:
        w = 7 * len(nome) + 45
        if largura_linha and largura_linha + w > 900:
            linhas_legenda, largura_linha = linhas_legenda + 1, 0
        largura_linha += w
    margem_topo = 16 + 24 * linhas_legenda
    _layout_base(fig, 150 + 30 * len(ordem) + margem_topo)
    fig.update_layout(
        barmode="overlay", hovermode="closest",
        margin=dict(l=10, r=30, t=margem_topo, b=40),
        # legenda só explica as cores; quem filtra é o painel de filtros
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0,
                    itemclick=False, itemdoubleclick=False),
        xaxis=dict(type="date"),
        yaxis=dict(autorange="reversed", automargin=True, showgrid=False, showdividers=True,
                   dividercolor=COR_GRADE, dividerwidth=1),
    )
    if ano is not None:
        fig.update_xaxes(range=[f"{ano}-01-01", f"{ano}-12-31"])
    return fig


# --- 5. Interface ----------------------------------------------------------
def _fmt_litros(v: float) -> str:
    return f"{v:,.0f} L".replace(",", ".")


def renderizar_aba_equipamentos(client_bq, project_id: str, modo_demo: bool, bombas_alocadas: pd.DataFrame = None):
    """`bombas_alocadas`: a carga horária de gold.bombas_alocadas que o app.py
    já faz (com bomba_id) — a aba usa ela em vez de consultar a view de novo
    (ver o comentário de custo no topo da seção de dados)."""
    st.caption(
        "Busque com o que tiver em mãos: **nome ou código do CIGAM**, **CNPJ/CPF** (do CIGAM ou do sistema, "
        "ou só a raiz de 8 dígitos), **nº da bomba**, **serial** ou o nome/cliente_id do sistema. A busca "
        "traduz para o cliente do **sistema CTA** que opera as bombas e mostra todas as que abasteceram nos "
        "postos dele, os seriais (equipamentos) que passaram por elas, quando trocaram, o período e o volume."
    )
    with st.form("busca_equipamentos_form"):
        col_busca, col_btn = st.columns([5, 1], vertical_alignment="bottom")
        with col_busca:
            termo = st.text_input("Cliente", placeholder="Ex.: FORCA AMBIENTAL LTDA · 002112 · 20.217.115/0001-40 · "
                                                           "bomba 547952619 · serial 12632")
        with col_btn:
            clicou = st.form_submit_button("Buscar", use_container_width=True, type="primary")
    if clicou:
        st.session_state["eq_termo"] = termo.strip() or None

    termo = st.session_state.get("eq_termo")
    if not termo:
        return

    chaves = None
    if modo_demo:
        diretorio, parque_demo = gerar_dados_demo()
        chaves = gerar_chaves_demo()
    else:
        try:
            diretorio = carregar_clientes_sistema(client_bq, project_id)
        except Forbidden as e:
            _mostrar_falta_de_permissao(project_id, e)
            return
        try:
            chaves = montar_chaves_busca(carregar_cadastro_bombas(client_bq, project_id),
                                         carregar_empresas_cigam(client_bq, project_id), bombas_alocadas)
        except (Forbidden, NotFound, BadRequest) as e:
            st.caption("Busca só por nome, cliente_id e CNPJ do sistema — sem a ponte com o CIGAM, as bombas "
                       "e os seriais: " + str(getattr(e, "message", e))[:300])

    candidatos = buscar_clientes_sistema(diretorio, termo, chaves)
    if candidatos.empty:
        st.error(f"Nenhum cliente do sistema encontrado para '{termo}'. A busca cobre os clientes com bomba "
                 f"cadastrada no CTA e no Ipiranga Connect.")
        return
    if len(candidatos) == 1:
        escolhido = candidatos.iloc[0]
        st.caption(f"Achado por: {_motivo_curto(escolhido['_motivo'])}")
    else:
        # um cliente por bloco e os POSTOS dele (cidade, nº de bombas):
        # escolher o posto já abre o cliente filtrado nele
        opcoes = candidatos.head(200)
        aloc_busca = bombas_alocadas
        if modo_demo:
            cad_busca = parque_demo[0].rename(columns={"cliente_id_atual": "cliente_id"}).assign(base="cta")
            aloc_busca = gerar_alocacao_demo().rename(columns={"pagante_codigo": "cliente_cigam_pagante",
                                                                "local_codigo": "cliente_cigam_local"}).assign(base="cta")
        else:
            try:
                cad_busca = carregar_cadastro_bombas(client_bq, project_id)
            except (Forbidden, NotFound, BadRequest):
                cad_busca = None
        # último dia de uso por bomba, pra contar equipamento ATIVO na mesma
        # régua do card da aba (carga compartilhada de 12 h; no demo, o uso fixo)
        if modo_demo:
            uso_demo = parque_demo[1].dropna(subset=["bomba_id"])
            ultimo_uso = {("cta", b): d for b, d in uso_demo.groupby("bomba_id")["data"].max().items()}
            ref_busca = uso_demo["data"].max()
        else:
            try:
                pas = carregar_passagens_seriais(client_bq, project_id)
                ultimo_uso = pas.groupby(["base", "bomba_id"])["fim"].max().to_dict()
            except (Forbidden, NotFound, BadRequest):
                ultimo_uso = {}
            ref_busca = pd.Timestamp.today().normalize()
        tabela_busca = postos_dos_candidatos(opcoes, cad_busca, aloc_busca, ultimo_uso, ref_busca)

        # a escolha fica guardada (cliente, posto): o botão do cliente aberto
        # acende, então as duas partes contam a mesma coisa mesmo depois que
        # a tabela é redesenhada
        estado = st.session_state.setdefault(f"eq_busca_estado_{termo}", {"cliente": None, "posto": None, "versao": 0})
        chave_pilulas = f"eq_abrir_postos_{termo}"
        pendente = st.session_state.pop(f"{chave_pilulas}_pendente", None)
        if pendente is not None:
            st.session_state[chave_pilulas] = pendente

        # um botão por cliente que tem posto, na ordem da tabela, com o nº de postos
        sub = tabela_busca[(tabela_busca["Empresa (cliente no sistema)"] == "") & tabela_busca["_posto"].notna()]
        qtd_postos = sub.groupby("_i").size().to_dict()
        cab = tabela_busca[tabela_busca["Empresa (cliente no sistema)"] != ""]
        nomes_cli = {int(i): f"{e.split(' · ')[0]} ({qtd_postos[i]})"
                     for i, e in zip(cab["_i"], cab["Empresa (cliente no sistema)"]) if i in qtd_postos}
        st.markdown(f"**{len(candidatos)} clientes encontrados** — marque a linha para abrir o cliente"
                    + ("; os botões abrem os postos de cada um" if nomes_cli else ""))
        abertos = set()
        if nomes_cli:
            abertos = set(st.pills(
                "Ver os postos de", list(nomes_cli), selection_mode="multi",
                format_func=lambda i: nomes_cli[i], key=chave_pilulas,
                help="Mostra os postos do cliente embaixo dele; marcar um posto abre o cliente já filtrado nele. "
                     "Abrir um cliente pela tabela acende o botão dele. Os postos onde a empresa buscada paga "
                     "bombas vêm primeiro.") or [])
        tabela_busca = tabela_busca[(tabela_busca["Empresa (cliente no sistema)"] != "")
                                    | tabela_busca["_i"].isin(abertos)].reset_index(drop=True)
        chave_abertos = "-".join(str(i) for i in sorted(abertos))
        evento = st.dataframe(
            tabela_busca.drop(columns=["_i", "_posto"]), hide_index=True, use_container_width=True,
            on_select="rerun", selection_mode="single-row",
            key=f"eq_escolha_{termo}_{chave_abertos}_{estado['versao']}",
            height=min(420, 38 + 35 * len(tabela_busca)))
        linhas_sel = list(getattr(getattr(evento, "selection", None), "rows", []) or [])
        if linhas_sel:
            sel = tabela_busca.iloc[linhas_sel[0]]
            i_sel = int(sel["_i"])
            # linha do CLIENTE abre o cliente inteiro; linha de POSTO, só o posto
            posto_sel = sel["_posto"] if (sel["Empresa (cliente no sistema)"] == ""
                                          and isinstance(sel["_posto"], str)) else None
            estado.update(cliente=i_sel, posto=posto_sel, versao=estado["versao"] + 1)
            esc = opcoes.iloc[i_sel]
            st.session_state[f"eq_postos_{esc['base']}_{esc['cliente_id']}"] = [posto_sel] if posto_sel else []
            if i_sel in nomes_cli and i_sel not in abertos:
                st.session_state[f"{chave_pilulas}_pendente"] = sorted(abertos | {i_sel})
            st.rerun()
        if estado["cliente"] is None or estado["cliente"] >= len(opcoes):
            return
        escolhido = opcoes.iloc[estado["cliente"]]
        st.caption(f"Achado por: {_motivo_curto(escolhido['_motivo'])}")

    base, cliente_id = escolhido["base"], escolhido["cliente_id"]
    if modo_demo:
        cadastro, uso = parque_demo
        if cliente_id != "900101":
            cadastro, uso = cadastro.iloc[0:0], uso.iloc[0:0]
    else:
        try:
            cadastro, uso = carregar_parque_cliente(client_bq, project_id, base, cliente_id)
        except Forbidden as e:
            _mostrar_falta_de_permissao(project_id, e)
            return
        except NotFound as e:
            _mostrar_tabela_inexistente(e)
            return

    st.subheader(escolhido["cliente_nome"], anchor=False)
    st.caption(" · ".join(filter(None, [
        f"Base {BASES_SISTEMA.get(base, base)}", f"cliente_id {cliente_id}",
        f"CNPJ {escolhido['cnpj']}" if escolhido.get("cnpj") else None,
        f"TRR {escolhido['trr_nome']}" if escolhido.get("trr_nome") else None,
    ])))

    if uso.empty and cadastro.empty:
        st.info("Nenhuma bomba cadastrada nem abastecimento registrado nos postos deste cliente.")
        return

    uso_b = uso.dropna(subset=["bomba_id"])
    por_serial = seriais_por_bomba(uso_b)
    # equipamentos que funcionaram como duplo/triplo no cliente (inclusive
    # retirados) — a mesma régua das transferências
    canais = canais_do_cliente(por_serial)
    trocas = derivar_trocas(por_serial)
    resumo = montar_resumo_bombas(cadastro, uso, por_serial, trocas, cliente_id)
    litros_sem_bomba = float(uso.loc[uso["bomba_id"].isna(), "litros"].sum())

    # alocação CIGAM de hoje: sem acesso ou sem dado, a aba segue sem a cor
    # por pagante em vez de quebrar
    aviso_alocacao = None
    ativas = resumo.loc[resumo["situacao"].isin(SITUACOES_ATIVAS), "bomba_id"].dropna()
    if modo_demo:
        alocacao = gerar_alocacao_demo()
    elif ativas.empty:
        alocacao = None
    elif bombas_alocadas is None or bombas_alocadas.empty or "bomba_id" not in bombas_alocadas:
        alocacao = None
        aviso_alocacao = "Sem a alocação CIGAM (quem paga cada bomba): o app não carregou a alocação de bombas."
    else:
        alocacao = alocacao_do_cliente(bombas_alocadas, carregar_cadastro_bombas(client_bq, project_id),
                                       base, cliente_id, ativas)
    resumo = anexar_alocacao(resumo, alocacao) if alocacao is not None else resumo
    # sinal HOJE: o fato atualiza 1x por dia, então "hoje" é a data de hoje;
    # no demo os dados são fixos e a referência é o último dia deles
    referencia_sinal = (uso["data"].max() if modo_demo and not uso.empty
                        else pd.Timestamp.today().normalize())
    resumo = marcar_sinal(resumo, referencia_sinal)

    # percurso de cada serial fora das bombas do cliente (outros clientes e
    # outras bases): é o que mostra transferência não documentada
    aviso_percurso = None
    seriais_cliente = tuple(sorted(por_serial["serial"].dropna().unique())) if not por_serial.empty else ()
    if modo_demo:
        percurso = gerar_percurso_demo(cadastro, uso, alocacao)
    elif not seriais_cliente:
        percurso = None
    else:
        try:
            percurso = percurso_dos_seriais(carregar_passagens_seriais(client_bq, project_id),
                                            carregar_cadastro_bombas(client_bq, project_id), seriais_cliente)
        except (Forbidden, NotFound, BadRequest) as e:
            percurso = None
            aviso_percurso = "Sem o percurso dos seriais fora do cliente: " + str(getattr(e, "message", e))[:300]
    transferencias, percursos = derivar_transferencias(percurso, base, cliente_id)
    origens = {(r["serial"], r["_bomba_destino"]): f"{r['tipo']}: {r['de']}"
               for _, r in transferencias.iterrows()} if not transferencias.empty else {}

    # --- filtros: UM painel que vale para tudo abaixo (números, gráficos,
    # tabelas e transferências). Antes eram três filtros soltos — bombas do
    # gráfico, botões de pagante e "mostrar todas" na linha do tempo — que
    # não conversavam, e a busca de bombas não achava o serial.
    grupos_bomba = grupos_das_bombas(resumo)
    posicao_grupo = {}
    for g, r in grupos_bomba.values():
        posicao_grupo[g] = min(r, posicao_grupo.get(g, r))
    opcoes_grupo = sorted(posicao_grupo, key=lambda g: (posicao_grupo[g], g))
    volume_total = resumo.set_index("bomba_id")["litros_total"].to_dict()
    rotulos_busca = {r["bomba_id"]: rotulo_de_busca(r) for _, r in resumo.iterrows()}
    anos = sorted(uso_b["data"].dt.year.unique().tolist()) if not uso_b.empty else []

    postos_bomba = postos_do_resumo(resumo)
    opcoes_posto = sorted({v for v in postos_bomba.values()}, key=lambda v: v[1])
    # rótulo do filtro na MESMA régua da busca e do card: equipamentos ativos
    # (sinalizando) de instalados no posto
    inst_hoje = resumo[resumo["situacao"].isin(SITUACOES_ATIVAS) & resumo["equipamento_id"].notna()]
    equip_por_posto = {}
    for posto_rot, g in inst_hoje.assign(_p=inst_hoje["bomba_id"].map(lambda b: postos_bomba.get(b, ("", 0))[0])) \
            .groupby("_p"):
        sinal = g.groupby("equipamento_id")["sinalizando"].any()
        equip_por_posto[posto_rot] = _fmt_equip(int(sinal.sum()), int(len(sinal)))

    st.markdown("**Filtros** · valem para os números, os gráficos, as tabelas e as transferências abaixo")
    col_p, col_g, col_po, col_b = st.columns([1, 2.1, 2.1, 2.8])
    with col_p:
        periodo = st.selectbox("Período", ["Todo o período"] + [str(a) for a in reversed(anos)],
                               key=f"eq_periodo_{base}_{cliente_id}")
    ano = None if periodo == "Todo o período" else int(periodo)
    with col_g:
        grupos_escolhidos = st.multiselect(
            "Pagante hoje / situação", opcoes_grupo, default=[], placeholder="Todos",
            key=f"eq_grupos_{base}_{cliente_id}")
    with col_po:
        # postos com bomba instalada hoje primeiro, depois por nome; um posto
        # vindo da busca que não exista mais nas opções é descartado
        chave_postos = f"eq_postos_{base}_{cliente_id}"
        if chave_postos in st.session_state:
            validos = {v[0] for v in opcoes_posto}
            st.session_state[chave_postos] = [p for p in st.session_state[chave_postos] if p in validos]
        postos_escolhidos = st.multiselect(
            "Posto", [v[0] for v in opcoes_posto], placeholder="Todos",
            format_func=lambda p: f"{p} ({equip_por_posto.get(p, 'nenhum instalado')} equip. ativos)",
            key=f"eq_postos_{base}_{cliente_id}")
    candidatas = [b for b in resumo["bomba_id"]
                  if (not grupos_escolhidos or grupos_bomba.get(b, ("", 0))[0] in grupos_escolhidos)
                  and (not postos_escolhidos or postos_bomba.get(b, ("", 0))[0] in postos_escolhidos)]
    candidatas.sort(key=lambda b: -volume_total.get(b, 0.0))
    with col_b:
        # a chave muda com os grupos: trocar o pagante limpa a escolha de
        # bombas que talvez nem estejam mais nas opções
        bombas_escolhidas = st.multiselect(
            "Bombas", candidatas, default=[], format_func=lambda b: rotulos_busca.get(b, b),
            placeholder="Todas — busque por nome, nº da bomba, serial atual ou ID operacional",
            key=f"eq_bombas_{base}_{cliente_id}_{'|'.join(sorted(grupos_escolhidos))}_{'|'.join(sorted(postos_escolhidos))}")
    filtradas = set(bombas_escolhidas) if bombas_escolhidas else set(candidatas)
    filtro_ativo = bool(grupos_escolhidos or postos_escolhidos or bombas_escolhidas)
    if filtro_ativo:
        st.caption(f"Filtro ativo: {len(filtradas)} de {resumo['bomba_id'].nunique()} bombas.")

    resumo_f = resumo[resumo["bomba_id"].isin(filtradas)]
    uso_f = uso_b[uso_b["bomba_id"].isin(filtradas)]
    trocas_f = trocas[trocas["bomba_id"].isin(filtradas)]
    por_serial_f = por_serial[por_serial["bomba_id"].isin(filtradas)]
    canais_f = canais[canais["bombas"].map(lambda bs: any(b in filtradas for b in bs))] if not canais.empty else canais
    if transferencias is not None and not transferencias.empty:
        transferencias_f = transferencias[
            transferencias["_bomba_saida"].isin(filtradas) | transferencias["_bomba_chegada"].isin(filtradas)
            | transferencias["_bomba_destino"].isin(filtradas)]
    else:
        transferencias_f = transferencias
    uso_periodo = uso_f if ano is None else uso_f[uso_f["data"].dt.year == ano]
    trocas_periodo = trocas_f if ano is None else trocas_f[trocas_f["data"].dt.year == ano]
    ranking = uso_periodo.groupby("bomba_id")["litros"].sum().sort_values(ascending=False).index.tolist()

    # --- números do período (com o filtro) ---
    equipados = resumo_f[resumo_f["situacao"].isin(["Instalada", "Bomba inativa no cadastro"])]
    # ATIVO = instalado no cadastro E abastecendo: equipamento (serial) conta
    # se qualquer canal dele abasteceu nos últimos DIAS_SINAL_ATIVO dias
    eq_sinal = equipados.groupby("equipamento_id")["sinalizando"].any() if not equipados.empty else pd.Series(dtype=bool)
    qtd_sinal, qtd_cadastro = int(eq_sinal.sum()), int(len(eq_sinal))
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Bombas com uso no período", len(ranking))
    m2.metric("Equipamentos ativos hoje", qtd_sinal,
              delta=(f"{qtd_cadastro - qtd_sinal} instalado(s) sem sinal" if qtd_cadastro > qtd_sinal else None),
              delta_color="off", delta_arrow="off",
              help=f"Equipamentos (seriais distintos — um duplo atende 2 bombas) instalados no cadastro que "
                   f"abasteceram nos últimos {DIAS_SINAL_ATIVO} dias (até {referencia_sinal:%d/%m/%Y}). "
                   f"{qtd_cadastro} estão instalados no cadastro; os sem sinal aparecem na coluna 'Sinal' da "
                   "tabela de bombas e com 'sem sinal' na linha do tempo.")
    m3.metric("Volume no período", _fmt_litros(uso_periodo["litros"].sum()))
    if not uso_periodo.empty:
        m4.metric("Período com abastecimento",
                  f"{uso_periodo['data'].min():%m/%y} – {uso_periodo['data'].max():%m/%y}",
                  help=f"{uso_periodo['data'].min():%d/%m/%Y} a {uso_periodo['data'].max():%d/%m/%Y}")
    m5.metric("Trocas de equipamento", contar_trocas_por_equipamento(trocas_periodo),
              help="Primeira vez que um serial novo aparece numa bomba, pelo serial gravado em cada abastecimento. "
                   "Contada uma vez por equipamento: num duplo, os dois canais trocam juntos e isso é UMA troca "
                   "(no gráfico, cada canal ganha a sua ★).")

    # --- volume: as bombas do filtro, até MAX_BOMBAS_GRAFICO (mais volume primeiro) ---
    no_grafico = ranking[:MAX_BOMBAS_GRAFICO]
    col_t, col_gr = st.columns([3, 1.4], vertical_alignment="bottom")
    with col_gr:
        # todo o período por dia vira um emaranhado de linhas — semana por
        # padrão; ao focar num ano, dia (como no gráfico original)
        padrao = "Dia" if ano is not None else "Semana"
        granularidade = st.segmented_control(
            "Agrupar volume por", list(GRANULARIDADES), default=padrao,
            key=f"eq_granularidade_{base}_{cliente_id}_{periodo}") or padrao
    with col_t:
        st.markdown(f"**Volume abastecido por {granularidade.lower()}** · ★ = troca de serial")
        if len(ranking) > MAX_BOMBAS_GRAFICO:
            st.caption(f"Mostrando as {MAX_BOMBAS_GRAFICO} de maior volume entre as {len(ranking)} do filtro — "
                       "escolha bombas no filtro acima para ver outras.")
    if not no_grafico:
        st.info("Nenhuma bomba do filtro abasteceu no período.")
    else:
        # cor do PAGANTE de hoje, um tom por bomba — o mesmo padrão da linha
        # do tempo e da tabela de bombas (antes cada bomba tinha uma cor solta)
        cores = cores_por_bomba(resumo, no_grafico)
        st.plotly_chart(plotar_volume(uso_f, trocas_f, resumo, no_grafico, cores, granularidade, ano),
                        use_container_width=True)

    # --- linha do tempo: todas as bombas do filtro ---
    periodos = _periodos_de_uso(uso_f, resumo)
    agrupar = "posto"
    if not periodos.empty:
        if ano is not None:
            periodos = periodos[(periodos["inicio"].dt.year <= ano) & (periodos["fim_real"].dt.year >= ano)]
        col_tt, col_ag = st.columns([3, 1.4], vertical_alignment="bottom")
        with col_ag, st.container(horizontal=True, horizontal_alignment="right"):
            # na ponta direita, alinhado à borda do gráfico
            agrupar_rotulo = st.segmented_control(
                "Agrupar linha do tempo por", ["Posto", "Pagante"], default="Posto",
                key=f"eq_agrupar_{base}_{cliente_id}") or "Posto"
        agrupar = agrupar_rotulo.lower()
        with col_tt:
            st.markdown(
                f"**Períodos de uso por bomba**, agrupados por "
                + ("posto (os equipamentos do mesmo posto juntos) e, dentro dele, por quem paga hoje"
                   if agrupar == "posto" else "quem paga hoje e, dentro dele, por posto")
                + f" · um segmento por serial; pausas de mais de {DIAS_SEM_USO_QUEBRA_PERIODO} dias sem abastecer "
                  "quebram o segmento")
        st.caption("O número entre parênteses (ex.: CA003 (#787084)) é o **ID da bomba no sistema CTA** — "
                   "identifica o canal (a bomba), não o equipamento: o serial aparece ao passar o mouse e na tabela "
                   "abaixo. **#2, #3, #4** no fim = bombas do mesmo equipamento (duplo, triplo, quádruplo); linha "
                   "só com o ID = bomba com o mesmo nome do posto. O \" - Cidade/UF\" depois do nome vem da cidade do **posto** no cadastro do sistema CTA "
                   "(campo livre, sem validação; não é o endereço do CIGAM). Posto sem cidade aproveitável mostra só a UF."
                   + (" **Comboio (móvel):** a Cidade/UF é a do posto em que ele está cadastrado, não onde está "
                      "agora — pode ter mudado de obra sem atualização no cadastro."
                      if resumo_f.get("tanque_movel", pd.Series(dtype=object)).eq(True).any() else "")
                   + ("" if agrupar == "posto" else
                                " Com mais de um posto, o nome do posto abre o rótulo (Posto › Bomba)."))
        if "pagante_rotulo" in resumo:
            st.caption("O trecho do serial instalado **hoje** tem a cor de quem **paga** a bomba hoje "
                       "(alocação CIGAM). A alocação é só o estado atual, então os trechos anteriores ficam cinza.")
            for linha in alertas_de_pagante(resumo_f) + alertas_id_operacional(resumo_f) + alertas_duplo(resumo_f):
                st.warning(linha, icon="⚠️")
        elif aviso_alocacao:
            st.caption(aviso_alocacao)
        qtd_bombas_timeline = periodos["bomba_id"].nunique()
        if qtd_bombas_timeline > LIMITE_TIMELINE_COMPLETA:
            # cliente grande chega a 200+ bombas (~6000px): sem filtro, mostra
            # as de maior volume e diz como ver as outras
            maiores = sorted(periodos["bomba_id"].unique(), key=lambda b: -volume_total.get(b, 0.0))
            periodos = periodos[periodos["bomba_id"].isin(maiores[:LIMITE_TIMELINE_COMPLETA])]
            st.caption(f"Mostrando as {LIMITE_TIMELINE_COMPLETA} bombas de maior volume entre as "
                       f"{qtd_bombas_timeline} do filtro — filtre por pagante ou bomba acima para ver as outras.")
        if periodos.empty:
            st.caption("Nenhuma bomba do filtro abasteceu no período.")
        else:
            st.plotly_chart(plotar_periodos(periodos, trocas_periodo, resumo, ano, escolhido["cliente_nome"], origens,
                                            transferencias_f, canais_f, agrupar),
                            use_container_width=True)

    st.markdown("**Bombas do cliente** (todo o período" + (", com o filtro" if filtro_ativo else "")
                + (", posto a posto" if agrupar == "posto" else "") + ")")
    if agrupar == "posto":
        # mesma ordem da linha do tempo: posto a posto, maior volume primeiro
        resumo_f = resumo_f.assign(_posto=resumo_f["bomba_id"].map(lambda b: postos_bomba.get(b, ("", 10 ** 6))[1])) \
            .sort_values(["_posto", "litros_total"], ascending=[True, False]).drop(columns="_posto")
    colunas = {
        "rotulo": "Bomba", "posto_nome": "Posto", "tanque_nome": "Tanque", "instalacao": "Instalação",
        "equipamento_tipo": "Equipamento", "id_op": "ID operacional", "serial_equipamento": "Serial atual", "situacao": "Situação hoje",
        "sinal": "Sinal", "pagante_rotulo": "Pagante hoje (CIGAM)", "local_rotulo": "Local hoje (CIGAM)",
        "primeiro_abastecimento": "Primeiro abastecimento", "ultimo_abastecimento": "Último abastecimento",
        "dias_com_uso": "Dias com uso", "litros_total": "Volume total (L)",
        "media_por_dia_de_uso": "Média por dia de uso (L)", "trocas_serial": "Trocas de serial",
        "historico_seriais": "Seriais (período de uso)",
    }
    for col in ("pagante_rotulo", "local_rotulo", "id_op"):
        if col not in resumo:
            del colunas[col]
    tabela = resumo_f[list(colunas)].rename(columns=colunas)
    if "Pagante hoje (CIGAM)" in tabela:
        tabela["Pagante hoje (CIGAM)"] = [
            ("⚠ " if fora else "") + (p if isinstance(p, str) else "—")
            for p, fora in zip(tabela["Pagante hoje (CIGAM)"], resumo_f["pagante_fora_do_grupo"])]
    tabela["Serial atual"] = tabela["Serial atual"].fillna("—")
    tabela["Equipamento"] = tabela["Equipamento"].fillna("—")
    for col in ("ID operacional", "Local hoje (CIGAM)"):
        if col in tabela:
            tabela[col] = tabela[col].where(tabela[col].notna(), "—")
    tabela["Volume total (L)"] = tabela["Volume total (L)"].round(0)
    # quadradinho na cor da bomba (pagante de hoje, tom por bomba) — liga a
    # linha da tabela à linha dos gráficos
    cores_tabela = cores_por_bomba(resumo, list(resumo_f["bomba_id"]))
    tabela.insert(0, "", "■")
    cores_linhas = [cores_tabela.get(b, COR_SERIAL_ANTERIOR) for b in resumo_f["bomba_id"]]
    tabela_estilo = tabela.style.apply(lambda col: [f"color: {c}; font-size: 1.2em" for c in cores_linhas], subset=[""])
    st.dataframe(
        tabela_estilo, use_container_width=True, hide_index=True,
        column_config={
            "Primeiro abastecimento": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Último abastecimento": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Volume total (L)": st.column_config.NumberColumn(format="localized"),
            "Média por dia de uso (L)": st.column_config.NumberColumn(format="localized"),
        },
    )

    if resumo_f["instalacao"].astype(str).str.contains("nome sugere").any():
        st.caption("**Instalação** vem do cadastro do sistema CTA: tanque registrado como comboio = móvel. "
                   "\"nome sugere fixo/comboio\" = o nome do tanque ou da bomba aponta o contrário do cadastro "
                   "(ex.: tanque com o nome do posto marcado como comboio).")

    equipamentos = montar_resumo_equipamentos(por_serial_f, resumo)
    if not equipamentos.empty and percursos:
        equipamentos["percurso"] = equipamentos["serial"].map(percursos).fillna("—")
    if not equipamentos.empty:
        st.markdown(f"**Equipamentos (seriais) que passaram pelo cliente** ({len(equipamentos)})")
        st.dataframe(
            equipamentos.rename(columns={
                "serial": "Serial", "tipo": "Tipo", "onde": "Onde esteve no cliente (duplo = canais juntos; → = mudou)",
                "primeiro": "Primeiro uso", "ultimo": "Último uso", "litros": "Volume (L)", "situacao": "Situação hoje",
                "percurso": "Percurso completo (todos os clientes e bases)",
            }).assign(**{"Volume (L)": lambda d: d["Volume (L)"].round(0)}),
            use_container_width=True, hide_index=True,
            column_config={
                "Primeiro uso": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Último uso": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Volume (L)": st.column_config.NumberColumn(format="localized"),
            },
        )

    renderizar_transferencias(transferencias_f, aviso_percurso)

    if litros_sem_bomba > 0 and not filtro_ativo:
        st.caption(f"Além das bombas acima, {_fmt_litros(litros_sem_bomba)} foram abastecidos sem bomba "
                   "identificada (integração externa / lançamento manual) nos postos do cliente que têm bomba "
                   "cadastrada. Posto sem nenhuma bomba cadastrada não entra nesta conta.")

    with st.expander("Como ler e até onde o dado vai"):
        sem_serial = float(uso_b.loc[uso_b["serial"].isna(), "litros"].sum()) if "serial" in uso_b else 0.0
        st.markdown(f"""
- **Fonte:** `{TABELA_USO}` (DW), abastecimentos do sistema ({BASES_SISTEMA.get(base, base)}) somados por
  dia, bomba e serial, com a limpeza de `silver.abastecimentos_validos` (não removido, completo, volume > 0, de
  2020 até hoje), nos postos **atuais** do cliente que têm bomba cadastrada. Bomba que abasteceu num posto que
  hoje não é mais do cliente não aparece. O fato é atualizado pela carga diária do DW. Link2Pump não está no fato.
- **Serial:** é o gravado em **cada abastecimento** (o "Número Serial da automação" do relatório de
  abastecimentos do sistema). "Serial instalado hoje" vem do cadastro atual da bomba.
- **★ Troca de serial:** primeiro dia em que um serial novo abasteceu na bomba. Um serial que volta pra uma
  bomba onde já esteve não gera estrela de novo.
- **Sem serial:** {_fmt_litros(sem_serial)} abastecidos nas bombas do cliente não têm serial gravado — entram no
  volume, mas não em nenhum equipamento.
- **Zeros no gráfico:** entre o primeiro e o último abastecimento da bomba, período sem registro aparece como 0 L
  (bomba parada ou sem comunicação — o dado não distingue os dois).
- **Equipamento ≠ bomba:** um equipamento duplo atende 2 bombas (canais 1 e 2) e as duas mostram o mesmo serial.
""")
