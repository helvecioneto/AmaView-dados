#!/usr/bin/env python3
"""
Previsão de modelo para o AmaView: o tempo do ECMWF (IFS e AIFS, dados
abertos) e o aerossol de queimadas do CAMS (Copernicus), recortados na
Amazônia Legal.

- **IFS** (`data.ecmwf.int`, sem conta, CC BY 4.0): rodadas de 00 e 12 UTC,
  grade de 0,25°, passos de 3 h até +144 h e de 6 h até +360 h. Só os campos
  usados são baixados, por intervalo de bytes (o `.index` de cada passo diz
  onde cada campo está no GRIB global de ~140 MB).
- **AIFS** (o modelo de aprendizado de máquina do ECMWF, AIFS Single; mesmo
  servidor, mesma licença): rodadas de 00 e 12 UTC (as de 06 e 18 não são
  usadas, para andar junto com o IFS), grade de 0,25°, passos de 6 h até
  +360 h. Os mesmos campos do IFS menos a rajada (`10fg`), que o AIFS não tem.
  Baixado depois do IFS na mesma execução, nunca em paralelo a ele: o servidor
  aberto limita pedidos (429).
- **GFS** (NOAA, domínio público): rodadas de 00 e 12 UTC, 0,25°, passos de
  3 h até +384 h. O filtro do NOMADS já devolve só o recorte (~200 KB por
  passo); a prontidão é conferida na cópia da AWS (sem limite de pedidos). A
  chuva é a acumulada desde o início (o GFS publica também a de 3/6 h); as
  nuvens, as do instante (não a média do intervalo); a rajada (`GUST`) é a do
  instante.
- **ICON** (DWD, CC BY 4.0, "Fonte: Deutscher Wetterdienst"): rodadas de 00 e
  12 UTC, ~13 km em grade icosaédrica, passos de 3 h até +180 h. Um arquivo
  global por variável e passo (bz2, 2–4 MB): ~1,4 GB por rodada. Cada ponto
  da grade publicada (0,125°) recebe a célula do ICON mais próxima; a
  correspondência sai das coordenadas das células (CLAT/CLON) e fica guardada
  em `icon-grade.npz`. É o último da execução (o mais demorado).
- **CAMS** (Atmosphere Data Store, conta e licença; chave em
  `/etc/amaview/ads-key`): rodadas de 00 e 12 UTC, grade de 0,4°, passos de 3 h
  até +120 h. O pedido entra na fila do ADS; a rodada seguinte do timer vê se
  ficou pronto, sem ficar esperando.
- **Aerossol de queimadas** = AOD da matéria orgânica + AOD do carbono negro a
  550 nm (as espécies que o CAMS emite pelo fogo, via GFAS) — a mesma camada
  que o ECMWF desenha como "biomass burning" (`composition_bbaod550`).
- O GRIB é lido pela libeccodes (pacote `libeccodes0`, por ctypes): os campos
  do IFS vêm em compressão CCSDS, que não dá para ler sem ela.

Saída (abaixo de PREVISAO_RAIZ, servida pelo nginx em /previsao/vN/):

  previsao/v1/indice.json                          rodadas, passos, grade, variáveis e escalas
  previsao/v1/<modelo>/<AAAAMMDDHH>/mapa/<var>/<hhh>.bin   grade inteira de um passo
  previsao/v1/<modelo>/<AAAAMMDDHH>/serie/<bi>_<bj>.bin    bloco de 2°×2°: todos os passos e variáveis

Os .bin são uint16 little-endian; valor = offset + escala × q; 65535 = sem dado.
Grade: linha i → lat = LAT0 − i·passo; coluna j → lon = LON0 + j·passo. Bloco
(bi, bj) = linhas [bi·b, bi·b+b) e colunas [bj·b, bj·b+b), com b = 2°/passo;
dentro do bloco, a ordem é [variável][passo][linha][coluna]. Os dois modelos
têm os mesmos blocos (2° = 8 pontos do IFS = 5 do CAMS).

  previsao.py                 uma rodada (o que o timer chama)
  previsao.py ifs AAAAMMDDHH  força uma rodada do IFS (teste)
  previsao.py aifs AAAAMMDDHH força uma rodada do AIFS (teste)
  previsao.py gfs AAAAMMDDHH  força uma rodada do GFS (teste)
  previsao.py icon AAAAMMDDHH força uma rodada do ICON (teste)
  previsao.py cams AAAAMMDDHH força o pedido do CAMS (teste; espera o ADS)
"""
import bz2
import ctypes
import ctypes.util
import json
import math
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Sobe quando o formato dos .bin muda (cache imutável no navegador).
VERSAO = 1
RAIZ = os.environ.get("PREVISAO_RAIZ", "/var/cache/amaview-previsao")
PASTA = os.path.join(RAIZ, "previsao", f"v{VERSAO}")
ESTADO = os.path.join(RAIZ, "estado.json")
CHAVE_ADS = os.environ.get("PREVISAO_CHAVE_ADS", "/etc/amaview/ads-key")
AGENTE = "AmaView-previsao (+https://amaview.helvecio.uk/)"

# Recorte: a Amazônia Legal (5,3°N–18°S, 74°W–44°W) com folga, em múltiplos de
# 2° para os blocos fecharem exatos nas duas grades.
LAT0, LAT1 = 8.0, -20.0
LON0, LON1 = -76.0, -42.0
BLOCO_GRAUS = 2.0
SEM_DADO = 65535

# Quantas rodadas de cada modelo ficam no disco (a atual e as anteriores, para
# o app não perder o que está mostrando quando a nova entra).
RODADAS_GUARDADAS = 3
# Rodada que falhou: tentar de novo só depois disto.
ESPERA_FALHA_S = 1800

