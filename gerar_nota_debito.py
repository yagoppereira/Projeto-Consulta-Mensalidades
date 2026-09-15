"""
Gera uma "Nota de Débito" — documento de cobrança avulso que lista títulos já
em aberto (licenciamento, locação de equipamento etc.) com vencimento e
valor, pra clientes que precisam desse relatório consolidado pra alimentar
o sistema de contas a pagar deles.

Não é uma NF-e/NFS-e nem depende de uma: é só um recibo/demonstrativo pra
transmissão interna do cliente, sem passar pela emissão fiscal. A assinatura
do bloco 5 é sempre da CTA (emissor) — é a CTA quem autoriza os dados de
pagamento ali informados, não o cliente.

Layout segue a especificação de design ctasmart (Poppins + DM Sans, paleta
Cobalt/Night/Steel/Off White/Fuel Green, cards com cantos arredondados,
tabela de títulos com cabeçalho escuro e linhas zebradas).

Também dá pra gerar pela aba "Nota de Débito" do app Streamlit (app.py), que
importa a função `gerar_pdf_nota_debito` deste módulo.
"""

import os
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from gerar_espelho_nfse import formatar_moeda

# --- paleta e tipografia (design system ctasmart) ---------------------------
COBALT = colors.HexColor("#0040D0")
NIGHT = colors.HexColor("#060D1F")
STEEL = colors.HexColor("#8A95A8")
OFF_WHITE = colors.HexColor("#F2F4F8")
FUEL_GREEN = colors.HexColor("#00C389")
STEEL_HAIRLINE_26 = colors.Color(0x8A / 255, 0x95 / 255, 0xA8 / 255, alpha=0.26)
STEEL_HAIRLINE_34 = colors.Color(0x8A / 255, 0x95 / 255, 0xA8 / 255, alpha=0.34)

_PASTA_FONTES = os.path.join(os.path.dirname(__file__), "fonts")


def _registrar_fontes():
    """Poppins (títulos/rótulos/total) + DM Sans (corpo/tabela) — registradas
    uma vez só; se os arquivos não estiverem presentes (ex: ambiente sem os
    TTFs baixados), cai pros fonts base do reportlab em vez de quebrar."""
    fontes = {
        "Poppins": "Poppins-400.ttf", "Poppins-SemiBold": "Poppins-600.ttf", "Poppins-Bold": "Poppins-700.ttf",
        "DMSans": "DMSans-400.ttf", "DMSans-Medium": "DMSans-500.ttf", "DMSans-Bold": "DMSans-700.ttf",
    }
    disponiveis = set(pdfmetrics.getRegisteredFontNames())
    for nome, arquivo in fontes.items():
        if nome in disponiveis:
            continue
        caminho = os.path.join(_PASTA_FONTES, arquivo)
        if os.path.exists(caminho):
            pdfmetrics.registerFont(TTFont(nome, caminho))
    return "Poppins" in pdfmetrics.getRegisteredFontNames()


_FONTES_OK = _registrar_fontes()
POPPINS = "Poppins" if _FONTES_OK else "Helvetica"
POPPINS_SB = "Poppins-SemiBold" if _FONTES_OK else "Helvetica-Bold"
POPPINS_B = "Poppins-Bold" if _FONTES_OK else "Helvetica-Bold"
DMSANS = "DMSans" if _FONTES_OK else "Helvetica"
DMSANS_M = "DMSans-Medium" if _FONTES_OK else "Helvetica"
DMSANS_B = "DMSans-Bold" if _FONTES_OK else "Helvetica-Bold"

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

DESCRICAO_LICENCIAMENTO = "Licenciamento de software"
DESCRICAO_ALUGUEL = "Fatura de locação de equipamento"


