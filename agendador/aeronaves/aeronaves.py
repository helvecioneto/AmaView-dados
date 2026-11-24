#!/usr/bin/env python3
"""
Aeronaves de serviço público sobre a Amazônia Legal, para o AmaView: a
posição que cada uma transmite por ADS-B, lida das redes abertas de
receptores voluntários.

- **Quem entra.** Todas as aeronaves com posição na caixa, como as
  embarcações do AIS, menos as que o dono pediu para esconder (PIA e LADD,
  `dbFlags` 4 e 8). As de serviço público vêm marcadas com a categoria:
  - as militares brasileiras (endereço ICAO do Brasil no filtro militar da rede);
  - as de órgão público no RAB da ANAC (bombeiros, Defesa Civil, saúde,
    polícias, meio ambiente, governos), com o nome do órgão;
  - os aviões-tanque e helicópteros das empresas contratadas para o combate a
    incêndio (`CONTRATADAS`).
  As outras saem como `outro`, só com matrícula, modelo e voo: nunca o dono.
- **Atraso para fiscalização.** Polícia, meio ambiente (IBAMA, ICMBio, FUNAI),
  governo e as Forças Armadas, menos os cargueiros grandes, saem com
  `ATRASO_S` (1 h) de atraso. Combate a incêndio, bombeiros, Defesa Civil,
  saúde e os cargueiros da FAB saem em tempo real. A posição recente das
  atrasadas fica só no estado (`/var/lib`), que o nginx não serve.
- **Fontes, em ordem.** adsb.fi (1 pedido/s, sem teto diário), adsb.lol se a
  adsb.fi falhar (teto de 500 pedidos por dia e por IP, medido em 24/11/2026
  no `x-ratelimit-limit: 500;w=86400`: só entra com cota, e só nos círculos
  ativos) e a OpenSky se as duas falharem (a caixa inteira numa consulta de
  4 créditos, no máximo a cada `OPENSKY_INTERVALO_S`, com a conta em
  OPENSKY_CLIENT_ID/OPENSKY_CLIENT_SECRET, se houver).
- **Pouca chamada.** A caixa é coberta por `GRADE`, 27 círculos de 250 milhas
  náuticas (o maior raio das redes). Cada rodada pede um terço deles, em
  giro (todos a cada 3 min), mais os que tiveram aeronave na última meia
  hora (hoje, os poucos com receptor perto) e o filtro militar, que é uma
  chamada só para o mundo todo.
- **Dado como a fonte publica.** adsb.lol e adsb.fi dão a altitude em pés e a
  velocidade em nós; a OpenSky, em metros e m/s. Cada ponto leva a fonte, e
  o app mostra a unidade dela.

Saída (abaixo de AERONAVES_RAIZ, servida pelo nginx em /aeronaves/v1/):

  aeronaves/v1/atual.json   as aeronaves e as trilhas das últimas `JANELA_S` (6 h)

  aeronaves.py          uma rodada (o que o timer chama, a cada minuto)
  aeronaves.py frota    refaz a lista de aeronaves públicas e imprime o resumo
"""
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSAO = 1
RAIZ = os.environ.get("AERONAVES_RAIZ", "/var/cache/amaview-aeronaves")
ESTADO_DIR = os.environ.get("AERONAVES_ESTADO", "/var/lib/amaview-aeronaves")
PASTA = os.path.join(RAIZ, "aeronaves", f"v{VERSAO}")
ARQ_ATUAL = os.path.join(PASTA, "atual.json")
ARQ_ESTADO = os.path.join(ESTADO_DIR, "estado.json")
ARQ_FROTA = os.path.join(ESTADO_DIR, "frota.json")
AGENTE = "AmaView-aeronaves/1 (+https://amaview.com.br)"