IFS_URL = "https://data.ecmwf.int/forecasts"
IFS_PASSOS = list(range(0, 145, 3)) + list(range(150, 361, 6))
AIFS_PASSOS = list(range(0, 361, 6))
GFS_PASSOS = list(range(0, 385, 3))
GFS_FILTRO = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
GFS_AWS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
# Pausa entre pedidos ao NOMADS (ele bloqueia quem passa de ~120 por minuto).
GFS_PAUSA_S = 0.5
ICON_URL = "https://opendata.dwd.de/weather/nwp/icon/grib"
ICON_PASSOS = list(range(0, 181, 3))
# Variável do DWD → shortName do ECMWF (o nome que o resto do código usa).
ICON_VARS = {"t_2m": "2t", "td_2m": "2d", "tot_prec": "tp", "u_10m": "10u", "v_10m": "10v", "vmax_10m": "10fg", "clct": "tcc"}
# Downloads simultâneos do servidor do DWD (~1 MB/s por conexão, medido da Oracle em 4/11/2026).
ICON_PARALELO = 4
# Os modelos do servidor aberto do ECMWF: caminho na árvore e passos.
ECMWF_ABERTO = {
    "ifs": ("ifs/0p25/oper", IFS_PASSOS),
    "aifs": ("aifs-single/0p25/oper", AIFS_PASSOS),
}
# Downloads simultâneos (intervalos de bytes) no servidor de dados abertos.
IFS_PARALELO = 2
# Esperas seguidas por um 429 antes de desistir do arquivo (e da rodada).
TENTATIVAS_429 = 5
ADS_URL = "https://ads.atmosphere.copernicus.eu/api/retrieve/v1"
ADS_CONJUNTO = "cams-global-atmospheric-composition-forecasts"
CAMS_PASSOS = list(range(0, 121, 3))
# O pedido parado na fila do ADS por mais que isto é abandonado e refeito.
CAMS_DESISTIR_S = 6 * 3600

# Variáveis publicadas: nome → (unidade, offset, escala, descrição).
VARS = {
    "ifs": {
        "t2m": ("°C", -60.0, 0.01, "temperatura a 2 m"),
        "ur": ("%", 0.0, 0.01, "umidade relativa a 2 m"),
        "chuva": ("mm", 0.0, 0.01, "precipitação no intervalo que termina no passo"),
        "u10": ("m/s", -100.0, 0.01, "vento a 10 m, componente leste"),
        "v10": ("m/s", -100.0, 0.01, "vento a 10 m, componente norte"),
        "rajada": ("m/s", 0.0, 0.01, "rajada máxima a 10 m no intervalo"),
        "nuvens": ("%", 0.0, 0.01, "cobertura total de nuvens"),
    },
    "cams": {
        "bbaod": ("", 0.0, 0.0001, "AOD de queimadas a 550 nm (matéria orgânica + carbono negro)"),
        "aod": ("", 0.0, 0.0001, "AOD total a 550 nm"),
        "pm25": ("µg/m³", 0.0, 0.1, "MP2,5 na superfície"),
    },
}
# O AIFS publica as mesmas variáveis do IFS, menos a rajada (não há `10fg`).
VARS["aifs"] = {k: v for k, v in VARS["ifs"].items() if k != "rajada"}
# GFS: as do IFS; a rajada é a do instante (o GFS não publica a máxima do intervalo).
VARS["gfs"] = {**VARS["ifs"], "rajada": ("m/s", 0.0, 0.01, "rajada a 10 m no instante do passo")}
# ICON: as do IFS; a rajada é a máxima da última saída do modelo (1 h até +78 h, 3 h depois).
VARS["icon"] = {**VARS["ifs"], "rajada": ("m/s", 0.0, 0.01, "rajada máxima a 10 m na última saída do modelo")}
# O ICON é publicado em 0,125° (o modelo tem ~13 km): 0,25° jogaria fora a resolução que é a razão de tê-lo.
PASSO_GRADE = {"ifs": 0.25, "aifs": 0.25, "gfs": 0.25, "icon": 0.125, "cams": 0.4}
# Campos do IFS que são baixados (shortName do ECMWF).
IFS_CAMPOS = ("2t", "2d", "tp", "10u", "10v", "10fg", "tcc")
CAMS_CAMPOS = {
    "organic_matter_aerosol_optical_depth_550nm": "omaod550",
    "black_carbon_aerosol_optical_depth_550nm": "bcaod550",
    "total_aerosol_optical_depth_550nm": "aod550",
    "particulate_matter_2.5um": "pm2p5",
}
# paramId do ECMWF, caso a libeccodes do sistema não conheça o shortName.
CAMS_PARAM = {210210: "omaod550", 210211: "bcaod550", 210207: "aod550", 210073: "pm2p5"}


def dimensoes(modelo: str) -> tuple[int, int, int]:
    """(linhas, colunas, pontos por bloco) da grade recortada do modelo."""
    p = PASSO_GRADE[modelo]
    return round((LAT0 - LAT1) / p), round((LON1 - LON0) / p), round(BLOCO_GRAUS / p)


# ---------------------------------------------------------------------------
# GRIB pela libeccodes (ctypes)


