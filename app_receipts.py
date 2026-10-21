import io
import json
import re
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
API_URL = (
    "https://mingle-ionapi.inforcloudsuite.com/US45PBYRE7XKA5QB_PRD/WM/"
    "wmwebservice_rest/US45PBYRE7XKA5QB_PRD_COBALTLIKABLECAT_PRD_SCE_PRD_4_wmwhse1/receipts"
)
MAX_ITENS = 500  # limite de receiptdetails por chamada (1000 dava timeout)
TIMEOUT = 120
FIXO = "— valor fixo —"
NENHUMA = "— nenhuma —"

st.set_page_config(page_title="Receipts WMS", page_icon="📦", layout="wide")


# ----------------------------------------------------------------------------
# Utilitários
# ----------------------------------------------------------------------------
def parse_numero(valor) -> float:
    """Lê números em vários formatos regionais: 12.00000, 10,50000, 2,323.00000, 2.323,00000."""
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return 0.0 if pd.isna(valor) else float(valor)
    s = str(valor).strip().replace(" ", "")
    if s == "" or s.lower() == "nan":
        return 0.0
    if "," in s and "." in s:
        # o último separador é o decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def fmt_qtd(v: float):
    return int(v) if float(v).is_integer() else v


def limpar(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def sugerir(colunas, *palavras):
    for c in colunas:
        nome = re.sub(r"[^a-z0-9]", "", str(c).lower())
        if any(p in nome for p in palavras):
            return c
    return None


# ----------------------------------------------------------------------------
# Autenticação ION API (usa o campo "pu" do .ionapi, não o "iu")
# ----------------------------------------------------------------------------
def obter_token(ionapi: dict) -> str:
    token_url = ionapi["pu"].rstrip("/") + "/" + ionapi["ot"].lstrip("/")
    resp = requests.post(
        token_url,
        data={
            "grant_type": "password",
            "username": ionapi["saak"],
            "password": ionapi["sask"],
            "client_id": ionapi["ci"],
            "client_secret": ionapi["cs"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ----------------------------------------------------------------------------
# Montagem dos payloads
# ----------------------------------------------------------------------------
def valor_campo(linha, escolha, fixo):
    if escolha == FIXO or escolha is None:
        return fixo
    return limpar(linha.get(escolha))


def montar_payloads(df, m, cfg):
    """Agrupa por receiptkey e quebra em partes de até MAX_ITENS receiptdetails.
    Parte 1 mantém o receiptkey; as seguintes recebem sufixo P2, P3..."""
    payloads = []
    for rk, grupo in df.groupby(m["receiptkey"], sort=False):
        rk = limpar(rk)
        linhas = grupo.to_dict("records")
        for i in range(0, len(linhas), MAX_ITENS):
            parte = i // MAX_ITENS + 1
            chave = rk if parte == 1 else f"{rk}P{parte}"
            bloco = linhas[i : i + MAX_ITENS]
            storer_header = valor_campo(bloco[0], m["storerkey"], cfg["storerkey_fixo"])

            detalhes = []
            for linha in bloco:
                esperada = parse_numero(linha.get(m["qtyexpected"]))
                if m["qtyreceived"] == FIXO:
                    recebida = esperada
                else:
                    recebida = parse_numero(linha.get(m["qtyreceived"]))
                detalhes.append(
                    {
                        "sku": limpar(linha.get(m["sku"])),
                        "storerkey": valor_campo(linha, m["storerkey"], cfg["storerkey_fixo"]),
                        "qtyexpected": fmt_qtd(esperada),
                        "qtyreceived": fmt_qtd(recebida),
                        "toid": "",  # fixo em branco
                        "status": cfg["status_detalhe"],
                        "conditioncode": valor_campo(linha, m["conditioncode"], cfg["conditioncode_fixo"]),
                        "toloc": valor_campo(linha, m["toloc"], cfg["toloc_fixo"]),
                        "lottable02": chave,  # igual ao receiptkey enviado
                        "lottable03": limpar(linha.get(m["toid"])),  # vem da coluna mapeada como toid
                    }
                )

            payloads.append(
                {
                    "receiptkey": chave,
                    "storerkey": storer_header,
                    "status": cfg["status_header"],
                    "receiptdetails": detalhes,
                }
            )
    return payloads


def payload_para_csv(payload: dict) -> str:
    linhas = []
    for d in payload["receiptdetails"]:
        linhas.append(
            {"receiptkey": payload["receiptkey"], "header_storerkey": payload["storerkey"],
             "header_status": payload["status"], **d}
        )
    return pd.DataFrame(linhas).to_csv(index=False, sep=";")


# ----------------------------------------------------------------------------
# Envio
# ----------------------------------------------------------------------------
def enviar(payloads, ionapi, progresso, texto):
    resultados = []
    token = obter_token(ionapi)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    total = len(payloads)
    for n, p in enumerate(payloads, start=1):
        texto.write(f"Enviando {p['receiptkey']} ({len(p['receiptdetails'])} itens) — {n}/{total}")
        try:
            r = requests.post(API_URL, headers=headers, json=p, timeout=TIMEOUT)
            if r.status_code == 401:  # token expirou: renova e tenta de novo
                headers["Authorization"] = f"Bearer {obter_token(ionapi)}"
                r = requests.post(API_URL, headers=headers, json=p, timeout=TIMEOUT)
            ok = r.status_code in (200, 201, 202, 204)
            resposta = r.text[:4000]
            codigo = r.status_code
        except requests.exceptions.Timeout:
            ok, codigo, resposta = False, "timeout", f"Sem resposta em {TIMEOUT}s"
        except Exception as e:  # noqa: BLE001
            ok, codigo, resposta = False, "erro", str(e)
        resultados.append(
            {"receiptkey": p["receiptkey"], "itens": len(p["receiptdetails"]),
             "ok": ok, "codigo": codigo, "resposta": resposta, "payload": p}
        )
        progresso.progress(n / total)
    return resultados


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------
st.title("📦 Recebimento em Massa — Infor WMS")
st.caption(
    f"Cada linha da planilha é um item recebido. Linhas com o mesmo receiptkey formam a mesma nota. "
    f"Máximo de {MAX_ITENS} itens por chamada; o excedente vira novas chamadas com receiptkey + P2, P3..."
)

with st.sidebar:
    st.header("Configuração")
    arq_ion = st.file_uploader("Arquivo .ionapi", type=["ionapi", "json"])
    st.subheader("Valores padrão")
    status_header = st.text_input("Status (header)", value="0")
    status_detalhe = st.text_input("Status (detalhe)", value="0")
    storerkey_fixo = st.text_input("Proprietário fixo", value="")
    toloc_fixo = st.text_input("Local fixo", value="")
    conditioncode_fixo = st.text_input("Status saldo fixo", value="OK")

arq = st.file_uploader("Planilha de recebimento", type=["xlsx", "xlsm", "xls", "csv"])

if arq is None:
    st.info("Suba a planilha para começar.")
    st.stop()

try:
    if arq.name.lower().endswith(".csv"):
        bruto = arq.getvalue()
        try:
            df = pd.read_csv(io.BytesIO(bruto), dtype=str, sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(bruto), dtype=str, sep=None, engine="python", encoding="latin-1")
    else:
        xls = pd.ExcelFile(arq)
        aba = st.selectbox("Aba", xls.sheet_names) if len(xls.sheet_names) > 1 else xls.sheet_names[0]
        df = xls.parse(aba, dtype=str)
except Exception as e:  # noqa: BLE001
    st.error(f"Não consegui ler a planilha: {e}")
    st.stop()

df.columns = [str(c).strip() for c in df.columns]
df = df.dropna(how="all")
cols = list(df.columns)
st.write(f"**{len(df)} linhas** · {len(cols)} colunas")
st.dataframe(df.head(10), use_container_width=True)

st.subheader("Mapeamento de colunas")


def escolher(rotulo, sugestao, opcoes_extra=()):
    opcoes = list(opcoes_extra) + cols
    idx = opcoes.index(sugestao) if sugestao in opcoes else 0
    return st.selectbox(rotulo, opcoes, index=idx)


c1, c2, c3, c4 = st.columns(4)
with c1:
    mapa = {
        "receiptkey": escolher("receiptkey", sugerir(cols, "receiptkey", "nota", "nf", "recibo")),
        "sku": escolher("sku", sugerir(cols, "sku", "item", "material")),
    }
with c2:
    mapa["qtyexpected"] = escolher("qtyexpected", sugerir(cols, "qtyexpected", "qtd", "quant"))
    mapa["qtyreceived"] = escolher(
        "qtyreceived", sugerir(cols, "qtyreceived", "recebida"), opcoes_extra=[FIXO]
    )
with c3:
    mapa["toid"] = escolher("toid (vai para lottable03)", sugerir(cols, "toid", "lpn", "id"))
    mapa["toloc"] = escolher("toloc", sugerir(cols, "toloc", "local", "endereco"), opcoes_extra=[FIXO])
with c4:
    mapa["storerkey"] = escolher("storerkey", sugerir(cols, "storer", "cliente"), opcoes_extra=[FIXO])
    mapa["conditioncode"] = escolher(
        "conditioncode", sugerir(cols, "condition", "condicao"), opcoes_extra=[FIXO]
    )

st.caption("Quando a coluna é “valor fixo”, vale o que estiver na barra lateral. Se qtyreceived for fixo, copia a qtyexpected.")

cfg = {
    "status_header": status_header,
    "status_detalhe": status_detalhe,
    "storerkey_fixo": storerkey_fixo,
    "toloc_fixo": toloc_fixo,
    "conditioncode_fixo": conditioncode_fixo,
}

obrigatorias_ok = len({mapa["receiptkey"], mapa["sku"], mapa["qtyexpected"]}) == 3
if not obrigatorias_ok:
    st.warning("receiptkey, sku e qtyexpected precisam apontar para colunas diferentes.")
    st.stop()

payloads = montar_payloads(df, mapa, cfg)
resumo = pd.DataFrame(
    [{"receiptkey": p["receiptkey"], "itens": len(p["receiptdetails"])} for p in payloads]
)

st.subheader("Prévia do envio")
a, b = st.columns(2)
a.metric("Chamadas", len(payloads))
b.metric("Itens", int(resumo["itens"].sum()))
st.dataframe(resumo, use_container_width=True, hide_index=True)
with st.expander("Ver exemplo de payload (primeira chamada, 2 primeiros itens)"):
    exemplo = {**payloads[0], "receiptdetails": payloads[0]["receiptdetails"][:2]}
    st.code(json.dumps(exemplo, indent=2, ensure_ascii=False), language="json")

enviar_btn = st.button("🚀 Enviar para o WMS", type="primary", disabled=arq_ion is None)
if arq_ion is None:
    st.caption("Suba o .ionapi na barra lateral para liberar o envio.")

if enviar_btn:
    try:
        ionapi = json.load(arq_ion)
    except Exception as e:  # noqa: BLE001
        st.error(f"Arquivo .ionapi inválido: {e}")
        st.stop()
    barra = st.progress(0.0)
    status_txt = st.empty()
    try:
        st.session_state["resultados"] = enviar(payloads, ionapi, barra, status_txt)
        st.session_state["quando"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    except Exception as e:  # noqa: BLE001
        st.error(f"Falha na autenticação: {e}")
        st.stop()
    status_txt.empty()

resultados = st.session_state.get("resultados")
if resultados:
    ok = [r for r in resultados if r["ok"]]
    erros = [r for r in resultados if not r["ok"]]
    st.subheader(f"Resultado — {st.session_state.get('quando', '')}")
    x, y = st.columns(2)
    x.metric("Enviadas com sucesso", len(ok))
    y.metric("Com erro", len(erros))

    if erros:
        st.error("Partes com erro")
        for i, r in enumerate(erros):
            with st.expander(f"❌ {r['receiptkey']} — {r['itens']} itens — HTTP {r['codigo']}"):
                st.code(r["resposta"] or "(sem corpo na resposta)")
                d1, d2 = st.columns(2)
                d1.download_button(
                    "⬇️ JSON desta parte",
                    data=json.dumps(r["payload"], indent=2, ensure_ascii=False),
                    file_name=f"{r['receiptkey']}.json",
                    mime="application/json",
                    key=f"json_{i}",
                )
                d2.download_button(
                    "⬇️ CSV desta parte",
                    data=payload_para_csv(r["payload"]),
                    file_name=f"{r['receiptkey']}.csv",
                    mime="text/csv",
                    key=f"csv_{i}",
                )
    else:
        st.success("Todas as partes foram enviadas com sucesso.")

    if ok:
        with st.expander(f"✅ Partes enviadas ({len(ok)})"):
            st.dataframe(
                pd.DataFrame([{"receiptkey": r["receiptkey"], "itens": r["itens"], "http": r["codigo"]} for r in ok]),
                use_container_width=True,
                hide_index=True,
            )
