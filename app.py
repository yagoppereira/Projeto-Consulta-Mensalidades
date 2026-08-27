# ============================================================================
# RELATÓRIO DE HISTÓRICO POR CLIENTE — CIGAM (versão web / Streamlit)
# App interno: colega digita o cliente (nome, código CIGAM ou CNPJ) e clica
# em Buscar. Usa uma service account do Google Cloud pra acessar BigQuery e
# Google Sheets — ninguém precisa de acesso individual a esses dados, só ao
# link do app.
#
# COMO RODAR LOCALMENTE:  streamlit run app.py
# COMO IMPLANTAR:         veja o guia que acompanha este arquivo
# ============================================================================

import json
import re
import os
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from google.cloud import bigquery
from google.oauth2 import service_account
import gspread
from gspread_dataframe import get_as_dataframe
import streamlit as st

st.set_page_config(page_title="Histórico de Mensalidade — CIGAM", layout="wide")

# --- 1. Configuração ---
PROJECT_ID = "hip-bonito-453017-m2"

SHEET_ID_BOMBAS = "1k_-lA-wBq4E9_qLuWFBQUGzUwfA56vtmIlWsPhw130Y"
ABA_BOMBAS = "Bombas_Alocadas"

SHEET_ID_MENSALIDADES = "1zzz2lXQ0aZuADYaA-uPMuQ58yUBhEqO8H-KdOYwEmWY"
ABA_MENSALIDADES = "Base_Clientes"

PRIORIDADE_TIPO = ["E", "c", "R"]
TAMANHO_CODIGO_CONTRATO = 8

SITUACAO_CONTRATO_LABELS = {"A": "Ativo", "E": "Encerrado/Cancelado", "S": "Suspenso", "C": "Cancelado", "P": "Pendente"}
MOTIVO_CANCELAMENTO_LABELS = {
    "01": "Solicitou cancelamento", "02": "Trocou CNPJ faturamento", "03": "Contrato duplicado",
    "04": "Contrato estava com erro", "05": "Unificação de contratos", "06": "Inadimplência financeira",
}
MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

# mapeamento código de material -> descrição do item (tabela oficial do
# CIGAM, usada tanto no parcelasPadrao_json quanto no itensNf_json)
MAPA_CODIGO_MATERIAL = {
    "90000100001": "LICENCIAMENTO",
    "90000100002": "ALUGUEL",
    "90000100010": "ALUGUEL",
    "90000100012": "LICENCIAMENTO PEDESTAL SIMPLES",
    "90000100013": "LICENCIAMENTO PEDESTAL DUPLO",
    "90000100014": "LICENCIAMENTO PEDESTAL TRIPLO",
    "90000100015": "LICENCIAMENTO PEDESTAL QUADRUPLO",
    "90000100016": "LICENCIAMENTO MOBILE SIMPLES",
    "90000100017": "LICENCIAMENTO MOBILE DUPLO",
    "90000100018": "LICENCIAMENTO JOBSITE SIMPLES",
    "90000100019": "LICENCIAMENTO JOBSITE DUPLO",
    "90000100020": "LICENCIAMENTO JOBSITE TRIPLO",
    "90000100021": "LICENCIAMENTO JOBSITE QUADRUPLO",
    "90000100022": "LICENCIAMENTO SONDA",
    "90000100023": "LICENCIAMENTO DATA BI",
}


def formatar_mes_ano(periodo: pd.Period) -> str:
    return f"{MESES_PT[periodo.month - 1].capitalize()}/{str(periodo.year)[2:]}"


def formatar_moeda(valor: float) -> str:
    s = f"{valor:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def mostrar_tabela_com_download(df_ou_styler, nome_arquivo: str, chave: str, **kwargs_dataframe):
    """
    Mostra a tabela normalmente (st.dataframe) E, logo abaixo, um botão de
    download em CSV com ';' como separador — o ícone de exportar que o
    Streamlit já mostra nativamente ao passar o mouse na tabela SEMPRE usa
    vírgula e não dá pra configurar, o que abre errado no Excel BR (que
    espera ';', já que a vírgula é o separador decimal por aqui). Esse
    botão próprio resolve isso; o ícone nativo continua existindo do lado
    (não dá pra removê-lo), mas agora tem a opção certa também.
    """
    df_exportar = df_ou_styler.data if hasattr(df_ou_styler, "data") else df_ou_styler
    st.dataframe(df_ou_styler, **kwargs_dataframe)
    csv_bytes = df_exportar.to_csv(sep=";", index=False).encode("utf-8-sig")  # utf-8-sig: Excel BR abre acentuação certo
    st.download_button(
        "⬇️ Baixar CSV (separado por ;)", data=csv_bytes, file_name=nome_arquivo,
        mime="text/csv", key=chave,
    )


def parse_data_flexivel(serie: pd.Series) -> pd.Series:
    """
    Datas vindas do Google Sheets podem voltar tanto em ISO (quando o
    Sheets reconhece a célula como data, ex: '2020-10-26 00:00:00') quanto
    em dd/mm/aaaa (quando fica como texto puro). Tenta formatos explícitos
    em sequência (sem deixar o pandas "adivinhar", que é o que gera aquele
    warning de dayfirst) até cobrir todos os valores da série.
    """
    serie = serie.astype(str).str.strip()
    serie = serie.replace({"": None, "None": None, "nan": None, "NaT": None})

    resultado = pd.Series(pd.NaT, index=serie.index, dtype="datetime64[ns]")
    formatos = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"]
    for fmt in formatos:
        faltando = resultado.isna() & serie.notna()
        if not faltando.any():
            break
        resultado.loc[faltando] = pd.to_datetime(serie[faltando], format=fmt, errors="coerce")
    return resultado


# --- 2. Autenticação (service account — nenhum colega precisa de acesso
# individual ao BigQuery/Sheets, só ao link do app). As credenciais ficam
# em st.secrets["gcp_service_account"] (configuradas no host do app, nunca
# neste arquivo) — veja o guia de implantação que acompanha este script.
#
# MODO DEMO: se ninguém configurou st.secrets ainda (ex: testando o app
# antes de pedir a service account pro admin do DW), o app roda sozinho
# com dados fictícios em vez de quebrar. Dá pra ver a interface, buscar
# um cliente de exemplo, ver o gráfico com composição/equipamento/cores
# funcionando de verdade — só troca pra dado real quando a service
# account estiver configurada.
MODO_DEMO = "gcp_service_account" not in st.secrets

if MODO_DEMO:
    st.warning(
        "⚠ **Modo demonstração** — sem credenciais configuradas ainda, "
        "mostrando dados fictícios pra você testar a interface. "
        "Configure `st.secrets['gcp_service_account']` (veja o guia de "
        "implantação) pra usar com dados reais.",
        icon="🧪",
    )


@st.cache_resource(show_spinner="Conectando ao BigQuery e Google Sheets...")
def obter_clientes():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ],
    )
    client_bq = bigquery.Client(project=PROJECT_ID, credentials=creds)
    client_gs = gspread.authorize(creds)
    return client_bq, client_gs


client_bq, client_gs = (None, None) if MODO_DEMO else obter_clientes()


def cnpj_invalido(cnpj: str) -> bool:
    """Placeholder comum na base (ex: '00.000.000/0000-00') — todos os
    dígitos iguais não é um CNPJ/CPF real e não deve ser usado como chave
    de busca/consolidação (senão junta clientes não relacionados)."""
    return not cnpj or len(set(cnpj)) == 1


# --- 3. Carrega as planilhas (Base_Clientes + Bombas_Alocadas) ---
def carregar_aba(sheet_id: str, aba: str) -> pd.DataFrame:
    sh = client_gs.open_by_key(sheet_id)
    ws = sh.worksheet(aba)
    df = get_as_dataframe(ws, evaluate_formulas=True, dtype=str)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    return df.reset_index(drop=True)


def gerar_planilhas_demo():
    """
    Base_Clientes/Bombas_Alocadas fictícias, no MESMO formato (tudo como
    string, igual vem do Google Sheets de verdade) — dá pra testar a
    interface inteira sem credenciais. 2 clientes de exemplo, cobrindo os
    cenários mais interessantes já validados no app:
      - 900001 TRANSPORTE DEMO LTDA: par aluguel(90011, cancelado)/
        licenciamento(90012) que depois "virou" união pedestal+sonda —
        testa pareamento, cancelamento parcial, cor por tipo, composição
        real por mês e detecção de equipamento novo.
      - 900002 INDÚSTRIA EXEMPLO S.A.: contrato único com 2 linhas do
        MESMO serial (equipamento "duplo") — testa a contagem por serial
        único.
    """
    linhas_mensalidades = [
        {
            "codigo_cliente": "900001", "codigoContrato": "90011", "Descricao_Material": "ALUGUEL",
            "Preco_Unitario": "180.00", "situacaoContrato": "E",
            "Descricao_Cancelamento": "CONTRATO DUPLICADO", "observacao": "PEDESTAL DEMO - Cidade Exemplo, PR",
            "contratoTerceiro": "", "diaVencimento": "28", "primeira_parcela": "2023-01-28",
            "ultima_parcela": "2023-06-28", "CNPJ_CPF": "12.345.678/0001-00",
            "Cliente_Nome": "TRANSPORTE DEMO LTDA", "Descricao": "PEDESTAL DEMO",
            "dataCriacao": "10/01/2023",
        },
        {
            "codigo_cliente": "900001", "codigoContrato": "90012", "Descricao_Material": "Mensalidade Unificada",
            "Preco_Unitario": "1875.00", "situacaoContrato": "A",
            "Descricao_Cancelamento": "", "observacao": "PEDESTAL DEMO - Cidade Exemplo, PR",
            "contratoTerceiro": "", "diaVencimento": "28", "primeira_parcela": "2023-01-28",
            "ultima_parcela": "", "CNPJ_CPF": "12.345.678/0001-00",
            "Cliente_Nome": "TRANSPORTE DEMO LTDA", "Descricao": "PEDESTAL + SONDA DEMO",
            "dataCriacao": "10/01/2023",
        },
        {
            "codigo_cliente": "900002", "codigoContrato": "90021", "Descricao_Material": "LICENCIAMENTO",
            "Preco_Unitario": "725.00", "situacaoContrato": "A",
            "Descricao_Cancelamento": "", "observacao": "EQUIP DEMO S10/S500 - Cidade Exemplo, PR",
            "contratoTerceiro": "", "diaVencimento": "10", "primeira_parcela": "2024-03-10",
            "ultima_parcela": "", "CNPJ_CPF": "98.765.432/0001-55",
            "Cliente_Nome": "INDÚSTRIA EXEMPLO S.A.", "Descricao": "EQUIP DEMO S10/S500",
            "dataCriacao": "01/03/2024",
        },
    ]

    linhas_bombas = [
        {
            "bomba_nome": "Demo - Pedestal", "serial_equipamento": "DEMO0090012",
            "cliente_cigam_local": "900001", "cliente_cigam_pagante": "900001",
            "local_nome": "TRANSPORTE DEMO LTDA", "pagante_nome": "TRANSPORTE DEMO LTDA",
            "local_cnpj": "12.345.678/0001-00", "pagante_cnpj": "12.345.678/0001-00",
        },
        {
            "bomba_nome": "Demo - S10", "serial_equipamento": "DEMO0090021",
            "cliente_cigam_local": "900002", "cliente_cigam_pagante": "900002",
            "local_nome": "INDÚSTRIA EXEMPLO S.A.", "pagante_nome": "INDÚSTRIA EXEMPLO S.A.",
            "local_cnpj": "98.765.432/0001-55", "pagante_cnpj": "98.765.432/0001-55",
        },
        {
            # mesmo serial que a linha acima -> equipamento "duplo"
            "bomba_nome": "Demo - S500", "serial_equipamento": "DEMO0090021",
            "cliente_cigam_local": "900002", "cliente_cigam_pagante": "900002",
            "local_nome": "INDÚSTRIA EXEMPLO S.A.", "pagante_nome": "INDÚSTRIA EXEMPLO S.A.",
            "local_cnpj": "98.765.432/0001-55", "pagante_cnpj": "98.765.432/0001-55",
        },
    ]

    return pd.DataFrame(linhas_mensalidades), pd.DataFrame(linhas_bombas)


@st.cache_data(ttl=3600, show_spinner="Carregando planilhas (Base_Clientes + Bombas_Alocadas)...")
def carregar_dados_base():
    if MODO_DEMO:
        df_mensalidades, df_bombas = gerar_planilhas_demo()
    else:
        df_mensalidades = carregar_aba(SHEET_ID_MENSALIDADES, ABA_MENSALIDADES)
        df_bombas = carregar_aba(SHEET_ID_BOMBAS, ABA_BOMBAS)

    df_mensalidades["codigo_cliente"] = pd.to_numeric(df_mensalidades["codigo_cliente"], errors="coerce").astype("Int64")
    df_mensalidades["Preco_Unitario"] = pd.to_numeric(df_mensalidades["Preco_Unitario"], errors="coerce")
    df_mensalidades["situacaoContrato"] = df_mensalidades["situacaoContrato"].astype(str).str.strip().str.upper()
    df_mensalidades["primeira_parcela"] = parse_data_flexivel(df_mensalidades["primeira_parcela"])
    df_mensalidades["ultima_parcela"] = parse_data_flexivel(df_mensalidades["ultima_parcela"])

    df_bombas["cliente_cigam_pagante"] = pd.to_numeric(df_bombas["cliente_cigam_pagante"], errors="coerce").astype("Int64")
    df_bombas["cliente_cigam_local"] = pd.to_numeric(df_bombas["cliente_cigam_local"], errors="coerce").astype("Int64")

    # colunas de CNPJ/CPF normalizadas (só dígitos) para permitir busca e
    # consolidação de cadastros duplicados (mesmo CNPJ, codigo_cliente diferente)
    df_mensalidades["_cnpj_norm"] = df_mensalidades["CNPJ_CPF"].astype(str).str.replace(r"\D", "", regex=True)
    df_bombas["_pagante_cnpj_norm"] = df_bombas["pagante_cnpj"].astype(str).str.replace(r"\D", "", regex=True)

    # invalida placeholders (ex: '00.000.000/0000-00') nas colunas
    # normalizadas, pra não serem usados na consolidação por CNPJ
    df_mensalidades.loc[df_mensalidades["_cnpj_norm"].apply(cnpj_invalido), "_cnpj_norm"] = ""
    df_bombas.loc[df_bombas["_pagante_cnpj_norm"].apply(cnpj_invalido), "_pagante_cnpj_norm"] = ""

    return df_mensalidades, df_bombas


df_mensalidades, df_bombas = carregar_dados_base()


# --- 4. Localizar cliente por nome, código CIGAM ou CNPJ/CPF ---
def normalizar_digitos(valor: str) -> str:
    return "".join(ch for ch in str(valor) if ch.isdigit())