class Eccodes:
    def __init__(self):
        nome = ctypes.util.find_library("eccodes") or "libeccodes.so.0"
        lib = ctypes.CDLL(nome)
        lib.codes_handle_new_from_message.restype = ctypes.c_void_p
        lib.codes_handle_new_from_message.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.codes_handle_delete.argtypes = [ctypes.c_void_p]
        for f in ("codes_get_long", "codes_get_double", "codes_get_size", "codes_get_double_array", "codes_get_string"):
            getattr(lib, f).restype = ctypes.c_int
        self.lib = lib

    def ler(self, msg: bytes) -> dict:
        """Uma mensagem GRIB → {chaves, 'valores' (lat × lon, norte em cima), 'lats', 'lons'}."""
        import numpy as np

        lib = self.lib
        h = lib.codes_handle_new_from_message(None, msg, len(msg))
        if not h:
            raise ValueError("mensagem GRIB ilegível")
        h = ctypes.c_void_p(h)
        try:
            def longo(k):
                v = ctypes.c_long()
                if lib.codes_get_long(h, k.encode(), ctypes.byref(v)):
                    raise KeyError(k)
                return v.value

            def real(k):
                v = ctypes.c_double()
                if lib.codes_get_double(h, k.encode(), ctypes.byref(v)):
                    raise KeyError(k)
                return v.value

            def texto(k):
                buf = ctypes.create_string_buffer(256)
                n = ctypes.c_size_t(256)
                if lib.codes_get_string(h, k.encode(), buf, ctypes.byref(n)):
                    return None
                return buf.value.decode()

            n = ctypes.c_size_t()
            lib.codes_get_size(h, b"values", ctypes.byref(n))
            vals = np.empty(n.value, dtype=np.float64)
            m = ctypes.c_size_t(n.value)
            if lib.codes_get_double_array(h, b"values", vals.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), ctypes.byref(m)):
                raise ValueError("valores ilegíveis")
            ni, nj = longo("Ni"), longo("Nj")
            lat1, lon1 = real("latitudeOfFirstGridPointInDegrees"), real("longitudeOfFirstGridPointInDegrees")
            di, dj = real("iDirectionIncrementInDegrees"), real("jDirectionIncrementInDegrees")
            sobe = longo("jScansPositively") == 1
            perdido = real("missingValue") if longo("bitmapPresent") else None
            grade = vals.reshape(nj, ni)
            if perdido is not None:
                grade = np.where(grade == perdido, np.nan, grade)
            lats = lat1 + (np.arange(nj) * dj if sobe else -np.arange(nj) * dj)
            if sobe:
                grade, lats = grade[::-1], lats[::-1]
            lons = (lon1 + np.arange(ni) * di + 180.0) % 360.0 - 180.0
            try:
                param = longo("paramId")
            except KeyError:
                param = None
            try:
                passo = longo("endStep")
            except KeyError:
                passo = longo("step")
            try:
                inicio = longo("startStep")
            except KeyError:
                inicio = passo
            return {
                "nome": texto("shortName"),
                "unidade": texto("units"),
                "inicio": inicio,
                "param": param,
                "passo": passo,
                "data": longo("dataDate"),
                "hora": longo("dataTime"),
                "valores": grade,
                "lats": lats,
                "lons": lons,
            }
        finally:
            lib.codes_handle_delete(h)


def ler_bruto(ec: "Eccodes", msg: bytes) -> dict:
    """Uma mensagem de grade não regular (o ICON) → {nome, unidade, passo, valores (1-D)}."""
    import numpy as np

    lib = ec.lib
    h = lib.codes_handle_new_from_message(None, msg, len(msg))
    if not h:
        raise ValueError("mensagem GRIB ilegível")
    h = ctypes.c_void_p(h)
    try:
        def texto(k):
            buf = ctypes.create_string_buffer(256)
            n = ctypes.c_size_t(256)
            return None if lib.codes_get_string(h, k.encode(), buf, ctypes.byref(n)) else buf.value.decode()

        def longo(k):
            v = ctypes.c_long()
            if lib.codes_get_long(h, k.encode(), ctypes.byref(v)):
                raise KeyError(k)
            return v.value

        n = ctypes.c_size_t()
        lib.codes_get_size(h, b"values", ctypes.byref(n))
        vals = np.empty(n.value, dtype=np.float64)
        m = ctypes.c_size_t(n.value)
        if lib.codes_get_double_array(h, b"values", vals.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), ctypes.byref(m)):
            raise ValueError("valores ilegíveis")
        try:
            passo = longo("endStep")
        except KeyError:
            passo = None
        return {"nome": texto("shortName"), "unidade": texto("units"), "passo": passo, "valores": vals}
    finally:
        lib.codes_handle_delete(h)


def mensagens(dado: bytes):
    """Fatia um arquivo GRIB (1 ou 2, várias mensagens) em mensagens."""
    p = 0
    while True:
        p = dado.find(b"GRIB", p)
        if p < 0 or p + 16 > len(dado):
            return
        edicao = dado[p + 7]
        if edicao == 2:
            n = int.from_bytes(dado[p + 8 : p + 16], "big")
        elif edicao == 1:
            n = int.from_bytes(dado[p + 4 : p + 7], "big")
        else:
            p += 4
            continue
        if n <= 0 or p + n > len(dado):
            return
        yield dado[p : p + n]
        p += n


# Unidades que o IFS e o AIFS publicam para o mesmo campo (conferidas em 4/11/2026):
# a chuva do IFS vem em m e a do AIFS em kg/m² (= mm); as nuvens do IFS em fração
# (0–1) e as do AIFS em %. Tudo vai para a unidade do IFS antes da conversão; uma
# unidade desconhecida derruba a rodada (melhor que publicar chuva 1.000× maior).
UNIDADES = {
    "tp": {"m": 1.0, "kg m**-2": 0.001},
    "tcc": {"(0 - 1)": 1.0, "%": 0.01},
}


def normalizar(nome: str, unidade: str | None, valores):
    """Valores do campo na unidade do IFS (só `tp` e `tcc` diferem entre os modelos)."""
    fatores = UNIDADES.get(nome)
    if fatores is None:
        return valores
    if unidade not in fatores:
        raise ValueError(f"{nome} em unidade desconhecida: {unidade!r}")
    f = fatores[unidade]
    return valores if f == 1.0 else valores * f


def recortar(campo: dict, modelo: str):
    """Grade global (ou já recortada) → recorte do modelo (float32, NaN onde falta)."""
    import numpy as np

    passo = PASSO_GRADE[modelo]
    ni, nj, _ = dimensoes(modelo)
    lats_q = LAT0 - np.arange(ni) * passo
    lons_q = LON0 + np.arange(nj) * passo
    # Índice do ponto mais próximo; a grade do ECMWF cai exatamente nesses pontos.
    li = np.array([int(np.argmin(np.abs(campo["lats"] - la))) for la in lats_q])
    lj = np.array([int(np.argmin(np.abs(campo["lons"] - lo))) for lo in lons_q])
    if np.abs(campo["lats"][li] - lats_q).max() > passo / 4 or np.abs(campo["lons"][lj] - lons_q).max() > passo / 4:
        raise ValueError(f"grade do {modelo} não cobre o recorte")
    return campo["valores"][np.ix_(li, lj)].astype(np.float32)


# ---------------------------------------------------------------------------
# Conversões e codificação


def umidade_relativa(t_c, td_c):
    """Magnus (Alduchov e Eskridge), em %."""
    import numpy as np

    a, b = 17.625, 243.04
    return np.clip(100.0 * np.exp(a * td_c / (b + td_c) - a * t_c / (b + t_c)), 0.0, 100.0)