# Amazônia Legal com ~1° de folga (oeste, sul, leste, norte).
CAIXA = (-75.0, -19.5, -42.5, 6.5)
# 6 h de trilha: o dobro do período padrão da barra (3 h) e um arquivo que
# cabe na revalidação de minuto a minuto. Períodos maiores mostram as
# aeronaves só nas últimas 6 h.
JANELA_S = 6 * 3600
ATRASO_S = 3600
# Posição mais velha que isto, no momento da consulta, não entra (a rede
# devolve a última posição de quem saiu do alcance por até ~1 min).
POSICAO_MAX_S = 60
# Dois pontos da mesma aeronave a menos disto: fica o primeiro. As de serviço
# público a cada minuto; as outras a cada dois (o arquivo fica pela metade).
PASSO_PUBLICA_S = 50
PASSO_OUTRA_S = 110
GIRO = 3
ATIVO_S = 1800
# Cota da adsb.lol que fica guardada (não gastar até o último pedido do dia).
LOL_RESERVA = 30
RAIO_NM = 250
# Círculos de 250 NM que cobrem a CAIXA inteira (lat, lon): fileiras a cada
# 5,6° com deslocamento de meio passo, as que não tocam a caixa fora
# (conferido em test_aeronaves.py numa grade de 0,25°).
GRADE = [
    (-17.5, -73.0), (-17.5, -65.67), (-17.5, -58.35), (-17.5, -51.02), (-17.5, -43.69),
    (-11.9, -76.57), (-11.9, -69.43), (-11.9, -62.29), (-11.9, -55.15), (-11.9, -48.01), (-11.9, -40.86),
    (-6.3, -73.0), (-6.3, -65.97), (-6.3, -58.94), (-6.3, -51.91), (-6.3, -44.88),
    (-0.7, -76.49), (-0.7, -69.51), (-0.7, -62.52), (-0.7, -55.53), (-0.7, -48.54), (-0.7, -41.55),
    (4.9, -73.0), (4.9, -65.99), (4.9, -58.97), (4.9, -51.96), (4.9, -44.95),
]
# Bits de `dbFlags`: 1 militar, 4 PIA, 8 LADD (o dono pediu para não aparecer).
ESCONDER = 4 | 8
REFAZER_FROTA_S = 24 * 3600
PRAZO_S = 20

RAB_URL = "https://sistemas.anac.gov.br/dadosabertos/Aeronaves/RAB/dados_aeronaves.csv"
DB_URL = "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"

# Índice da fonte em cada ponto (o app escolhe a unidade por ele).
FONTES = [
    # adsb.fi: 1 pedido por segundo, um de cada vez.
    {"id": "adsb.fi", "base": "https://opendata.adsb.fi/api/v2", "ponto": "/lat/{lat}/lon/{lon}/dist/{r}", "espera": 1.1},
    # adsb.lol: 500 pedidos por dia; com 4 ao mesmo tempo, 429 também.
    {"id": "adsb.lol", "base": "https://api.adsb.lol/v2", "ponto": "/point/{lat}/{lon}/{r}", "espera": 0.5, "cota": True},
]
OPENSKY = 2
NOMES_FONTE = ["adsb.fi", "adsb.lol", "OpenSky"]
CREDITOS = {
    "adsb.lol": "Aeronaves: adsb.lol (ODbL 1.0)",
    "adsb.fi": "Aeronaves: adsb.fi",
    "OpenSky": "Aeronaves: The OpenSky Network",
}
OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_INTERVALO_S = 120

# ---------------------------------------------------------------------------
# Classificação
# ---------------------------------------------------------------------------