def codigos_pelo_cnpj(cnpj: str) -> set:
    """Todos os codigo_cliente (nas duas bases) que têm esse CNPJ/CPF."""
    if cnpj_invalido(cnpj):
        return set()
    m = df_mensalidades[df_mensalidades["_cnpj_norm"] == cnpj]
    b = df_bombas[df_bombas["_pagante_cnpj_norm"] == cnpj]
    return set(m["codigo_cliente"].dropna().astype(int)) | set(b["cliente_cigam_pagante"].dropna().astype(int))


def buscar_cliente(identificador: str):
    """
    Resolve um cliente e retorna (lista_de_codigos, nome, cnpj) — a lista
    pode ter mais de 1 codigo_cliente quando o mesmo CNPJ aparece cadastrado
    sob códigos CIGAM diferentes (duplicidade comum na base). Se não achar
    ou achar nome ambíguo, imprime os candidatos e retorna None.

    Aceita como identificador: CNPJ/CPF (11 ou 14 dígitos, com ou sem
    pontuação), código CIGAM exato, ou parte do nome do cliente.
    """
    identificador = str(identificador).strip()
    digitos = normalizar_digitos(identificador)

    # --- CNPJ (14 dígitos) ou CPF (11 dígitos) ---
    if len(digitos) in (11, 14):
        codigos = codigos_pelo_cnpj(digitos)
        if not codigos:
            st.error(f"Nenhum cliente encontrado com CNPJ/CPF {digitos}.")
            return None
        m = df_mensalidades[df_mensalidades["codigo_cliente"].isin(codigos)]
        nome = m["Cliente_Nome"].iloc[0] if len(m) else \
            df_bombas[df_bombas["cliente_cigam_pagante"].isin(codigos)]["pagante_nome"].iloc[0]
        return sorted(codigos), nome, digitos

    # --- código CIGAM exato ---
    if identificador.isdigit():
        cod = int(identificador)
        candidatos_m = df_mensalidades[df_mensalidades["codigo_cliente"] == cod]
        candidatos_b = df_bombas[df_bombas["cliente_cigam_pagante"] == cod]
        if candidatos_m.empty and candidatos_b.empty:
            st.error(f"Nenhum cliente encontrado com código {cod}.")
            return None
        nome = candidatos_m["Cliente_Nome"].iloc[0] if len(candidatos_m) else candidatos_b["pagante_nome"].iloc[0]
        cnpj_val = candidatos_m["_cnpj_norm"].iloc[0] if len(candidatos_m) else \
            (candidatos_b["_pagante_cnpj_norm"].iloc[0] if len(candidatos_b) else "")
        if cnpj_val:
            codigos = codigos_pelo_cnpj(cnpj_val) | {cod}
            return sorted(codigos), nome, cnpj_val
        return [cod], nome, None

    # --- nome (parcial, case-insensitive) ---
    m = df_mensalidades[df_mensalidades["Cliente_Nome"].str.upper().str.contains(identificador.upper(), na=False)]
    b = df_bombas[df_bombas["pagante_nome"].str.upper().str.contains(identificador.upper(), na=False)]

    candidatos = pd.concat([
        m[["codigo_cliente", "Cliente_Nome", "_cnpj_norm"]].rename(columns={"Cliente_Nome": "nome", "_cnpj_norm": "cnpj"}),
        b[["cliente_cigam_pagante", "pagante_nome", "_pagante_cnpj_norm"]].rename(
            columns={"cliente_cigam_pagante": "codigo_cliente", "pagante_nome": "nome", "_pagante_cnpj_norm": "cnpj"}),
    ]).drop_duplicates(subset="codigo_cliente")

    if len(candidatos) == 0:
        st.error(f"Nenhum cliente encontrado com nome contendo '{identificador}'.")
        return None

    # se todos os candidatos compartilham o mesmo CNPJ, trata como 1 cliente só
    cnpjs_unicos = set(candidatos["cnpj"].dropna()) - {""}
    if len(candidatos) > 1 and len(cnpjs_unicos) != 1:
        st.warning(f"Encontrados {len(candidatos)} clientes com nome contendo '{identificador}' — informe o código ou CNPJ exato:")
        st.dataframe(candidatos.sort_values("nome"), use_container_width=True)
        return None

    if len(cnpjs_unicos) == 1:
        cnpj_comum = next(iter(cnpjs_unicos))
        codigos = codigos_pelo_cnpj(cnpj_comum) | set(candidatos["codigo_cliente"].astype(int))
        return sorted(codigos), candidatos.iloc[0]["nome"], cnpj_comum

    linha = candidatos.iloc[0]
    return [int(linha["codigo_cliente"])], linha["nome"], None


# --- 5. BigQuery: histórico real de parcelas por contrato ---
def normalizar_codigo_contrato(codigo: str) -> str:
    codigo = codigo.strip()
    if codigo.isdigit() and len(codigo) < TAMANHO_CODIGO_CONTRATO:
        return codigo.zfill(TAMANHO_CODIGO_CONTRATO)
    return codigo


COLUNAS_PARCELA = [
    "lancamento", "fatura", "complemento", "situacao", "tipo", "valor",
    "vencimento", "vencimentoOriginal", "emissao", "data", "previsao",
]

# contador de quantas vezes o fallback pro campo JSON foi usado nesta sessão
# (em vez de imprimir uma linha por subcontrato, mostramos 1 resumo no final)
_contador_fallback_json = {"qtd": 0}


@st.cache_data(show_spinner=False)
def obter_dados_demo_bq():
    """
    Parcelas + itens de NF fictícios pros 3 contratos demo (90011, 90012,
    90021) — cobre do mês inicial até o mês ATUAL (sempre parece "em dia"
    quando você testar). Gera exatamente os cenários que já validamos de
    verdade: pareamento aluguel+licenciamento, cancelamento parcial do
    aluguel, transição pra "Mensalidade Unificada" (pedestal+sonda) e um
    equipamento novo entrando nos últimos meses.

    Retorna (parcelas_por_subcodigo: dict[str, DataFrame], itens_por_nf: dict[str, list]).
    """
    hoje = pd.Timestamp.now().to_period("M")
    parcelas = {}
    itens_nf = {}

    def _add_parcela(subcod, mes, valor, fatura):
        subcod = normalizar_codigo_contrato(subcod)
        venc = f"28/{mes.month:02d}/{mes.year}"
        linha = {
            "lancamento": len(parcelas.get(subcod, [])) + 1, "fatura": fatura,
            "complemento": 1, "situacao": "L", "tipo": "E", "valor": valor,
            "vencimento": venc, "vencimentoOriginal": venc,
            "emissao": venc, "data": venc, "previsao": False,
        }
        parcelas.setdefault(subcod, []).append(linha)

    # --- cliente demo 1: aluguel(90011, cancela em jun/23) + licenciamento(90012) ---
    fim_aluguel = pd.Period("2023-06", freq="M")
    inicio_pedestal_sonda = pd.Period("2023-07", freq="M")
    inicio_equip_novo = hoje - 2  # equipamento novo "entrou" há 2 meses
    mes = pd.Period("2023-01", freq="M")
    while mes <= hoje:
        fatura_lic = f"DEMO90012{mes}"
        if mes <= fim_aluguel:
            fatura_alug = f"DEMO90011{mes}"
            _add_parcela("90011", mes, 180.00, fatura_alug)
            itens_nf[fatura_alug] = [("ALUGUEL", 180.00, f"PEDESTAL DEMO - Cidade Exemplo, PR LICENCIAMENTO DE SOFTWARE PERIODO: {formatar_mes_ano(mes)}")]
            _add_parcela("90012", mes, 725.00, fatura_lic)
            itens_nf[fatura_lic] = [("LICENCIAMENTO", 725.00, f"PEDESTAL DEMO - Cidade Exemplo, PR LICENCIAMENTO DE SOFTWARE PERIODO: {formatar_mes_ano(mes)}")]
        elif mes < inicio_equip_novo:
            _add_parcela("90012", mes, 1150.00, fatura_lic)
            itens_nf[fatura_lic] = [
                ("LICENCIAMENTO PEDESTAL SIMPLES", 900.00, f"PEDESTAL DEMO - Cidade Exemplo, PR LICENCIAMENTO DE SOFTWARE PERIODO: {formatar_mes_ano(mes)}"),
                ("LICENCIAMENTO SONDA", 250.00, f"SONDA DEMO - Cidade Exemplo, PR LICENCIAMENTO DE SOFTWARE PERIODO: {formatar_mes_ano(mes)}"),
            ]
        else:
            _add_parcela("90012", mes, 1875.00, fatura_lic)
            itens_nf[fatura_lic] = [
                ("LICENCIAMENTO PEDESTAL SIMPLES", 900.00, "ID 5551001 - PEDESTAL DEMO - Cidade Exemplo, PR"),
                ("LICENCIAMENTO SONDA", 250.00, "ID 5551002 - SONDA DEMO - Cidade Exemplo, PR"),
                ("LICENCIAMENTO PEDESTAL SIMPLES", 725.00, "ID 5551003 - PEDESTAL NOVO - Cidade Exemplo, PR"),
            ]
        mes += 1

    # --- cliente demo 2: licenciamento único (90021), equipamento "duplo" ---
    mes = pd.Period("2024-03", freq="M")
    while mes <= hoje:
        fatura = f"DEMO90021{mes}"
        _add_parcela("90021", mes, 725.00, fatura)
        itens_nf[fatura] = [("LICENCIAMENTO", 725.00, f"ID 5552001 - EQUIP DEMO S10/S500 - Cidade Exemplo, PR LICENCIAMENTO DE SOFTWARE PERIODO: {formatar_mes_ano(mes)}")]
        mes += 1

    parcelas_df = {cod: pd.DataFrame(linhas) for cod, linhas in parcelas.items()}
    return parcelas_df, itens_nf