def quantizar(valores, offset: float, escala: float):
    import numpy as np

    q = np.round((np.asarray(valores, dtype=np.float64) - offset) / escala)
    q = np.where(np.isfinite(q), np.clip(q, 0, SEM_DADO - 1), SEM_DADO)
    return q.astype("<u2")


def converter_ifs(brutos: dict[int, dict], modelo: str = "ifs") -> dict:
    """{passo: {shortName: grade}} → {var: array [passo][lat][lon]} nas unidades publicadas (IFS ou AIFS)."""
    import numpy as np

    ni, nj, _ = dimensoes(modelo)
    vazio = np.full((ni, nj), np.nan, np.float32)
    passos = sorted(brutos)
    pega = lambda s, k: brutos[s].get(k, vazio)  # noqa: E731
    out = {v: np.empty((len(passos), ni, nj), np.float32) for v in VARS[modelo]}
    tp_ant = None
    for i, s in enumerate(passos):
        t = pega(s, "2t") - 273.15
        out["t2m"][i] = t
        out["ur"][i] = umidade_relativa(t, pega(s, "2d") - 273.15)
        tp = pega(s, "tp") * 1000.0  # m acumulados desde o início → mm
        # Chuva do intervalo: a diferença dos acumulados (sem negativo de arredondamento).
        out["chuva"][i] = vazio if tp_ant is None else np.maximum(tp - tp_ant, 0.0)
        tp_ant = tp
        out["u10"][i] = pega(s, "10u")
        out["v10"][i] = pega(s, "10v")
        if "rajada" in out:
            out["rajada"][i] = pega(s, "10fg")
        out["nuvens"][i] = pega(s, "tcc") * 100.0
    return out


def converter_cams(brutos: dict[int, dict]) -> dict:
    import numpy as np

    ni, nj, _ = dimensoes("cams")
    vazio = np.full((ni, nj), np.nan, np.float32)
    passos = sorted(brutos)
    pega = lambda s, k: brutos[s].get(k, vazio)  # noqa: E731
    out = {v: np.empty((len(passos), ni, nj), np.float32) for v in VARS["cams"]}
    for i, s in enumerate(passos):
        out["bbaod"][i] = pega(s, "omaod550") + pega(s, "bcaod550")
        out["aod"][i] = pega(s, "aod550")
        out["pm25"][i] = pega(s, "pm2p5") * 1e9  # kg/m³ → µg/m³
    return out


def escrever_atomico(caminho: str, dado: bytes) -> None:
    temp = f"{caminho}.parcial-{os.getpid()}"
    with open(temp, "wb") as f:
        f.write(dado)
    os.replace(temp, caminho)


