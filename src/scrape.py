"""
Etapa 1 — coleta de dados de tabuademares.com/br/paraiba/joao-pessoa.

Objetivo: gravar em data/raw/ os CSVs que as Etapas 2 e 3 vão consumir.

    mares_<ano>.csv      tábua de marés do ano (4 marés por dia)
    mares_previsao.csv   marés do mês corrente, para cruzar com a previsão
    ondas.csv            altura de onda hora a hora (~7 dias à frente)
    vento.csv            velocidade do vento hora a hora (~7 dias à frente)

De onde vem cada dado:
    - tábua mensal: POST em URL_BASE com o campo fecha=AAAA-MM-01.
      A resposta é HTML com a tabela <table id="tabla_mareas">.
    - ondas e vento: GET no endpoint JSON que o próprio site usa (URL_API).
      Uma única resposta traz as duas coisas, hora a hora, por ~7 dias.

Rodar com: uv run python src/scrape.py
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests
from bs4 import BeautifulSoup

URL_BASE = "https://tabuademares.com/br/paraiba/joao-pessoa"

URL_API = "https://io-s2.nautide.com/v003/wcache"
PARAMS_API = {
    "c": "br179",
    "l": "-7.11390",
    "g": "-34.82094",
    "u": "/br/paraiba/joao-pessoa",
    "x": "MjkwMQ==",
    "d": "tabuademares.com",
}

# Servidores rejeitam clientes sem User-Agent. Identifique-se.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) "
        "Gecko/20100101 "
        "Firefox/152.0"
    )
}

# Segundos de pausa entre requests. Não tire isso.
PAUSA = 1.5
TIMEOUT = 30

DIR_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"

COLS_ONDAS = ["data_hora", "onda_altura_m", "swell_altura_m",
              "swell_direcao_graus", "swell_periodo_s"]
COLS_VENTO = ["data_hora", "vento_kmh", "rajada_kmh",
              "vento_direcao_graus", "vento_direcao"]

RE_LINHA = re.compile(
    r"^(\d{1,2})\s+(?:Seg|Ter|Qua|Qui|Sex|Sáb|Dom)\s+"
    r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})"                
    r"((?:\s+\d{1,2}:\d{2}\s+-?\d+,\d+\s*m){1,4})"     
    r"\s+(\d{1,3})\s+(muito alto|alto|médio|baixo)"    
)
RE_EVENTO = re.compile(r"(\d{1,2}):(\d{2})\s+(-?\d+,\d+)\s*m")


def baixar_html(url: str, dados_post: dict | None = None) -> str:
    """Baixa uma página e devolve o texto da resposta.

    Usa POST quando `dados_post` é passado (é assim que o site troca de mês);
    GET no resto. Também serve para o endpoint JSON da previsão: a resposta
    é devolvida como texto e quem chama decide como interpretar.
    """
    headers = {
        **HEADERS,
        "Referer": "https://tabuademares.com/",
        "Origin": "https://tabuademares.com",
    }

    if dados_post is None:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    else:
        resp = requests.post(url, headers=headers, data=dados_post, timeout=TIMEOUT)
    resp.raise_for_status()

    if "charset" not in resp.headers.get("content-type", "").lower():
        resp.encoding = "utf-8"
    return resp.text


def parsear_mares(html: str, ano: int, mes: int) -> list[dict]:
    """Extrai da tábua mensal uma linha por evento de maré.

    Capturado por evento: data_hora, altura_m e tipo (alta/baixa), mais o
    que é do dia: nascer/pôr do sol, coeficiente de maré e atividade.
    A fase da lua não aparece como texto na tabela (é um ícone), então
    fica de fora.
    """
    soup = BeautifulSoup(html, "html.parser")
    tabela = soup.find("table", id="tabla_mareas")
    if tabela is None:
        raise ValueError(f"table#tabla_mareas não encontrada em {ano}-{mes:02d}")

    eventos = []
    for tr in tabela.find_all("tr"):
        m = RE_LINHA.match(tr.get_text(" ", strip=True))
        if not m:
            continue                    
        dia = int(m.group(1))
        for hh, mm, altura in RE_EVENTO.findall(m.group(4)):
            eventos.append({
                "data_hora": f"{ano}-{mes:02d}-{dia:02d} {int(hh):02d}:{mm}",
                "altura_m": float(altura.replace(",", ".")),
                "nascer_sol": m.group(2),
                "por_sol": m.group(3),
                "coeficiente": int(m.group(5)),
                "atividade": m.group(6),
            })

    if len(eventos) > 1:
        for i, ev in enumerate(eventos):
            vizinho = eventos[i - 1] if i > 0 else eventos[1]
            ev["tipo"] = "alta" if ev["altura_m"] > vizinho["altura_m"] else "baixa"

    return eventos


def parsear_previsao(html: str, ano: int) -> list[dict]:
    """Extrai leituras horárias de onda e de vento (uma linha por hora).

    `html` é o texto da resposta JSON da previsão. O JSON tem um bloco por
    dia (data2.weather) e, dentro dele, 24 leituras (hourly). Uma função só
    dá conta de onda e vento: o main separa as colunas em dois CSVs.

    A data já vem com o ano ("2026-10-10"), então `ano` não é usado aqui;
    o parâmetro continua na assinatura original.
    """
    if not html.strip():
        raise ValueError("resposta vazia da API de previsão")
    dados = json.loads(html)

    linhas = []
    for dia in dados["data2"]["weather"]:
        for h in dia["hourly"]:
            hora = int(h["time"]) // 100       
            linhas.append({
                "data_hora": f"{dia['date']} {hora:02d}:00",
                # onda
                "onda_altura_m": float(h["sigHeight_m"]),
                "swell_altura_m": float(h["swellHeight_m"]),
                "swell_direcao_graus": float(h["swellDir"]),
                "swell_periodo_s": float(h["swellPeriod_secs"]),
                # vento
                "vento_kmh": float(h["windspeedKmph"]),
                "rajada_kmh": float(h["WindGustKmph"]),
                "vento_direcao_graus": float(h["winddirDegree"]),
                "vento_direcao": h["winddir16Point"],
            })
    return linhas


def coletar_mares_do_ano(ano: int) -> pd.DataFrame:
    """Junta os 12 meses da tábua de marés de `ano` num DataFrame."""
    linhas = []
    for mes in range(1, 13):
        html = baixar_html(URL_BASE, dados_post={"fecha": f"{ano}-{mes:02d}-01"})
        try:
            eventos = parsear_mares(html, ano, mes)
        except ValueError as erro:
            print("aviso:", erro)
            eventos = []
        print(f"{ano}-{mes:02d}: {len(eventos)} eventos")
        linhas += eventos
        time.sleep(PAUSA)

    df = pd.DataFrame(linhas)
    if df.empty:
        return df
    return df.sort_values("data_hora").reset_index(drop=True)


def montar_url_previsao(quando: datetime) -> str:
    """URL do endpoint JSON, com `f` = data e hora de referência."""
    params = {**PARAMS_API, "f": quando.strftime("%Y-%m-%d %H:%M")}
    return f"{URL_API}?{urlencode(params)}"


def salvar(nome: str, df: pd.DataFrame) -> None:
    """Grava o CSV e imprime quantas linhas ele recebeu."""
    df.to_csv(DIR_RAW / f"{nome}.csv", index=False)
    aviso = "   <-- VAZIO!" if df.empty else ""
    print(f"{nome}.csv: {len(df)} linhas{aviso}")


def main(ano: int = 2025) -> None:
    DIR_RAW.mkdir(parents=True, exist_ok=True)
    hoje = datetime.now()

    mares_ano = coletar_mares_do_ano(ano)
    salvar(f"mares_{ano}", mares_ano)

    # 2. marés do mês corrente -> mares_previsao.csv
    html = baixar_html(URL_BASE, dados_post={"fecha": f"{hoje:%Y-%m}-01"})
    mares_mes = pd.DataFrame(parsear_mares(html, hoje.year, hoje.month))
    salvar("mares_previsao", mares_mes)
    time.sleep(PAUSA)

    texto = baixar_html(montar_url_previsao(hoje))
    previsao = pd.DataFrame(parsear_previsao(texto, hoje.year))
    salvar("ondas", previsao[COLS_ONDAS])
    salvar("vento", previsao[COLS_VENTO])

    html = baixar_html(URL_BASE, dados_post={"fecha": "2025-01-01"})
    tabela = BeautifulSoup(html, "html.parser").find("table", id="tabla_mareas")
    print(len(html), tabela is not None)
    if tabela:
        for tr in tabela.find_all("tr")[:6]:
            print(repr(tr.get_text(" ", strip=True)))


if __name__ == "__main__":
    main()