# Aviões-tanque: Air Tractor 301/401/502/602/802 e Canadair 215T/415.
TANQUE = {"AT3T", "AT4T", "AT5T", "AT6T", "AT8T", "CL2T", "CL4T", "CL2P"}
# Cargueiros militares que levam brigadista, água e socorro: KC-390, Hércules, C-105/C-295, KC-30.
CARGUEIRO = {"E390", "C130", "C30J", "C295", "CN35", "A332"}
# Helicópteros pelo designador ICAO, para quando não há a classe do RAB.
HELI = {
    "AS50", "AS55", "AS65", "AS32", "AS3B", "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "H125", "H130",
    "H135", "H145", "H160", "H175", "H60", "S70", "S76", "S92", "A109", "A119", "A139", "A169", "B06", "B407", "B412",
    "B429", "B505", "BK17", "R22", "R44", "R66", "UH1", "MI8", "LYNX", "NH90", "EH10",
}

# Categoria → (rótulo, em tempo real?). A ordem de `OPERADORES` decide o empate.
CATEGORIAS = {
    "fogo": ("Combate a incêndio", True),
    "bombeiros": ("Bombeiros", True),
    "defesaCivil": ("Defesa Civil", True),
    "saude": ("Saúde", True),
    "cargueiro": ("Transporte militar", True),
    "militar": ("Forças Armadas", False),
    "policia": ("Polícia e segurança", False),
    "ambiental": ("Meio ambiente", False),
    "governo": ("Governo", False),
    "outro": ("Outras aeronaves", True),
}
PUBLICAS = {c for c in CATEGORIAS if c != "outro"}

OPERADORES = [
    ("bombeiros", r"BOMBEIRO"),
    ("defesaCivil", r"DEFESA CIVIL"),
    ("saude", r"\bSAMU\b|SECRETARIA.{0,30}SA[UÚ]DE|FUNDO.{0,30}SA[UÚ]DE"),
    ("policia", r"POL[IÍ]CIA|SEGURAN[ÇC]A P[UÚ]BLICA|BRIGADA MILITAR|CASA MILITAR|RODOVI[AÁ]RIA FEDERAL|REPRESS[ÃA]O|"
                r"DETRAN|PENITENCI|PRISIONAL|RECEITA FEDERAL"),
    ("ambiental", r"\bIBAMA\b|INSTITUTO BRASILEIRO DO MEIO AMBIENTE|ICMBIO|CHICO MENDES|FUNAI|POVOS IND[IÍ]GENAS|"
                  r"MEIO AMBIENTE|\bSEMAS?\b|NATURATINS"),
    ("militar", r"COMANDO D[AOE] (AERON[AÁ]UTICA|EX[EÉ]RCITO|MARINHA)|FOR[ÇC]A A[EÉ]REA|MINIST[EÉ]RIO DA DEFESA"),
    ("governo", r"^(GOVERNO|ESTADO) D[OAE]S? |^SECRETARIA (DE ESTADO|ESTADUAL)|CASA CIVIL|^MINIST[EÉ]RIO|^UNI[ÃA]O\b"),
]
OPERADORES_RE = [(c, re.compile(r)) for c, r in OPERADORES]
# Nome com estas palavras não é órgão público, mesmo citando um estado ("Aeroclube do Estado de…").
NAO_PUBLICO = re.compile(r"AEROCLUBE|ASSOCIA[ÇC][ÃA]O|ASSEMBLEIA|ESCOLA|\bLTDA\b|\bS/?A\b|EIRELI|\bME\b|FEDERA[ÇC][ÃA]O")

# Empresas com contrato de combate a incêndio: só os aviões-tanque e os
# helicópteros delas entram, e sempre como "fogo". Revisar a cada temporada
# (contratos no PNCP). O resto da frota delas (carga, táxi aéreo) fica de fora.
CONTRATADAS = [
    {"re": re.compile(r"AEROTERRA"), "orgao": "IBAMA", "contrato": "53/2026"},
    {"re": re.compile(r"HELISUL"), "orgao": "IBAMA", "contrato": "52/2026"},
    {"re": re.compile(r"RIO MADEIRA AVIA|^RIMA\b"), "orgao": "IBAMA", "contrato": "51/2026"},
]


