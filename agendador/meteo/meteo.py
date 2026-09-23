#!/usr/bin/env python3
"""
Estações meteorológicas para o AmaView: as automáticas e convencionais do
INMET (pelo nó brasileiro do WIS2 da OMM) e os METAR dos aeródromos (NOAA
Aviation Weather Center), nas últimas 48 h, na Amazônia Legal.

- **INMET via WIS2.** O nó oficial (`wis2bra.inmet.gov.br`, OGC API Features)
  entrega cada observação já decodificada do BUFR, uma feature por variável
  por estação, sem token. O navegador não pode ler direto: a resposta vem com
  `Access-Control-Allow-Origin` duplicado e o CORS quebra. Uma hora HH chega aos
  poucos, até ~HH+60 min: a contagem (`limit=1`, `numberMatched`) diz se
  chegou coisa nova, e só então a hora inteira é baixada de novo.
- **METAR.** O arquivo `metars.cache.csv.gz` da AWC (o mundo inteiro, ~256 KB,
  atualizado a cada minuto) traz só a observação MAIS RECENTE de cada
  aeródromo: a série de 48 h é acumulada aqui. O preenchimento inicial (e o de
  buracos, de hora em hora) usa a API da AWC, em lotes pequenos porque ela
  corta a resposta em 400 observações.
- **Recorte.** Estações dentro da Amazônia Legal (IBGE) ou a até 100 km da
  divisa: a faixa dá contexto na borda do mapa (Bolívia, Peru, Goiás) sem
  trazer o Brasil inteiro.
- **Nada é inventado.** Hora sem observação fica `null`; a chuva só entra
  quando o período é de exatamente 1 h (as convencionais mandam 12 e 24 h, que
  não se somam à série horária).

Saída (abaixo de METEO_RAIZ, servida pelo nginx em /meteo/v1/):

  meteo/v1/estacoes.json   cadastro + última leitura de cada estação
  meteo/v1/serie.json      48 h: grade horária do INMET e instantes próprios do METAR

  meteo.py            uma rodada (o que o timer chama)
"""
import csv
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("METEO_RAIZ", "/var/cache/amaview-meteo")
PASTA = os.path.join(RAIZ, "meteo", f"v{VERSAO}")
HORAS = os.path.join(RAIZ, "horas")
ESTADO = os.path.join(RAIZ, "estado.json")
METAR_ESTADO = os.path.join(RAIZ, "metar.json")
CADASTRO = os.path.join(RAIZ, "cadastro.json")
AGENTE = "AmaView-meteo (+https://helvecioneto.github.io/AmaView/)"
# O cadastro do INMET recusa quem não parece navegador.
AGENTE_NAVEGADOR = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

WIS2 = "https://wis2bra.inmet.gov.br/oapi/collections/urn:wmo:md:br-inmet:synop/items"
CADASTRO_URLS = ("https://apitempo.inmet.gov.br/estacoes/T", "https://apitempo.inmet.gov.br/estacoes/M")
AWC_CACHE = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
AWC_API = "https://aviationweather.gov/api/data/metar"

# Caixa que contém a Amazônia Legal com folga (a faixa de 100 km cabe nela).
BBOX = (-75.0, -19.5, -42.5, 6.0)
POLIGONO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amazonia_legal.json")
FAIXA_KM = 100.0

JANELA_H = 48
# Horas recentes: a contagem é conferida em toda rodada (a hora chega aos poucos).
RECENTES_H = 4
# Até 12 h: conferida de hora em hora (estação atrasada ainda pode chegar).
CONFERIR_ATE_H = 12
CONFERIR_S = 3600
# Hora que falhou: tentar de novo depois disto.
ESPERA_FALHA_S = 900
# Por rodada, no máximo: o preenchimento inicial das 48 h cabe em uma só.
MAX_DOWNLOADS = 60
# Preenchimento dos METAR pela API: de hora em hora (ou na primeira rodada).
METAR_API_S = 3600
METAR_LOTE = 6
CADASTRO_S = 24 * 3600