def buscar_parcelas_bq(codigo_contrato: str) -> pd.DataFrame:
    """
    Busca as parcelas de um contrato. Primeiro tenta o array nativo
    `parcelasContrato` (mais rápido, via UNNEST). Se vier vazio — o que
    acontece para vários contratos mais antigos, mesmo com parcelas
    liquidadas reais no CIGAM — cai para o campo `parcelasContrato_json`
    (string JSON), que tem a MESMA estrutura de campos (fatura, situacao
    L/J, tipo E/c, valor, vencimento, emissao, previsao) e está populado
    mesmo quando o array nativo não está.
    """
    if MODO_DEMO:
        parcelas_demo, _ = obter_dados_demo_bq()
        return parcelas_demo.get(normalizar_codigo_contrato(codigo_contrato), pd.DataFrame(columns=COLUNAS_PARCELA)).copy()

    query = f"""
    SELECT p.lancamento, p.fatura, p.complemento, p.situacao, p.tipo, p.valor,
           p.vencimento, p.vencimentoOriginal, p.emissao, p.data, p.previsao
    FROM `{PROJECT_ID}.bronze.cigam__contratos` c, UNNEST(c.parcelasContrato) AS p
    WHERE c.codigoContrato = @codigo_contrato
    ORDER BY p.vencimento
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("codigo_contrato", "STRING", codigo_contrato)]
    )
    try:
        df = client_bq.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
        if not df.empty:
            return df
    except Exception:
        # se 'emissao'/'data' não existirem no array nativo (schema pode
        # divergir do JSON), cai direto pro fallback JSON em vez de quebrar.
        # Sem aviso visível no app (é um detalhe técnico interno, não
        # algo que o colega usando o app precise ver) — só segue o fluxo.
        pass

    # fallback: array nativo vazio (ou query nativa falhou) -> tenta o campo JSON
    query_json = f"""
    SELECT parcelasContrato_json
    FROM `{PROJECT_ID}.bronze.cigam__contratos`
    WHERE codigoContrato = @codigo_contrato
    """
    resultado = client_bq.query(query_json, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    if resultado.empty or pd.isna(resultado["parcelasContrato_json"].iloc[0]):
        return pd.DataFrame(columns=COLUNAS_PARCELA)

    try:
        registros = json.loads(resultado["parcelasContrato_json"].iloc[0])
    except (json.JSONDecodeError, TypeError):
        return pd.DataFrame(columns=COLUNAS_PARCELA)

    if not registros:
        return pd.DataFrame(columns=COLUNAS_PARCELA)

    _contador_fallback_json["qtd"] += 1
    df_json = pd.DataFrame(registros)
    colunas_presentes = [c for c in COLUNAS_PARCELA if c in df_json.columns]
    return df_json[colunas_presentes]


def preparar_dados_subcontrato(df_parcelas: pd.DataFrame) -> pd.DataFrame:
    """Mesma lógica de dedup do script de contrato único, mas retorna a
    mensalidade por mês (sem separar juros) + o(s) número(s) de fatura/NF
    daquele mês — usada como peça para somar vários subcontratos de um
    grupo unificado."""
    if df_parcelas.empty:
        return pd.DataFrame(columns=["mes", "valor", "fatura"])

    df = df_parcelas.copy()
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")

    # FILTRO PRINCIPAL: o campo 'previsao' é a marcação OFICIAL do CIGAM
    # pra "essa parcela é só previsão/projeção futura, ainda não é uma
    # cobrança de verdade" (o checkbox "Previsão" que aparece na tela de
    # Parcelas do Contrato). É mais confiável que qualquer heurística por
    # data — usa direto o que o próprio CIGAM já marcou.
    def eh_previsao(valor):
        if pd.isna(valor):
            return False
        if isinstance(valor, (bool,)):
            return valor
        return str(valor).strip().lower() in ("true", "1", "sim", "s", "yes", "y")

    if "previsao" in df.columns:
        df = df[~df["previsao"].apply(eh_previsao)]

    # data_ref = mês de REFERÊNCIA da mensalidade (usado só pra agrupar no
    # eixo do gráfico, ex: "PERÍODO: Novembro/2020") — continua vindo do
    # vencimento, isso não muda
    df["data_ref"] = pd.to_datetime(df["vencimentoOriginal"], dayfirst=True, errors="coerce")
    df["data_ref"] = df["data_ref"].fillna(pd.to_datetime(df["vencimento"], dayfirst=True, errors="coerce"))

    # já o CRITÉRIO DE CORTE (é provisão futura ou já foi lançado de
    # verdade?) usa a data de EMISSÃO da NF. Vencimento é só "quando
    # vence", não indica se a cobrança já foi de fato emitida — uma
    # parcela pode vencer daqui a poucos dias mas já ter sido emitida há
    # semanas (inclui), ou o contrário: vencimento já passado mas nunca
    # emitida (é só provisão, exclui).
    #
    # IMPORTANTE: uso APENAS 'emissao', não caio pra 'data' (data do
    # lançamento contábil) linha a linha — o CIGAM parece criar o
    # lançamento de provisões futuras com antecedência (campo 'data'
    # preenchido) mesmo sem ter emitido a NF ainda (campo 'emissao' só
    # preenche quando a NF sai de verdade). Usar 'data' como fallback
    # por linha incluía provisão por engano. Só recorro a 'data' (pro
    # SUBCONTRATO INTEIRO, não linha a linha) se ele não tiver NENHUMA
    # emissão registrada — sinal de que esse schema não usa esse campo.
    if "emissao" in df.columns:
        df["data_emissao"] = pd.to_datetime(df["emissao"], dayfirst=True, errors="coerce")
    else:
        df["data_emissao"] = pd.NaT

    mes_atual = pd.Timestamp.now().to_period("M")
    if df["data_emissao"].notna().any():
        # tem pelo menos uma emissão real registrada nesse subcontrato ->
        # usa 'emissao' como critério; linhas sem emissão ficam de fora
        # (são provisão, mesmo que 'data'/vencimento já tenham passado)
        df = df[df["data_emissao"].notna() & (df["data_emissao"].dt.to_period("M") <= mes_atual)]
    elif "data" in df.columns and pd.to_datetime(df["data"], dayfirst=True, errors="coerce").notna().any():
        # nenhuma linha tem 'emissao' preenchida -> esse schema
        # provavelmente não usa esse campo; cai pra 'data' como um todo
        df["data_lancamento"] = pd.to_datetime(df["data"], dayfirst=True, errors="coerce")
        df = df[df["data_lancamento"].notna() & (df["data_lancamento"].dt.to_period("M") <= mes_atual)]
    else:
        # fallback final: nem emissao nem data existem -> volta a usar
        # vencimento, mesma lógica de antes, pra não zerar o histórico
        df = df[df["data_ref"].isna() | (df["data_ref"].dt.to_period("M") <= mes_atual)]

    df["mes"] = df["data_ref"].dt.to_period("M")
    df["chave"] = df["fatura"].fillna(df["lancamento"].astype(str))

    df = df[df["situacao"] != "J"].copy()  # juros à parte, não entram na mensalidade
    df["prioridade"] = df["tipo"].apply(lambda t: PRIORIDADE_TIPO.index(t) if t in PRIORIDADE_TIPO else 99)
    df = df.sort_values("prioridade").drop_duplicates(subset="chave", keep="first")

    return df.groupby("mes", as_index=False).agg(
        valor=("valor", "sum"),
        fatura=("fatura", lambda s: ", ".join(sorted(set(str(x) for x in s if pd.notna(x) and str(x).strip())))),
    )



def obter_historico_unificado(codigo_contrato_grupo: str) -> pd.DataFrame:
    """
    Recebe um codigoContrato como vem na Base_Clientes (pode ser um único
    código, ex: '7859', ou um par unificado, ex: '2135/2136'). Busca cada
    subcontrato no BigQuery, deduplica, e SOMA por mês — reproduzindo o
    "Preco_Unitario" combinado da Base_Clientes, mas com histórico mensal.
    Mantém também os números de NF de cada subcontrato naquele mês (um
    grupo unificado aluguel+licenciamento tem 2 NFs por mês, uma de cada).

    Também monta a coluna 'composicao_mes': a composição % REAL de cada
    mês (ex: "Aluguel: R$ 180,00 (19,0%) | Licenciamento: R$ 768,60
    (81,0%)"), buscando os itens de cada NF em
    `cigam__notas_fiscais.itensNf_json` — numa consulta só em lote (não
    uma por mês), cobrindo o histórico inteiro.

    Isso funciona pros dois jeitos que uma composição pode acontecer:
    (a) contratos SEPARADOS, cada um com sua própria NF de 1 item (ex:
    aluguel + licenciamento, dois códigos de contrato via "/"), ou
    (b) UM contrato só, mas a MESMA nota fiscal cobrindo vários materiais
    juntos (ex: licenciamento pedestal + sonda na mesma NF, mesmo código
    de contrato) — o cálculo é feito depois de somar por mês, olhando
    todos os itens de todas as NFs daquele mês juntas.
    """
    subcodigos = [normalizar_codigo_contrato(c) for c in str(codigo_contrato_grupo).split("/")]
    partes = []
    for cod in subcodigos:
        df_parcelas = buscar_parcelas_bq(cod)
        partes.append(preparar_dados_subcontrato(df_parcelas))

    if not partes or all(p.empty for p in partes):
        return pd.DataFrame(columns=["mes", "valor", "fatura", "composicao_mes"])

    combinado = pd.concat(partes, ignore_index=True)

    # Pra cada mês, quantos subcontratos DEVERIAM ter contribuído — não é
    # um número fixo (ex: sempre 2), é calculado por mês, olhando se
    # aquele mês cai DENTRO do período ativo de cada subcontrato. Isso é
    # importante porque um subcontrato pode encerrar de vez (ex: aluguel
    # cancelado) e o outro continuar sozinho pra sempre — os meses depois
    # disso são normais (só o que restou mesmo), não "incompletos". Só é
    # incompleto quando um mês cai DENTRO do período em que o subcontrato
    # ainda estava ativo, mas ele pulou aquele mês específico (ex:
    # licenciamento "não rodou" um mês no meio do período em que sempre
    # cobrou normalmente).
    ranges_partes = [(p["mes"].min(), p["mes"].max()) for p in partes if not p.empty]

    def _qtd_esperada_no_mes(mes):
        return sum(1 for (mn, mx) in ranges_partes if mn <= mes <= mx)

    agrupado = combinado.groupby("mes", as_index=False).agg(
        valor=("valor", "sum"),
        fatura=("fatura", lambda s: ", ".join(sorted(set(x for x in s if x)))),
        qtd_subcontratos_no_mes=("valor", "count"),
    ).sort_values("mes").reset_index(drop=True)
    agrupado["qtd_subcontratos_esperados"] = agrupado["mes"].apply(_qtd_esperada_no_mes)
    agrupado["completo"] = agrupado["qtd_subcontratos_no_mes"] >= agrupado["qtd_subcontratos_esperados"]
    agrupado = agrupado.drop(columns=["qtd_subcontratos_no_mes", "qtd_subcontratos_esperados"])

    # composição real mês a mês: busca em lote os itens de TODAS as NFs
    # que apareceram (de qualquer mês, de qualquer subcontrato) — depois,
    # pra cada mês, junta os itens de todas as NFs daquele mês (pode vir
    # de uma NF só com vários itens, ou de várias NFs com 1 item cada)
    todas_faturas = set()
    for f in agrupado["fatura"].dropna():
        todas_faturas.update(x.strip() for x in str(f).split(",") if x.strip())
    mapa_itens_por_nf = buscar_itens_notas_fiscais_lote(list(todas_faturas)) if todas_faturas else {}

    def _itens_do_mes(fatura_str):
        itens_mes = []
        for f in str(fatura_str).split(","):
            f = f.strip()
            if f in mapa_itens_por_nf:
                itens_mes.extend(mapa_itens_por_nf[f])
        return itens_mes

    def _composicao_do_mes(fatura_str):
        itens_mes = _itens_do_mes(fatura_str)
        if len(itens_mes) < 2:
            return ""
        agregados = {}
        for desc, val, _texto in itens_mes:
            if val is None or pd.isna(val):
                continue
            agregados[desc] = agregados.get(desc, 0) + val
        total = sum(agregados.values())
        if not total:
            return ""
        partes_txt = [
            f"{str(desc).capitalize()}: {formatar_moeda(val)} ({val / total * 100:.1f}%)"
            for desc, val in sorted(agregados.items(), key=lambda x: -x[1])
        ]
        # limita a quantidade de partes mostradas — um contrato com muitos
        # tipos de material diferentes (raro, mas acontece) não deve virar
        # uma linha gigante ilegível
        return resumir_lista(partes_txt, max_itens=6, max_chars_item=80)

    def _descricao_do_mes(fatura_str):
        """Texto livre da NF (local do equipamento + período de
        referência real, ex: 'PEDESTAL SIMPLES - Feira de Santana, BA
        LICENCIAMENTO DE SOFTWARE PERIODO: Setembro/2025') — útil mesmo
        quando a NF só tem 1 item (sem % de composição pra mostrar).
        Limitado (resumir_lista) porque um contrato com muitos
        equipamentos/seriais (ex: 17 itens numa NF só) gera 1 texto único
        por item, e sem cortar isso vira uma caixa de hover gigante e
        ilegível, cobrindo o gráfico inteiro."""
        itens_mes = _itens_do_mes(fatura_str)
        textos = sorted(set(t for _, _, t in itens_mes if t))
        return resumir_lista(textos, max_itens=6, max_chars_item=60)

    if mapa_itens_por_nf:
        agrupado["composicao_mes"] = agrupado["fatura"].apply(_composicao_do_mes)
        agrupado["descricao_mes"] = agrupado["fatura"].apply(_descricao_do_mes)
        # lista COMPLETA (não resumida) de itens por mês — guardada à
        # parte pra dar pra comparar mês a mês e apontar qual equipamento
        # específico entrou/saiu quando o valor muda
        agrupado["itens_mes_lista"] = agrupado["fatura"].apply(_itens_do_mes)
    else:
        agrupado["composicao_mes"] = ""
        agrupado["descricao_mes"] = ""
        agrupado["itens_mes_lista"] = [[] for _ in range(len(agrupado))]
    return agrupado


# --- 6. Gráfico interativo (Plotly) ---
# Paleta oficial da CTA (Guia de Degradês e Identidade Visual, jul/2026):
#   CTA Verde #19e098 · CTA Violeta #574ae2 · CTA Azul #382fd8 · CTA Preto
#   #1E1E1E · CTA Cinza Claro #F1F1F1 — mais os tons das séries
#   sequenciais (verde/azul, 5 tons cada) e a paleta derivada pra
#   documentos formais (Azul Escuro #1a1a2e, Azul Muito Claro #f0f1fd).
#
# paleta GENÉRICA de fallback (usada só quando não dá pra identificar o
# tipo do grupo pelo texto) — monta com o que sobra da paleta oficial
# depois de reservar as cores semânticas abaixo (união/licenciamento/
# aluguel/sonda/aumento/redução), pra nunca colidir com elas
PALETA_CONTRATOS = [
    "#574ae2",  # CTA Violeta
    "#0fad72",  # série verde, tom 4
    "#8f87ec",  # série azul, tom 2
    "#087a50",  # série verde, tom 5 (mais escuro)
    "#c4bff6",  # série azul, tom 1 (mais claro)
    "#b3f7e0",  # série verde, tom 1 (mais claro)
]

# cores reservadas exclusivamente para os marcadores de aumento/redução —
# a própria CTA já define essa convenção na "Série Divergente" do guia de
# cores: Azul = negativo, Verde = positivo (não vermelho/verde genérico)
COR_AUMENTO = "#19e098"  # CTA Verde
COR_REDUCAO = "#1a1a2e"  # Azul Escuro (documentos formais) — bem mais
# escuro/dessaturado que o Azul Institucional (#382fd8) e o tom escuro da
# série azul (#2318a8) já usados nas linhas de contrato, pra não colidir
# visualmente com elas quando o marcador cai em cima de uma linha azul

# cores por TIPO de contrato (identificado pelo texto — descrição,
# composição, observação — não por ordem arbitrária). Se o tipo do grupo
# mudar (ex: passou a ser "Mensalidade Unificada" recentemente, antes
# era só licenciamento), a cor acompanha automaticamente, porque a
# classificação é refeita a cada vez com o dado mais recente disponível.
COR_UNIAO_ALUGUEL_LICENCIAMENTO = "#382fd8"  # CTA Azul
COR_SO_LICENCIAMENTO = "#2318a8"  # série azul, tom mais escuro
COR_SO_ALUGUEL = "#66efc1"  # série verde, tom 2
COR_SONDA = "#F1F1F1"  # CTA Cinza Claro


def classificar_cor_grupo(h: dict) -> str:
    """
    Decide a cor da linha pelo TEXTO (descrição, composição, observação,
    e o item/composição do mês mais recente disponível), não por ordem
    arbitrária. Prioridade: sonda > união aluguel+licenciamento > só
    aluguel > só licenciamento > (fallback: paleta genérica, quando não
    dá pra identificar nada).
    """
    df_hist = h.get("df")
    ultima_composicao, ultima_descricao = "", ""
    if df_hist is not None and not df_hist.empty:
        df_ordenado = df_hist.sort_values("mes")
        if "composicao_mes" in df_ordenado.columns:
            nao_vazias = df_ordenado["composicao_mes"].fillna("")
            nao_vazias = nao_vazias[nao_vazias != ""]
            if len(nao_vazias):
                ultima_composicao = nao_vazias.iloc[-1]
        if "descricao_mes" in df_ordenado.columns:
            nao_vazias = df_ordenado["descricao_mes"].fillna("")
            nao_vazias = nao_vazias[nao_vazias != ""]
            if len(nao_vazias):
                ultima_descricao = nao_vazias.iloc[-1]

    texto_completo = " ".join(str(x) for x in [
        h.get("descricao", ""), h.get("composicao", ""), h.get("observacao", ""),
        ultima_composicao, ultima_descricao,
    ]).upper()

    tem_aluguel = "ALUGUEL" in texto_completo
    tem_licenciamento = "LICENCIAMENTO" in texto_completo or "MENSALIDADE UNIFICADA" in texto_completo
    tem_sonda = "SONDA" in texto_completo

    if tem_sonda and not (tem_aluguel and tem_licenciamento):
        return COR_SONDA
    if tem_aluguel and tem_licenciamento:
        return COR_UNIAO_ALUGUEL_LICENCIAMENTO
    if tem_aluguel:
        return COR_SO_ALUGUEL
    if tem_licenciamento:
        return COR_SO_LICENCIAMENTO
    return None  # sem sinal textual -> cai pro fallback da paleta genérica


def plotar_historico_multi(
    historicos: list, titulo: str, subtitulo: str = "", incluir_total: bool = True,
    mostrar_texto_variacao: bool = False, limiar_anotacao_pct: float = 5.0,
):
    """
    Plota UM gráfico com uma linha por grupo de contrato (cada `historicos[i]`
    é um dict com 'grupo', 'descricao', 'df' (mes/valor), e opcionalmente
    'data_cancelamento' e 'motivo_cancelamento'). Todas as linhas
    compartilham o mesmo eixo X (união de todos os meses de todos os
    contratos).

    Cada linha, suas anotações de aumento/redução (R$ e %) e seu marcador
    de cancelamento formam um único "legendgroup" — clicar na legenda
    esconde/mostra tudo junto (não fica anotação "órfã" no ar quando a
    linha é escondida).

    Todo ponto de mudança ganha um marcador colorido (verde/vermelho) —
    isso sempre aparece. O TEXTO da variação (R$/%) escrito no gráfico
    fica DESLIGADO por padrão (`mostrar_texto_variacao=False`), porque em
    séries com muitas mudanças (mesmo só as "grandes") o texto se
    amontoa e vira ruído visual — a informação completa continua
    disponível passando o mouse em cima do ponto (hover). Ligue
    `mostrar_texto_variacao=True` só se a linha tiver poucas mudanças e
    o texto no gráfico realmente ajudar.

    Botões no topo permitem filtrar Todos / Só Ativos / Só Encerrados.
    """
    historicos_validos = [h for h in historicos if not h["df"].empty]
    if not historicos_validos:
        st.info(f"Sem histórico de parcelas cobradas para: {titulo}")
        return None, ""

    todos_periodos = sorted(set().union(*[set(h["df"]["mes"]) for h in historicos_validos]))
    todos_meses_str = [str(p) for p in todos_periodos]
    todos_eixo_labels = [formatar_mes_ano(p) for p in todos_periodos]
    posicoes_texto = ["top center", "bottom center", "top left", "bottom right"]

    fig = go.Figure()
    trace_situacao = []  # paralelo a fig.data, para os botões de filtro
    detalhes_mudancas_console = []  # (grupo, mes_fmt, direcao, valor_bruto, pct, novos, removidos, alterados) — sem resumir, pra imprimir no console

    for i, h in enumerate(historicos_validos):
        df_m = h["df"].sort_values("mes").reset_index(drop=True)

        # preenche os meses que faltam no MEIO do período (do primeiro ao
        # último mês desse contrato) com um "buraco" (NaN) — sem isso, o
        # Plotly conecta os pontos vizinhos com uma linha reta, dando a
        # entender (errado) que houve cobrança contínua quando na
        # verdade aquele mês não teve nenhuma parcela emitida. Com o NaN,
        # a linha quebra visivelmente nesse trecho.
        if len(df_m) > 1:
            intervalo_completo = pd.period_range(df_m["mes"].min(), df_m["mes"].max(), freq="M")
            df_m = df_m.set_index("mes").reindex(intervalo_completo).rename_axis("mes").reset_index()

        meses_str = [str(m) for m in df_m["mes"]]
        eixo_labels = [formatar_mes_ano(p) for p in df_m["mes"]]
        valores = pd.to_numeric(df_m["valor"], errors="coerce").tolist()
        # "completo" = todos os subcontratos desse grupo emitiram NF nesse
        # mês. Quando não é completo (ex: só o aluguel rodou, o
        # licenciamento não), o valor somado fica artificialmente baixo —
        # NÃO É uma redução de preço de verdade, é falta de dado. Meses
        # sem essa informação (linhas de grupo único, sem "/") contam
        # como completos por padrão.
        completos = df_m["completo"].fillna(False).tolist() if "completo" in df_m.columns else [True] * len(valores)
        cor = classificar_cor_grupo(h) or PALETA_CONTRATOS[i % len(PALETA_CONTRATOS)]
        grupo_legenda = f"grupo_{i}"
        situacao_grupo = h.get("situacao")

        # variação só é calculada ENTRE meses completos — um mês
        # incompleto não vira nem origem nem destino de "aumento/redução",
        # senão a queda artificial apareceria como se fosse real
        variacoes_pct, variacoes_bruto = [None] * len(valores), [None] * len(valores)
        origem_variacao_idx = [None] * len(valores)  # de qual mês anterior essa variação foi calculada
        ultimo_idx_completo = None
        for j in range(len(valores)):
            if pd.isna(valores[j]) or not completos[j]:
                continue
            if ultimo_idx_completo is not None:
                ant, atu = valores[ultimo_idx_completo], valores[j]
                if ant != 0 and atu != ant:
                    variacoes_bruto[j] = atu - ant
                    variacoes_pct[j] = (atu - ant) / ant * 100
                    origem_variacao_idx[j] = ultimo_idx_completo
            ultimo_idx_completo = j

        # equipamento(s) que entraram/saíram entre o mês de origem da
        # variação e o mês atual — identificados pelo "ID xxxxx" que
        # aparece no texto de cada item, comparando as duas listas
        itens_mes_lista = df_m["itens_mes_lista"].tolist() if "itens_mes_lista" in df_m.columns else [[]] * len(valores)

        def _extrair_id_equipamento(texto):
            m = re.search(r"ID\s*(\d+)", str(texto))
            return m.group(1) if m else None

        def _mudancas_equipamento(idx_anterior, idx_atual, resumir=True):
            """Compara os equipamentos do mês de comparação com os deste
            mês, retornando 3 listas: equipamentos novos, removidos, e —
            importante — equipamentos que JÁ EXISTIAM nos dois meses mas
            tiveram o VALOR alterado (não só entrada/saída de equipamento,
            um equipamento que já estava lá pode ter ficado mais caro).
            resumir=False retorna as listas SEM cortar (usado no relatório
            impresso no console, que não tem limite de espaço como o
            hover do gráfico)."""
            if idx_anterior is None:
                return "", "", ""
            itens_ant = itens_mes_lista[idx_anterior] or []
            itens_atu = itens_mes_lista[idx_atual] or []
            mapa_ant = {_extrair_id_equipamento(t): (v, t) for _, v, t in itens_ant if _extrair_id_equipamento(t)}
            mapa_atu = {_extrair_id_equipamento(t): (v, t) for _, v, t in itens_atu if _extrair_id_equipamento(t)}

            novos = [
                f"{mapa_atu[k][1]} ({formatar_moeda(mapa_atu[k][0])})" if pd.notna(mapa_atu[k][0]) else mapa_atu[k][1]
                for k in mapa_atu if k not in mapa_ant
            ]
            removidos = [
                f"{mapa_ant[k][1]} ({formatar_moeda(mapa_ant[k][0])})" if pd.notna(mapa_ant[k][0]) else mapa_ant[k][1]
                for k in mapa_ant if k not in mapa_atu
            ]

            alterados = []
            for k in mapa_atu:
                if k not in mapa_ant:
                    continue
                v_ant, _t_ant = mapa_ant[k]
                v_atu, t_atu = mapa_atu[k]
                if pd.isna(v_ant) or pd.isna(v_atu) or abs(v_atu - v_ant) < 0.01:
                    continue
                sinal = "+" if v_atu > v_ant else "-"
                alterados.append(f"{t_atu} ({sinal}{formatar_moeda(abs(v_atu - v_ant))})")

            if not resumir:
                return novos, removidos, alterados

            txt_novos = resumir_lista(novos, max_itens=4, max_chars_item=55) if novos else ""
            txt_removidos = resumir_lista(removidos, max_itens=4, max_chars_item=55) if removidos else ""
            txt_alterados = resumir_lista(alterados, max_itens=4, max_chars_item=65) if alterados else ""
            return txt_novos, txt_removidos, txt_alterados

        # cabeçalho fixo do hover (repetido em todo ponto da linha): descrição
        # do material + observação. "Item (situação atual)" foi removido —
        # agora que temos o item real de cada mês (via NF), a versão
        # "atual" genérica não agrega mais nada, só duplicava informação
        #
        # RÓTULO DINÂMICO: se o grupo teve um cancelamento PARCIAL (só um
        # dos códigos foi cancelado, ex: aluguel 90011, mas o
        # licenciamento 90012 continuou), o rótulo "90011/90012" some
        # sendo enganoso pros meses DEPOIS do cancelamento — dá a entender
        # que os dois ainda estão em vigor. A partir do mês do
        # cancelamento, o cabeçalho passa a usar só o(s) código(s) que
        # realmente continuam ativos.
        codigos_grupo = h.get("codigos_grupo") or []
        codigos_ativos = h.get("codigos_ativos") or []
        teve_cancelamento_parcial = bool(codigos_ativos) and len(codigos_ativos) < len(codigos_grupo)
        rotulo_pos_cancelamento = "/".join(codigos_ativos) if teve_cancelamento_parcial else None

        mes_cancel = None
        data_cancel = h.get("data_cancelamento")
        if data_cancel and str(data_cancel).strip():
            data_cancel_dt = pd.to_datetime(str(data_cancel), dayfirst=True, errors="coerce")
            if pd.notna(data_cancel_dt):
                mes_cancel = str(data_cancel_dt.to_period("M"))

        def _cabecalho_para_mes(mes_str_ponto):
            """Antes do cancelamento (ou se não houve cancelamento
            parcial): usa o grupo completo, ex: '90011/90012'. A partir
            do mês do cancelamento: usa só o(s) código(s) ainda ativo(s),
            ex: '90012' — reflete o que está em vigor de verdade naquele
            mês, não o que era em vigor lá no início do histórico."""
            rotulo = h["grupo"]
            if rotulo_pos_cancelamento and mes_cancel and mes_str_ponto >= mes_cancel:
                rotulo = rotulo_pos_cancelamento
            texto = f"<b>Contrato {rotulo}</b> — {h['descricao']}"
            if h.get("observacao"):
                texto += f"<br>Obs (situação atual): {h['observacao']}"
            return texto

        faturas = df_m["fatura"].tolist() if "fatura" in df_m.columns else [""] * len(valores)
        composicoes_mes = df_m["composicao_mes"].fillna("").tolist() if "composicao_mes" in df_m.columns else [""] * len(valores)
        descricoes_mes = df_m["descricao_mes"].fillna("").tolist() if "descricao_mes" in df_m.columns else [""] * len(valores)

        hover_texts = []
        for j, (mes_fmt, val) in enumerate(zip(eixo_labels, valores)):
            cabecalho_hover = _cabecalho_para_mes(meses_str[j])
            if pd.isna(val):
                texto = f"{cabecalho_hover}<br>{mes_fmt}: <i>sem parcela emitida neste mês</i>"
                hover_texts.append(texto)
                continue
            texto = f"{cabecalho_hover}<br>{mes_fmt}: {formatar_moeda(val)}"
            if faturas[j]:
                texto += f"<br>NF: {faturas[j]}"
            # composição REAL desse mês específico (buscada na NF de
            # verdade daquele mês). SEM fallback pra "composição atual" —
            # já que buscamos nota por nota, misturar uma aproximação de
            # outra época só confundiria; se não achar a composição real
            # dessa NF, simplesmente não mostra nada em vez de arriscar
            # mostrar algo que pode não bater com o período
            if composicoes_mes[j]:
                texto += f"<br>Composição (deste mês): {composicoes_mes[j]}"
            # texto livre da NF (local + período de referência real) —
            # aparece mesmo quando a NF só tem 1 item (sem % de composição),
            # ex: "PEDESTAL SIMPLES - Feira de Santana, BA LICENCIAMENTO
            # DE SOFTWARE PERIODO: Setembro/2025"
            if descricoes_mes[j]:
                texto += f"<br>Item (deste mês): {descricoes_mes[j]}"
            if not completos[j]:
                texto += (
                    "<br><b>⚠ Dado incompleto:</b> nem todos os itens deste grupo "
                    "emitiram NF neste mês (ex: licenciamento não rodou) — "
                    "valor não reflete a mensalidade cheia, não é redução de preço"
                )
            elif variacoes_pct[j] is not None:
                direcao = "Aumento" if variacoes_bruto[j] > 0 else "Redução"
                texto += f"<br>{direcao}: {formatar_moeda(abs(variacoes_bruto[j]))} ({variacoes_pct[j]:+.1f}%)"
                # aponta qual equipamento específico entrou/saiu/mudou de
                # valor entre o mês de comparação e este, quando dá pra
                # identificar — "valor alterado" é o mais direto pra
                # responder "onde exatamente aumentou"
                txt_novos, txt_removidos, txt_alterados = _mudancas_equipamento(origem_variacao_idx[j], j)
                if txt_alterados:
                    texto += f"<br>↕ Valor alterado: {txt_alterados}"
                if txt_novos:
                    texto += f"<br>+ Equipamento(s) novo(s): {txt_novos}"
                if txt_removidos:
                    texto += f"<br>− Equipamento(s) removido(s): {txt_removidos}"
                # versão COMPLETA (sem resumir) guardada à parte pra
                # imprimir no console depois do gráfico — o hover tem
                # espaço limitado, o console não
                novos_full, removidos_full, alterados_full = _mudancas_equipamento(
                    origem_variacao_idx[j], j, resumir=False
                )
                if novos_full or removidos_full or alterados_full:
                    detalhes_mudancas_console.append((
                        h["grupo"], mes_fmt, direcao, variacoes_bruto[j], variacoes_pct[j],
                        novos_full, removidos_full, alterados_full,
                    ))
            hover_texts.append(texto)

        # linha principal — única com showlegend=True do grupo.
        # connectgaps=False é explícito (já é o padrão) pra garantir que
        # meses sem parcela (valor=NaN, preenchidos no reindex acima)
        # quebrem a linha visualmente, em vez de serem "costurados"
        fig.add_trace(go.Scatter(
            x=meses_str, y=valores, mode="lines+markers", connectgaps=False,
            name=f"{h['grupo']} — {h['descricao']}"[:40],
            legendgroup=grupo_legenda, showlegend=True,
            line=dict(color=cor, width=2), marker=dict(size=6, color=cor),
            hovertext=hover_texts, hoverinfo="text",
        ))
        trace_situacao.append(situacao_grupo)

        # marcador âmbar (nem verde nem vermelho) nos meses "incompletos"
        # — sinaliza visualmente que aquele ponto não é comparável (não é
        # aumento nem redução de verdade, é só falta de NF de uma parte
        # do grupo), sem confundir com os marcadores de variação real
        idx_incompletos = [j for j, c in enumerate(completos) if not c and pd.notna(valores[j])]
        if idx_incompletos:
            fig.add_trace(go.Scatter(
                x=[meses_str[j] for j in idx_incompletos],
                y=[valores[j] for j in idx_incompletos],
                mode="markers",
                marker=dict(size=10, color="#f59e0b", symbol="triangle-up", line=dict(color="white", width=1)),
                legendgroup=grupo_legenda, showlegend=False,
                hovertext=[hover_texts[j] for j in idx_incompletos], hoverinfo="text",
            ))
            trace_situacao.append(situacao_grupo)

        # TODOS os pontos de mudança ganham um marcador colorido (sem texto)
        idx_variacao = [j for j, p in enumerate(variacoes_pct) if p is not None]
        if idx_variacao:
            fig.add_trace(go.Scatter(
                x=[meses_str[j] for j in idx_variacao],
                y=[valores[j] for j in idx_variacao],
                mode="markers",
                marker=dict(
                    size=8,
                    color=[COR_AUMENTO if variacoes_bruto[j] > 0 else COR_REDUCAO for j in idx_variacao],
                    line=dict(color="white", width=1),
                ),
                legendgroup=grupo_legenda, showlegend=False, hoverinfo="skip",
            ))
            trace_situacao.append(situacao_grupo)

        # só as mudanças RELEVANTES (>= limiar) ganham o texto escrito no
        # gráfico — e só quando mostrar_texto_variacao=True (desligado por
        # padrão pra não empilhar rótulos; a info completa está no hover)
        if mostrar_texto_variacao:
            idx_relevantes = [j for j in idx_variacao if abs(variacoes_pct[j]) >= limiar_anotacao_pct]
            if idx_relevantes:
                fig.add_trace(go.Scatter(
                    x=[meses_str[j] for j in idx_relevantes],
                    y=[valores[j] for j in idx_relevantes],
                    mode="text",
                    text=[
                        f"{'+' if variacoes_bruto[j] > 0 else '-'}{formatar_moeda(abs(variacoes_bruto[j]))} ({variacoes_pct[j]:+.1f}%)"
                        for j in idx_relevantes
                    ],
                    textposition=posicoes_texto[i % len(posicoes_texto)],
                    textfont=dict(
                        size=10,
                        color=[COR_AUMENTO if variacoes_bruto[j] > 0 else COR_REDUCAO for j in idx_relevantes],
                    ),
                    legendgroup=grupo_legenda, showlegend=False, hoverinfo="skip",
                ))
                trace_situacao.append(situacao_grupo)

        # marcador de cancelamento + linha vertical, ambos como trace do
        # mesmo legendgroup (em vez de fig.add_vline, que é um "shape" do
        # layout e não esconde com a legenda).
        #
        # IMPORTANTE: a data de cancelamento vem de QUALQUER subcontrato
        # do grupo (ex: um par aluguel+licenciamento) — se só o aluguel
        # foi cancelado mas o licenciamento continuou, o GRUPO não
        # "acabou" ali, só uma parte dele. Se a linha continua tendo
        # valor depois dessa data, isso é cancelamento PARCIAL: marcador
        # âmbar (não preto) com texto deixando claro que é só uma parte,
        # não o contrato inteiro. Só usa o losango preto "fim de verdade"
        # quando não há mais nada depois.
        # (data_cancel/mes_cancel já foram calculados mais acima, junto
        # com o cabeçalho do hover — reaproveitados aqui)
        if data_cancel and str(data_cancel).strip():
            if mes_cancel in meses_str:
                idx = meses_str.index(mes_cancel)
                motivo = h.get("motivo_cancelamento") or ""
                continua_depois = any(pd.notna(v) for v in valores[idx + 1:])

                if continua_depois:
                    hover_cancel = (
                        f"<b>⚠ Cancelamento PARCIAL</b><br>Um dos contratos deste grupo "
                        f"({h['grupo']}) foi cancelado em {data_cancel}"
                    )
                    if motivo:
                        hover_cancel += f" — Motivo: {motivo}"
                    hover_cancel += "<br>Contrato segue de outra forma."
                    cor_marcador, simbolo, tamanho = "#f59e0b", "circle-open", 13
                else:
                    hover_cancel = f"<b>Contrato {h['grupo']} cancelado em {data_cancel}</b>"
                    if motivo:
                        hover_cancel += f"<br>Motivo: {motivo}"
                    cor_marcador, simbolo, tamanho = "black", "diamond", 14

                fig.add_trace(go.Scatter(
                    x=[meses_str[idx]], y=[valores[idx]], mode="markers",
                    marker=dict(size=tamanho, color=cor_marcador, symbol=simbolo, line=dict(color="white", width=1.5)),
                    legendgroup=grupo_legenda, showlegend=False,
                    hovertext=[hover_cancel], hoverinfo="text",
                ))
                trace_situacao.append(situacao_grupo)
                fig.add_trace(go.Scatter(
                    x=[meses_str[idx], meses_str[idx]], y=[0, max(valores)],
                    mode="lines", line=dict(color=cor_marcador, width=1, dash="dash"),
                    opacity=0.35, legendgroup=grupo_legenda, showlegend=False, hoverinfo="skip",
                ))
                trace_situacao.append(situacao_grupo)

    def adicionar_linha_total(subconjunto, variante, nome, visivel_inicialmente):
        """Adiciona uma linha 'Total' somando só os historicos do
        subconjunto informado (ex: só os ativos). `variante` é a tag usada
        pra decidir quando essa linha aparece nos botões de filtro."""
        if len(subconjunto) < 1:
            return
        df_total = pd.concat([h["df"] for h in subconjunto], ignore_index=True)
        df_total = df_total.groupby("mes", as_index=False)["valor"].sum().sort_values("mes").reset_index(drop=True)
        meses_str_t = [str(m) for m in df_total["mes"]]
        eixo_labels_t = [formatar_mes_ano(p) for p in df_total["mes"]]
        valores_t = df_total["valor"].tolist()

        var_pct_t, var_bruto_t = [None] * len(valores_t), [None] * len(valores_t)
        for j in range(1, len(valores_t)):
            ant, atu = valores_t[j - 1], valores_t[j]
            if pd.notna(ant) and pd.notna(atu) and ant != 0 and atu != ant:
                var_bruto_t[j] = atu - ant
                var_pct_t[j] = (atu - ant) / ant * 100

        hover_total = []
        for j, (m, v) in enumerate(zip(eixo_labels_t, valores_t)):
            texto = f"<b>{nome}</b><br>{m}: {formatar_moeda(v)}"
            if var_pct_t[j] is not None:
                direcao = "Aumento" if var_bruto_t[j] > 0 else "Redução"
                texto += f"<br>{direcao}: {formatar_moeda(abs(var_bruto_t[j]))} ({var_pct_t[j]:+.1f}%)"
            hover_total.append(texto)

        tag = f"total::{variante}"
        legendgroup_total = f"total_{variante}"

        fig.add_trace(go.Scatter(
            x=meses_str_t, y=valores_t, mode="lines", name=nome,
            legendgroup=legendgroup_total, showlegend=True, visible=visivel_inicialmente,
            line=dict(color="black", width=3, dash="dot"),
            hovertext=hover_total, hoverinfo="text",
        ))
        trace_situacao.append(tag)

        idx_var_t = [j for j, p in enumerate(var_pct_t) if p is not None]
        if idx_var_t:
            fig.add_trace(go.Scatter(
                x=[meses_str_t[j] for j in idx_var_t],
                y=[valores_t[j] for j in idx_var_t],
                mode="markers",
                marker=dict(
                    size=8,
                    color=[COR_AUMENTO if var_bruto_t[j] > 0 else COR_REDUCAO for j in idx_var_t],
                    line=dict(color="white", width=1),
                ),
                legendgroup=legendgroup_total, showlegend=False, hoverinfo="skip",
                visible=visivel_inicialmente,
            ))
            trace_situacao.append(tag)

            if mostrar_texto_variacao:
                idx_var_t_relevantes = [j for j in idx_var_t if abs(var_pct_t[j]) >= limiar_anotacao_pct]
                if idx_var_t_relevantes:
                    fig.add_trace(go.Scatter(
                        x=[meses_str_t[j] for j in idx_var_t_relevantes],
                        y=[valores_t[j] for j in idx_var_t_relevantes],
                        mode="text",
                        text=[
                            f"{'+' if var_bruto_t[j] > 0 else '-'}{formatar_moeda(abs(var_bruto_t[j]))} ({var_pct_t[j]:+.1f}%)"
                            for j in idx_var_t_relevantes
                        ],
                        textposition="top center",
                        textfont=dict(size=10, color="#111827"),
                        legendgroup=legendgroup_total, showlegend=False, hoverinfo="skip",
                        visible=visivel_inicialmente,
                    ))
                    trace_situacao.append(tag)

    if incluir_total and len(historicos_validos) > 1:
        # 3 variantes pré-calculadas do Total, uma pra cada botão de
        # filtro — "dinâmico" no sentido de que o Total muda junto com o
        # filtro Ativos/Encerrados (checkbox por contrato individual não
        # dá pra fazer de forma confiável só com Plotly, sem um backend).
        ativos_hist = [h for h in historicos_validos if h.get("situacao") == "A"]
        encerrados_hist = [h for h in historicos_validos if h.get("situacao") == "E"]
        adicionar_linha_total(historicos_validos, "todos", "Total (todos os contratos)", True)
        adicionar_linha_total(ativos_hist, "A", "Total (só ativos)", False)
        adicionar_linha_total(encerrados_hist, "E", "Total (só encerrados)", False)

    # --- botões de filtro Todos / Só Ativos / Só Encerrados ---
    # cada botão também escolhe qual variante do Total aparece (ver
    # adicionar_linha_total acima) — "Só Ativos" mostra o Total recalculado
    # só com os contratos ativos, não o total geral escondido atrás do filtro
    def visibilidade(filtro):
        resultado = []
        for s in trace_situacao:
            if s.startswith("total::"):
                variante = s.split("::", 1)[1]
                alvo = "todos" if filtro is None else filtro
                resultado.append(variante == alvo)
            else:
                resultado.append(filtro is None or s == filtro)
        return resultado

    # título muda de texto junto com o filtro clicado (Todos/Só Ativos/Só
    # Encerrados) — continua ajudando mesmo com o destaque visual nos
    # próprios botões, é um reforço redundante de propósito
    def _titulo_com_filtro(rotulo_filtro):
        return f"{titulo}<br><sup>{subtitulo} — Filtro atual: <b>{rotulo_filtro}</b></sup>"

    # cores do botão selecionado vs. não selecionado — só muda
    # bgcolor/bordercolor (preenchimento), NUNCA borderwidth. Mudar a
    # ESPESSURA da borda é o que causava o "pulo" antes (o botão ficava
    # fisicamente maior); mudar só a cor de preenchimento não altera o
    # tamanho renderizado em nada, então é seguro.
    COR_BOTAO_ATIVO_BG, COR_BOTAO_ATIVO_BORDA = COR_UNIAO_ALUGUEL_LICENCIAMENTO, "#F1F1F1"
    COR_BOTAO_INATIVO_BG, COR_BOTAO_INATIVO_BORDA = "#26263A", COR_UNIAO_ALUGUEL_LICENCIAMENTO

    def _estilo_dos_3_botoes(indice_selecionado):
        """Cada botão é o SEU PRÓPRIO updatemenu (não um menu com 3
        botões) — só assim dá pra colorir cada um independentemente.
        Clicar em qualquer um deles recolore os 3 juntos: o clicado fica
        'preenchido' (ativo), os outros dois voltam a ficar 'vazados'."""
        estilo = {}
        for i in range(3):
            ativo = i == indice_selecionado
            estilo[f"updatemenus[{i}].bgcolor"] = COR_BOTAO_ATIVO_BG if ativo else COR_BOTAO_INATIVO_BG
            estilo[f"updatemenus[{i}].bordercolor"] = COR_BOTAO_ATIVO_BORDA if ativo else COR_BOTAO_INATIVO_BORDA
        return estilo

    def _relayout_do_filtro(indice_selecionado, rotulo_filtro):
        return {**_estilo_dos_3_botoes(indice_selecionado), "title.text": _titulo_com_filtro(rotulo_filtro)}

    # posições fixas lado a lado (âncora à esquerda) — a primeira
    # tentativa deixou folga grande demais entre os botões (achando que
    # precisava de mais "colchão" de segurança contra sobreposição);
    # aperta bem mais aqui, ficando com cara de grupo de abas coladas,
    # não botões soltos e desalinhados
    _config_botoes = [
        ("Todos", None, 0, 0.800),
        ("Só Ativos", "A", 1, 0.833),
        ("Só Encerrados", "E", 2, 0.882),
    ]

    updatemenus_filtro = []
    for rotulo, filtro, idx, x_pos in _config_botoes:
        ativo_inicial = idx == 0  # "Todos" começa selecionado
        updatemenus_filtro.append(dict(
            type="buttons",
            buttons=[dict(
                label=rotulo, method="update",
                args=[{"visible": visibilidade(filtro)}, _relayout_do_filtro(idx, rotulo)],
            )],
            x=x_pos, y=1.05, xanchor="left", yanchor="top",
            showactive=False,
            bgcolor=COR_BOTAO_ATIVO_BG if ativo_inicial else COR_BOTAO_INATIVO_BG,
            bordercolor=COR_BOTAO_ATIVO_BORDA if ativo_inicial else COR_BOTAO_INATIVO_BORDA,
            borderwidth=1,
            font=dict(size=13, color="#F1F1F1"),
            pad=dict(t=8, b=8, l=8, r=8),
        ))

    passo = max(1, len(todos_meses_str) // 24)

    # janela inicial visível menor (últimos N meses) pra não precisar dar
    # zoom manual toda vez — use o zoom/pan do próprio Plotly (ícones da
    # barra de ferramentas) pra ver o histórico completo além dela
    janela_padrao = 20
    if len(todos_meses_str) > janela_padrao:
        range_inicial = [todos_meses_str[-janela_padrao], todos_meses_str[-1]]
    else:
        range_inicial = [todos_meses_str[0], todos_meses_str[-1]] if todos_meses_str else None

    altura_fig = max(950, 65 * len(historicos_validos))

    # limite de zoom-out: sem isso, o Plotly deixa a pessoa afastar o
    # zoom indefinidamente, mostrando um espaço vazio gigante em volta de
    # uma linha quase reta (como aconteceu com um cliente de valor
    # praticamente constante). Calcula o min/max REAL de todos os traces
    # já adicionados (incluindo a linha "Total") e trava o zoom/pan
    # nesse intervalo, com uma margem pequena — dá pra afastar o
    # suficiente pra ver tudo, mas não além disso.
    todos_valores_y = []
    for trace in fig.data:
        if trace.y is not None:
            todos_valores_y.extend(v for v in trace.y if v is not None and not (isinstance(v, float) and pd.isna(v)))
    todos_valores_y = [v for v in todos_valores_y if isinstance(v, (int, float))]

    if todos_valores_y:
        y_min_real, y_max_real = min(todos_valores_y), max(todos_valores_y)
        margem_y = (y_max_real - y_min_real) * 0.08 or max(y_max_real * 0.1, 50)
        y_min_permitido = max(0, y_min_real - margem_y)
        y_max_permitido = y_max_real + margem_y
    else:
        y_min_permitido = y_max_permitido = None

    fig.update_layout(
        title=dict(
            text=_titulo_com_filtro("Todos"), x=0.01, y=0.98, yanchor="top",
            font=dict(size=18),
        ),
        xaxis_title="Mês de referência", yaxis_title="Valor (R$)",
        yaxis_tickprefix="R$ ", yaxis_tickformat=",.2f",
        yaxis=dict(minallowed=y_min_permitido, maxallowed=y_max_permitido) if y_min_permitido is not None else {},
        template="plotly_white", hovermode="closest",
        height=altura_fig,
        width=1900,  # bem mais largo — usa mais espaço da tela
        # legenda HORIZONTAL, embaixo do gráfico (não mais à esquerda) —
        # tentar alinhar a legenda com precisão ao lado do eixo Y é frágil
        # (a largura dos rótulos do eixo varia com os valores de cada
        # cliente, e uma posição fixa que funciona hoje pode voltar a
        # colidir amanhã). Embaixo do eixo X ela nunca disputa espaço
        # com o eixo Y, independente do cliente/valores.
        legend=dict(
            orientation="h", yanchor="top", y=-0.16, xanchor="center", x=0.5,
            font=dict(size=13), groupclick="togglegroup",
        ),
        margin=dict(t=170, b=170, l=100, r=90),
        updatemenus=updatemenus_filtro,
        xaxis=dict(
            tickangle=-45, type="category", domain=[0, 1],
            categoryorder="array", categoryarray=todos_meses_str,
            tickvals=todos_meses_str[::passo], ticktext=todos_eixo_labels[::passo],
            range=range_inicial,
            rangeslider=dict(visible=False),
            # minallowed/maxallowed REMOVIDO daqui de propósito — eixo do
            # tipo "category" (não numérico) não respeita esse limite de
            # forma confiável, causava um comportamento estranho de
            # "ciclar" ao tentar dar zoom-out além do permitido repetidas
            # vezes. No eixo Y (numérico de verdade, logo abaixo) esse
            # mesmo recurso funciona corretamente.
        ),
    )

    # detalhamento COMPLETO (sem resumir) de todas as mudanças de
    # equipamento detectadas — o hover do gráfico corta pra caber no
    # espaço, aqui não tem esse limite, é o lugar pra "abrir" a
    # informação completa quando precisar investigar de verdade. Devolvido
    # como markdown pro caller mostrar num expander do Streamlit.
    linhas_detalhe_md = []
    for grupo, mes_fmt, direcao, bruto, pct, novos, removidos, alterados in detalhes_mudancas_console:
        linhas_detalhe_md.append(f"**{mes_fmt} — Contrato {grupo} — {direcao}: {formatar_moeda(abs(bruto))} ({pct:+.1f}%)**")
        if alterados:
            linhas_detalhe_md.append("- Valor alterado:")
            linhas_detalhe_md += [f"  - {item}" for item in alterados]
        if novos:
            linhas_detalhe_md.append("- Equipamento(s) novo(s):")
            linhas_detalhe_md += [f"  - {item}" for item in novos]
        if removidos:
            linhas_detalhe_md.append("- Equipamento(s) removido(s):")
            linhas_detalhe_md += [f"  - {item}" for item in removidos]
        linhas_detalhe_md.append("")
    detalhes_md = "\n".join(linhas_detalhe_md)

    return fig, detalhes_md


def plotar_contratos_lado_a_lado(historicos: list, nome_cliente: str, apenas_ativos: bool = True, cols: int = 3):
    """
    Mostra um "pequeno múltiplo" (mini-gráfico) por contrato, lado a lado
    em grade, em vez de tudo empilhado num gráfico só. Por padrão só os
    contratos ATIVOS (apenas_ativos=True) — use apenas_ativos=False para
    ver todos, incluindo encerrados.
    """
    grupos_plot = [h for h in historicos if not h["df"].empty and (not apenas_ativos or h.get("situacao") == "A")]
    if not grupos_plot:
        st.info(f"Nenhum contrato para exibir com esse filtro (apenas_ativos={apenas_ativos}).")
        return None

    n = len(grupos_plot)
    linhas = (n + cols - 1) // cols
    titulos = [f"{h['grupo']} — {h['descricao']}"[:35] for h in grupos_plot]

    fig = make_subplots(rows=linhas, cols=cols, subplot_titles=titulos, vertical_spacing=0.12, horizontal_spacing=0.06)

    for idx, h in enumerate(grupos_plot):
        r, c = idx // cols + 1, idx % cols + 1
        df_m = h["df"].sort_values("mes")
        eixo_labels = [formatar_mes_ano(p) for p in df_m["mes"]]
        valores = pd.to_numeric(df_m["valor"], errors="coerce").tolist()
        cor = classificar_cor_grupo(h) or PALETA_CONTRATOS[idx % len(PALETA_CONTRATOS)]
        hover = [f"{m}: {formatar_moeda(v)}" for m, v in zip(eixo_labels, valores)]
        fig.add_trace(go.Scatter(
            x=eixo_labels, y=valores, mode="lines+markers",
            line=dict(color=cor, width=2), marker=dict(size=4, color=cor),
            hovertext=hover, hoverinfo="text", showlegend=False,
        ), row=r, col=c)
        fig.update_xaxes(tickangle=-45, tickfont=dict(size=8), row=r, col=c)
        fig.update_yaxes(tickprefix="R$ ", tickfont=dict(size=8), row=r, col=c)

    fig.update_layout(
        title=f"Histórico individual por contrato — {nome_cliente}"
              + (" (só ativos)" if apenas_ativos else " (todos)"),
        height=max(320, 260 * linhas), width=max(1000, 380 * cols),
        template="plotly_white", showlegend=False,
    )
    return fig


# --- 7. Relatório completo do cliente ---
def buscar_data_cancelamento_bq(codigo_contrato_grupo: str):
    """
    A Base_Clientes não tem a data exata de cancelamento (só motivo e
    dataCriacao). Busca no BigQuery (cigam__contratos) para cada subcódigo
    do grupo e retorna a primeira data não vazia encontrada.
    """
    subcodigos = [normalizar_codigo_contrato(c) for c in str(codigo_contrato_grupo).split("/")]
    if MODO_DEMO:
        if normalizar_codigo_contrato("90011") in subcodigos:
            return "30/06/2023"  # data ficticia do cancelamento parcial demo
        return None
    for cod in subcodigos:
        query = f"""
        SELECT dataCancelamento
        FROM `{PROJECT_ID}.bronze.cigam__contratos`
        WHERE codigoContrato = @codigo_contrato
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("codigo_contrato", "STRING", cod)]
        )
        df = client_bq.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
        if not df.empty:
            valor = df["dataCancelamento"].iloc[0]
            if pd.notna(valor) and str(valor).strip():
                return str(valor)
    return None


