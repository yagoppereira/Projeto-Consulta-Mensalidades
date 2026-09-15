"""
Gera uma "Nota de Débito" — documento de cobrança avulso que lista títulos já
em aberto (licenciamento, locação de equipamento etc.) com vencimento e
valor, pra clientes que precisam desse relatório consolidado pra alimentar
o sistema de contas a pagar deles.

Não é uma NF-e/NFS-e nem depende de uma: é só um recibo/demonstrativo pra
transmissão interna do cliente, sem passar pela emissão fiscal.

Também dá pra gerar pela aba "Nota de Débito" do app Streamlit (app.py), que
importa a função `gerar_pdf_nota_debito` deste módulo.
"""

from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from gerar_espelho_nfse import formatar_moeda

AZUL_MARCA = colors.HexColor("#1D4ED8")

EMISSOR = {
    "razao_social": "CTA Comercio de Controles Eletrônicos SA",
    "cnpj": "15.001.448/0001-05",
    "insc_estadual": "096/3481177",
    "endereco": "Av. Dr. Nilo Peçanha, 2400 – Boa Vista – Porto Alegre/RS",
}

DADOS_PAGAMENTO = {
    "banco": "341 – Banco Itaú",
    "agencia": "3115",
    "conta_corrente": "20770-0",
    "chave_pix": "15.001.448/0001-05",
    "favorecido": "CTA Comercio de Controles Eletrônicos SA",
}


