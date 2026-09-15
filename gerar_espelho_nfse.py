"""
Gera um "espelho" (prévia, sem validade fiscal) das Notas Fiscais de Serviço
a partir de uma Proposta Comercial da CTA Smart (PDF).

Cada proposta assinada gera 3 notas fiscais de serviço, sempre considerando
só os equipamentos "CTA" (Pedestal, Mobile) — acessórios e periféricos
(Antena, Válvula, Chaveiro, Kit Wi-Fi etc.) são vendidos como mercadoria à
parte, o operacional emite essa nota de venda por fora deste espelho:
  1. Adesão                    (parametrização/configuração — cobrança única)
  2. Instalação + Deslocamento (instalação física + deslocamento técnico — cobrança única)
  3. Licenciamento             (mensalidade — comodato do equipamento + licenciamento)

O script lê o ANEXO I da proposta (tabela de produtos e o quadro "Preços
Totais"), calcula a base de cálculo, o ISSQN e o valor líquido de cada nota,
e desenha um PDF no mesmo formato do modelo de NFS-e da Prefeitura de Porto
Alegre — mas claramente identificado como espelho/prévia, já que o número da
nota e o código de verificação só existem depois da emissão oficial.

O parser é orientado a cabeçalho de coluna, não a posição fixa: aceita tanto
o modelo de proposta com uma tabela única (Produto/Qtd/Adesão Unitária/
Instalação Unitária/Adesão + Instalação Total/Mensalidade Unitária/
Mensalidade Total) quanto o modelo com desconto por item, que separa
"Adesão e Instalação" de "Mensalidades" em duas tabelas e já traz o valor
por linha com desconto aplicado (coluna "Adesão com Desconto").

O endereço do Tomador não vem na proposta — o script busca automaticamente
na Receita Federal (via BrasilAPI, a partir do CNPJ) quando o documento do
cliente é um CNPJ; para CPF não há consulta pública, o campo fica em branco.

Uso:
    python gerar_espelho_nfse.py caminho/da/proposta.pdf [--saida DIR] [--aliquota 2.0]

Dependências (não fazem parte do requirements.txt do app Streamlit):
    pip install pdfplumber reportlab requests
"""

import argparse
import os
import re
import unicodedata
from datetime import date, timedelta

import pdfplumber
import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.enums import TA_CENTER, TA_LEFT

PRESTADOR = {
    "razao_social": "CTA COMERCIO DE CONTROLES ELETRONICOS S.A.",
    "endereco": "AVENIDA DOUTOR NILO PECANHA, 2400, BOA VISTA",
    "cep_cidade": "CEP: 91340.550 – PORTO ALEGRE/RS",
    "cnpj": "15.001.448/0001-05",
    "insc_municipal": "-",
    "insc_estadual": "0963481177",
}

# Enquadramento fiscal de cada tipo de nota — os códigos abaixo foram lidos
# diretamente de NFS-e já emitidas pela CTA Smart para o mesmo tipo de
# serviço; a alíquota default (2%) é a de Porto Alegre/RS.
#
# "cod_atividade" da Instalação muda pra "0-DESLOCAMENTO" quando a proposta
# cobra deslocamento técnico (visto em NFS-e real que soma os dois na mesma
# nota); sem deslocamento, fica "0-INSTALACAO".
TIPOS_NOTA = {
    "adesao": {
        "titulo": "ADESÃO",
        "codigo_servico": "90000100003",
        "descricao_servico": "CONFIGURAÇAO",
        "cod_atividade": "0-CONFIGURACAO",
        "item_lc116": "0",
        "cnae": "6203100",
        "filtro_produto": lambda p: p["adesao_total"] > 0,
    },
    "instalacao": {
        "titulo": "INSTALAÇÃO + DESLOCAMENTO",
        "codigo_servico": "90000100004",
        "descricao_servico": "INSTALAÇAO",
        "cod_atividade": "0-INSTALACAO",
        "item_lc116": "0",
        "cnae": "3329599",
        "filtro_produto": lambda p: p["instalacao_total"] > 0,
    },
    "licenciamento": {
        "titulo": "LICENCIAMENTO",
        "codigo_servico": "90000100001",
        "descricao_servico": "LICENCIAMENTO",
        "cod_atividade": "0-LICENCIAMENTO -10500100",
        "item_lc116": "0",
        "cnae": "6202300",
        "filtro_produto": lambda p: p["mensalidade_total"] > 0,
    },
}