# Nome da variável no WIS2 → chave curta publicada.
VARIAVEIS = {
    "air_temperature": "t",
    "dewpoint_temperature": "td",
    "relative_humidity": "ur",
    "wind_speed": "vv",
    "wind_direction": "dv",
    "maximum_wind_gust_speed": "raj",
    "pressure_reduced_to_mean_sea_level": "p",
    "non_coordinate_pressure": "ps",
    "total_precipitation_or_total_water_equivalent": "chuva",
}
CHAVES_INMET = ("t", "td", "ur", "vv", "dv", "raj", "p", "ps", "chuva")
CHAVES_METAR = ("t", "td", "ur", "vv", "dv", "raj", "p", "vis")

KT_MS = 0.514444
MI_M = 1609.344


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def hora_cheia(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def nome_hora(t: datetime) -> str:
    return t.strftime("%Y%m%d%H")


# ---------------------------------------------------------------------------
# Recorte: Amazônia Legal + faixa de 100 km


def carregar_aneis(caminho: str = POLIGONO) -> list[list[tuple[float, float]]]:
    with open(caminho) as f:
        return [[(p[0], p[1]) for p in anel] for anel in json.load(f)["aneis"]]


def dentro(lon: float, lat: float, aneis) -> bool:
    """Par-ímpar sobre todos os anéis (buracos inclusos)."""
    d = False
    for anel in aneis:
        for (x1, y1), (x2, y2) in zip(anel, anel[1:]):
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                d = not d
    return d


def distancia_km(lon: float, lat: float, aneis) -> float:
    """Distância até a borda mais próxima (plano local; erro desprezível em 100 km)."""
    kx = 111.32 * math.cos(math.radians(lat))
    ky = 110.57
    melhor = float("inf")
    for anel in aneis:
        for (x1, y1), (x2, y2) in zip(anel, anel[1:]):
            ax, ay = (x1 - lon) * kx, (y1 - lat) * ky
            bx, by = (x2 - lon) * kx, (y2 - lat) * ky
            dx, dy = bx - ax, by - ay
            L = dx * dx + dy * dy
            u = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
            px, py = ax + u * dx, ay + u * dy
            melhor = min(melhor, px * px + py * py)
    return math.sqrt(melhor)


_REGIAO: dict[tuple, bool] = {}


def na_regiao(lon, lat, aneis, faixa_km: float = FAIXA_KM) -> bool:
    """Dentro da Amazônia Legal ou a até `faixa_km` da divisa (memorizado: as estações não andam)."""
    if not (isinstance(lon, (int, float)) and isinstance(lat, (int, float))):
        return False
    if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
        return False
    k = (round(lon, 3), round(lat, 3), id(aneis), faixa_km)
    if k not in _REGIAO:
        _REGIAO[k] = dentro(lon, lat, aneis) or distancia_km(lon, lat, aneis) <= faixa_km
    return _REGIAO[k]


# ---------------------------------------------------------------------------
# Rede


def baixar(url: str, tentativas: int = 3, agente: str = AGENTE, timeout: int = 60) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": agente, "Accept-Encoding": "gzip"})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(pedido, timeout=timeout) as r:
                corpo = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    corpo = gzip.decompress(corpo)
                return corpo
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
            time.sleep(1 + 2 * i)
    raise RuntimeError("inalcançável")


def url_wis2(hora: datetime, limite: int, propriedades: str) -> str:
    q = {
        "f": "json",
        "limit": str(limite),
        "bbox": ",".join(str(v) for v in BBOX),
        "datetime": iso(hora),
        "properties": propriedades,
    }
    return f"{WIS2}?{urllib.parse.urlencode(q, safe=',:')}"


def contar_hora(hora: datetime) -> int:
    d = json.loads(baixar(url_wis2(hora, 1, "name")))
    return int(d.get("numberMatched") or 0)


# ---------------------------------------------------------------------------
# INMET (WIS2): uma hora


def um_decimal(v) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        return None
    return round(float(v), 1)


def periodo_h(fenomeno: str | None) -> float | None:
    """'AAAA-MM-DDTHH:MM:SSZ/AAAA-…' → horas do intervalo; instante (sem '/') → None."""
    if not fenomeno or "/" not in fenomeno:
        return None
    try:
        a, b = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in fenomeno.split("/", 1))
    except ValueError:
        return None
    return (b - a).total_seconds() / 3600