def resumir_lista(itens: list, max_itens: int = 3, max_chars_item: int = 60) -> str:
    """Evita hover/legendas gigantes: mostra os primeiros N itens (truncados)
    e resume o resto como '(+M itens)'. Útil quando um único codigoContrato
    tem uma linha por equipamento/serial na Base_Clientes (pode chegar a
    dezenas de itens no mesmo contrato)."""
    itens = [str(i)[:max_chars_item] for i in itens]
    if len(itens) <= max_itens:
        return " | ".join(itens)
    return " | ".join(itens[:max_itens]) + f"  (+{len(itens) - max_itens} itens)"


def agrupar_historicos_para_grafico(historicos: list, max_linhas: int = 8) -> list:
    """
    Evita gráfico ilegível quando o cliente tem muitos grupos de contrato
    (comum quando há um histórico de contratos recriados por erro
    cadastral). Contratos ATIVOS sempre ficam como linha própria. Entre os
    demais (encerrados/outros), mantém os de maior valor total como linha
    própria e agrupa o restante numa única linha "Outros (N contratos)".
    """
    ativos = [h for h in historicos if h.get("situacao") == "A"]
    resto = [h for h in historicos if h.get("situacao") != "A"]

    vagas_para_resto = max(0, max_linhas - len(ativos))
    resto_ordenado = sorted(resto, key=lambda h: h.get("valor_total", 0), reverse=True)
    resto_mantido = resto_ordenado[:vagas_para_resto]
    resto_agrupado = resto_ordenado[vagas_para_resto:]

    resultado = ativos + resto_mantido

    if resto_agrupado:
        dfs_validos = [h["df"] for h in resto_agrupado if not h["df"].empty]
        if dfs_validos:
            df_outros = pd.concat(dfs_validos, ignore_index=True).groupby("mes", as_index=False)["valor"].sum()
            df_outros["fatura"] = ""  # não faz sentido listar NF de um bucket agregado de vários contratos
        else:
            df_outros = pd.DataFrame(columns=["mes", "valor", "fatura"])
        codigos_agrupados = ", ".join(str(h["grupo"]) for h in resto_agrupado)
        resultado.append({
            "grupo": "Outros",
            "descricao": f"{len(resto_agrupado)} contrato(s) encerrado(s)",
            "df": df_outros,
            "data_cancelamento": None,
            "motivo_cancelamento": None,
            "descricao_item": "",
            "observacao": f"Códigos agrupados: {resumir_lista(codigos_agrupados.split(', '), max_itens=8)}",
            "qtd_itens": sum(h.get("qtd_itens", 0) for h in resto_agrupado),
            "situacao": "E",
            "valor_total": sum(h.get("valor_total", 0) for h in resto_agrupado),
        })
        st.caption(
            f"⚠ {len(resto_agrupado)} contrato(s) de menor relevância foram agrupados em "
            f"uma única linha 'Outros' no gráfico, para manter a leitura possível "
            f"(veja a lista completa na tabela de itens/contratos acima)."
        )

    return resultado


