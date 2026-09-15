"""
Gera um "espelho" (prévia, sem validade fiscal) das Notas Fiscais de Serviço
a partir de uma Proposta Comercial da CTA Smart (PDF).

Cada proposta assinada gera 3 notas fiscais separadas:
  1. Adesão        (parametrização/configuração do sistema — cobrança única)
  2. Instalação     (deslocamento técnico + instalação física — cobrança única)
  3. Licenciamento  (mensalidade — comodato do equipamento + licenciamento)

O script lê o ANEXO I da proposta (tabela de produtos e o quadro "Preços
Totais"), calcula a base de cálculo, o ISSQN e o valor líquido de cada nota,
e desenha um PDF no mesmo formato do modelo de NFS-e da Prefeitura de Porto
Alegre — mas claramente identificado como espelho/prévia, já que o número da
nota e o código de verificação só existem depois da emissão oficial.

Uso:
    python gerar_espelho_nfse.py caminho/da/proposta.pdf [--saida DIR] [--aliquota 2.0]

Dependências (não fazem parte do requirements.txt do app Streamlit):
    pip install pdfplumber reportlab
"""

import argparse
import os
import re
from datetime import date, timedelta

import pdfplumber
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
TIPOS_NOTA = {
    "adesao": {
        "titulo": "ADESÃO",
        "codigo_servico": "90000100003",
        "descricao_servico": "CONFIGURAÇAO",
        "cod_atividade": "0-CONFIGURACAO",
        "item_lc116": "0",
        "cnae": "6203100",
        "filtro_produto": lambda p: p["adesao_unit"] > 0,
    },
    "instalacao": {
        "titulo": "INSTALAÇÃO",
        "codigo_servico": "90000100004",
        "descricao_servico": "INSTALAÇAO",
        "cod_atividade": "0-INSTALACAO",
        "item_lc116": "0",
        "cnae": "3329599",
        "filtro_produto": lambda p: p["instalacao_unit"] > 0,
    },
    "licenciamento": {
        "titulo": "LICENCIAMENTO",
        "codigo_servico": "90000100001",
        "descricao_servico": "LICENCIAMENTO",
        "cod_atividade": "0-LICENCIAMENTO -10500100",
        "item_lc116": "0",
        "cnae": "6202300",
        "filtro_produto": lambda p: p["mensalidade_unit"] > 0,
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


def _split_coluna(celula: str) -> list:
    return [linha.strip() for linha in (celula or "").split("\n")]


def _extrair_tabela_produtos(tabelas) -> list:
    for tabela in tabelas:
        if not tabela or not tabela[0]:
            continue
        cabecalho = [c.strip() if c else "" for c in tabela[0]]
        if cabecalho[0] != "Produto":
            continue
        produtos = []
        for linha in tabela[1:]:
            colunas = [_split_coluna(c) for c in linha]
            n = len(colunas[0])
            for i in range(n):
                produtos.append({
                    "nome": colunas[0][i],
                    "qtd": to_float(colunas[1][i]),
                    "adesao_unit": to_float(colunas[2][i]),
                    "instalacao_unit": to_float(colunas[3][i]),
                    "mensalidade_unit": to_float(colunas[5][i]),
                })
        return produtos
    raise ValueError("Não encontrei a tabela de produtos (ANEXO I) na proposta.")


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

    produtos = _extrair_tabela_produtos(tabelas)
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
    mensalidade_total = precos.get("Investimento Total", (0.0, 0.0))[1]
    adesao_instalacao_total = precos.get("Investimento Total", (0.0, 0.0))[0]

    adesao_total = sum(p["qtd"] * p["adesao_unit"] for p in produtos)
    instalacao_total = sum(p["qtd"] * p["instalacao_unit"] for p in produtos) + deslocamento_total

    diferenca = adesao_instalacao_total - (adesao_total + instalacao_total)
    if abs(diferenca) > 0.02:
        print(
            f"[aviso] Adesão ({formatar_moeda(adesao_total)}) + Instalação "
            f"({formatar_moeda(instalacao_total)}) não bate com o Investimento Total "
            f"da proposta ({formatar_moeda(adesao_instalacao_total)}); diferença de "
            f"{formatar_moeda(diferenca)}. Confira a proposta manualmente."
        )

    if not (nome_tomador and cidade_uf_doc):
        raise ValueError("Não consegui identificar o Tomador do Serviço na proposta.")

    return {
        "numero_proposta": numero_proposta.group(1) if numero_proposta else "",
        "data_proposta": data_proposta.group(1) if data_proposta else "",
        "produtos": produtos,
        "tomador": {
            "nome": nome_tomador.group(1).strip(),
            "cidade": cidade_uf_doc.group(1).strip(),
            "uf": cidade_uf_doc.group(2).strip(),
            "documento": cidade_uf_doc.group(3).strip(),
            "email": email_resp.group(1).strip() if email_resp else "",
            "responsavel": nome_resp.group(1).strip() if nome_resp else "",
        },
        "adesao_total": adesao_total,
        "instalacao_total": instalacao_total,
        "mensalidade_total": mensalidade_total,
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
        itens = [p for p in dados["produtos"] if tipo["filtro_produto"](p)]
        descricao_itens = " + ".join(p["nome"] for p in itens) or "(nenhum item identificado)"

        if chave == "adesao":
            valor_total = dados["adesao_total"]
            vencimentos = _vencimentos_parcelados(valor_total, dados["parcelas_adesao"], data_emissao)
        elif chave == "instalacao":
            valor_total = dados["instalacao_total"]
            vencimentos = _vencimentos_parcelados(valor_total, dados["parcelas_instalacao"], data_emissao)
        else:  # licenciamento — mensalidade recorrente, cobrada mês a mês após carência
            valor_total = dados["mensalidade_total"]
            primeira_cobranca = data_emissao + timedelta(days=30 * dados["carencia_meses"] + 30)
            vencimentos = [(primeira_cobranca, valor_total)]

        total_issqn = round(valor_total * aliquota / 100, 2)

        notas.append({
            "chave": chave,
            "tipo": tipo,
            "descricao": f'{tipo["codigo_servico"]} - {tipo["descricao_servico"]} {descricao_itens} - Proposta Nº {dados["numero_proposta"]}',
            "vencimentos": vencimentos,
            "aliquota": aliquota,
            "valor_total_servicos": valor_total,
            "base_calculo": valor_total,
            "total_issqn": total_issqn,
            "valor_liquido": valor_total,  # ISSQN não retido nos exemplos de referência
            "recorrente": chave == "licenciamento",
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

    # tomador
    tomador_txt = (
        f"<b>Tomador do Serviço</b><br/>{tomador['nome']} - CPF/CNPJ: {tomador['documento']}<br/>"
        f"{tomador['cidade']}/{tomador['uf']}<br/>"
        f"E-mail: {tomador['email'] or '(não informado na proposta)'}"
        "<br/><i>Endereço completo e telefone não constam na proposta — confira no cadastro do cliente "
        "antes de emitir a nota oficial.</i>"
    )
    elementos.append(Table([[Paragraph(tomador_txt, _estilo_texto())]], colWidths=[largura_util], style=borda))

    # vencimentos
    if nota["recorrente"]:
        linha_venc = " ".join(
            f"{formatar_data(d)}  {formatar_moeda(v)}" for d, v in nota["vencimentos"]
        ) + "  (recorrente mensalmente enquanto o contrato estiver ativo)"
    else:
        linha_venc = "     ".join(f"{formatar_data(d)}  {formatar_moeda(v)}" for d, v in nota["vencimentos"])
    elementos.append(Table(
        [[Paragraph(f"<b>Vencimentos</b><br/>{linha_venc}", _estilo_texto())]],
        colWidths=[largura_util], style=borda,
    ))

    # descrição dos serviços
    elementos.append(Table(
        [[Paragraph(f"<b>Descrição dos Serviços</b><br/>{nota['descricao']}", _estilo_texto())]],
        colWidths=[largura_util], style=borda,
    ))

    # ISSQN
    cabecalho_issqn = [_celula(t, negrito=True) for t in
                       ["Cod.Atividade do Município", "Alíquota", "Item da LC 116/2003", "Cod. Nacional Ativ. Econômica"]]
    valores_issqn = [_celula(t) for t in [
        nota["tipo"]["cod_atividade"], f'{nota["aliquota"]:.2f}'.replace(".", ","),
        nota["tipo"]["item_lc116"], nota["tipo"]["cnae"],
    ]]
    tabela_issqn_1 = Table([cabecalho_issqn, valores_issqn], colWidths=[largura_util / 4] * 4, style=borda)

    cabecalho_issqn_2 = [_celula(t, negrito=True) for t in [
        "Valor total dos serviços", "Dedução da base de Cálculo", "Base de Cálculo",
        "Desconto Padrão", "Total ISSQN", "ISSQN Retido",
    ]]
    valores_issqn_2 = [_celula(t) for t in [
        formatar_moeda(nota["valor_total_servicos"]), formatar_moeda(0), formatar_moeda(nota["base_calculo"]),
        formatar_moeda(0), formatar_moeda(nota["total_issqn"]), "Não",
    ]]
    tabela_issqn_2 = Table([cabecalho_issqn_2, valores_issqn_2], colWidths=[largura_util / 6] * 6, style=borda)

    elementos.append(Table(
        [[Paragraph("<b>Imposto Sobre Serviços de Qualquer Natureza - ISSQN</b>", _estilo_texto())],
         [tabela_issqn_1], [tabela_issqn_2]],
        colWidths=[largura_util], style=TableStyle([("BOX", (0, 0), (-1, -1), 0.75, colors.black)]),
    ))

    # retenções
    cabecalho_ret = [_celula(t, negrito=True) for t in ["PIS", "COFINS", "INSS", "IR", "CSLL", "Outras retenções", "ISSQN"]]
    valores_ret = [_celula(formatar_moeda(0)) for _ in range(6)] + [_celula("0,00")]
    elementos.append(Table(
        [[Paragraph("<b>Retenções de impostos</b>", _estilo_texto())],
         [Table([cabecalho_ret, valores_ret], colWidths=[largura_util / 7] * 7, style=borda)]],
        colWidths=[largura_util], style=TableStyle([("BOX", (0, 0), (-1, -1), 0.75, colors.black)]),
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