def valido(chave: str, v: float) -> bool:
    """Faixas físicas: fora delas é defeito de sensor, não tempo."""
    faixas = {
        "t": (-10, 50), "td": (-30, 40), "ur": (1, 100), "vv": (0, 75), "dv": (0, 360),
        "raj": (0, 90), "p": (900, 1060), "ps": (700, 1060), "chuva": (0, 250), "vis": (0, 100000),
    }
    lo, hi = faixas.get(chave, (-math.inf, math.inf))
    return lo <= v <= hi


def ler_hora(cru: dict, hora: datetime, aneis) -> tuple[dict, dict]:
    """
    Features de uma hora → ({wigos: {chave: valor}}, {wigos: [lon, lat, alt]}).

    Só as observações do próprio horário (`reportTime`) e só as estações da
    região. A chuva entra apenas com período de 1 h.
    """
    alvo = iso(hora)
    obs: dict[str, dict] = {}
    pos: dict[str, list] = {}
    regiao: dict[str, bool] = {}
    for f in cru.get("features", []):
        p = f.get("properties") or {}
        w = p.get("wigos_station_identifier")
        chave = VARIAVEIS.get(p.get("name"))
        if not w or not chave or p.get("reportTime") != alvo:
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if w not in regiao:
            regiao[w] = len(coords) >= 2 and na_regiao(coords[0], coords[1], aneis)
            if regiao[w]:
                alt = coords[2] if len(coords) > 2 and isinstance(coords[2], (int, float)) else None
                pos[w] = [round(coords[0], 4), round(coords[1], 4), None if alt is None else round(alt, 1)]
        if not regiao[w]:
            continue
        if chave == "chuva":
            h = periodo_h(p.get("phenomenonTime"))
            if h is None or abs(h - 1) > 0.01:
                continue
        v = um_decimal(p.get("value"))
        if v is None or not valido(chave, v):
            continue
        obs.setdefault(w, {})[chave] = v
    return obs, pos


def baixar_hora(hora: datetime, aneis) -> tuple[dict, dict, int]:
    cru = json.loads(baixar(url_wis2(hora, 20000, "name,value,reportTime,phenomenonTime,wigos_station_identifier"), timeout=120))
    obs, pos = ler_hora(cru, hora, aneis)
    return obs, pos, int(cru.get("numberMatched") or len(cru.get("features", [])))


# ---------------------------------------------------------------------------
# Cadastro do INMET (nome, código, UF)


def ler_cadastro(listas: list) -> dict:
    """
    WIGOS → {c, n, uf}. As automáticas casam pelo `CD_WSI` (0-76-0-…); as
    convencionais aparecem no WIS2 pelo índice da OMM (0-20000-0-82332), que é
    o `CD_ESTACAO` delas.
    """
    out = {}
    for lista in listas:
        for e in lista or []:
            if not isinstance(e, dict):
                continue
            nome = (e.get("DC_NOME") or "").strip()
            info = {"c": (e.get("CD_ESTACAO") or "").strip(), "n": nome_legivel(nome), "uf": (e.get("SG_ESTADO") or "").strip()}
            if e.get("TP_ESTACAO") == "Convencional" and info["c"].isdigit():
                out[f"0-20000-0-{info['c']}"] = {**info, "conv": True}
            if e.get("CD_WSI"):
                out[e["CD_WSI"].strip()] = info
    return out


def nome_legivel(nome: str) -> str:
    """'BOA VISTA (AERO)' → 'Boa Vista (Aero)'; nome já em caixa mista fica como está."""
    nome = " ".join(nome.split())
    if not nome.isupper():
        return nome
    minusculas = {"de", "da", "do", "das", "dos", "e"}
    partes = nome.lower().split(" ")
    return " ".join(p if (i and p in minusculas) else re.sub(r"[a-zà-ú]", lambda m: m.group(0).upper(), p, count=1) for i, p in enumerate(partes))