def categoria_do_operador(nome: str) -> str | None:
    n = nome.upper().strip()
    if not n or n == "INDISPONÍVEL" or NAO_PUBLICO.search(n):
        return None
    for cat, r in OPERADORES_RE:
        if r.search(n):
            return cat
    return None


def contratada(nome: str) -> dict | None:
    n = nome.upper()
    for c in CONTRATADAS:
        if c["re"].search(n):
            return c
    return None


def eh_heli(tipo: str, classe: str = "") -> bool:
    return classe.upper().startswith("H") or tipo.upper() in HELI


def classificar_civil(operadores: list[str], tipo: str, classe: str) -> dict | None:
    """Aeronave do RAB → {c, o, k?} ou None (não é de serviço público)."""
    tipo = tipo.upper()
    for nome in operadores:
        cat = categoria_do_operador(nome)
        if cat:
            # Avião-tanque de órgão público combate incêndio, seja quem for o dono.
            if tipo in TANQUE:
                cat = "fogo"
            return {"c": cat, "o": nome.strip()}
    for nome in operadores:
        k = contratada(nome)
        if k and (tipo in TANQUE or eh_heli(tipo, classe)):
            return {"c": "fogo", "o": nome.strip(), "k": f"Contratada pelo {k['orgao']} (contrato {k['contrato']})"}
    return None


def classificar_militar(tipo: str, reg: str) -> dict:
    tipo = (tipo or "").upper()
    reg = (reg or "").upper()
    if reg.startswith("FAB"):
        orgao = "Força Aérea Brasileira"
    elif reg.startswith("EB"):
        orgao = "Exército Brasileiro"
    elif reg.startswith(("N-", "MB")):
        orgao = "Marinha do Brasil"
    else:
        orgao = "Forças Armadas"
    if tipo in TANQUE:
        return {"c": "fogo", "o": orgao}
    return {"c": "cargueiro" if tipo in CARGUEIRO else "militar", "o": orgao}


def hex_brasil(h: str) -> bool:
    """Bloco ICAO do Brasil: E40000–E7FFFF."""
    try:
        return 0xE40000 <= int(h, 16) <= 0xE7FFFF
    except ValueError:
        return False


def marca(m: str) -> str:
    """'PPNBS' → 'PP-NBS'."""
    m = m.strip().upper().replace("-", "")
    return f"{m[:2]}-{m[2:]}" if len(m) == 5 and m[0] == "P" else m


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------


def baixar(url: str, prazo: float = PRAZO_S, cabecalhos: dict | None = None, dados: bytes | None = None) -> bytes:
    h = {"User-Agent": AGENTE, "Accept-Encoding": "gzip"}
    h.update(cabecalhos or {})
    with urllib.request.urlopen(urllib.request.Request(url, data=dados, headers=h), timeout=prazo) as r:
        corpo = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            corpo = gzip.decompress(corpo)
        return corpo


def baixar_json(url: str, **kw):
    return json.loads(baixar(url, **kw))


def baixar_json_cab(url: str, prazo: float = PRAZO_S, cabecalhos: dict | None = None):
    """Como `baixar_json`, mas devolve também os cabeçalhos (a cota vem neles)."""
    h = {"User-Agent": AGENTE, "Accept-Encoding": "gzip"}
    h.update(cabecalhos or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=prazo) as r:
        corpo = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            corpo = gzip.decompress(corpo)
        return json.loads(corpo), r.headers


# ---------------------------------------------------------------------------
# Frota: quem é de serviço público (RAB + banco aberto de endereços)
# ---------------------------------------------------------------------------