def to_float(valor) -> float:
    """Converte número no formato BR ('1.234,56', '0,00', '-', None) para float."""
    if valor is None:
        return 0.0
    texto = str(valor).strip()
    if texto in ("", "-"):
        return 0.0
    texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def formatar_moeda(valor: float) -> str:
    s = f"{valor:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def formatar_data(d: date) -> str:
    return d.strftime("%d/%m/%y")


def _formatar_cep(cep: str) -> str:
    digitos = re.sub(r"\D", "", cep or "")
    return f"{digitos[:5]}-{digitos[5:]}" if len(digitos) == 8 else cep


def _formatar_telefone(numero: str) -> str:
    digitos = re.sub(r"\D", "", numero or "")
    if len(digitos) == 11:
        return f"({digitos[:2]}) {digitos[2:7]}-{digitos[7:]}"
    if len(digitos) == 10:
        return f"({digitos[:2]}) {digitos[2:6]}-{digitos[6:]}"
    return numero


def consultar_endereco_cnpj(documento: str) -> dict:
    """Busca o endereço do Tomador na Receita Federal via BrasilAPI.

    A proposta só traz cidade/UF do cliente, não o endereço completo. Só dá
    pra consultar quando o documento é CNPJ (14 dígitos) — CPF não tem
    consulta pública de endereço. Qualquer falha de rede/CNPJ inválido
    retorna vazio em vez de derrubar a geração do espelho.
    """
    digitos = re.sub(r"\D", "", documento or "")
    if len(digitos) != 14:
        return {}
    try:
        resposta = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{digitos}", timeout=15)
        resposta.raise_for_status()
        dados = resposta.json()
    except Exception as erro:
        print(f"[aviso] Não consegui consultar o CNPJ {documento} na Receita Federal: {erro}")
        return {}

    return {
        "logradouro": dados.get("logradouro") or "",
        "numero": dados.get("numero") or "",
        "complemento": dados.get("complemento") or "",
        "bairro": dados.get("bairro") or "",
        "municipio": dados.get("municipio") or "",
        "uf": dados.get("uf") or "",
        "cep": _formatar_cep(dados.get("cep", "")),
        "telefone": _formatar_telefone(dados.get("ddd_telefone_1", "")),
    }


def _split_coluna(celula: str) -> list:
    return [linha.strip() for linha in (celula or "").split("\n")]