def montar_resumo_cliente_md(nome_cliente, codigos_cliente, cnpj, contratos, equipamentos, historicos) -> str:
    """Monta o resumo do cliente como markdown (em vez de print), pra
    renderizar via st.markdown na versão web."""
    linhas = []

    qtd_ativos = contratos.loc[contratos["situacaoContrato"] == "A", "codigoContrato"].nunique()
    qtd_encerrados = contratos.loc[contratos["situacaoContrato"] == "E", "codigoContrato"].nunique()
    qtd_outros = contratos["codigoContrato"].nunique() - qtd_ativos - qtd_encerrados
    mensalidade_atual = pd.to_numeric(
        contratos.loc[contratos["situacaoContrato"] == "A", "Preco_Unitario"], errors="coerce"
    ).sum()

    # ordem: Contratos, Equipamentos, depois o resto (valores derivados
    # dos dois primeiros vêm logo em seguida a cada um)
    linhas.append(
        f"**Contratos:** {qtd_ativos} ativo(s), {qtd_encerrados} encerrado(s)"
        + (f", {qtd_outros} em outra situação" if qtd_outros else "")
    )

    if len(equipamentos):
        # um serial pode aparecer em mais de uma linha (equipamento
        # "múltiplo": mesmo chassi físico, vários bicos/produtos — duplo,
        # triplo, quádruplo, etc. — não vale a pena distinguir cada
        # variação no texto) — pra contar EQUIPAMENTOS de verdade, usamos
        # serial único, não a quantidade de linhas
        qtd_registros = len(equipamentos)
        qtd_equip_unicos = equipamentos["serial_equipamento"].nunique()
        qtd_outro_local = equipamentos["cliente_cigam_local"].ne(equipamentos["cliente_cigam_pagante"]).sum()

        texto_qtd = f"{qtd_equip_unicos} equipamento(s) (serial único)"
        linhas.append(
            f"**Equipamentos sob responsabilidade:** {texto_qtd}"
            + (f" ({qtd_outro_local} registro(s) em local diferente do pagante)" if qtd_outro_local else "")
        )
    else:
        linhas.append("**Equipamentos sob responsabilidade:** 0")

    linhas.append(f"**Mensalidade atual** (soma dos contratos ativos): {formatar_moeda(mensalidade_atual)}")

    if len(equipamentos):
        qtd_equip_unicos = equipamentos["serial_equipamento"].nunique()
        valor_medio_equip = mensalidade_atual / qtd_equip_unicos
        linhas.append(f"**Valor médio por equipamento** (mensalidade atual / qtd. equipamentos únicos): {formatar_moeda(valor_medio_equip)}")
    else:
        linhas.append("**Valor médio por equipamento:** N/D (sem equipamentos cadastrados como pagante)")

    # "Principal motivo de cancelamento" (com contagem simples) foi
    # removido — mesmo motivo da linha "Grupos de contrato no gráfico"
    # que já tinha saído daqui: um número baixo (ex: "1 contrato(s)") não
    # é um sinal útil por si só, e pode até enganar (parece indicar perda
    # de cliente quando às vezes é só uma baixa administrativa, tipo um
    # cancelamento parcial de um componente de um grupo que continua
    # ativo). A informação individual continua na tabela de itens/
    # contratos encerrados, lá embaixo. O que vale destacar aqui é só o
    # PADRÃO (5+ contratos com o mesmo motivo administrativo) — isso sim
    # é um sinal real de algo sistemático acontecendo.
    motivos = contratos.loc[contratos["situacaoContrato"] == "E", "Descricao_Cancelamento"].value_counts()
    if len(motivos):
        principal = motivos.index[0]
        if motivos.iloc[0] >= 5 and principal in ("CONTRATO ESTAVA COM ERRO", "CONTRATO DUPLICADO"):
            linhas.append(
                f"**Padrão de contratos recriados detectado:** {motivos.iloc[0]} contrato(s) cancelado(s) "
                f"por '{principal}' — os valores desses contratos foram agrupados em 'Outros' no gráfico."
            )

    datas_validas = contratos["primeira_parcela"].dropna()
    ultima_parcela_valida = contratos["ultima_parcela"].dropna()
    if len(datas_validas):
        # "até" só aparece se existir alguma ultima_parcela preenchida —
        # um cliente só com contratos ainda ativos pode não ter nenhuma
        # (contrato ativo não tem "última parcela" definida ainda), nesse
        # caso .max() em uma série toda NaT quebraria o .strftime()
        texto_periodo = f"**Período coberto:** {contratos['primeira_parcela'].min().strftime('%m/%Y')}"
        if len(ultima_parcela_valida):
            texto_periodo += f" a {ultima_parcela_valida.max().strftime('%m/%Y')}"
        else:
            texto_periodo += " até o momento (contrato(s) ainda em aberto)"
        linhas.append(texto_periodo)

    # "Grupos de contrato no gráfico" foi removido daqui — comparava
    # len(historicos) com a quantidade de códigos "crus" da Base_Clientes,
    # mas a diferença normal (ex: 1 de 2) é só a unificação aluguel+
    # licenciamento acontecendo direito, não perda de informação; ficava
    # confuso sem esse contexto. Quando ALGO realmente é agrupado por
    # falta de espaço (o "Outros" no gráfico), já existe um aviso
    # específico e claro pra isso (em agrupar_historicos_para_grafico).

    return "\n\n".join(linhas)