def ler_rab(texto: str) -> dict:
    """CSV do RAB → {marca: {c, o, k?, t}} só das de serviço público, não canceladas."""
    linhas = texto.splitlines()
    if linhas and linhas[0].lstrip("﻿").startswith("Atualizado"):
        linhas = linhas[1:]
    saida = {}
    for row in csv.DictReader(linhas, delimiter=";"):
        if (row.get("DT_CANC") or "").strip():
            continue
        nomes = []
        for col in ("OPERADORES", "PROPRIETARIOS"):
            try:
                nomes += [str(o.get("NOME", "")) for o in json.loads(row.get(col) or "[]")]
            except (json.JSONDecodeError, AttributeError):
                pass
        tipo = (row.get("CD_TIPO_ICAO") or "").strip().upper()
        cl = classificar_civil(nomes, tipo, (row.get("CD_CLS") or "").strip())
        if cl:
            cl["t"] = tipo
            saida[marca(row.get("MARCAS", ""))] = cl
    return saida


def ler_db(gz: bytes) -> tuple[dict, dict]:
    """tar1090-db → ({marca: hex} do Brasil, {hex: {r, t, d}} das militares do Brasil)."""
    por_marca, militares = {}, {}
    with gzip.open(io.BytesIO(gz), "rt", encoding="utf-8", errors="replace") as f:
        for linha in f:
            c = linha.rstrip("\n").split(";")
            if len(c) < 4 or not hex_brasil(c[0]):
                continue
            h, r, t, flags = c[0].lower(), c[1].upper(), c[2].upper(), c[3]
            if r:
                por_marca[r] = h
            if flags[:1] == "1":
                militares[h] = {"r": r, "t": t, "d": c[4] if len(c) > 4 else ""}
    return por_marca, militares


def refazer_frota() -> dict:
    rab = baixar(RAB_URL, prazo=120).decode("utf-8-sig", errors="replace")
    publicas = ler_rab(rab)
    por_marca, militares = ler_db(baixar(DB_URL, prazo=120))
    for m, a in publicas.items():
        h = por_marca.get(m)
        if h:
            a["h"] = h
    frota = {"feita": int(time.time()), "publicas": publicas, "militares": militares}
    escrever_json(ARQ_FROTA, frota)
    return frota


def frota_atual(agora: float) -> dict:
    frota = ler_json(ARQ_FROTA, None)
    if frota and agora - frota.get("feita", 0) < REFAZER_FROTA_S:
        return frota
    try:
        return refazer_frota()
    except Exception as e:  # noqa: BLE001 — a frota de ontem serve
        print(f"frota: não refiz ({e}); sigo com a anterior", flush=True)
        if frota:
            return frota
        raise


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------


def url_ponto(fonte: dict, lat: float, lon: float) -> str:
    return fonte["base"] + fonte["ponto"].format(lat=lat, lon=lon, r=RAIO_NM)


def consultar_rede(fonte: dict, circulos: list[int], estado: dict, agora: float) -> list[tuple[int | None, dict]]:
    """Uma rede: o filtro militar e os círculos pedidos, um de cada vez. Erro em qualquer chamada sobe (a próxima rede assume)."""
    pedidos = [(None, fonte["base"] + "/mil")] + [(i, url_ponto(fonte, *GRADE[i])) for i in circulos]
    cota = estado.setdefault("cota", {}).get(fonte["id"]) if fonte.get("cota") else None
    if cota and cota.get("zera", 0) > agora and cota.get("restante", 10**6) - len(pedidos) < LOL_RESERVA:
        raise RuntimeError(f"sem cota até {time.strftime('%H:%M UTC', time.gmtime(cota['zera']))}")
    vistos: list[tuple[int | None, dict]] = []
    for n, (i, url) in enumerate(pedidos):
        if n:
            time.sleep(fonte["espera"])
        try:
            r, cab = baixar_json_cab(url)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            # Limite de taxa: uma nova tentativa depois do tempo que a rede pedir (até 5 s).
            time.sleep(min(5.0, float(e.headers.get("Retry-After") or 2)))
            r, cab = baixar_json_cab(url)
        if fonte.get("cota") and cab.get("x-ratelimit-remaining"):
            try:
                estado["cota"][fonte["id"]] = {
                    "restante": int(cab["x-ratelimit-remaining"]),
                    "zera": agora + int(cab.get("x-ratelimit-reset") or 0),
                }
            except ValueError:
                pass
        vistos += [(i, a) for a in (r.get("ac") or r.get("aircraft") or [])]
    return vistos