def atualizar_cadastro(estado: dict) -> dict:
    atual = ler_json(CADASTRO, {})
    if atual and time.time() - estado.get("cadastro_em", 0) < CADASTRO_S:
        return atual
    try:
        listas = [json.loads(baixar(u, agente=AGENTE_NAVEGADOR)) for u in CADASTRO_URLS]
        novo = ler_cadastro(listas)
        if len(novo) > 100:
            escrever_atomico(CADASTRO, json.dumps(novo, ensure_ascii=False).encode())
            estado["cadastro_em"] = time.time()
            return novo
    except Exception as e:  # noqa: BLE001 — sem cadastro, a estação sai com o WIGOS no lugar do nome
        print(f"cadastro do INMET falhou: {e}", file=sys.stderr)
    estado["cadastro_em"] = time.time() - CADASTRO_S + 3600
    return atual


OSCAR = "https://oscar.wmo.int/surface/rest/api/search/station"
OSCAR_ESPERA_S = 24 * 3600


def completar_pelo_oscar(wigos: list[str], estado: dict, limite: int = 30) -> dict:
    """
    Estações que o WIS2 do INMET transmite mas que não estão no cadastro do
    INMET (as sinóticas dos aeroportos, operadas pelo DECEA, com o índice da
    OMM): o nome vem do OSCAR/Surface, o cadastro oficial da OMM. Guardado para
    sempre; a consulta que falha espera um dia.
    """
    oscar: dict = estado.setdefault("oscar", {})
    for w in wigos:
        if limite <= 0:
            break
        st = oscar.get(w)
        if st and (st.get("n") or time.time() - st.get("falhou", 0) < OSCAR_ESPERA_S):
            continue
        limite -= 1
        try:
            d = json.loads(baixar(f"{OSCAR}?{urllib.parse.urlencode({'wigosId': w})}", tentativas=2, timeout=30))
            r = (d.get("stationSearchResults") or [None])[0]
            if not r or not r.get("name"):
                oscar[w] = {"falhou": time.time()}
                continue
            indice = w.rsplit("-", 1)[-1]
            oscar[w] = {"c": indice if indice.isdigit() else w, "n": nome_legivel(r["name"]), "uf": None, "oscar": True}
        except Exception as e:  # noqa: BLE001
            oscar[w] = {"falhou": time.time()}
            print(f"OSCAR {w}: falhou — {e}", file=sys.stderr)
    return {w: v for w, v in oscar.items() if v.get("n")}


# ---------------------------------------------------------------------------
# METAR


def ur_de(t: float | None, td: float | None) -> float | None:
    """Umidade relativa pela fórmula de Magnus (a que a OMM recomenda)."""
    if t is None or td is None:
        return None
    a, b = 17.625, 243.04
    ur = 100 * math.exp(a * td / (b + td) - a * t / (b + t))
    return round(min(100.0, ur))


VIS_RAW = re.compile(r"\s(\d{4})(?:NDV)?\s")


def visibilidade_m(raw: str, milhas: str | None) -> int | None:
    """Metros. No Brasil o METAR traz 4 dígitos (9999 = 10 km ou mais); CAVOK também é ≥ 10 km."""
    corpo = (raw or "").split(" RMK ", 1)[0]
    if " CAVOK" in corpo:
        return 10000
    m = VIS_RAW.search(corpo.split(" TEMPO ", 1)[0].split(" BECMG ", 1)[0] + " ")
    if m:
        v = int(m.group(1))
        return 10000 if v == 9999 else v
    if milhas:
        try:
            return min(10000, round(float(milhas.rstrip("+")) * MI_M))
        except ValueError:
            return None
    return None


def num(s) -> float | None:
    if s is None or s == "":
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def registro_metar(ms_obs: int, raw: str, t, td, dv, vv_kt, raj_kt, p_hpa, vis_m, wx: str, tipo: str) -> dict:
    r = {"ms": ms_obs, "raw": raw.strip()}
    if tipo == "SPECI" or raw.startswith("SPECI"):
        r["speci"] = True
    vals = {
        "t": um_decimal(t),
        "td": um_decimal(td),
        "ur": ur_de(um_decimal(t), um_decimal(td)),
        "dv": None if dv is None else round(dv),
        "vv": None if vv_kt is None else um_decimal(vv_kt * KT_MS),
        "raj": None if raj_kt is None else um_decimal(raj_kt * KT_MS),
        "p": um_decimal(p_hpa),
        "vis": vis_m,
    }
    for k, v in vals.items():
        if v is not None and valido(k, v):
            r[k] = v
    if wx:
        r["wx"] = wx.strip()
    return r