def formatar_data(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _rotulo_secao_texto(texto: str) -> str:
    """Uppercase simples. O tracking .18em do design system não dá pra
    replicar de verdade: reportlab não tem letter-spacing nativo em
    Paragraph, e inserir espaços finos entre letras não funciona aqui — o
    layout do Paragraph trata espaços como pontos de quebra de linha
    uniformes, então a diferença de largura entre os tipos de espaço se
    perde e as palavras colam umas nas outras em vez de ganhar tracking."""
    return texto.upper()


def _estilo(fonte=DMSANS, tamanho=9, alinhamento=TA_LEFT, cor=colors.black, leading=None):
    return ParagraphStyle(
        "texto", fontName=fonte, fontSize=tamanho, alignment=alinhamento,
        textColor=cor, leading=leading or tamanho + 3,
    )


def _p(texto, **kwargs):
    return Paragraph(texto, _estilo(**kwargs))


def _rotulo_secao(numero: str, texto: str):
    return _p(f"{numero} · {_rotulo_secao_texto(texto)}", fonte=POPPINS_SB, tamanho=7.5, cor=COBALT)


def _cartao(conteudo_linhas, largura, risco_topo=None):
    """Card com cantos arredondados (raio ~9pt / 12px) e hairline Steel 34%,
    igual ao 'grid de 2 colunas' da especificação. `risco_topo` desenha uma
    régua de 4px (ex: Fuel Green no bloco de pagamento) colada no topo."""
    linhas = list(conteudo_linhas)
    estilo = [
        ("BOX", (0, 0), (-1, -1), 0.75, STEEL_HAIRLINE_34),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
    ]
    if risco_topo is not None:
        estilo.append(("LINEABOVE", (0, 0), (-1, 0), 4, risco_topo))
        estilo.append(("TOPPADDING", (0, 0), (-1, 0), 12))
    return Table([[linha] for linha in linhas], colWidths=[largura], style=TableStyle(estilo))


def gerar_pdf_nota_debito(numero: str, data_emissao: date, contato: dict,
                           destinatario: dict, itens: list, caminho_saida: str,
                           signatario: dict = None) -> float:
    """Desenha o PDF e retorna o valor total (soma dos itens).

    `signatario` (opcional): {"nome": str, "imagem": bytes}. A imagem entra
    acima da linha de assinatura no bloco 5; sem ela, fica só a linha em
    branco. Razão social/CNPJ do bloco 5 são sempre da CTA (emissor) — é a
    CTA que autoriza os dados de pagamento ali informados.
    """
    doc = SimpleDocTemplate(
        caminho_saida, pagesize=A4,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )
    largura_util = doc.width
    elementos = []

    sem_borda = TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])

    # cabeçalho: wordmark à esquerda, número e data à direita, régua Cobalt 3px abaixo
    elementos.append(Table(
        [[
            _p("ctasmart", fonte=POPPINS_B, tamanho=15, cor=COBALT),
            [
                _p(f"NOTA DE DÉBITO Nº {numero}", fonte=POPPINS_B, tamanho=13, cor=NIGHT, alinhamento=TA_RIGHT),
                _p(f"Data de Emissão: {formatar_data(data_emissao)}", fonte=DMSANS, tamanho=8.5, cor=STEEL, alinhamento=TA_RIGHT),
            ],
        ]],
        colWidths=[largura_util * 0.4, largura_util * 0.6], style=sem_borda,
    ))
    elementos.append(Spacer(1, 6))
    elementos.append(Table([[""]], colWidths=[largura_util], rowHeights=[3],
                            style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), COBALT)])))
    elementos.append(Spacer(1, 12))

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
    bloco_emissor = _cartao([_rotulo_secao("1", "Dados do Emissor"), _p(emissor_txt)], largura_util * 0.485)
    bloco_destinatario = _cartao([_rotulo_secao("2", "Dados do Destinatário"), _p(destinatario_txt)], largura_util * 0.485)
    elementos.append(Table(
        [[bloco_emissor, bloco_destinatario]],
        colWidths=[largura_util * 0.5, largura_util * 0.5], style=sem_borda,
    ))
    elementos.append(Spacer(1, 12))

    # 3 · Descrição do débito
    elementos.append(_rotulo_secao("3", "Descrição do Débito"))
    elementos.append(Spacer(1, 5))

    total = sum(item["valor"] for item in itens)
    cabecalho_tabela = [_p(t, fonte=DMSANS_B, tamanho=8, cor=colors.white, alinhamento=TA_CENTER) for t in
                         ["Item", "Título", "Nº NFS-e", "Descrição dos serviços / despesas",
                          "Vencimento", "Valor (R$)"]]
    linhas_tabela = [cabecalho_tabela]
    for i, item in enumerate(itens, start=1):
        linhas_tabela.append([
            _p(f"{i:02d}", fonte=DMSANS, alinhamento=TA_CENTER),
            _p(item["titulo"], fonte=DMSANS, alinhamento=TA_CENTER),
            _p(item.get("numero_nfse") or "-", fonte=DMSANS, alinhamento=TA_CENTER),
            _p(item["descricao"], fonte=DMSANS),
            _p(formatar_data(item["vencimento"]), fonte=DMSANS, alinhamento=TA_CENTER),
            _p(formatar_moeda(item["valor"]), fonte=DMSANS, alinhamento=TA_RIGHT),
        ])
    n_linhas = len(linhas_tabela)
    tabela_debito = Table(
        linhas_tabela,
        colWidths=[largura_util * 0.07, largura_util * 0.17, largura_util * 0.12,
                   largura_util * 0.31, largura_util * 0.14, largura_util * 0.19],
        repeatRows=1,  # cabeçalho repete se a tabela quebrar de página
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NIGHT),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, OFF_WHITE]),
            ("LINEBELOW", (0, 0), (-1, n_linhas - 2), 0.5, STEEL_HAIRLINE_26),
            ("BOX", (0, 0), (-1, -1), 0.75, STEEL_HAIRLINE_34),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]),
    )
    elementos.append(tabela_debito)
    elementos.append(Table(
        [["", _p("TOTAL", fonte=POPPINS_B, tamanho=9.5, cor=NIGHT, alinhamento=TA_RIGHT),
          _p(formatar_moeda(total), fonte=POPPINS_B, tamanho=10.5, cor=COBALT, alinhamento=TA_RIGHT)]],
        colWidths=[largura_util * 0.60, largura_util * 0.22, largura_util * 0.18],
        style=TableStyle([
            ("LINEABOVE", (1, 0), (-1, 0), 2, NIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]),
    ))
    elementos.append(Spacer(1, 14))

    # 4 · Dados para pagamento / 5 · Assinatura e aceite — sempre juntos
    # (a especificação pede as duas na última página; KeepTogether garante
    # que pelo menos não quebram uma da outra no meio)
    pagamento_txt = (
        f"Banco: {DADOS_PAGAMENTO['banco']}<br/>"
        f"Agência: {DADOS_PAGAMENTO['agencia']}<br/>"
        f"Conta Corrente: {DADOS_PAGAMENTO['conta_corrente']}<br/>"
        f"Chave PIX: {DADOS_PAGAMENTO['chave_pix']}<br/>"
        f"Favorecido: {DADOS_PAGAMENTO['favorecido']}"
    )
    linhas_assinatura = [_rotulo_secao("5", "Assinatura e Aceite")]
    if signatario and signatario.get("imagem"):
        img = Image(BytesIO(signatario["imagem"]), width=110, height=40)
        img.hAlign = "LEFT"
        linhas_assinatura.append(img)
    else:
        linhas_assinatura.append(Spacer(1, 24))
    linhas_assinatura.append(_p(
        f"_______________________________<br/>{EMISSOR['razao_social']}<br/>CNPJ: {EMISSOR['cnpj']}",
        fonte=DMSANS,
    ))

    bloco_pagamento = _cartao([_rotulo_secao("4", "Dados para Pagamento"), _p(pagamento_txt)],
                              largura_util * 0.485, risco_topo=FUEL_GREEN)
    bloco_assinatura = _cartao(linhas_assinatura, largura_util * 0.485)
    elementos.append(KeepTogether([Table(
        [[bloco_pagamento, bloco_assinatura]],
        colWidths=[largura_util * 0.5, largura_util * 0.5], style=sem_borda,
    )]))

    doc.build(elementos)
    return total