def ponto_adsb(a: dict, agora: float, fonte: int) -> list | None:
    """Aeronave vista → [t, lat, lon, altitude|None, velocidade|None, rumo|None, no chão, fonte].

    Altitude e velocidade na unidade da fonte: pés e nós (adsb.lol, adsb.fi),
    metros e m/s (OpenSky)."""
    lat, lon = a.get("lat"), a.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    idade = a.get("seen_pos", a.get("seen", 0)) or 0
    if idade > POSICAO_MAX_S:
        return None
    alt = a.get("alt_baro")
    chao = 1 if alt == "ground" else 0
    if not isinstance(alt, (int, float)):
        alt = a.get("alt_geom") if isinstance(a.get("alt_geom"), (int, float)) else None
    gs = a.get("gs") if isinstance(a.get("gs"), (int, float)) else None
    rumo = a.get("track") if isinstance(a.get("track"), (int, float)) else a.get("true_heading")
    rumo = rumo if isinstance(rumo, (int, float)) else None
    return [int(agora - idade), round(lat, 4), round(lon, 4), None if alt is None else int(alt),
            None if gs is None else round(gs, 1), None if rumo is None else round(rumo), chao, fonte]


def na_caixa(p: list) -> bool:
    o, s, l, n = CAIXA
    return s <= p[1] <= n and o <= p[2] <= l


def hex_de(a: dict) -> str | None:
    """Endereço ICAO de verdade (o `~` marca um endereço inventado pelo TIS-B ou pelo MLAT)."""
    h = str(a.get("hex") or "").lower()
    return None if not h or h.startswith("~") else h


def identificar(a: dict, frota: dict, por_hex: dict, publicas: dict, estado_av: dict) -> dict | None:
    """Metadados de uma aeronave vista, ou None se ela não deve aparecer."""
    h = hex_de(a)
    if not h or int(a.get("dbFlags") or 0) & ESCONDER:
        return None
    ind = str(a.get("flight") or "").strip().upper()
    r = str(a.get("r") or "").strip().upper()
    if hex_brasil(h):
        meta = por_hex.get(h) or publicas.get(r) or publicas.get(marca(ind))
        if meta:
            return meta
        mil = frota["militares"].get(h)
        if mil is not None or int(a.get("dbFlags") or 0) & 1:
            r = r or (mil or {}).get("r") or ""
            t = str(a.get("t") or (mil or {}).get("t") or "").upper()
            meta = {"r": r, "t": t, "d": a.get("desc") or (mil or {}).get("d") or ""}
            meta.update(classificar_militar(t, r))
            return meta
    if h in estado_av and not (a.get("r") or a.get("t")):
        # OpenSky não manda matrícula nem modelo: fica o que a rede já disse antes.
        return estado_av[h]["m"]
    if not r and re.fullmatch(r"P[PRSTU][A-Z]{3}", ind):
        r = marca(ind)
    return {"r": r, "t": str(a.get("t") or "").upper(), "d": a.get("desc") or "", "c": "outro"}


def indices(frota: dict) -> tuple[dict, dict]:
    """Da frota: as públicas por endereço e por marca."""
    por_hex, publicas = {}, {}
    for m, a in frota["publicas"].items():
        meta = {"r": m, "t": a.get("t", ""), "d": "", "c": a["c"], "o": a["o"]}
        if a.get("k"):
            meta["k"] = a["k"]
        publicas[m] = meta
        if a.get("h"):
            por_hex[a["h"]] = meta
    return por_hex, publicas