def ler_csv_metar(corpo: bytes, aneis) -> tuple[dict, dict]:
    """CSV da AWC → ({icao: registro}, {icao: [lon, lat, alt]})."""
    if corpo[:2] == b"\x1f\x8b":  # o arquivo é .csv.gz (não é compressão de transporte)
        corpo = gzip.decompress(corpo)
    texto = corpo.decode("utf-8", "replace")
    leitor = csv.reader(io.StringIO(texto))
    cab = next(leitor, None)
    if not cab or "raw_text" not in cab:
        raise ValueError("CSV da AWC sem cabeçalho")
    i = {nome: cab.index(nome) for nome in cab if nome in cab}
    obs, pos = {}, {}
    for linha in leitor:
        if len(linha) < len(cab) - 5:
            continue
        g = lambda nome: linha[i[nome]] if nome in i and i[nome] < len(linha) else ""  # noqa: E731
        lat, lon = num(g("latitude")), num(g("longitude"))
        icao = g("station_id")
        if lat is None or lon is None or not re.fullmatch(r"[A-Z0-9]{4}", icao or ""):
            continue
        if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]) or not na_regiao(lon, lat, aneis):
            continue
        try:
            quando = datetime.fromisoformat(g("observation_time").replace("Z", "+00:00"))
        except ValueError:
            continue
        altim = num(g("altim_in_hg"))
        dv = num(g("wind_dir_degrees"))
        vv = num(g("wind_speed_kt"))
        if vv == 0:
            dv = None  # calmaria: sem direção
        raw = g("raw_text")
        obs[icao] = registro_metar(
            ms(quando), raw, num(g("temp_c")), num(g("dewpoint_c")), dv, vv, num(g("wind_gust_kt")),
            None if altim is None else altim * 33.8639, visibilidade_m(raw, g("visibility_statute_mi")),
            g("wx_string"), g("metar_type"),
        )
        elev = num(g("elevation_m"))
        pos[icao] = [round(lon, 4), round(lat, 4), elev]
    return obs, pos


def ler_api_metar(lista: list) -> tuple[dict, dict, dict]:
    """JSON da API da AWC → ({icao: [registros]}, {icao: pos}, {icao: nome})."""
    obs, pos, nomes = {}, {}, {}
    for m in lista or []:
        icao = m.get("icaoId")
        t_obs = m.get("obsTime")
        raw = m.get("rawOb") or ""
        if not icao or not isinstance(t_obs, (int, float)) or not raw:
            continue
        dv = m.get("wdir")
        dv = dv if isinstance(dv, (int, float)) else None  # "VRB" → sem direção
        vv = num(m.get("wspd"))
        if vv == 0:
            dv = None
        vis = m.get("visib")
        obs.setdefault(icao, []).append(
            registro_metar(
                int(t_obs * 1000), raw, num(m.get("temp")), num(m.get("dewp")), dv, vv, num(m.get("wgst")),
                num(m.get("altim")), visibilidade_m(raw, None if vis is None else str(vis)),
                m.get("wxString") or "", m.get("metarType") or "",
            )
        )
        if isinstance(m.get("lat"), (int, float)) and isinstance(m.get("lon"), (int, float)):
            pos[icao] = [round(m["lon"], 4), round(m["lat"], 4), num(m.get("elev"))]
        if m.get("name"):
            nomes[icao] = str(m["name"]).strip()
    return obs, pos, nomes


def nome_aerodromo(nome: str | None, icao: str) -> tuple[str, str | None]:
    """'Manaus/Gomes Intl, AM, BR' → ('Manaus/Gomes', 'BR'). A UF da AWC às vezes erra (Alta Floresta 'SP'): fica fora."""
    if not nome:
        return icao, None
    partes = [p.strip() for p in nome.split(",")]
    base = re.sub(r"\s+(Arpt|Intl|Airport|Aeropuerto)\b\.?", "", partes[0]).strip() or icao
    pais = partes[-1] if len(partes) >= 2 and re.fullmatch(r"[A-Z]{2}", partes[-1]) else None
    return base, pais