def montar_grupos_contrato(contratos: pd.DataFrame) -> list:
    """
    Monta a lista de grupos de contrato a plotar. A Base_Clientes já une
    ALUGUEL+LICENCIAMENTO num único codigoContrato (ex: '2135/2136') — mas
    só faz isso para contratos ATIVOS (lógica do script de extração). Para
    manter a mesma lógica também nos encerrados, pareamos aqui.

    Critério: contratos "vizinhos" (código próximo, dentro de uma janela
    pequena) — NÃO exigimos mais que a situação atual bata, porque na
    prática o aluguel pode ser cancelado enquanto o licenciamento
    correspondente continua ativo (ex: parou de se praticar aluguel, mas
    o licenciamento seguiu sozinho). Se tiver mais de um candidato dentro
    da janela, prioriza o que tem a MESMA dataCriacao (sinal mais forte
    de que nasceram juntos); sem isso, usa o código mais próximo.

    Fora da janela de proximidade não pareia — preferimos deixar linhas
    separadas a arriscar unir contratos que não têm relação nenhuma (já
    aconteceu de um código "vizinho por acaso" ser de outro cliente/
    situação completamente diferente).

    Retorna lista de tuplas (codigo_grupo, DataFrame com as linhas
    originais que compõem esse grupo).
    """
    JANELA_CODIGO_VIZINHO = 5  # mesma janela usada na unificação original (script de extração)

    contratos = contratos.copy()
    contratos["_cod_num"] = pd.to_numeric(
        contratos["codigoContrato"].astype(str).str.split("/").str[0], errors="coerce"
    )
    tem_barra = contratos["codigoContrato"].astype(str).str.contains("/")

    grupos = []
    for cod, sub in contratos[tem_barra].groupby("codigoContrato"):
        grupos.append((cod, sub))

    restante = contratos[~tem_barra]
    alugueis = restante[restante["Descricao_Material"] == "ALUGUEL"]
    # o candidato a parear com um aluguel NÃO precisa ter
    # Descricao_Material == "LICENCIAMENTO" exatamente — um contrato pode
    # já vir rotulado "Mensalidade Unificada" na origem (ex: combina
    # pedestal+sonda) sem que isso mude o fato de que ele é o par do
    # aluguel vizinho. Exigir esse texto exato foi o que fazia um aluguel
    # nunca achar seu par de verdade, mesmo com código adjacente. A
    # proteção contra pareamento errado continua sendo a janela de
    # código, não o texto da descrição.
    candidatos_pareamento = restante[restante["Descricao_Material"] != "ALUGUEL"]

    usados_lic_idx = set()
    for idx_a, alug in alugueis.iterrows():
        candidatos = candidatos_pareamento[
            (~candidatos_pareamento.index.isin(usados_lic_idx))
            & ((candidatos_pareamento["_cod_num"] - alug["_cod_num"]).abs() <= JANELA_CODIGO_VIZINHO)
        ]
        if candidatos.empty:
            # nenhum candidato com código vizinho -> não pareia, fica
            # como linha própria em vez de arriscar um pareamento sem
            # relação nenhuma
            grupos.append((alug["codigoContrato"], contratos.loc[[idx_a]]))
            continue

        # entre os vizinhos, prioriza quem tem a MESMA dataCriacao
        # (sinal mais forte de que nasceram juntos); sem isso, o mais
        # próximo em código
        mesma_data = candidatos[candidatos["dataCriacao"] == alug["dataCriacao"]]
        pool = mesma_data if len(mesma_data) else candidatos
        dist = (pool["_cod_num"] - alug["_cod_num"]).abs()
        idx_l = dist.idxmin()
        cod_lic = candidatos_pareamento.loc[idx_l, "codigoContrato"]

        # o candidato escolhido pode ter MAIS DE UMA linha com esse mesmo
        # codigoContrato (ex: pedestal + sonda como linhas separadas do
        # 4912) — pega todas elas, não só a que "ganhou" a escolha,
        # senão a(s) linha(s) restante(s) sobra(m) como um grupo
        # duplicado (ex: "4912" aparecendo separado de "4911/4912")
        indices_mesmo_contrato = candidatos_pareamento[candidatos_pareamento["codigoContrato"] == cod_lic].index
        usados_lic_idx.update(indices_mesmo_contrato)

        cod_par = f"{alug['codigoContrato']}/{cod_lic}"
        grupos.append((cod_par, pd.concat([contratos.loc[[idx_a]], contratos.loc[indices_mesmo_contrato]])))

    # sobras (candidatos que não pareamos com nenhum aluguel) — agrupa
    # por codigoContrato antes de virar grupo próprio, porque um único
    # código pode ter várias linhas (um item por equipamento/serial, ex:
    # o caso do contrato 7683 com 17 seriais); sem agrupar, cada linha
    # virava um "grupo" separado por engano
    sobras = candidatos_pareamento[~candidatos_pareamento.index.isin(usados_lic_idx)]
    for cod, sub in sobras.groupby("codigoContrato"):
        grupos.append((cod, sub))

    return grupos