def token_opensky(estado: dict, agora: float) -> str | None:
    cid, seg = os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET")
    if not cid or not seg:
        return None
    tk = estado.get("opensky_token") or {}
    if tk.get("ate", 0) > agora + 60:
        return tk["v"]
    corpo = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": seg}).encode()
    r = baixar_json(OPENSKY_TOKEN, dados=corpo, cabecalhos={"Content-Type": "application/x-www-form-urlencoded"})
    estado["opensky_token"] = {"v": r["access_token"], "ate": agora + int(r.get("expires_in", 1800))}
    return r["access_token"]


def consultar_opensky(estado: dict, agora: float) -> list[tuple[int | None, dict]]:
    """OpenSky na caixa → aeronaves no formato da adsb.lol (só os campos usados), na unidade dela."""
    o, s, l, n = CAIXA
    url = f"{OPENSKY_URL}?lamin={s}&lomin={o}&lamax={n}&lomax={l}"
    tk = token_opensky(estado, agora)
    r = baixar_json(url, cabecalhos={"Authorization": f"Bearer {tk}"} if tk else None, prazo=30)
    t_resp = r.get("time") or agora
    saida = []
    for v in r.get("states") or []:
        if v[5] is None or v[6] is None or v[3] is None:
            continue
        saida.append((None, {
            "hex": v[0], "flight": v[1], "lat": v[6], "lon": v[5], "seen_pos": max(0, t_resp - v[3]),
            # Metros e m/s, como a OpenSky publica.
            "alt_baro": "ground" if v[8] else v[7], "gs": v[9], "track": v[10],
        }))
    return saida


# ---------------------------------------------------------------------------
# Rodada
# ---------------------------------------------------------------------------


def circulos_da_rodada(estado: dict, agora: float) -> tuple[list[int], list[int]]:
    """(os desta rodada, só os ativos). Desta rodada: um terço da grade em giro, mais os ativos."""
    ativos = estado.get("ativos", {})
    so_ativos = [i for i in range(len(GRADE)) if agora - ativos.get(str(i), 0) < ATIVO_S]
    giro = int(estado.get("giro", 0)) % GIRO
    estado["giro"] = giro + 1
    return sorted(set(so_ativos) | {i for i in range(len(GRADE)) if i % GIRO == giro}), so_ativos


def rodada(agora: float | None = None) -> dict:
    agora = agora or time.time()
    frota = frota_atual(agora)
    estado = ler_json(ARQ_ESTADO, {})
    av = estado.setdefault("av", {})
    ativos = estado.setdefault("ativos", {})
    por_hex, publicas = indices(frota)
    circulos, so_ativos = circulos_da_rodada(estado, agora)

    vistos, fonte = None, None
    for i, f in enumerate(FONTES):
        try:
            # A reserva com cota diária só pede os círculos ativos.
            vistos, fonte = consultar_rede(f, so_ativos if f.get("cota") else circulos, estado, agora), i
            break
        except Exception as e:  # noqa: BLE001 — a próxima rede responde
            print(f"{f['id']}: falhou ({e})", flush=True)
    if vistos is None and agora - estado.get("opensky_ultimo", 0) >= OPENSKY_INTERVALO_S:
        estado["opensky_ultimo"] = agora
        try:
            vistos, fonte = consultar_opensky(estado, agora), OPENSKY
        except Exception as e:  # noqa: BLE001
            print(f"OpenSky: falhou ({e})", flush=True)

    novos = 0
    if vistos is not None:
        for circulo, a in vistos:
            meta = identificar(a, frota, por_hex, publicas, av)
            if not meta:
                continue
            p = ponto_adsb(a, agora, fonte)
            if not p or not na_caixa(p):
                continue
            if circulo is not None:
                ativos[str(circulo)] = agora
            h = hex_de(a)
            reg = av.setdefault(h, {"m": meta, "p": []})
            reg["m"] = {**meta, "cs": str(a.get("flight") or "").strip() or reg["m"].get("cs", "")}
            passo = PASSO_PUBLICA_S if meta["c"] in PUBLICAS else PASSO_OUTRA_S
            if reg["p"] and p[0] - reg["p"][-1][0] < passo:
                continue
            reg["p"].append(p)
            novos += 1
        estado["fonte"] = fonte
        estado["consultado"] = int(agora)

    # Janela publicada, mais o atraso para quem sai atrasada.
    for h in list(av):
        av[h]["p"] = [p for p in av[h]["p"] if p[0] >= agora - JANELA_S - ATRASO_S]
        if not av[h]["p"]:
            del av[h]

    escrever_json(ARQ_ESTADO, estado)
    publicado = publicar(av, estado, agora)
    pub = sum(1 for x in publicado["aeronaves"] if x["c"] != "outro")
    print(
        f"{NOMES_FONTE[fonte] if fonte is not None else 'sem fonte'}: {len(circulos)} círculos · {novos} pontos novos · "
        f"{len(publicado['aeronaves'])} aeronaves publicadas ({pub} de serviço público, "
        f"{sum(1 for x in publicado['aeronaves'] if x['a'])} com atraso)",
        flush=True,
    )
    return publicado