def fundir_metar(acumulado: dict, icao: str, registros: list, limite_ms: int) -> int:
    """Junta por instante (o mais novo vence: correção COR substitui) e corta a janela."""
    serie = {r["ms"]: r for r in acumulado.get(icao, []) if r["ms"] >= limite_ms}
    novos = 0
    for r in registros:
        if r["ms"] < limite_ms or not any(k in r for k in CHAVES_METAR):
            continue  # "WO ATTN" e afins: METAR sem nenhuma medição
        if r["ms"] not in serie or serie[r["ms"]] != r:
            novos += 1
        serie[r["ms"]] = r
    acumulado[icao] = [serie[k] for k in sorted(serie)]
    return novos


# ---------------------------------------------------------------------------
# Disco


def escrever_atomico(caminho: str, dado: bytes) -> None:
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
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


def compacto(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


# ---------------------------------------------------------------------------
# Montagem do que o app lê


def perto_km(a: list, b: list) -> float:
    kx = 111.32 * math.cos(math.radians(a[1]))
    return math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * 110.57)


def horas_da_janela(agora: datetime) -> list[datetime]:
    """As 48 horas cheias, da mais velha à atual."""
    fim = hora_cheia(agora)
    return [fim - timedelta(hours=JANELA_H - 1 - k) for k in range(JANELA_H)]


def montar(agora: datetime, horas: dict, posicoes: dict, cadastro: dict, metar: dict, metar_pos: dict, metar_nomes: dict) -> tuple[dict, dict]:
    grade = horas_da_janela(agora)
    t0 = ms(grade[0])
    idx = {nome_hora(h): i for i, h in enumerate(grade)}
    n = len(grade)

    inmet: dict[str, dict] = {}
    for nome, obs in horas.items():
        i = idx.get(nome)
        if i is None:
            continue
        for w, vals in obs.items():
            linha = inmet.setdefault(w, {})
            for k, v in vals.items():
                linha.setdefault(k, [None] * n)[i] = v

    estacoes = []
    serie_inmet = {}
    aerodromos = [metar_pos[i] for i in metar if metar[i] and metar_pos.get(i)]
    for w in sorted(inmet):
        p = posicoes.get(w)
        if not p:
            continue
        cad = cadastro.get(w, {})
        # A sinótica de aeroporto (fora do cadastro do INMET) mede no mesmo
        # lugar que o METAR, que é mais frequente e traz o tempo presente:
        # dois símbolos empilhados no mesmo ponto só atrapalhariam a leitura.
        if cad.get("oscar") and any(perto_km(p, a) < 5 for a in aerodromos):
            continue
        cod = cad.get("c") or w
        linha = inmet[w]
        # Última leitura: a hora mais nova com temperatura (ou qualquer coisa).
        ult = None
        for i in range(n - 1, -1, -1):
            if any(linha[k][i] is not None for k in linha):
                ult = i
                break
        if ult is None:
            continue
        u = {"ms": t0 + ult * 3_600_000, **{k: linha[k][ult] for k in CHAVES_INMET if k in linha and linha[k][ult] is not None}}
        chuva = linha.get("chuva")
        if chuva:
            ult24 = [v for v in chuva[max(0, ult - 23): ult + 1] if v is not None]
            if ult24:
                u["chuva24"] = round(sum(ult24), 1)
                u["chuva24n"] = len(ult24)
        e = {"k": "i", "c": cod, "w": w, "n": cad.get("n") or cod, "uf": cad.get("uf") or None, "x": p[0], "y": p[1], "z": p[2], "u": u}
        if w.startswith("0-20000-"):
            e["conv"] = True  # sinótica com índice da OMM (convencional ou de aeroporto)
        if cad.get("oscar"):
            e["oscar"] = True  # nome do OSCAR/Surface da OMM: fora do cadastro do INMET
        estacoes.append(e)
        serie_inmet[cod] = {k: linha[k] for k in CHAVES_INMET if k in linha}

    serie_metar = {}
    for icao in sorted(metar):
        regs = metar[icao]
        p = metar_pos.get(icao)
        if not regs or not p:
            continue
        colunas: dict[str, list] = {"ms": [r["ms"] for r in regs]}
        for k in CHAVES_METAR:
            if any(k in r for r in regs):
                colunas[k] = [r.get(k) for r in regs]
        colunas["wx"] = [r.get("wx") for r in regs]
        colunas["raw"] = [r["raw"] for r in regs]
        if any(r.get("speci") for r in regs):
            colunas["speci"] = [1 if r.get("speci") else 0 for r in regs]
        serie_metar[icao] = colunas
        ult = regs[-1]
        u = {k: ult[k] for k in ("ms", *CHAVES_METAR, "wx", "raw") if k in ult}
        nome, pais = nome_aerodromo(metar_nomes.get(icao), icao)
        estacoes.append({"k": "m", "c": icao, "n": nome, "uf": None, "pais": pais, "x": p[0], "y": p[1], "z": p[2], "u": u})

    gerado = iso(agora)
    fontes = {
        "inmet": "INMET — estações automáticas e convencionais, pelo nó WIS2 da OMM (wis2bra.inmet.gov.br)",
        "metar": "NOAA Aviation Weather Center — METAR/SPECI dos aeródromos",
    }
    cab = {"versao": VERSAO, "gerado": gerado, "fontes": fontes, "recorte": f"Amazônia Legal (IBGE) + {FAIXA_KM:.0f} km"}
    estacoes_json = {**cab, "estacoes": estacoes}
    serie_json = {**cab, "t0": t0, "passoMin": 60, "slots": n, "inmet": serie_inmet, "metar": serie_metar}
    return estacoes_json, serie_json