def formatar_data(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _estilo(negrito=False, tamanho=9, alinhamento=TA_LEFT, cor=colors.black):
    return ParagraphStyle(
        "texto", fontName="Helvetica-Bold" if negrito else "Helvetica",
        fontSize=tamanho, leading=tamanho + 3, alignment=alinhamento, textColor=cor,
    )


def _p(texto, **kwargs):
    return Paragraph(texto, _estilo(**kwargs))


def _titulo_secao(numero: str, texto: str):
    return _p(f"{numero} · {texto}", negrito=True, tamanho=9, cor=AZUL_MARCA)


def gerar_pdf_nota_debito(numero: str, data_emissao: date, contato: dict,
                           destinatario: dict, itens: list, caminho_saida: str) -> float:
    """Desenha o PDF e retorna o valor total (soma dos itens)."""
    largura_util = 180 * mm
    doc = SimpleDocTemplate(
        caminho_saida, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )
    elementos = []

    caixa = TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ])
    sem_borda = TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])

    # cabeçalho: wordmark à esquerda, número e data à direita
    elementos.append(Table(
        [[
            _p("ctasmart", negrito=True, tamanho=16, cor=AZUL_MARCA),
            [
                _p(f"NOTA DE DÉBITO Nº {numero}", negrito=True, tamanho=14, alinhamento=TA_RIGHT),
                _p(f"Data de Emissão: {formatar_data(data_emissao)}", tamanho=9, alinhamento=TA_RIGHT),
            ],
        ]],
        colWidths=[largura_util * 0.4, largura_util * 0.6], style=sem_borda,
    ))
    elementos.append(Spacer(1, 10))

    # 1 · Emissor / 2 · Destinatário
    partes_contato = [v for v in (contato.get("nome"), contato.get("telefone"), contato.get("email")) if v]
    linha_contato = f"<br/>Contato: {' – '.join(partes_contato)}" if partes_contato else ""
    emissor_txt = (
        f"{EMISSOR['razao_social']}<br/>"
        f"CNPJ / CPF: {EMISSOR['cnpj']}<br/>"
        f"Inscrição Estadual: {EMISSOR['insc_estadual']}<br/>"
        f"Endereço: {EMISSOR['endereco']}"
        f"{linha_contato}"
    )
    destinatario_txt = (
        f"Razão Social: {destinatario.get('razao_social') or ''}<br/>"
        f"CNPJ: {destinatario.get('cnpj') or ''}<br/>"
        f"Endereço: {destinatario.get('endereco') or ''}"
    )
    bloco_emissor = Table(
        [[_titulo_secao("1", "DADOS DO EMISSOR")], [_p(emissor_txt)]],
        colWidths=[largura_util * 0.48], style=caixa,
    )
    bloco_destinatario = Table(
        [[_titulo_secao("2", "DADOS DO DESTINATÁRIO")], [_p(destinatario_txt)]],
        colWidths=[largura_util * 0.48], style=caixa,
    )
    elementos.append(Table(
        [[bloco_emissor, bloco_destinatario]],
        colWidths=[largura_util * 0.49, largura_util * 0.51], style=sem_borda,
    ))
    elementos.append(Spacer(1, 10))

    # 3 · Descrição do débito
    elementos.append(_titulo_secao("3", "DESCRIÇÃO DO DÉBITO"))
    elementos.append(Spacer(1, 4))

    total = sum(item["valor"] for item in itens)
    cabecalho_tabela = [_p(t, negrito=True, alinhamento=TA_CENTER) for t in
                         ["Item", "Título", "Nº NFS-e", "Descrição dos serviços / despesas",
                          "Vencimento", "Valor (R$)"]]
    linhas_tabela = [cabecalho_tabela]
    for i, item in enumerate(itens, start=1):
        linhas_tabela.append([
            _p(f"{i:02d}", alinhamento=TA_CENTER),
            _p(item["titulo"], alinhamento=TA_CENTER),
            _p(item.get("numero_nfse") or "-", alinhamento=TA_CENTER),
            _p(item["descricao"]),
            _p(formatar_data(item["vencimento"]), alinhamento=TA_CENTER),
            _p(formatar_moeda(item["valor"]), alinhamento=TA_RIGHT),
        ])
    tabela_debito = Table(
        linhas_tabela,
        colWidths=[largura_util * 0.07, largura_util * 0.17, largura_util * 0.12,
                   largura_util * 0.31, largura_util * 0.14, largura_util * 0.19],
        style=TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("LINEBELOW", (0, 0), (-1, 0), 1, AZUL_MARCA),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]),
    )
    elementos.append(tabela_debito)
    elementos.append(Spacer(1, 6))
    elementos.append(Table(
        [[_p("TOTAL", negrito=True, tamanho=11, alinhamento=TA_RIGHT),
          _p(formatar_moeda(total), negrito=True, tamanho=11, alinhamento=TA_RIGHT)]],
        colWidths=[largura_util * 0.84, largura_util * 0.16], style=sem_borda,
    ))
    elementos.append(Spacer(1, 14))

    # 4 · Dados para pagamento / 5 · Assinatura e aceite
    pagamento_txt = (
        f"Banco: {DADOS_PAGAMENTO['banco']}<br/>"
        f"Agência: {DADOS_PAGAMENTO['agencia']}<br/>"
        f"Conta Corrente: {DADOS_PAGAMENTO['conta_corrente']}<br/>"
        f"Chave PIX: {DADOS_PAGAMENTO['chave_pix']}<br/>"
        f"Favorecido: {DADOS_PAGAMENTO['favorecido']}"
    )
    assinatura_txt = (
        "<br/><br/>_______________________________<br/>"
        f"{destinatario.get('razao_social') or ''}<br/>"
        f"CNPJ: {destinatario.get('cnpj') or ''}"
    )
    bloco_pagamento = Table(
        [[_titulo_secao("4", "DADOS PARA PAGAMENTO")], [_p(pagamento_txt)]],
        colWidths=[largura_util * 0.48], style=caixa,
    )
    bloco_assinatura = Table(
        [[_titulo_secao("5", "ASSINATURA E ACEITE")], [_p(assinatura_txt)]],
        colWidths=[largura_util * 0.48], style=caixa,
    )
    elementos.append(Table(
        [[bloco_pagamento, bloco_assinatura]],
        colWidths=[largura_util * 0.49, largura_util * 0.51], style=sem_borda,
    ))

    doc.build(elementos)
    return total