def publicar(av: dict, estado: dict, agora: float) -> dict:
    """O que vai ao ar: a trilha das atrasadas só até `agora - ATRASO_S`."""
    aeronaves, fontes = [], set()
    for h, reg in sorted(av.items()):
        m = reg["m"]
        tempo_real = CATEGORIAS.get(m["c"], ("", False))[1]
        corte = agora - ATRASO_S if not tempo_real else agora
        pts = [p for p in reg["p"] if corte - JANELA_S <= p[0] <= corte]
        if not pts:
            continue
        fontes.update(p[7] for p in pts)
        item = {"h": h, "r": m.get("r", ""), "t": m.get("t", ""), "d": m.get("d", ""), "c": m["c"], "a": not tempo_real,
                "cs": m.get("cs", ""), "p": pts}
        # O nome do operador só de órgão público e de contratada; de particular, nunca.
        if m["c"] != "outro" and m.get("o"):
            item["o"] = m["o"]
        if m.get("k"):
            item["k"] = m["k"]
        aeronaves.append(item)
    consultado = estado.get("consultado")
    saida = {
        "gerado": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(agora)),
        "consultado": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(consultado)) if consultado else None,
        "fonte": NOMES_FONTE[estado["fonte"]] if estado.get("fonte") is not None else None,
        "fontes": NOMES_FONTE,
        "atrasoMin": ATRASO_S // 60,
        "janelaHoras": JANELA_S // 3600,
        "categorias": {c: {"nome": n, "tempoReal": tr} for c, (n, tr) in CATEGORIAS.items()},
        "attribution": {NOMES_FONTE[i]: CREDITOS[NOMES_FONTE[i]] for i in sorted(fontes)},
        "aeronaves": aeronaves,
    }
    os.makedirs(PASTA, exist_ok=True)
    escrever_json(ARQ_ATUAL, saida, compacto=True)
    return saida


# ---------------------------------------------------------------------------
# Arquivos
# ---------------------------------------------------------------------------


def escrever_json(caminho: str, dado, compacto: bool = False) -> None:
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    temp = f"{caminho}.parcial-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(dado, f, ensure_ascii=False, separators=(",", ":") if compacto else None)
    os.replace(temp, caminho)


def ler_json(caminho: str, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def main() -> int:
    if sys.argv[1:2] == ["frota"]:
        frota = refazer_frota()
        cont = {}
        for a in frota["publicas"].values():
            cont[a["c"]] = cont.get(a["c"], 0) + 1
        com_hex = sum(1 for a in frota["publicas"].values() if a.get("h"))
        print(f"{len(frota['publicas'])} aeronaves públicas no RAB ({com_hex} com endereço conhecido): {cont}")
        print(f"{len(frota['militares'])} militares do Brasil no banco aberto")
        return 0
    rodada()
    return 0


if __name__ == "__main__":
    sys.exit(main())