def _normalizar_cabecalho(texto: str) -> str:
    """'Adesão +\\nInstalação Total' -> 'adesao instalacao total'.

    Cabeçalhos de tabela na proposta vêm com quebra de linha por célula e às
    vezes mudam de "Adesão Unitária" pra "Adesão com Desconto" dependendo do
    modelo de proposta (com ou sem desconto por item) — casar por texto
    normalizado em vez de posição de coluna aguenta as duas variações.
    """
    texto = unicodedata.normalize("NFKD", (texto or "").replace("\n", " "))
    texto = texto.encode("ascii", "ignore").decode()
    texto = re.sub(r"[^a-zA-Z0-9 ]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip().lower()


# mapeia cabeçalho normalizado -> papel do valor daquela coluna. Colunas de
# "com Desconto" já vêm com o valor por linha corrigido (qtd × unitário ×
# (1 - desconto)); quando presentes, são preferidas às colunas "Unitária"
# cruas, que exigiriam multiplicar por Qtd e não têm desconto embutido.
PAPEL_POR_CABECALHO = {
    "produto": "nome",
    "qtd": "qtd",
    "adesao unitaria": "adesao_unitaria",
    "instalacao unitaria": "instalacao_unitaria",
    "adesao instalacao total": "adesao_instalacao_total",
    "adesao com desconto": "adesao_total_linha",
    "mensalidade unitaria": "mensalidade_unitaria",
    "mensalidade total": "mensalidade_total_linha",
}


def _extrair_produtos(tabelas) -> list:
    """Lê todas as tabelas 'Produto' da proposta (pode ser uma só, com tudo
    junto, ou duas — 'Adesão e Instalação' e 'Mensalidades' — dependendo do
    modelo) e mescla por nome do produto, já que a mesma lista de produtos
    aparece em ambas quando estão separadas."""
    produtos = {}
    ordem = []
    for tabela in tabelas:
        if not tabela or not tabela[0]:
            continue
        cabecalho_normalizado = [_normalizar_cabecalho(c) for c in tabela[0]]
        papel_por_indice = {i: PAPEL_POR_CABECALHO[c] for i, c in enumerate(cabecalho_normalizado) if c in PAPEL_POR_CABECALHO}
        if papel_por_indice.get(0) != "nome":
            continue
        for linha in tabela[1:]:
            colunas = [_split_coluna(c) for c in linha]
            n = len(colunas[0])
            for i in range(n):
                valores = {papel: (colunas[idx][i] if idx < len(colunas) and i < len(colunas[idx]) else "")
                           for idx, papel in papel_por_indice.items()}
                nome = valores.get("nome", "").strip()
                if not nome:
                    continue
                if nome not in produtos:
                    produtos[nome] = {"nome": nome}
                    ordem.append(nome)
                registro = produtos[nome]
                for campo, bruto in valores.items():
                    if campo != "nome":
                        registro[campo] = to_float(bruto)
    if not produtos:
        raise ValueError("Não encontrei a tabela de produtos (ANEXO I) na proposta.")

    resultado = []
    for nome in ordem:
        p = produtos[nome]
        qtd = p.get("qtd", 0.0)
        if "adesao_total_linha" in p:
            adesao = p["adesao_total_linha"]
        elif "adesao_instalacao_total" in p:
            adesao = p["adesao_instalacao_total"] - p.get("instalacao_unitaria", 0.0) * qtd
        else:
            adesao = p.get("adesao_unitaria", 0.0) * qtd
        instalacao = p.get("instalacao_unitaria", 0.0) * qtd
        mensalidade = p.get("mensalidade_total_linha", p.get("mensalidade_unitaria", 0.0) * qtd)
        resultado.append({
            "nome": nome, "qtd": qtd,
            "adesao_total": round(adesao, 2), "instalacao_total": round(instalacao, 2),
            "mensalidade_total": round(mensalidade, 2),
        })
    return resultado


def _extrair_precos_totais(tabelas) -> dict:
    for tabela in tabelas:
        rotulos = [linha[0] for linha in tabela if linha and linha[0]]
        if "Investimento Total" in rotulos:
            precos = {}
            for linha in tabela:
                if not linha or not linha[0]:
                    continue
                precos[linha[0].strip()] = (to_float(linha[1]), to_float(linha[2]) if len(linha) > 2 else 0.0)
            return precos
    raise ValueError("Não encontrei o quadro 'Preços Totais' na proposta.")


def parse_proposta(caminho_pdf: str) -> dict:
    with pdfplumber.open(caminho_pdf) as pdf:
        texto_completo = "\n".join(pagina.extract_text() or "" for pagina in pdf.pages)
        tabelas = []
        for pagina in pdf.pages:
            tabelas.extend(pagina.extract_tables())

    # extract_text() lê a página inteira em ordem visual e intercala o texto
    # de colunas vizinhas quando uma célula quebra em mais de uma linha (ex:
    # "Carência de 3" fica colado com o início da coluna ao lado antes de
    # "meses" aparecer). Pra campos que vivem dentro de tabelas, procuramos
    # dentro das células já isoladas por extract_tables() em vez do texto da
    # página inteira.
    texto_tabelas = "\n".join(celula for tabela in tabelas for linha in tabela for celula in linha if celula)

    produtos = _extrair_produtos(tabelas)
    precos = _extrair_precos_totais(tabelas)

    numero_proposta = re.search(r"Proposta:\s*(\d+)", texto_completo)
    data_proposta = re.search(r"Porto Alegre,\s*(\d{2}/\d{2}/\d{4})", texto_completo)

    nome_tomador = re.search(r"^À\s+(.+)$", texto_completo, re.MULTILINE)
    cidade_uf_doc = re.search(
        r"^(.+?),\s*([A-Z]{2})\s+N° do Documento:\s*([\d./-]+)", texto_completo, re.MULTILINE
    )
    email_resp = re.search(r"E-mail do Responsável:\s*(\S+)", texto_completo)
    nome_resp = re.search(r"Caro\(a\) Sr\.\(a\)\s+(.+)", texto_completo)

    parcelas_adesao = re.search(r"Adesão[^:\n]*:\s*([\d/]+)\s*Dias", texto_tabelas)
    parcelas_instalacao = re.search(r"^Instalação:\s*([\d/]+)\s*Dias", texto_tabelas, re.MULTILINE)
    carencia = re.search(r"Carência de (\d+)\s+mes", texto_tabelas)

    deslocamento_total = precos.get("Deslocamento Técnico Automação", (0.0, 0.0))[0] + \
        precos.get("Deslocamento Técnico Medição", (0.0, 0.0))[0]
    adesao_instalacao_total_proposta = precos.get("Investimento Total", (0.0, 0.0))[0]

    # a proposta pode ter CTAs (controladores) junto com acessórios/periféricos
    # (Antena, Válvula, Chaveiro, Kit Wi-Fi...); só os CTAs entram nesta nota
    # de serviço — o resto é mercadoria, vendida à parte pelo operacional
    produtos_cta = [p for p in produtos if p["nome"].strip().upper().startswith("CTA")]

    adesao_total = sum(p["adesao_total"] for p in produtos_cta)
    instalacao_total = sum(p["instalacao_total"] for p in produtos_cta) + deslocamento_total
    mensalidade_total = sum(p["mensalidade_total"] for p in produtos_cta)

    # conferência: soma de TODOS os produtos (CTAs + acessórios) precisa bater
    # com o Investimento Total da proposta — isso pega erro de leitura de
    # tabela sem depender do filtro "só CTA" de cima
    adesao_todos = sum(p["adesao_total"] for p in produtos)
    instalacao_todos = sum(p["instalacao_total"] for p in produtos) + deslocamento_total
    diferenca = adesao_instalacao_total_proposta - (adesao_todos + instalacao_todos)
    if abs(diferenca) > 0.02:
        print(
            f"[aviso] Adesão ({formatar_moeda(adesao_todos)}) + Instalação "
            f"({formatar_moeda(instalacao_todos)}) de todos os produtos não bate com o "
            f"Investimento Total da proposta ({formatar_moeda(adesao_instalacao_total_proposta)}); "
            f"diferença de {formatar_moeda(diferenca)}. Confira a proposta manualmente."
        )

    if not (nome_tomador and cidade_uf_doc):
        raise ValueError("Não consegui identificar o Tomador do Serviço na proposta.")

    documento_tomador = cidade_uf_doc.group(3).strip()
    endereco_tomador = consultar_endereco_cnpj(documento_tomador)

    return {
        "numero_proposta": numero_proposta.group(1) if numero_proposta else "",
        "data_proposta": data_proposta.group(1) if data_proposta else "",
        "produtos_cta": produtos_cta,
        "tomador": {
            "nome": nome_tomador.group(1).strip(),
            "cidade": cidade_uf_doc.group(1).strip(),
            "uf": cidade_uf_doc.group(2).strip(),
            "documento": documento_tomador,
            "email": email_resp.group(1).strip() if email_resp else "",
            "responsavel": nome_resp.group(1).strip() if nome_resp else "",
            "endereco": endereco_tomador,
        },
        "adesao_total": adesao_total,
        "instalacao_total": instalacao_total,
        "mensalidade_total": mensalidade_total,
        "deslocamento_total": deslocamento_total,
        "parcelas_adesao": [int(d) for d in parcelas_adesao.group(1).split("/")] if parcelas_adesao else [30],
        "parcelas_instalacao": [int(d) for d in parcelas_instalacao.group(1).split("/")] if parcelas_instalacao else [30],
        "carencia_meses": int(carencia.group(1)) if carencia else 0,
    }


def _vencimentos_parcelados(valor_total: float, dias: list, data_base: date) -> list:
    n = len(dias)
    parcela = round(valor_total / n, 2)
    valores = [parcela] * n
    valores[-1] = round(valor_total - parcela * (n - 1), 2)  # absorve arredondamento na última
    return [(data_base + timedelta(days=d), v) for d, v in zip(dias, valores)]


def montar_notas(dados: dict, aliquota: float, data_emissao: date) -> list:
    notas = []
    for chave, tipo in TIPOS_NOTA.items():
        itens = [p for p in dados["produtos_cta"] if tipo["filtro_produto"](p)]
        linhas_descricao = [
            f'{tipo["codigo_servico"]} - {tipo["descricao_servico"]} {p["nome"].upper()} - Proposta Nº {dados["numero_proposta"]}'
            for p in itens
        ] or ["(nenhum CTA identificado na proposta para este tipo de nota)"]
        cod_atividade = tipo["cod_atividade"]

        if chave == "adesao":
            valor_total = dados["adesao_total"]
            vencimentos = _vencimentos_parcelados(valor_total, dados["parcelas_adesao"], data_emissao)
        elif chave == "instalacao":
            valor_total = dados["instalacao_total"]
            vencimentos = _vencimentos_parcelados(valor_total, dados["parcelas_instalacao"], data_emissao)
            if dados["deslocamento_total"] > 0:
                linhas_descricao.append("90000100008 - DESLOCAMENTO")
                cod_atividade = "0-DESLOCAMENTO"
        else:  # licenciamento — mensalidade recorrente, cobrada mês a mês após carência
            valor_total = dados["mensalidade_total"]
            primeira_cobranca = data_emissao + timedelta(days=30 * dados["carencia_meses"] + 30)
            vencimentos = [(primeira_cobranca, valor_total)]

        total_issqn = round(valor_total * aliquota / 100, 2)

        notas.append({
            "chave": chave,
            "tipo": {**tipo, "cod_atividade": cod_atividade},
            "descricao_linhas": linhas_descricao,
            "vencimentos": vencimentos,
            "aliquota": aliquota,
            "valor_total_servicos": valor_total,
            "base_calculo": valor_total,
            "total_issqn": total_issqn,
            "valor_liquido": valor_total,  # ISSQN não retido nos exemplos de referência
        })
    return notas


def _estilo_texto(negrito=False, tamanho=9, alinhamento=TA_LEFT):
    return ParagraphStyle(
        "texto", fontName="Helvetica-Bold" if negrito else "Helvetica",
        fontSize=tamanho, leading=tamanho + 2, alignment=alinhamento,
    )


def _celula(texto, negrito=False, tamanho=7, alinhamento=TA_LEFT):
    """Paragraph pequeno com quebra de linha automática — evita que texto
    de cabeçalhos longos (ex: 'Dedução da base de Cálculo') vaze para a
    célula vizinha, o que aconteceria com strings soltas na Table."""
    return Paragraph(texto, _estilo_texto(negrito=negrito, tamanho=tamanho, alinhamento=alinhamento))


def gerar_pdf_nota(nota: dict, tomador: dict, data_emissao: date, numero_proposta: str, caminho_saida: str):
    largura_util = 180 * mm
    doc = SimpleDocTemplate(
        caminho_saida, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
    )
    elementos = []
    borda = TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])
    # grades de valor (ISSQN/Retenções): título + valor centralizados e
    # alinhados ao meio da célula, senão um cabeçalho que quebra em 2 linhas
    # (ex: "Dedução da base de Cálculo") fica desalinhado com os vizinhos
    # de 1 linha só
    borda_centro = TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    # título
    elementos.append(Table(
        [[Paragraph("NFS-e - ESPELHO DE NOTA FISCAL DE SERVIÇOS (PRÉVIA — SEM VALIDADE FISCAL)",
                    _estilo_texto(negrito=True, tamanho=11, alinhamento=TA_CENTER))]],
        colWidths=[largura_util], style=borda,
    ))

    # cabeçalho: nº / emitida em / código verificação
    tabela_cabecalho = Table(
        [[
            Paragraph("Nº<br/><i>a gerar na emissão oficial</i>", _estilo_texto()),
            Paragraph(f"Serviço<br/><b>{nota['tipo']['titulo']}</b> — referente à Proposta Nº {numero_proposta}", _estilo_texto()),
            Paragraph(f"Prévia gerada em<br/>{formatar_data(date.today())}", _estilo_texto()),
        ]],
        colWidths=[largura_util * 0.25, largura_util * 0.5, largura_util * 0.25], style=borda,
    )
    elementos.append(tabela_cabecalho)

    # prestador
    prestador_txt = (
        f"<b>Prestador do Serviço</b><br/>{PRESTADOR['razao_social']}<br/>{PRESTADOR['endereco']}<br/>"
        f"{PRESTADOR['cep_cidade']}<br/>CNPJ: {PRESTADOR['cnpj']} - Insc. Municipal: {PRESTADOR['insc_municipal']} - "
        f"Insc. Estadual: {PRESTADOR['insc_estadual']}"
    )
    elementos.append(Table([[Paragraph(prestador_txt, _estilo_texto())]], colWidths=[largura_util], style=borda))

    # tomador — endereço vem da consulta de CNPJ (proposta só traz cidade/UF)
    endereco = tomador.get("endereco") or {}
    if endereco.get("logradouro"):
        linha_endereco = f"{endereco['logradouro']}, {endereco['numero']}"
        if endereco.get("complemento"):
            linha_endereco += f" - {endereco['complemento']}"
        if endereco.get("bairro"):
            linha_endereco += f" - {endereco['bairro']}"
        linha_endereco += f" - Cep: {endereco['cep']}<br/>{endereco['municipio']}/{endereco['uf']}"
        linha_telefone = f"Telefone: {endereco['telefone']} " if endereco.get("telefone") else ""
    else:
        linha_endereco = (
            f"{tomador['cidade']}/{tomador['uf']}<br/>"
            "<i>Endereço completo não encontrado na consulta de CNPJ — confira no cadastro do cliente "
            "antes de emitir a nota oficial.</i>"
        )
        linha_telefone = ""
    tomador_txt = (
        f"<b>Tomador do Serviço</b><br/>{tomador['nome']} - CPF/CNPJ: {tomador['documento']}<br/>"
        f"{linha_endereco}<br/>"
        f"{linha_telefone}E-mail: {tomador['email'] or '(não informado na proposta)'}"
    )
    elementos.append(Table([[Paragraph(tomador_txt, _estilo_texto())]], colWidths=[largura_util], style=borda))

    # vencimentos
    linha_venc = "     ".join(f"{formatar_data(d)}  {formatar_moeda(v)}" for d, v in nota["vencimentos"])
    elementos.append(Table(
        [[Paragraph(f"<b>Vencimentos</b><br/>{linha_venc}", _estilo_texto())]],
        colWidths=[largura_util], style=borda,
    ))

    # descrição dos serviços
    descricao_html = "<br/>".join(nota["descricao_linhas"])
    elementos.append(Table(
        [[Paragraph(f"<b>Descrição dos Serviços</b><br/>{descricao_html}", _estilo_texto())]],
        colWidths=[largura_util], style=borda,
    ))

    # ISSQN
    cabecalho_issqn = [_celula(t, negrito=True, alinhamento=TA_CENTER) for t in
                       ["Cod.Atividade do Município", "Alíquota", "Item da LC 116/2003", "Cod. Nacional Ativ. Econômica"]]
    valores_issqn = [_celula(t, alinhamento=TA_CENTER) for t in [
        nota["tipo"]["cod_atividade"], f'{nota["aliquota"]:.2f}'.replace(".", ","),
        nota["tipo"]["item_lc116"], nota["tipo"]["cnae"],
    ]]
    tabela_issqn_1 = Table([cabecalho_issqn, valores_issqn], colWidths=[largura_util / 4] * 4, style=borda_centro)

    cabecalho_issqn_2 = [_celula(t, negrito=True, alinhamento=TA_CENTER) for t in [
        "Valor total dos serviços", "Dedução da base de Cálculo", "Base de Cálculo",
        "Desconto Padrão", "Total ISSQN", "ISSQN Retido",
    ]]
    valores_issqn_2 = [_celula(t, alinhamento=TA_CENTER) for t in [
        formatar_moeda(nota["valor_total_servicos"]), formatar_moeda(0), formatar_moeda(nota["base_calculo"]),
        formatar_moeda(0), formatar_moeda(nota["total_issqn"]), "Não",
    ]]
    tabela_issqn_2 = Table([cabecalho_issqn_2, valores_issqn_2], colWidths=[largura_util / 6] * 6, style=borda_centro)

    # tabelas aninhadas (tabela dentro de célula de tabela) herdam o padding
    # default do reportlab (6pt) se não for zerado — como as tabelas internas
    # já são dimensionadas para ocupar largura_util inteira, esse padding
    # extra empurrava o conteúdo pra fora da borda externa
    sem_padding = TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ])

    elementos.append(Table(
        [[Paragraph("<b>Imposto Sobre Serviços de Qualquer Natureza - ISSQN</b>", _estilo_texto())],
         [tabela_issqn_1], [tabela_issqn_2]],
        colWidths=[largura_util], style=sem_padding,
    ))

    # retenções
    cabecalho_ret = [_celula(t, negrito=True, alinhamento=TA_CENTER) for t in
                      ["PIS", "COFINS", "INSS", "IR", "CSLL", "Outras retenções", "ISSQN"]]
    valores_ret = [_celula(formatar_moeda(0), alinhamento=TA_CENTER) for _ in range(6)] + \
        [_celula("0,00", alinhamento=TA_CENTER)]
    elementos.append(Table(
        [[Paragraph("<b>Retenções de impostos</b>", _estilo_texto())],
         [Table([cabecalho_ret, valores_ret], colWidths=[largura_util / 7] * 7, style=borda_centro)]],
        colWidths=[largura_util], style=sem_padding,
    ))

    # valor líquido
    elementos.append(Table(
        [[Paragraph("<b>Valor Líquido da Nota Fiscal</b>", _estilo_texto()),
          Paragraph(f"<b>{formatar_moeda(nota['valor_liquido'])}</b>", _estilo_texto(tamanho=12, alinhamento=TA_CENTER))]],
        colWidths=[largura_util * 0.7, largura_util * 0.3], style=borda,
    ))

    # informações complementares
    info_txt = (
        "<b>Informações Complementares</b><br/>"
        f"Espelho gerado automaticamente a partir da Proposta Nº {numero_proposta} para conferência de valores "
        "antes da emissão oficial na prefeitura de Porto Alegre. Este documento não possui validade fiscal."
    )
    elementos.append(Table([[Paragraph(info_txt, _estilo_texto())]], colWidths=[largura_util], style=borda))

    elementos.append(Spacer(1, 6))
    elementos.append(Paragraph(
        "Prefeitura de Porto Alegre - Secretaria da Fazenda<br/>"
        "Rua Siqueira Campos, 1300 - 4º andar - Bairro Centro Histórico - CEP: 90.010-907 - Porto Alegre RS.<br/>"
        "Tel.: 156 ou 51.32890140 para chamadas de outras cidades — Email: nfse@smf.prefpoa.com.br",
        _estilo_texto(tamanho=7),
    ))

    doc.build(elementos)