def montar_texto_composicao(subset: pd.DataFrame) -> str:
    """Se o grupo é um par ALUGUEL+LICENCIAMENTO montado por nós (não já
    unificado na origem), descreve o valor e % de cada parte — ex:
    'Aluguel: R$ 135,00 (20,0%) | Licenciamento: R$ 540,00 (80,0%)'."""
    materiais = subset["Descricao_Material"].unique()
    if len(materiais) < 2:
        return ""
    total = pd.to_numeric(subset["Preco_Unitario"], errors="coerce").sum()
    if not total:
        return ""
    partes = []
    for mat in materiais:
        valor_mat = pd.to_numeric(subset.loc[subset["Descricao_Material"] == mat, "Preco_Unitario"], errors="coerce").sum()
        pct = valor_mat / total * 100
        partes.append(f"{mat.capitalize()}: {formatar_moeda(valor_mat)} ({pct:.1f}%)")
    return " | ".join(partes)


def buscar_descricao_contrato_bq(codigo_contrato: str):
    """Busca a descrição do contrato (ex: 'ALUGUEL DE EQUIPAMENTO',
    'LICENCIAMENTO DE SOFTWARE') direto no cigam__contratos — usado
    quando a Base_Clientes já uniu o par e não guarda mais o tipo de
    cada parte separadamente."""
    if MODO_DEMO:
        cod_norm = normalizar_codigo_contrato(codigo_contrato)
        return {
            normalizar_codigo_contrato("90011"): "ALUGUEL DE EQUIPAMENTO (demo)",
            normalizar_codigo_contrato("90012"): "LICENCIAMENTO DE SOFTWARE (demo)",
            normalizar_codigo_contrato("90021"): "LICENCIAMENTO DE SOFTWARE (demo)",
        }.get(cod_norm)
    query = f"""
    SELECT descricao
    FROM `{PROJECT_ID}.bronze.cigam__contratos`
    WHERE codigoContrato = @codigo_contrato
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("codigo_contrato", "STRING", codigo_contrato)]
    )
    df = client_bq.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    if df.empty:
        return None
    valor = df["descricao"].iloc[0]
    return str(valor).strip() if pd.notna(valor) and str(valor).strip() else None


def buscar_itens_nota_fiscal(numero_nf: str) -> pd.DataFrame:
    """
    Busca os itens REAIS de uma nota fiscal específica — direto de
    `cigam__notas_fiscais.itensNf_json` — igual à tela "Itens da Parcela"
    do CIGAM: código do material, quantidade, preço unitário, descrição.
    Mais preciso que qualquer aproximação, já que é a NF de verdade.
    """
    if not numero_nf or not str(numero_nf).strip():
        return pd.DataFrame()
    if MODO_DEMO:
        _, itens_demo = obter_dados_demo_bq()
        itens = itens_demo.get(str(numero_nf).strip())
        if not itens:
            return pd.DataFrame()
        return pd.DataFrame([
            {"codigoMaterial": desc, "quantidade": 1, "precoUnitario": valor, "descricao": texto}
            for desc, valor, texto in itens
        ])
    query = f"""
    SELECT itensNf_json
    FROM `{PROJECT_ID}.bronze.cigam__notas_fiscais`
    WHERE nf = @numero_nf OR fatura = @numero_nf
    LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("numero_nf", "STRING", str(numero_nf).strip())]
    )
    df = client_bq.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)
    if df.empty or pd.isna(df["itensNf_json"].iloc[0]):
        return pd.DataFrame()
    try:
        itens = json.loads(df["itensNf_json"].iloc[0])
    except (json.JSONDecodeError, TypeError):
        return pd.DataFrame()
    if not itens:
        return pd.DataFrame()
    df_itens = pd.DataFrame(itens)
    colunas = [c for c in ["codigoMaterial", "quantidade", "precoUnitario", "descricao", "notaFiscal"] if c in df_itens.columns]
    return df_itens[colunas]