# ---------------------------------------------------------------------------
# Rodada


def rodada(agora: datetime | None = None) -> dict:
    agora = agora or agora_utc()
    os.makedirs(HORAS, exist_ok=True)
    os.makedirs(PASTA, exist_ok=True)
    aneis = carregar_aneis()
    estado = ler_json(ESTADO, {})
    cadastro = atualizar_cadastro(estado)
    horas_estado: dict[str, dict] = estado.get("horas", {})
    posicoes: dict[str, list] = estado.get("posicoes", {})
    grade = horas_da_janela(agora)
    vivas = {nome_hora(h) for h in grade}

    # Horas velhas saem do disco e do estado.
    for nome in os.listdir(HORAS):
        base = nome.split(".", 1)[0]
        if base not in vivas or (".parcial-" in nome and time.time() - os.path.getmtime(os.path.join(HORAS, nome)) > 600):
            os.remove(os.path.join(HORAS, nome))
    horas_estado = {k: v for k, v in horas_estado.items() if k in vivas}

    baixadas, falhas, conferidas = 0, 0, 0
    for h in reversed(grade):  # a hora nova primeiro
        nome = nome_hora(h)
        st = horas_estado.get(nome, {})
        idade_h = (hora_cheia(agora) - h).total_seconds() / 3600
        tem = os.path.exists(os.path.join(HORAS, f"{nome}.json"))
        if st.get("falhou") and time.time() - st["falhou"] < ESPERA_FALHA_S:
            continue
        if tem and idade_h >= CONFERIR_ATE_H:
            continue
        if idade_h >= RECENTES_H and time.time() - st.get("conferida", 0) < CONFERIR_S:
            continue
        if baixadas >= MAX_DOWNLOADS:
            break
        try:
            if tem or st.get("n") is not None:
                total = contar_hora(h)
                conferidas += 1
                st["conferida"] = time.time()
                if total == st.get("n") and tem:
                    horas_estado[nome] = st
                    continue
                if total == 0:
                    horas_estado[nome] = {**st, "n": 0}
                    continue
            obs, pos, total = baixar_hora(h, aneis)
            if total == 0:  # a hora ainda não começou a chegar
                horas_estado[nome] = {**st, "n": 0, "conferida": time.time()}
                continue
            baixadas += 1
            escrever_atomico(os.path.join(HORAS, f"{nome}.json"), compacto(obs))
            posicoes.update(pos)
            horas_estado[nome] = {"n": total, "conferida": time.time(), "estacoes": len(obs)}
            print(f"INMET {h:%d/%m %H}h: {len(obs)} estações ({total} observações no bbox)")
        except Exception as e:  # noqa: BLE001 — uma hora falhando não para as outras
            falhas += 1
            horas_estado[nome] = {**st, "falhou": time.time()}
            print(f"INMET {h:%d/%m %H}h: falhou — {e}", file=sys.stderr)

    horas = {}
    for nome in sorted(vivas):
        o = ler_json(os.path.join(HORAS, f"{nome}.json"), None)
        if o:
            horas[nome] = o
    sem_nome = sorted({w for o in horas.values() for w in o} - set(cadastro))
    if sem_nome:
        cadastro = {**completar_pelo_oscar(sem_nome, estado), **cadastro}

    # METAR: o cache do minuto sempre; a API para preencher de hora em hora.
    metar_est = ler_json(METAR_ESTADO, {"obs": {}, "pos": {}, "nomes": {}})
    acumulado, metar_pos, metar_nomes = metar_est["obs"], metar_est["pos"], metar_est["nomes"]
    limite = ms(grade[0])
    metar_novos = 0
    try:
        csv_obs, csv_pos = ler_csv_metar(baixar(AWC_CACHE), aneis)
        metar_pos.update(csv_pos)
        for icao, r in csv_obs.items():
            metar_novos += fundir_metar(acumulado, icao, [r], limite)
    except Exception as e:  # noqa: BLE001
        falhas += 1
        print(f"METAR (cache da AWC): falhou — {e}", file=sys.stderr)
    if time.time() - estado.get("metar_api", 0) >= METAR_API_S and metar_pos:
        ids = sorted(metar_pos)
        ok = True
        for k in range(0, len(ids), METAR_LOTE):
            lote = ids[k: k + METAR_LOTE]
            try:
                url = f"{AWC_API}?{urllib.parse.urlencode({'ids': ','.join(lote), 'hours': JANELA_H, 'format': 'json'}, safe=',')}"
                obs_api, pos_api, nomes_api = ler_api_metar(json.loads(baixar(url) or b"[]"))
                metar_nomes.update(nomes_api)
                for icao, regs in obs_api.items():
                    metar_pos.setdefault(icao, pos_api.get(icao))
                    metar_novos += fundir_metar(acumulado, icao, regs, limite)
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"METAR (API da AWC, {','.join(lote)}): falhou — {e}", file=sys.stderr)
        if ok:
            estado["metar_api"] = time.time()
    for icao in list(acumulado):
        acumulado[icao] = [r for r in acumulado[icao] if r["ms"] >= limite]
        if not acumulado[icao]:
            del acumulado[icao]
    escrever_atomico(METAR_ESTADO, compacto({"obs": acumulado, "pos": metar_pos, "nomes": metar_nomes}))

    estacoes_json, serie_json = montar(agora, horas, posicoes, cadastro, acumulado, metar_pos, metar_nomes)
    # A série primeiro: quem lê o cadastro novo acha a série da mesma rodada ou mais nova.
    escrever_atomico(os.path.join(PASTA, "serie.json"), compacto(serie_json))
    escrever_atomico(os.path.join(PASTA, "estacoes.json"), compacto(estacoes_json))

    estado["horas"] = horas_estado
    estado["posicoes"] = posicoes
    escrever_atomico(ESTADO, compacto(estado))
    n_inmet = sum(1 for e in estacoes_json["estacoes"] if e["k"] == "i")
    n_metar = len(estacoes_json["estacoes"]) - n_inmet
    return {"baixadas": baixadas, "conferidas": conferidas, "falhas": falhas, "inmet": n_inmet, "metar": n_metar, "metar_novos": metar_novos, "horas": len(horas)}


def main() -> None:
    r = rodada()
    print(
        f"rodada: {r['inmet']} estações INMET em {r['horas']} h, {r['metar']} aeródromos "
        f"({r['metar_novos']} METAR novos), {r['baixadas']} horas baixadas, {r['conferidas']} conferidas, {r['falhas']} falhas"
    )
    sys.exit(1 if r["falhas"] and not (r["baixadas"] or r["metar_novos"]) else 0)


if __name__ == "__main__":
    main()