def gerar_espelhos(caminho_proposta: str, pasta_saida: str, aliquota: float, data_emissao: date):
    dados = parse_proposta(caminho_proposta)
    notas = montar_notas(dados, aliquota, data_emissao)

    os.makedirs(pasta_saida, exist_ok=True)
    caminhos_gerados = []
    for nota in notas:
        nome_arquivo = f"espelho_nfse_{dados['numero_proposta']}_{nota['chave']}.pdf"
        caminho = os.path.join(pasta_saida, nome_arquivo)
        gerar_pdf_nota(nota, dados["tomador"], data_emissao, dados["numero_proposta"], caminho)
        caminhos_gerados.append(caminho)
        print(
            f"[{nota['tipo']['titulo']}] base de cálculo {formatar_moeda(nota['base_calculo'])} | "
            f"ISSQN {formatar_moeda(nota['total_issqn'])} | valor líquido {formatar_moeda(nota['valor_liquido'])} "
            f"-> {caminho}"
        )
    return caminhos_gerados


def main():
    parser = argparse.ArgumentParser(description="Gera espelhos de NFS-e (Adesão, Instalação, Licenciamento) a partir de uma Proposta CTA Smart.")
    parser.add_argument("proposta", help="Caminho do PDF da proposta comercial")
    parser.add_argument("--saida", default="espelhos_nfse", help="Pasta onde salvar os PDFs gerados (default: ./espelhos_nfse)")
    parser.add_argument("--aliquota", type=float, default=2.0, help="Alíquota do ISSQN em %% (default: 2.0, Porto Alegre/RS)")
    parser.add_argument("--data-emissao", default=None, help="Data base (DD/MM/AAAA) para calcular os vencimentos; default: hoje")
    args = parser.parse_args()

    data_emissao = date.today()
    if args.data_emissao:
        dia, mes, ano = (int(x) for x in args.data_emissao.split("/"))
        data_emissao = date(ano, mes, dia)

    gerar_espelhos(args.proposta, args.saida, args.aliquota, data_emissao)


if __name__ == "__main__":
    main()