def buscar_itens_notas_fiscais_lote(numeros_nf: list) -> dict:
    """
    Versão em LOTE de buscar_itens_nota_fiscal — busca os itens reais de
    VÁRIAS notas fiscais numa consulta só (em vez de 1 consulta por NF),
    pra dar pra montar a composição mês a mês de um grupo inteiro sem
    disparar dezenas de queries.

    Retorna um dict {numero_nf: [(descricao_material, valor, texto_item), ...]}
    — TODOS os itens daquela NF, não só o primeiro. Isso importa porque
    tem dois jeitos de uma "composição" acontecer: (a) contratos
    separados, cada um com sua própria NF de 1 item só (ex: aluguel +
    licenciamento), ou (b) UM contrato só, mas a MESMA nota fiscal
    cobrindo vários materiais juntos (ex: licenciamento pedestal + sonda
    na mesma NF). O `texto_item` é o campo `descricao` bruto da NF (ex:
    "PEDESTAL SIMPLES - Feira de Santana, BA LICENCIAMENTO DE SOFTWARE
    PERIODO: Setembro/2025") — traz local do equipamento e o período de
    referência real daquele lançamento, útil mesmo quando a NF só tem 1
    item (sem % de composição pra mostrar). O dict é indexado tanto por
    'nf' quanto por 'fatura' (os dois nomes que essa mesma informação
    recebe dependendo de onde vem).

    CUIDADO: o mesmo número de 'nf' pode aparecer em MAIS DE UMA linha em
    `cigam__notas_fiscais` — descobrimos um caso onde uma delas era a NF
    de serviço de verdade (`documento='NF'`) e a outra um movimento
    interno/acessório sem descrição nem material reconhecível
    (`documento` em branco, valores pequenos tipo taxa/ajuste). Por isso
    só considera itens com `documento='NF'`, e não sobrescreve uma
    entrada já encontrada com uma NF duplicada.
    """
    numeros_validos = sorted(set(str(n).strip() for n in numeros_nf if n and str(n).strip()))
    if not numeros_validos:
        return {}

    if MODO_DEMO:
        _, itens_demo = obter_dados_demo_bq()
        return {n: itens_demo[n] for n in numeros_validos if n in itens_demo}

    query = f"""
    SELECT nf, fatura, itensNf_json
    FROM `{PROJECT_ID}.bronze.cigam__notas_fiscais`
    WHERE nf IN UNNEST(@numeros) OR fatura IN UNNEST(@numeros)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("numeros", "STRING", numeros_validos)]
    )
    df = client_bq.query(query, job_config=job_config).to_dataframe(create_bqstorage_client=False)

    resultado = {}
    for _, row in df.iterrows():
        if pd.isna(row.get("itensNf_json")):
            continue
        try:
            itens = json.loads(row["itensNf_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not itens:
            continue
        lista_itens = []
        for item in itens:
            # existem casos em que o mesmo número de 'nf' aparece em MAIS
            # DE UMA linha na tabela cigam__notas_fiscais — uma é a NF de
            # serviço de verdade (documento='NF'), outra é algum
            # movimento interno/acessório sem descrição nem material
            # reconhecível (documento em branco, valores pequenos tipo
            # taxa/ajuste). Só considera item de documento='NF', senão a
            # composição mistura lixo com o item real.
            documento = str(item.get("documento", "")).strip().upper()
            if documento != "NF":
                continue
            cod_material = str(item.get("codigoMaterial", "")).strip()
            descricao = MAPA_CODIGO_MATERIAL.get(cod_material, cod_material or "Item")
            preco = pd.to_numeric(item.get("precoUnitario"), errors="coerce")
            qtd = pd.to_numeric(item.get("quantidade"), errors="coerce")
            valor_item = preco * qtd if pd.notna(preco) and pd.notna(qtd) else preco
            texto_item = str(item.get("descricao") or "").strip()
            lista_itens.append((descricao, valor_item, texto_item))
        if not lista_itens:
            continue
        for chave in (str(row.get("nf", "")).strip(), str(row.get("fatura", "")).strip()):
            if chave:
                # se já existe uma entrada pra essa chave (duas linhas do
                # BigQuery com o mesmo número de nf/fatura), NÃO
                # sobrescreve — mantém a primeira encontrada com item
                # válido, em vez de deixar a ordem arbitrária da consulta
                # decidir qual "ganha"
                resultado.setdefault(chave, lista_itens)
    return resultado


def calcular_composicao_real(codigo_contrato_grupo: str) -> str:
    """
    Reconstrói a composição % (aluguel vs licenciamento) pra grupos já
    unificados NA ORIGEM (Base_Clientes só tem 1 linha 'Mensalidade
    Unificada', sem o valor de cada parte — foi descartado na extração).

    Pra cada subcontrato, busca a NF mais recente e consulta o item REAL
    dela em `cigam__notas_fiscais.itensNf_json` (código do material,
    preço unitário) — mais preciso que aproximar pelo valor da parcela,
    já que é o dado de origem da nota fiscal de verdade. Se não achar a
    NF (ou o material não estiver mapeado), cai pra descrição do
    contrato como antes.
    """
    subcodigos = [normalizar_codigo_contrato(c) for c in str(codigo_contrato_grupo).split("/")]
    if len(subcodigos) < 2:
        return ""

    partes_info = []
    for cod in subcodigos:
        df_parcelas = buscar_parcelas_bq(cod)
        df_sub = preparar_dados_subcontrato(df_parcelas)
        if df_sub.empty:
            continue
        ultima_linha = df_sub.sort_values("mes").iloc[-1]
        ultimo_valor = pd.to_numeric(ultima_linha["valor"], errors="coerce")
        ultima_fatura = str(ultima_linha["fatura"]).split(",")[0].strip() if ultima_linha.get("fatura") else ""

        descricao = None
        valor_real = ultimo_valor
        if ultima_fatura:
            itens_nf = buscar_itens_nota_fiscal(ultima_fatura)
            if not itens_nf.empty:
                cod_material = str(itens_nf["codigoMaterial"].iloc[0]).strip()
                descricao = MAPA_CODIGO_MATERIAL.get(cod_material)
                preco_real = pd.to_numeric(itens_nf["precoUnitario"].iloc[0], errors="coerce")
                if pd.notna(preco_real):
                    valor_real = preco_real
        if not descricao:
            descricao = buscar_descricao_contrato_bq(cod) or cod

        partes_info.append((descricao, valor_real))

    total = sum(v for _, v in partes_info)
    if not total or len(partes_info) < 2:
        return ""

    return " | ".join(
        f"{desc.capitalize()}: {formatar_moeda(v)} ({v / total * 100:.1f}%)"
        for desc, v in partes_info
    )


def relatorio_cliente(
    identificador: str,
    incluir_total: bool = True,
    max_linhas_grafico: int = 8,
    mostrar_lado_a_lado: bool = True,
    mostrar_grade_individual: bool = True,
    mostrar_texto_variacao: bool = False,
    limiar_anotacao_pct: float = 5.0,
):
    """Monta o relatório inteiro na tela (Streamlit) pro cliente informado."""
    resultado = buscar_cliente(identificador)
    if resultado is None:
        st.warning(
            "Cliente não encontrado, ou o nome bate com mais de um cliente diferente "
            "— tente o código CIGAM ou o CNPJ pra ser mais específico."
        )
        return
    codigos_cliente, nome_cliente, cnpj = resultado

    st.header(nome_cliente, anchor=False)
    st.caption(f"CNPJ/CPF: {cnpj or 'N/D'}")
    if len(codigos_cliente) > 1:
        st.caption(f"Consolidando {len(codigos_cliente)} cadastros CIGAM sob o mesmo CNPJ: {codigos_cliente}")
    else:
        st.caption(f"Código CIGAM: {codigos_cliente[0]}")

    contratos = df_mensalidades[df_mensalidades["codigo_cliente"].isin(codigos_cliente)].copy()
    contratos["Situação"] = contratos["situacaoContrato"].map(SITUACAO_CONTRATO_LABELS).fillna(contratos["situacaoContrato"])
    contratos["Mensalidade"] = contratos["Preco_Unitario"].apply(lambda v: formatar_moeda(v) if pd.notna(v) else "")
    contratos["dataCriacao"] = parse_data_flexivel(contratos["dataCriacao"])
    # coluna de EXIBIÇÃO separada (dd/mm/aaaa, sem hora) — a "dataCriacao"
    # original continua como datetime de verdade, porque o pareamento de
    # contratos (montar_grupos_contrato) precisa comparar essas datas
    # entre si; só pra mostrar na tela que formatamos como texto
    contratos["Data Criação"] = contratos["dataCriacao"].dt.strftime("%d/%m/%Y")
    contratos["Data Criação"] = contratos["Data Criação"].fillna("")

    # busca a data de cancelamento no BigQuery só para os grupos encerrados
    # (evita consulta desnecessária para contratos ainda ativos)
    grupos_encerrados = contratos.loc[contratos["situacaoContrato"] == "E", "codigoContrato"].unique()
    mapa_data_cancelamento = {}
    for cod_grupo in grupos_encerrados:
        mapa_data_cancelamento[cod_grupo] = buscar_data_cancelamento_bq(cod_grupo)
    contratos["Data Cancelamento"] = contratos["codigoContrato"].map(mapa_data_cancelamento)

    equipamentos = df_bombas[df_bombas["cliente_cigam_pagante"].isin(codigos_cliente)].copy()
    if len(equipamentos):
        equipamentos["Local ≠ Pagante?"] = equipamentos["cliente_cigam_local"] != equipamentos["cliente_cigam_pagante"]
        # marca quando o serial se repete em mais de uma linha (equipamento
        # "múltiplo": mesmo chassi físico, vários bicos/produtos — duplo,
        # triplo, quádruplo, etc., não vale a pena nomear cada variação)
        # — pra não parecer que são equipamentos diferentes
        equipamentos["Múltiplo?"] = equipamentos.duplicated(subset="serial_equipamento", keep=False)
        equipamentos = equipamentos.sort_values(["local_nome", "serial_equipamento"])

    qtd_equip_unicos = equipamentos["serial_equipamento"].nunique() if len(equipamentos) else 0
    label_qtd_equip = str(qtd_equip_unicos)
    colunas_equip = ["bomba_nome", "serial_equipamento", "local_nome", "Local ≠ Pagante?", "Múltiplo?"]

    def _tabela_equipamentos_colorida(df_equip):
        """Colore as linhas que compartilham o mesmo serial (equipamento
        'múltiplo': duplo, triplo, quádruplo...) com a MESMA cor — dá pra
        ver de cara quais linhas são o mesmo equipamento físico, sem
        precisar comparar a coluna serial_equipamento manualmente."""
        df_sel = df_equip[colunas_equip]
        seriais_multiplos = df_sel.loc[df_sel["Múltiplo?"], "serial_equipamento"].unique()
        if len(seriais_multiplos) == 0:
            return df_sel
        paleta_multiplos = [
            COR_UNIAO_ALUGUEL_LICENCIAMENTO, COR_SO_LICENCIAMENTO, COR_SO_ALUGUEL,
        ] + PALETA_CONTRATOS
        mapa_cor = {serial: paleta_multiplos[i % len(paleta_multiplos)] for i, serial in enumerate(seriais_multiplos)}

        def _estilo_linha(linha):
            cor = mapa_cor.get(linha["serial_equipamento"])
            return [f"background-color: {cor}33"] * len(linha) if cor else [""] * len(linha)

        return df_sel.style.apply(_estilo_linha, axis=1)

    # --- monta o histórico (consultas ao BigQuery) ANTES de exibir
    # qualquer coisa na tela — o Resumo (que sobe pra logo depois do
    # cabeçalho) precisa dessa informação (quantidade de grupos), então
    # não dá mais pra deixar esse cálculo só na hora de desenhar o
    # gráfico, como era antes
    grupos = montar_grupos_contrato(contratos)
    _contador_fallback_json["qtd"] = 0
    with st.spinner(f"Buscando histórico de {len(grupos)} grupo(s) de contrato no BigQuery..."):
        historicos = []
        for cod_grupo, subset in grupos:
            materiais = subset["Descricao_Material"].dropna().unique()
            descricao = "Mensalidade Unificada" if len(materiais) > 1 else (materiais[0] if len(materiais) else "")
            df_hist = obter_historico_unificado(cod_grupo)

            # junta Descricao/observacao de TODAS as linhas desse grupo (um
            # codigoContrato pode ter várias linhas: um item por equipamento/serial,
            # por isso resumimos em vez de concatenar tudo)
            descricoes_item = [d for d in subset["Descricao"].dropna().unique() if str(d).strip()]
            observacoes = [o for o in subset["observacao"].dropna().unique() if str(o).strip()]

            # composição aluguel/licenciamento: primeiro tenta pelas linhas da
            # Base_Clientes (funciona pros pares que NÓS montamos, a partir de
            # encerrados); se vier vazio e o grupo já é um par ("/"), é porque
            # a Base_Clientes já uniu na origem e descartou o valor de cada
            # parte — nesse caso busca no BigQuery pra reconstruir
            composicao = montar_texto_composicao(subset)
            if not composicao and "/" in str(cod_grupo):
                composicao = calcular_composicao_real(cod_grupo)

            historicos.append({
                "grupo": cod_grupo,
                "descricao": descricao,
                "df": df_hist,
                "data_cancelamento": subset["Data Cancelamento"].dropna().iloc[0] if subset["Data Cancelamento"].notna().any() else None,
                "motivo_cancelamento": subset["Descricao_Cancelamento"].dropna().iloc[0] if subset["Descricao_Cancelamento"].notna().any() else None,
                "descricao_item": resumir_lista(descricoes_item),
                "observacao": resumir_lista(observacoes),
                "composicao": composicao,
                "qtd_itens": len(subset),
                # se QUALQUER linha do grupo ainda está ativa, o grupo
                # inteiro conta como ativo — antes pegava só a primeira
                # linha da lista, então um par aluguel(encerrado)/
                # licenciamento(ativo) virava "encerrado" por inteiro só
                # porque o aluguel aparecia primeiro, sumindo da grade de
                # mini-gráficos (que só mostra os ativos) mesmo o
                # licenciamento continuando cobrando
                "situacao": "A" if (subset["situacaoContrato"] == "A").any() else subset["situacaoContrato"].iloc[0],
                # códigos que compõem o grupo e quais deles CONTINUAM
                # ativos hoje — usado pra, depois de um cancelamento
                # parcial, o hover mostrar só o(s) código(s) de verdade
                # em vigor naquele mês, em vez de manter "90011/90012"
                # pra sempre mesmo com o 90011 já cancelado
                "codigos_grupo": subset["codigoContrato"].unique().tolist(),
                "codigos_ativos": subset.loc[subset["situacaoContrato"] == "A", "codigoContrato"].unique().tolist(),
                "valor_total": pd.to_numeric(df_hist["valor"], errors="coerce").sum() if not df_hist.empty else 0,
            })
    if _contador_fallback_json["qtd"] > 0:
        st.caption(
            f"({_contador_fallback_json['qtd']} subcontrato(s) usaram o fallback parcelasContrato_json "
            f"— array nativo vazio, comum em contratos mais antigos.)"
        )

    # --- 1) Resumo (logo após o cabeçalho, como pedido) ---
    st.subheader("Resumo", anchor=False)
    st.markdown(montar_resumo_cliente_md(nome_cliente, codigos_cliente, cnpj, contratos, equipamentos, historicos))

    # --- 2) Contratos ativos × Equipamentos ---
    if mostrar_lado_a_lado:
        contratos_ativos = contratos[contratos["situacaoContrato"] == "A"].sort_values("codigoContrato")
        if len(contratos_ativos):
            st.subheader("Contratos ativos × Equipamentos", anchor=False)
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown(f"**Contratos ativos ({len(contratos_ativos)})**")
                mostrar_tabela_com_download(
                    contratos_ativos[["Descricao_Material", "Descricao", "Mensalidade", "diaVencimento", "observacao"]],
                    f"contratos_ativos_{nome_cliente}.csv", "download_contratos_ativos",
                    use_container_width=True, hide_index=True,
                )
            with col_b:
                st.markdown(f"**Equipamentos — pagante ({label_qtd_equip})**")
                if len(equipamentos):
                    mostrar_tabela_com_download(
                        _tabela_equipamentos_colorida(equipamentos),
                        f"equipamentos_{nome_cliente}.csv", "download_equip_1",
                        use_container_width=True, hide_index=True,
                    )
                else:
                    st.caption("Nenhum equipamento.")
        else:
            st.subheader(f"Equipamentos sob responsabilidade — pagante ({label_qtd_equip})", anchor=False)
            if len(equipamentos):
                mostrar_tabela_com_download(
                    _tabela_equipamentos_colorida(equipamentos),
                    f"equipamentos_{nome_cliente}.csv", "download_equip_2",
                    use_container_width=True, hide_index=True,
                )
            else:
                st.caption("Nenhum equipamento encontrado para este cliente como pagante.")
    else:
        st.subheader(f"Equipamentos sob responsabilidade — pagante ({label_qtd_equip})", anchor=False)
        if len(equipamentos):
            mostrar_tabela_com_download(
                _tabela_equipamentos_colorida(equipamentos),
                f"equipamentos_{nome_cliente}.csv", "download_equip_3",
                use_container_width=True, hide_index=True,
            )
        else:
            st.caption("Nenhum equipamento encontrado para este cliente como pagante.")

    # --- 3) Histórico de mensalidade (gráfico principal) ---
    st.subheader("Histórico de mensalidade", anchor=False)
    historicos_grafico = agrupar_historicos_para_grafico(historicos, max_linhas=max_linhas_grafico)

    fig_principal, detalhes_md = plotar_historico_multi(
        historicos_grafico,
        titulo=f"Histórico de mensalidade — {nome_cliente}",
        subtitulo=f"{len(historicos_grafico)} linha(s) no gráfico ({len(historicos)} grupo(s) de contrato reais) — código(s) CIGAM {codigos_cliente} — CNPJ {cnpj or 'N/D'}",
        incluir_total=incluir_total,
        mostrar_texto_variacao=mostrar_texto_variacao,
        limiar_anotacao_pct=limiar_anotacao_pct,
    )
    if fig_principal is not None:
        st.plotly_chart(fig_principal, use_container_width=True)

    if detalhes_md:
        with st.expander("📋 Detalhamento completo das mudanças de equipamento por mês (sem resumir)"):
            st.markdown(detalhes_md)

    with st.expander("Descrição/observação por contrato (texto completo)"):
        if len(historicos) <= 15:
            for h in historicos:
                st.markdown(f"**Contrato {h['grupo']} ({h['descricao']}):**")
                st.write(f"Item: {h['descricao_item'] or '(sem descrição de item)'}")
                st.write(f"Obs.: {h['observacao'] or '(sem observação)'}")
                st.divider()
        else:
            st.caption(
                f"{len(historicos)} grupos é muita coisa pra listar em texto aqui — "
                f"veja na tabela de itens/contratos acima ou no hover do gráfico."
            )

    # --- 4) Histórico individual por contrato (mini-gráficos) ---
    if mostrar_grade_individual:
        st.subheader("Histórico individual por contrato (mini-gráficos)", anchor=False)
        fig_grade = plotar_contratos_lado_a_lado(historicos, nome_cliente, apenas_ativos=True)
        if fig_grade is not None:
            st.plotly_chart(fig_grade, use_container_width=True)

    # --- 5) Itens/contratos encerrados/cancelados — movido pro final,
    # como pedido; é informação de arquivo/histórico, não o primeiro que
    # a pessoa precisa ver
    qtd_encerrados_itens = (contratos["situacaoContrato"] == "E").sum()
    qtd_encerrados_grupos = contratos.loc[contratos["situacaoContrato"] == "E", "codigoContrato"].nunique()
    st.subheader(f"Itens/contratos encerrados/cancelados ({qtd_encerrados_itens} item(ns) / {qtd_encerrados_grupos} grupo(s))", anchor=False)
    mostrar_tabela_com_download(
        contratos[contratos["situacaoContrato"] == "E"][[
            "codigoContrato", "Descricao_Material", "Descricao",
            "Descricao_Cancelamento", "Data Cancelamento", "Data Criação",
            "observacao", "contratoTerceiro", "Mensalidade", "diaVencimento",
        ]].sort_values("codigoContrato"),
        f"contratos_encerrados_{nome_cliente}.csv", "download_encerrados",
        use_container_width=True, hide_index=True,
    )


# ============================================================================
# --- 8. Interface (Streamlit) ---
# ============================================================================
# logo: coloque um arquivo "logo.png" na raiz do repositório (mesma pasta
# do app.py) que ele aparece automaticamente ao lado do título. Usa HTML
# com a imagem embutida em base64 (em vez de st.columns) porque
# st.columns empilha verticalmente em telas estreitas — o que fazia a
# logo aparecer pequena e separada do título, numa linha própria. Assim
# os dois ficam sempre lado a lado, do tamanho que a gente definir.
def _renderizar_cabecalho():
    if os.path.exists("logo.png"):
        import base64
        logo_b64 = base64.b64encode(open("logo.png", "rb").read()).decode()
        st.markdown(
            f"""
            <div style="display:flex; align-items:center; gap:18px; margin-bottom:0.5rem;">
                <img src="data:image/png;base64,{logo_b64}" style="height:64px; width:auto;">
                <h1 style="margin:0; font-size:2.25rem;">Histórico de Mensalidade — CIGAM</h1>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.title("Histórico de Mensalidade — CIGAM", anchor=False)


_renderizar_cabecalho()
st.caption("Digite o nome do cliente, código CIGAM ou CNPJ/CPF e clique em Buscar.")

with st.form("busca_cliente_form"):
    identificador_input = st.text_input("Cliente:", placeholder="Ex: DNP TERRAPLANAGEM, 308, ou 57623761000117")
    col1, col2 = st.columns([1, 3])
    with col1:
        buscar_clicado = st.form_submit_button("🔍 Buscar", use_container_width=True)

if buscar_clicado:
    if not identificador_input.strip():
        st.warning("Digite um cliente pra buscar.")
    else:
        relatorio_cliente(identificador_input.strip())