def ler_json(caminho: str, padrao):
    try:
        with open(caminho) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def publicar(modelo: str, rodada: str, passos: list[int], dados: dict, pasta_modelo: str | None = None) -> dict:
    """Escreve a rodada (mapa e blocos) numa pasta temporária e troca de uma vez (`pasta_modelo`: outra que não a pública)."""
    import numpy as np

    ni, nj, b = dimensoes(modelo)
    final = os.path.join(pasta_modelo or os.path.join(PASTA, modelo), rodada)
    temp = f"{final}.parcial"
    shutil.rmtree(temp, ignore_errors=True)
    variaveis = list(VARS[modelo])
    q = {}
    for v in variaveis:
        _, off, esc, _ = VARS[modelo][v]
        q[v] = quantizar(dados[v], off, esc)
        pasta = os.path.join(temp, "mapa", v)
        os.makedirs(pasta)
        for k, s in enumerate(passos):
            with open(os.path.join(pasta, f"{s:03d}.bin"), "wb") as f:
                f.write(q[v][k].tobytes())
    pasta = os.path.join(temp, "serie")
    os.makedirs(pasta)
    tudo = np.stack([q[v] for v in variaveis])  # [var][passo][lat][lon]
    for bi in range(ni // b):
        for bj in range(nj // b):
            bloco = tudo[:, :, bi * b : (bi + 1) * b, bj * b : (bj + 1) * b]
            with open(os.path.join(pasta, f"{bi:02d}_{bj:02d}.bin"), "wb") as f:
                f.write(np.ascontiguousarray(bloco).tobytes())
    shutil.rmtree(final, ignore_errors=True)
    os.replace(temp, final)
    # Resumo para o log e para o índice: faixa de cada variável.
    faixa = {}
    for v in variaveis:
        x = dados[v][np.isfinite(dados[v])]
        faixa[v] = [round(float(x.min()), 3), round(float(x.max()), 3)] if x.size else None
    return faixa


def limpar(modelo: str, pasta_modelo: str | None = None) -> None:
    pasta = pasta_modelo or os.path.join(PASTA, modelo)
    if not os.path.isdir(pasta):
        return
    rodadas = sorted(n for n in os.listdir(pasta) if n.isdigit())
    for n in rodadas[:-RODADAS_GUARDADAS]:
        shutil.rmtree(os.path.join(pasta, n), ignore_errors=True)
    for n in os.listdir(pasta):
        if n.endswith(".parcial") and time.time() - os.path.getmtime(os.path.join(pasta, n)) > 3600:
            shutil.rmtree(os.path.join(pasta, n), ignore_errors=True)


# ---------------------------------------------------------------------------
# Rede


def espera_429(e: urllib.error.HTTPError, i: int) -> float:
    """Quanto esperar depois de um 429: o `Retry-After` do servidor (até 2 min) ou 15 s, 30 s, 60 s…"""
    try:
        return min(120.0, max(1.0, float(e.headers.get("Retry-After", ""))))
    except (TypeError, ValueError):
        return min(120.0, 15.0 * 2**i)


def baixar(url: str, cabecalhos: dict | None = None, dados: bytes | None = None, tentativas: int = 3, metodo: str | None = None) -> bytes:
    pedido = urllib.request.Request(url, data=dados, headers={"User-Agent": AGENTE, **(cabecalhos or {})}, method=metodo)
    # "Too Many Requests" (o servidor aberto do ECMWF limita; medido em 17/10/2026,
    # depois de ~1.800 pedidos em pouco tempo): espera e tenta de novo, mais vezes
    # que os erros do servidor.
    i = limite = 0
    while True:
        try:
            with urllib.request.urlopen(pedido, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and limite < TENTATIVAS_429:
                time.sleep(espera_429(e, limite))
                limite += 1
                continue
            if e.code < 500 or i == tentativas - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
        time.sleep(2 + 4 * i)
        i += 1


def rodadas_candidatas(agora: datetime, n: int = 4) -> list[datetime]:
    """As rodadas de 00 e 12 UTC mais recentes, da mais nova para a mais velha."""
    base = agora.replace(minute=0, second=0, microsecond=0, hour=12 if agora.hour >= 12 else 0)
    return [base - timedelta(hours=12 * k) for k in range(n)]


def nome_rodada(t: datetime) -> str:
    return t.strftime("%Y%m%d%H")


def instante_rodada(nome: str) -> datetime:
    return datetime.strptime(nome, "%Y%m%d%H").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# IFS (dados abertos do ECMWF)


def ifs_base(t: datetime, passo: int, modelo: str = "ifs") -> str:
    caminho = ECMWF_ABERTO[modelo][0]
    return f"{IFS_URL}/{t:%Y%m%d}/{t:%H}z/{caminho}/{t:%Y%m%d%H}0000-{passo}h-oper-fc"


def ifs_pronta(t: datetime, modelo: str = "ifs") -> bool:
    """A rodada só entra inteira: o último passo publicado é o sinal."""
    if modelo == "gfs":
        return gfs_pronta(t)
    if modelo == "icon":
        return icon_pronta(t)
    try:
        baixar(ifs_base(t, ECMWF_ABERTO[modelo][1][-1], modelo) + ".index", metodo="HEAD", tentativas=1)
        return True
    except urllib.error.HTTPError as e:
        # 404/403: ainda não publicada. Qualquer outra coisa (429 que não passou
        # com as esperas, erro do servidor) não diz nada sobre a rodada: sobe, e
        # a execução tenta de novo daqui a 20 min — sem pular para uma mais velha.
        if e.code in (403, 404):
            return False
        raise


def ifs_rodada(t: datetime, ec: Eccodes, modelo: str = "ifs") -> tuple[list[int], dict]:
    """Uma rodada do IFS ou do AIFS (dados abertos do ECMWF): só os campos usados, por intervalo de bytes."""
    from concurrent.futures import ThreadPoolExecutor

    if modelo == "gfs":
        return gfs_rodada(t, ec)
    if modelo == "icon":
        return icon_rodada(t, ec)
    passos = ECMWF_ABERTO[modelo][1]

    brutos: dict[int, dict] = {}
    baixados = 0
    t0 = time.time()
    # Cada pedido anda a ~0,3 MB/s (medido em 17/10/2026 a partir da Oracle):
    # em série, uma rodada levaria ~25 min. Os downloads vão em paralelo; a
    # leitura do GRIB fica numa linha só (a libeccodes não promete ser segura
    # entre threads).
    with ThreadPoolExecutor(max_workers=IFS_PARALELO) as pool:
        for s in passos:
            base = ifs_base(t, s, modelo)
            indice = [json.loads(l) for l in baixar(base + ".index").decode().splitlines() if l.strip()]
            campos = {}
            for e in indice:
                if e.get("levtype") == "sfc" and e.get("param") in IFS_CAMPOS and e["param"] not in campos:
                    campos[e["param"]] = (int(e["_offset"]), int(e["_length"]))
            # Um campo que some do índice não pode virar mapa vazio em silêncio: a rodada falha
            # (a rajada não existe no AIFS; no passo 0 do IFS também não há `10fg`).
            faltam = {c for c in IFS_CAMPOS if c != "10fg"} - set(campos)
            if faltam:
                raise ValueError(f"{modelo} +{s}h sem {sorted(faltam)} no índice")
            pedir = lambda ini_n: baixar(base + ".grib2", {"Range": f"bytes={ini_n[0]}-{sum(ini_n) - 1}"})  # noqa: E731
            msgs = dict(zip(campos, pool.map(pedir, campos.values())))
            brutos[s] = {}
            for nome, msg in msgs.items():
                baixados += len(msg)
                c = ec.ler(msg)
                brutos[s][nome] = normalizar(nome, c.get("unidade"), recortar(c, modelo))
    print(f"{modelo} {nome_rodada(t)}: {len(brutos)} passos, {baixados / 1e6:.0f} MB baixados em {(time.time() - t0) / 60:.1f} min")
    return passos, converter_ifs(brutos, modelo)


def faltando_404(f, *a, **k) -> bool:
    """Pergunta de prontidão: True se respondeu, False num 403/404 (ainda não publicado); outro erro sobe."""
    try:
        f(*a, **k)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return False
        raise


# ---------------------------------------------------------------------------
# GFS (NOAA)


def gfs_url(t: datetime, passo: int) -> str:
    """O filtro do NOMADS: só as variáveis e os níveis usados, só o recorte."""
    q = "&".join(
        [
            f"dir=%2Fgfs.{t:%Y%m%d}%2F{t:%H}%2Fatmos",
            f"file=gfs.t{t:%H}z.pgrb2.0p25.f{passo:03d}",
            *(f"var_{v}=on" for v in ("TMP", "DPT", "APCP", "UGRD", "VGRD", "GUST", "TCDC")),
            *(f"lev_{n}=on" for n in ("2_m_above_ground", "10_m_above_ground", "surface", "entire_atmosphere")),
            "subregion=",
            f"toplat={LAT0 + 0.5:g}&leftlon={LON0 - 0.5:g}&rightlon={LON1 + 0.5:g}&bottomlat={LAT1 - 0.5:g}",
        ]
    )
    return f"{GFS_FILTRO}?{q}"


def gfs_pronta(t: datetime) -> bool:
    url = f"{GFS_AWS}/gfs.{t:%Y%m%d}/{t:%H}/atmos/gfs.t{t:%H}z.pgrb2.0p25.f{GFS_PASSOS[-1]:03d}.idx"
    return faltando_404(baixar, url, metodo="HEAD", tentativas=1)


def gfs_campos(dado: bytes, ec: "Eccodes", passo: int) -> dict:
    """As mensagens de um passo do filtro → {shortName do ECMWF: recorte}, nas unidades do IFS."""
    import numpy as np

    campos: dict = {}
    for msg in mensagens(dado):
        c = ec.ler(msg)
        nome = "10fg" if c["nome"] in ("gust", "i10fg") else c["nome"]
        if nome not in IFS_CAMPOS or nome in campos:
            continue
        # A chuva acumulada desde o início (a de 3/6 h também vem); as nuvens do instante (não a média).
        if nome == "tp" and c["inicio"] != 0:
            continue
        if nome == "tcc" and c["inicio"] != c["passo"]:
            continue
        campos[nome] = normalizar(nome, c.get("unidade"), recortar(c, "gfs"))
    if passo == 0 and "tp" not in campos:
        ni, nj, _ = dimensoes("gfs")
        campos["tp"] = np.zeros((ni, nj), np.float32)
    faltam = set(IFS_CAMPOS) - set(campos)
    if faltam:
        raise ValueError(f"gfs +{passo}h sem {sorted(faltam)}")
    return campos


def gfs_rodada(t: datetime, ec: "Eccodes") -> tuple[list[int], dict]:
    brutos: dict[int, dict] = {}
    baixados = 0
    t0 = time.time()
    for s in GFS_PASSOS:
        dado = baixar(gfs_url(t, s))
        baixados += len(dado)
        brutos[s] = gfs_campos(dado, ec, s)
        time.sleep(GFS_PAUSA_S)
    print(f"gfs {nome_rodada(t)}: {len(brutos)} passos, {baixados / 1e6:.0f} MB baixados em {(time.time() - t0) / 60:.1f} min")
    return GFS_PASSOS, converter_ifs(brutos, "gfs")


# ---------------------------------------------------------------------------
# ICON (DWD)

ICON_GRADE = os.path.join(RAIZ, "icon-grade.npz")


def icon_url(t: datetime, var: str, passo: int) -> str:
    return f"{ICON_URL}/{t:%H}/{var}/icon_global_icosahedral_single-level_{t:%Y%m%d%H}_{passo:03d}_{var.upper()}.grib2.bz2"


def icon_pronta(t: datetime) -> bool:
    """O último passo de cada variável publicado (o DWD sobe uma variável por vez)."""
    return all(faltando_404(baixar, icon_url(t, v, ICON_PASSOS[-1]), metodo="HEAD", tentativas=1) for v in ICON_VARS)


def mais_proximos(clat, clon, lats_q, lons_q):
    """Para cada ponto (lats_q × lons_q), o índice da célula mais próxima (graus; distância com cos(lat))."""
    import numpy as np

    folga = 1.0
    dentro = np.nonzero((clat <= lats_q.max() + folga) & (clat >= lats_q.min() - folga) & (clon >= lons_q.min() - folga) & (clon <= lons_q.max() + folga))[0]
    la, lo = clat[dentro], clon[dentro]
    caixa = 0.25
    bi = np.floor(la / caixa).astype(np.int64)
    bj = np.floor(lo / caixa).astype(np.int64)
    baldes: dict = {}
    for k, chave in enumerate(zip(bi.tolist(), bj.tolist())):
        baldes.setdefault(chave, []).append(k)
    baldes = {k: np.array(v) for k, v in baldes.items()}
    out = np.empty((lats_q.size, lons_q.size), np.int64)
    for i, lat in enumerate(lats_q):
        c = math.cos(math.radians(lat))
        b0 = math.floor(lat / caixa)
        for j, lon in enumerate(lons_q):
            b1 = math.floor(lon / caixa)
            cand = [baldes[(b0 + di, b1 + dj)] for di in (-1, 0, 1) for dj in (-1, 0, 1) if (b0 + di, b1 + dj) in baldes]
            if not cand:
                raise ValueError(f"sem célula do ICON perto de {lat}, {lon}")
            cand = np.concatenate(cand)
            d = (la[cand] - lat) ** 2 + ((lo[cand] - lon) * c) ** 2
            out[i, j] = dentro[cand[int(np.argmin(d))]]
    return out


def icon_indices(t: datetime, ec: "Eccodes", ncelulas: int):
    """A correspondência ponto publicado → célula do ICON (calculada uma vez; refeita se a grade mudar)."""
    import numpy as np

    ni, nj, _ = dimensoes("icon")
    try:
        g = np.load(ICON_GRADE)
        if int(g["ncelulas"]) == ncelulas and g["idx"].shape == (ni, nj):
            return g["idx"]
    except (FileNotFoundError, KeyError, ValueError, OSError):
        pass
    coords = {}
    for v in ("clat", "clon"):
        url = f"{ICON_URL}/{t:%H}/{v}/icon_global_icosahedral_time-invariant_{t:%Y%m%d%H}_{v.upper()}.grib2.bz2"
        c = ler_bruto(ec, bz2.decompress(baixar(url)))
        coords[v] = c["valores"]
    if coords["clat"].size != ncelulas:
        raise ValueError(f"CLAT com {coords['clat'].size} células, os campos com {ncelulas}")
    clon = (coords["clon"] + 180.0) % 360.0 - 180.0
    lats_q = LAT0 - np.arange(ni) * PASSO_GRADE["icon"]
    lons_q = LON0 + np.arange(nj) * PASSO_GRADE["icon"]
    idx = mais_proximos(coords["clat"], clon, lats_q, lons_q)
    temp = f"{ICON_GRADE}.parcial-{os.getpid()}.npz"
    np.savez(temp, idx=idx, ncelulas=ncelulas)
    os.replace(temp, ICON_GRADE)
    print(f"icon: correspondência de {ni}×{nj} pontos calculada ({ncelulas} células)")
    return idx


def icon_rodada(t: datetime, ec: "Eccodes") -> tuple[list[int], dict]:
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np

    pedidos = [(s, v) for s in ICON_PASSOS for v in ICON_VARS if not (v == "vmax_10m" and s == 0)]
    brutos: dict[int, dict] = {s: {} for s in ICON_PASSOS}
    idx = None
    baixados = 0
    t0 = time.time()
    # Em lotes: os arquivos baixados não se acumulam na memória (MemoryMax do serviço).
    with ThreadPoolExecutor(max_workers=ICON_PARALELO) as pool:
        for k in range(0, len(pedidos), 4 * ICON_PARALELO):
            lote = pedidos[k : k + 4 * ICON_PARALELO]
            for (s, v), dado in zip(lote, pool.map(lambda sv: baixar(icon_url(t, sv[1], sv[0])), lote)):
                baixados += len(dado)
                c = ler_bruto(ec, bz2.decompress(dado))
                if idx is None:
                    idx = icon_indices(t, ec, c["valores"].size)
                nome = ICON_VARS[v]
                brutos[s][nome] = normalizar(nome, c["unidade"], c["valores"][idx].astype(np.float32))
    print(f"icon {nome_rodada(t)}: {len(ICON_PASSOS)} passos, {baixados / 1e6:.0f} MB baixados em {(time.time() - t0) / 60:.1f} min")
    return ICON_PASSOS, converter_ifs(brutos, "icon")


# ---------------------------------------------------------------------------
# CAMS (Atmosphere Data Store)


def chave_ads() -> str | None:
    try:
        with open(CHAVE_ADS) as f:
            for linha in f:
                if linha.startswith("key:") and linha.split(":", 1)[1].strip():
                    return linha.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError):
        pass
    return None


def ads(caminho: str, chave: str, corpo: dict | None = None) -> dict:
    cab = {"PRIVATE-TOKEN": chave, "Content-Type": "application/json"}
    dado = json.dumps(corpo).encode() if corpo is not None else None
    return json.loads(baixar(f"{ADS_URL}{caminho}", cab, dado, metodo="POST" if dado else "GET"))


def cams_pedido(t: datetime) -> dict:
    return {
        "inputs": {
            "variable": list(CAMS_CAMPOS),
            "date": [f"{t:%Y-%m-%d}/{t:%Y-%m-%d}"],
            "time": [f"{t:%H}:00"],
            "leadtime_hour": [str(s) for s in CAMS_PASSOS],
            "type": ["forecast"],
            "data_format": "grib",
            # Norte, oeste, sul, leste — com meia célula de folga.
            "area": [LAT0 + 0.2, LON0 - 0.2, LAT1 - 0.2, LON1 + 0.2],
        }
    }


def cams_ler(dado: bytes, ec: Eccodes) -> tuple[list[int], dict]:
    brutos: dict[int, dict] = {s: {} for s in CAMS_PASSOS}
    desconhecidos = set()
    for msg in mensagens(dado):
        c = ec.ler(msg)
        nome = c["nome"] if c["nome"] in CAMS_PARAM.values() else CAMS_PARAM.get(c["param"])
        if nome is None:
            desconhecidos.add((c["nome"], c["param"]))
            continue
        if c["passo"] in brutos:
            brutos[c["passo"]][nome] = recortar(c, "cams")
    if desconhecidos:
        print(f"cams: campos ignorados {sorted(desconhecidos)}", file=sys.stderr)
    if not any(brutos.values()):
        raise ValueError("o GRIB do CAMS veio sem os campos pedidos")
    faltam = sorted({c for s in CAMS_PASSOS for c in CAMS_CAMPOS.values() if c not in brutos[s]})
    if faltam:
        print(f"cams: faltam {faltam} em algum passo (ficam sem dado)", file=sys.stderr)
    return CAMS_PASSOS, converter_cams(brutos)


def cams_andar(estado: dict, agora: datetime, ec: Eccodes, forcar: str | None = None) -> str:
    """Um passo da máquina de estados do CAMS: pedir, conferir a fila, baixar."""
    chave = chave_ads()
    if not chave:
        estado["erro"] = "sem chave do ADS em " + CHAVE_ADS
        return "sem chave"
    feitas = set(estado.get("feitas", []))
    pedido = estado.get("pedido")
    if pedido and time.time() - pedido["desde"] > CAMS_DESISTIR_S:
        print(f"cams {pedido['rodada']}: {CAMS_DESISTIR_S // 3600} h na fila do ADS — refazendo", file=sys.stderr)
        pedido = None
    if not pedido:
        falhas = estado.setdefault("falhas", {})
        if forcar:
            alvos = [forcar]
        else:
            # Da mais nova para a mais velha, as ainda não feitas que não falharam
            # há pouco; nunca volta para trás da última feita.
            ultima = max(feitas) if feitas else ""
            alvos = [n for n in map(nome_rodada, rodadas_candidatas(agora, 2)) if n > ultima and time.time() - falhas.get(n, 0) > ESPERA_FALHA_S]
        r = None
        for alvo in alvos:
            try:
                r = ads(f"/processes/{ADS_CONJUNTO}/execution", chave, cams_pedido(instante_rodada(alvo)))
                break
            except urllib.error.HTTPError as e:
                # 400: a rodada ainda não está no ADS (a de 12 UTC só entra à noite) —
                # marca e pede a anterior nesta mesma execução.
                if e.code not in (400, 404):
                    raise
                falhas[alvo] = time.time()
                estado["erro"] = f"{alvo}: {e.code} {e.read().decode(errors='replace')[:200]}"
                print(f"cams {alvo}: recusada pelo ADS ({e.code}) — tentando a anterior", file=sys.stderr)
        if r is None:
            return "em dia" if not alvos else "nenhuma rodada disponível no ADS"
        pedido = {"rodada": alvo, "job": r["jobID"], "desde": time.time()}
        estado["pedido"] = pedido
        print(f"cams {alvo}: pedido {r['jobID']} ({r.get('status')})")
    s = ads(f"/jobs/{pedido['job']}", chave)
    status = s.get("status")
    if status in ("accepted", "running"):
        return f"{pedido['rodada']} na fila ({status}, {(time.time() - pedido['desde']) / 60:.0f} min)"
    estado.pop("pedido", None)
    if status != "successful":
        detalhe = ""
        try:
            ads(f"/jobs/{pedido['job']}/results", chave)
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode(errors="replace")[:300]
        estado.setdefault("falhas", {})[pedido["rodada"]] = time.time()
        estado["erro"] = f"{pedido['rodada']}: {status} {detalhe}".strip()
        return f"{pedido['rodada']} falhou ({status})"
    res = ads(f"/jobs/{pedido['job']}/results", chave)
    dado = baixar(res["asset"]["value"]["href"])
    passos, dados = cams_ler(dado, ec)
    faixa = publicar("cams", pedido["rodada"], passos, dados)
    estado["feitas"] = sorted(feitas | {pedido["rodada"]})[-RODADAS_GUARDADAS:]
    estado.setdefault("faixas", {})[pedido["rodada"]] = faixa
    estado["erro"] = None
    print(f"cams {pedido['rodada']}: {len(dado) / 1e6:.1f} MB, {(time.time() - pedido['desde']) / 60:.0f} min de fila · bbaod {faixa['bbaod']}")
    return f"{pedido['rodada']} publicada"


def ifs_andar(estado: dict, agora: datetime, ec: Eccodes, forcar: str | None = None, modelo: str = "ifs") -> str:
    """Um passo do IFS ou do AIFS: a rodada mais nova pronta que ainda não foi feita."""
    feitas = set(estado.get("feitas", []))
    falhas = estado.get("falhas", {})
    ultima = max(feitas) if feitas else ""
    if forcar:
        alvo = instante_rodada(forcar)
    else:
        alvo = next(
            (
                t
                for t in rodadas_candidatas(agora, 3)
                if nome_rodada(t) > ultima and time.time() - falhas.get(nome_rodada(t), 0) > ESPERA_FALHA_S and ifs_pronta(t, modelo)
            ),
            None,
        )
    if alvo is None:
        return "em dia"
    n = nome_rodada(alvo)
    try:
        passos, dados = ifs_rodada(alvo, ec, modelo)
        faixa = publicar(modelo, n, passos, dados)
    except Exception as e:  # noqa: BLE001
        falhas[n] = time.time()
        estado["falhas"] = {k: v for k, v in falhas.items() if time.time() - v < 86400}
        estado["erro"] = f"{n}: {e}"
        raise
    estado["feitas"] = sorted(feitas | {n})[-RODADAS_GUARDADAS:]
    estado.setdefault("faixas", {})[n] = faixa
    estado["erro"] = None
    print(f"{modelo} {n}: publicada · t2m {faixa['t2m']} · chuva {faixa['chuva']}")
    return f"{n} publicada"


# ---------------------------------------------------------------------------
# Índice


def montar_indice(estado: dict, agora: datetime) -> dict:
    """Só o que está inteiro no disco entra (a fonte da verdade são as pastas)."""
    modelos = {}
    fontes = {
        "ifs": ("ECMWF IFS (dados abertos), CC BY 4.0", IFS_PASSOS),
        "aifs": ("ECMWF AIFS Single, modelo de aprendizado de máquina (dados abertos), CC BY 4.0", AIFS_PASSOS),
        "gfs": ("NOAA GFS (domínio público)", GFS_PASSOS),
        "icon": ("ICON global — Fonte: Deutscher Wetterdienst (DWD), CC BY 4.0", ICON_PASSOS),
        "cams": ("CAMS global (Copernicus/ECMWF) — Contains modified Copernicus Atmosphere Monitoring Service information", CAMS_PASSOS),
    }
    for modelo, (fonte, passos) in fontes.items():
        pasta = os.path.join(PASTA, modelo)
        rodadas = sorted((n for n in os.listdir(pasta) if n.isdigit()), reverse=True) if os.path.isdir(pasta) else []
        ni, nj, b = dimensoes(modelo)
        st = estado.get(modelo, {})
        modelos[modelo] = {
            "fonte": fonte,
            "rodadas": rodadas,
            "passos": passos,
            "grade": {"lat0": LAT0, "lon0": LON0, "passo": PASSO_GRADE[modelo], "ni": ni, "nj": nj, "bloco": b},
            "vars": {v: {"u": u, "off": off, "esc": esc, "desc": d} for v, (u, off, esc, d) in VARS[modelo].items()},
            "faixas": {r: st.get("faixas", {}).get(r) for r in rodadas},
            "erro": st.get("erro"),
            "fila": st.get("pedido", {}).get("rodada") if st.get("pedido") else None,
        }
    return {"versao": VERSAO, "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"), "semDado": SEM_DADO, "modelos": modelos}


def rodada(agora: datetime | None = None, so: str | None = None, forcar: str | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    os.makedirs(PASTA, exist_ok=True)
    estado = ler_json(ESTADO, {})
    ec = Eccodes()
    resultado, falhou = {}, False
    # Em série: o AIFS só depois do IFS (o mesmo servidor aberto, que limita pedidos).
    # O ICON por último: é o mais demorado (~1,4 GB por rodada) e não deve atrasar os outros.
    def de(m):
        return lambda st, ag, e, f: ifs_andar(st, ag, e, f, m)

    for modelo, andar in (("ifs", ifs_andar), ("aifs", de("aifs")), ("gfs", de("gfs")), ("cams", cams_andar), ("icon", de("icon"))):
        if so and so != modelo:
            continue
        st = estado.setdefault(modelo, {})
        try:
            resultado[modelo] = andar(st, agora, ec, forcar)
        except Exception as e:  # noqa: BLE001 — um modelo falhando não para o outro
            falhou = True
            st["erro"] = st.get("erro") or str(e)
            resultado[modelo] = f"falhou — {e}"
            print(f"{modelo}: falhou — {e}", file=sys.stderr)
        # Guarda o progresso de cada modelo antes de seguir para o próximo.
        st["faixas"] = {k: v for k, v in st.get("faixas", {}).items() if k in st.get("feitas", [])}
        escrever_atomico(ESTADO, json.dumps(estado).encode())
        limpar(modelo)
    escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(montar_indice(estado, agora), separators=(",", ":"), ensure_ascii=False).encode())
    resultado["_falhou"] = falhou
    return resultado


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] in ("ifs", "aifs", "gfs", "icon", "cams"):
        modelo, alvo = sys.argv[1], sys.argv[2]
        if modelo == "cams":
            # Teste: pede e espera aqui mesmo, em vez de deixar para o próximo timer.
            agora = datetime.now(timezone.utc)
            r = rodada(agora, "cams", alvo)
            while "na fila" in r.get("cams", ""):
                print(r["cams"])
                time.sleep(30)
                r = rodada(agora, "cams")
        else:
            r = rodada(None, modelo, alvo)
        print(r)
        return
    r = rodada()
    print("rodada: " + " · ".join(f"{k} {v}" for k, v in r.items() if not k.startswith("_")))
    sys.exit(1 if r["_falhou"] else 0)


if __name__ == "__main__":
    main()
