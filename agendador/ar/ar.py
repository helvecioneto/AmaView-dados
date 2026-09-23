#!/usr/bin/env python3
"""
Qualidade do ar para o AmaView: um espelho das medições em solo na Amazônia
Legal e do modelo CAMS.

- **Rede de Qualidade do Ar do Acre** (UFAC/MPAC): sensores PurpleAir com o
  PM2,5 já corrigido pela própria rede. A API (FastAPI, sem chave) não manda
  CORS utilizável e não tem série por sensor — só a leitura mais recente de
  cada um e médias horárias por município. Então este espelho ACUMULA a série
  de cada sensor a partir da leitura mais recente, a cada rodada (5 min), numa
  janela móvel de 48 h, e publica junto a média horária por município que a
  rede calcula (o histórico que já existe quando o espelho começa).
- **MonitorAr** (MMA): as estações oficiais da Amazônia Legal, com o IQAr e a
  classificação CONAMA 491 que o próprio MonitorAr calcula, por poluente. A
  API devolve as últimas 24 medições horárias; o espelho acumula até 48 h.
  `dtMedicao` vem no horário de Brasília (UTC−3), sem fuso: medido em
  23/09/2026 às 16:09 BRT, o horário mais novo do Brasil inteiro era 16:00.
- **CAMS global** (ECMWF/Copernicus), pelo Open-Meteo: MODELO, não medição.
  Uma grade de 0,8° cobrindo a Amazônia Legal (os centros caem sobre pontos da
  grade de 0,4° do próprio CAMS: cada valor é o de uma célula do modelo, sem
  interpolação). O modelo roda de 12 em 12 h; o espelho olha o `meta.json` de
  hora em hora e só baixa a grade quando há rodada nova (ou a cada 6 h, por
  segurança) — ~720 pontos, poucas vezes por dia, bem dentro do limite de uso
  não comercial do Open-Meteo. O navegador nunca fala com o Open-Meteo.

- **RedeAr** (IPAM/UFPA, sem chave): ~90 sensores — PurpleAir espelhados
  (id = `sensor_index` da PurpleAir) e aparelhos próprios —, cada um com as
  ~15 leituras mais recentes (bruto dos canais A e B). O espelho aplica a
  MESMA correção que a rede do Acre usa (ver "Correção") e acumula a série.
- **AirGradient** (sem chave): a lista mundial 1×/dia; a leitura corrente de
  cada ponto da Amazônia Legal a cada rodada.
- **PurpleAir** e **OpenAQ** (opcionais, com chave em /etc/amaview): só os
  sensores que as fontes sem chave não trazem, de hora em hora, com o saldo
  de pontos da PurpleAir vigiado (para de consultar abaixo de 50 mil).

Correção dos sensores de baixo custo: a rede do Acre publica o PM2,5 com a
correção da LRAPA (0,5 × média dos canais A e B "atm" − 0,66, sem negativo),
conferida leitura a leitura contra o bruto da RedeAr em 23/09/2026. Todo
sensor com bruto recebe a mesma fórmula, para ficarem na mesma régua, com a
checagem de concordância A/B da EPA (descartada quando |A−B| > 5 µg/m³ E
> 70% da média). Um mesmo sensor vindo de duas fontes vira um ponto só.

Fora isso, nada é filtrado, suavizado ou reclassificado. A faixa de qualidade
do ar é calculada no navegador, com a tabela documentada no AmaView.

Saída (abaixo de AR_RAIZ, servida pelo nginx em /ar/v1/):

  ar/v1/pontos.json       sensores (série de 5 min), municípios (média horária) e estações oficiais
  ar/v1/cams.json         grade do modelo e o PM2,5 das últimas 48 h (o mapa)
  ar/v1/cams-series.json  as séries completas do modelo por célula (a ficha)

  ar.py            uma rodada (o que o timer chama)
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("AR_RAIZ", "/var/cache/amaview-ar")
PASTA = os.path.join(RAIZ, "ar", f"v{VERSAO}")
ESTADO = os.path.join(RAIZ, "estado.json")
# A grade crua do modelo (≈ 1 MB) fica fora do estado: ele é reescrito a cada 5 min.
CAMS_BRUTO = os.path.join(RAIZ, "cams-bruto.json")
AGENTE = "AmaView-ar (+https://helvecioneto.github.io/AmaView/)"

UFAC = "https://acrequalidadedoar.ufac.br/api"
# RedeAr: a produção parou em 21/07/2026; a homologação ("hmg") segue viva.
# A base em uso é a que tem a leitura mais recente (ver `atualizar_redear`).
REDEAR_HMG = "https://hmg.api.redear.org.br/v1"
REDEAR_PROD = "https://api.redear.org.br/v1"
AIRGRADIENT = "https://api.airgradient.com/public/api/v1/world/locations"
PURPLEAIR = "https://api.purpleair.com/v1"
OPENAQ = "https://api.openaq.org/v3"

# Chaves opcionais (o painel `amaview chave …` grava, modo 640 root:amaview-ar)
# e a configuração (KEY=valor), fora do repositório.
CHAVE_PURPLEAIR = os.environ.get("AR_CHAVE_PURPLEAIR", "/etc/amaview/purpleair-key")
CHAVE_OPENAQ = os.environ.get("AR_CHAVE_OPENAQ", "/etc/amaview/openaq-key")
CONF = os.environ.get("AR_CONF", "/etc/amaview/ar.conf")
CONF_PADRAO = {
    # Minutos entre consultas pagas à PurpleAir (o custo vem em pontos).
    "PURPLEAIR_INTERVALO_MIN": 60,
    # Abaixo deste saldo de pontos, o espelho para de consultar a PurpleAir.
    "PURPLEAIR_SALDO_MIN": 50000,
    "OPENAQ_INTERVALO_MIN": 60,
}
MONITORAR = "https://monitorar-backend.mma.gov.br/v1"
OPEN_METEO = "https://air-quality-api.open-meteo.com"

JANELA_H = 48
JANELA_S = JANELA_H * 3600
# Grade da série dos sensores: o timer roda de 5 em 5 min.
PASSO_SENSOR_S = 5 * 60
# O cadastro da rede muda raramente (sensor novo, troca de nome).
CADASTRO_S = 6 * 3600
# Média horária por município: a rede fecha a hora com 1–2 h de atraso.
HISTORICO_S = 15 * 60
MONITORAR_S = 15 * 60
# Metadados do CAMS no Open-Meteo: uma olhada por hora.
META_S = 55 * 60
# Mesmo sem rodada nova, rebaixar a grade depois disto (o `meta.json` pode falhar).
CAMS_MAX_S = 6 * 3600
# Coordenadas por chamada ao Open-Meteo (cada uma conta como uma chamada no limite de uso).
LOTE = 100
PASSO_CAMS = 0.8
VARS_CAMS = ["pm2_5", "pm10", "carbon_monoxide", "ozone", "nitrogen_dioxide", "aerosol_optical_depth"]

# Descobrir sensores novos (listas grandes ou pagas): uma vez por dia.
DESCOBERTA_S = 24 * 3600
# Os aparelhos próprios da RedeAr têm série de 48 h na API: completar a do
# espelho (lacunas de quando ele esteve fora) no máximo a cada 6 h.
REDEAR_SERIE_S = 6 * 3600
# A produção da RedeAr é conferida de tempos em tempos: se voltar a ter o dado
# mais novo, passa a ser a base.
REDEAR_CONFERE_S = 6 * 3600
# Deduplicação por proximidade (OpenAQ × fontes diretas).
MESMO_LUGAR_M = 200
# Um sensor de fonte horária pinta o mapa até esta folga depois da leitura.
TOL_PADRAO_MIN = 30
# Ids publicados: os PurpleAir usam o `sensor_index`; os outros ganham uma
# faixa própria, para nunca colidirem com ele.
ID_BASE = {"redear": 900_000_000, "airgradient": 910_000_000, "openaq": 920_000_000}
# Prioridade quando o mesmo sensor vem de mais de uma fonte.
PRIORIDADE = ("ufac", "redear", "purpleair", "airgradient", "openaq")
# Códigos de `fl` (os de 0 a 3 são os `channel_flags` da PurpleAir: 1 = canal A
# degradado, 2 = B, 3 = os dois). 4 = A e B discordam: leitura descartada.
FL_AB = 4

# Amazônia Legal: os nove estados; o Maranhão só a oeste do meridiano de 44°W.
UFS_AL = {"AC", "AM", "AP", "PA", "RO", "RR", "TO", "MT", "MA"}
MERIDIANO_MA = -44.0
BRT = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------------
# Rede


def baixar(url: str, tentativas: int = 3, timeout: int = 60, cabecalhos: dict | None = None) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE, "Accept": "application/json", **(cabecalhos or {})})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(pedido, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # 4xx (chave recusada, limite, pedido errado) não melhora repetindo.
            if 400 <= e.code < 500 or i == tentativas - 1:
                corpo = e.read()[:200].decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {e.code}: {corpo}") from None
            time.sleep(1 + 2 * i)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
            time.sleep(1 + 2 * i)
    raise RuntimeError("inalcançável")


def baixar_json(url: str, **kw):
    return json.loads(baixar(url, **kw))


# ---------------------------------------------------------------------------
# Tempo


def iso_para_s(texto) -> float | None:
    """'2026-09-23T18:53:46Z' → epoch s; sem fuso ('2026-09-23T18:53:46', a RedeAr) é UTC. None se não servir."""
    if not isinstance(texto, str):
        return None
    try:
        d = datetime.fromisoformat(texto.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def monitorar_para_s(texto) -> float | None:
    """'2026-09-23 14:00' (horário de Brasília, sem fuso) → epoch s."""
    if not isinstance(texto, str):
        return None
    try:
        return datetime.strptime(texto.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=BRT).timestamp()
    except ValueError:
        return None


def iso(s: float) -> str:
    return datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(s: float) -> int:
    return int(round(s * 1000))


def num(v, casas: int = 2):
    """Número finito arredondado (tira o resíduo de ponto flutuante), ou None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), casas)


# ---------------------------------------------------------------------------
# Grades de tempo


def grade_sensores(agora: float) -> tuple[float, int]:
    """(t0, n) da grade de 5 min: o último ponto é o próximo múltiplo de 5 min."""
    fim = (agora // PASSO_SENSOR_S + 1) * PASSO_SENSOR_S
    n = JANELA_S // PASSO_SENSOR_S + 1
    return fim - (n - 1) * PASSO_SENSOR_S, n


def grade_horaria(agora: float) -> tuple[float, int]:
    """(h0, 48): as 48 horas cheias até a hora corrente, inclusive."""
    hora = agora // 3600 * 3600
    return hora - (JANELA_H - 1) * 3600, JANELA_H


def serie_na_grade(pontos, t0: float, passo: float, n: int, arredondar: bool) -> list:
    """
    [(t, valor)] → lista de n valores (None onde não há). Com `arredondar`, cada
    ponto vai para o horário da grade mais próximo; sem, para a hora que o
    contém. Dois pontos no mesmo horário: fica o mais novo.
    """
    serie = [None] * n
    for t, v in sorted(pontos, key=lambda p: p[0]):
        k = (t - t0) / passo
        i = int(round(k)) if arredondar else int(math.floor(k))
        if 0 <= i < n and v is not None:
            serie[i] = v
    return serie


# ---------------------------------------------------------------------------
# Geometria (grade do modelo)


def no_poligono(x: float, y: float, anel) -> bool:
    dentro = False
    j = len(anel) - 1
    for i in range(len(anel)):
        xi, yi = anel[i]
        xj, yj = anel[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


def grade_cams(anel, passo: float = PASSO_CAMS) -> list[list[float]]:
    """
    Centros das células de `passo` graus que tocam a Amazônia Legal (o centro
    ou algum canto dentro do contorno). Os centros são múltiplos inteiros do
    passo: com 0,8°, caem sobre a grade de 0,4° do CAMS.
    """
    xs = [p[0] for p in anel]
    ys = [p[1] for p in anel]
    kx0, kx1 = math.floor(min(xs) / passo), math.ceil(max(xs) / passo)
    ky0, ky1 = math.floor(min(ys) / passo), math.ceil(max(ys) / passo)
    meio = passo / 2
    out = []
    for ky in range(ky1, ky0 - 1, -1):
        for kx in range(kx0, kx1 + 1):
            x, y = round(kx * passo, 2), round(ky * passo, 2)
            pontos = [(x, y), (x - meio, y - meio), (x + meio, y - meio), (x - meio, y + meio), (x + meio, y + meio)]
            if any(no_poligono(px, py, anel) for px, py in pontos):
                out.append([x, y])
    return out


def carregar_anel():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "amazonia_legal.json")) as f:
        return json.load(f)["anel"]


# ---------------------------------------------------------------------------
# Rede de Qualidade do Ar do Acre (UFAC/MPAC)


def ler_cadastro_ufac(cru) -> dict:
    """/sensors → {sensor_index: {cod, nome, mun}}."""
    out = {}
    for s in cru if isinstance(cru, list) else []:
        idx = s.get("sensor_index")
        if not isinstance(idx, int):
            continue
        out[str(idx)] = {"cod": s.get("code"), "nome": s.get("name"), "mun": s.get("municipio")}
    return out


def acumular_ufac(u: dict, ultimas, agora: float) -> int:
    """Junta a leitura mais recente de cada sensor à série acumulada. Devolve quantas eram novas."""
    novas = 0
    leituras: dict = u.setdefault("leituras", {})
    ult: dict = u.setdefault("ultimas", {})
    for x in ultimas if isinstance(ultimas, list) else []:
        idx = x.get("sensor_index")
        t = iso_para_s(x.get("time_stamp"))
        if not isinstance(idx, int) or t is None:
            continue
        k = str(idx)
        pm = num(x.get("pm2_5_corrected"))
        fl = x.get("channel_flags") if isinstance(x.get("channel_flags"), int) else None
        ult[k] = {"t": t, "pm": pm, "fl": fl, "lat": num(x.get("latitude"), 6), "lon": num(x.get("longitude"), 6)}
        if pm is None or agora - t > JANELA_S or t > agora + 600:
            continue
        lista = leituras.setdefault(k, [])
        if not any(abs(p[0] - t) < 1 for p in lista):
            lista.append([t, pm, fl])
            novas += 1
    for k in list(leituras):
        leituras[k] = [p for p in leituras[k] if agora - p[0] <= JANELA_S + PASSO_SENSOR_S]
        if not leituras[k]:
            del leituras[k]
    return novas


def ler_historico_ufac(cru) -> dict:
    """/readings/history (horária) → {município: {epoch s do início da hora: µg/m³}}."""
    out: dict = {}
    for x in cru if isinstance(cru, list) else []:
        mun = x.get("municipio")
        t = iso_para_s(x.get("bucket"))
        v = num(x.get("pm2_5_avg"))
        if isinstance(mun, str) and t is not None and v is not None:
            out.setdefault(mun, {})[str(int(t))] = v
    return out


def atualizar_ufac(estado: dict, agora: float) -> str:
    u = estado.setdefault("ufac", {})
    if agora - u.get("cadastro_em", 0) >= CADASTRO_S or not u.get("cadastro"):
        try:
            cad = ler_cadastro_ufac(baixar_json(f"{UFAC}/sensors"))
            if cad:
                u["cadastro"] = cad
                u["cadastro_em"] = agora
        except Exception as e:  # noqa: BLE001 — o cadastro velho serve
            print(f"ufac: cadastro falhou — {e}", file=sys.stderr)
    novas = acumular_ufac(u, baixar_json(f"{UFAC}/readings/latest-by-sensor"), agora)
    resumo = f"{novas} leituras novas"
    if agora - u.get("historico_em", 0) >= HISTORICO_S:
        try:
            inicio = datetime.fromtimestamp(agora - JANELA_S - 3600, timezone.utc).date().isoformat()
            fim = datetime.fromtimestamp(agora, timezone.utc).date().isoformat()
            q = urllib.parse.urlencode({"start": inicio, "end": fim, "granularity": "hourly"})
            u["municipios"] = ler_historico_ufac(baixar_json(f"{UFAC}/readings/history?{q}"))
            u["historico_em"] = agora
            resumo += f", médias de {len(u['municipios'])} municípios"
        except Exception as e:  # noqa: BLE001
            print(f"ufac: histórico falhou — {e}", file=sys.stderr)
    return resumo


def publicar_ufac(u: dict, agora: float) -> tuple[dict, dict]:
    t0, n = grade_sensores(agora)
    cad = u.get("cadastro", {})
    ult = u.get("ultimas", {})
    leituras = u.get("leituras", {})
    lista = []
    for k in sorted(set(cad) | set(ult), key=lambda k: (cad.get(k, {}).get("cod") or "~", int(k))):
        c = cad.get(k)
        x = ult.get(k)
        # Fora do cadastro e parado: sensor que já saiu da rede.
        if c is None and (x is None or agora - x["t"] > JANELA_S):
            continue
        if x is None or x.get("lat") is None or x.get("lon") is None:
            continue  # sem coordenada não há onde desenhar
        pontos = [(p[0], p[1]) for p in leituras.get(k, [])]
        lista.append(
            {
                "id": int(k),
                "cod": (c or {}).get("cod"),
                "nome": (c or {}).get("nome"),
                "mun": (c or {}).get("mun"),
                "lat": x["lat"],
                "lon": x["lon"],
                "ult": {"t": ms(x["t"]), "pm": x["pm"], "fl": x["fl"]},
                "fl": sorted({p[2] for p in leituras.get(k, []) if isinstance(p[2], int) and p[2] > 0}),
                "serie": serie_na_grade(pontos, t0, PASSO_SENSOR_S, n, arredondar=True),
                "fonte": "ufac",
                "uf": "AC",
                "dono": dono_por_nome((c or {}).get("nome")),
                "ref": int(k),
                "pa": True,
            }
        )
    h0, horas = grade_horaria(agora)
    municipios = {}
    for mun, horas_mun in sorted(u.get("municipios", {}).items()):
        serie = serie_na_grade([(int(t), v) for t, v in horas_mun.items()], h0, 3600, horas, arredondar=False)
        if any(v is not None for v in serie):
            municipios[mun] = serie
    sensores = {"t0": ms(t0), "passoMin": PASSO_SENSOR_S // 60, "slots": n, "lista": lista}
    return sensores, {"h0": ms(h0), "horas": horas, "serie": municipios}


# ---------------------------------------------------------------------------
# Sensores de baixo custo: correção, lugar, dono e série


def lrapa(atm: float) -> float:
    """
    Correção da LRAPA (Lane Regional Air Protection Agency) sobre o PM2,5 "atm"
    do PMS5003: 0,5 × PA − 0,66, sem negativo. É a que a rede do Acre publica
    como `pm2_5_corrected` — conferido em 23/09/2026 com 7 sensores que estão
    nas duas redes (UFAC e RedeAr): 0,5 × média(A, B) − 0,66 bate com o valor
    da UFAC até a terceira casa (ex.: A 10,0 e B 11,3 → 4,665). A série horária
    da UFAC tem zeros e nenhum negativo: o corte em zero é dela também.
    """
    return max(0.0, 0.5 * atm - 0.66)


def canal(v) -> float | None:
    """Leitura de um canal do PMS5003 que serve (número, 0–2000 µg/m³)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 2000 else None


def corrigir_ab(a, b) -> tuple[float | None, int | None]:
    """
    (PM2,5 corrigido, fl) a partir do "atm" dos canais A e B, com a checagem de
    concordância da EPA (Barkjohn et al., 2021): os canais discordam quando a
    diferença passa de 5 µg/m³ E de 70% da média — a leitura é descartada
    (fl = 4), porque não dá para saber qual canal está certo. Um canal só
    serve sozinho, marcado como o outro degradado (fl 1 ou 2, como a PurpleAir).
    Sem canal nenhum: (None, None).
    """
    va, vb = canal(a), canal(b)
    if va is not None and vb is not None:
        media = (va + vb) / 2
        dif = abs(va - vb)
        if dif > 5 and media > 0 and dif / media > 0.7:
            return None, FL_AB
        return num(lrapa(media)), 0
    if va is not None:
        return num(lrapa(va)), 2
    if vb is not None:
        return num(lrapa(vb)), 1
    return None, None


_MUNICIPIOS = None


def carregar_municipios() -> list:
    global _MUNICIPIOS
    if _MUNICIPIOS is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "municipios_al.json")) as f:
            _MUNICIPIOS = json.load(f)["municipios"]
    return _MUNICIPIOS


def municipio_em(lon: float, lat: float) -> tuple[str | None, str | None]:
    """(município, UF) da malha do IBGE que contém o ponto; (None, None) fora da Amazônia Legal."""
    for m in carregar_municipios():
        b = m["bb"]
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            # Paridade entre anéis: buracos (e ilhas) contam certo.
            if sum(1 for anel in m["aneis"] if no_poligono(lon, lat, anel)) % 2:
                return m["n"], m["uf"]
    return None, None


# Dono (órgão) pelo nome do sensor: as redes batizam os sensores com a sigla.
DONOS = [
    (r"^SEMA[_ -]?DCAM", "SEMA-AM"),
    (r"^SEMAS\b|^SEMAS[_ -]", "SEMAS-PA"),
    (r"^SEMA-(IFMT|UFMT|UNEMAT|IPAM|SABA|UNAERP)\b", "SEMA-MT / {1}"),
    (r"^SEMA[- ]MT\b", "SEMA-MT"),
    (r"UEA[_ ]?EDUCAIR", "UEA (EducAIR)"),
    (r"^MPAC|PROMOTORIA", "MPAC"),
    (r"^MPAP\b|^MPAP[_ -]", "MPAP"),
    (r"^MPRR\b|^MPRR[_ -]", "MPRR"),
    (r"TCE[-_ ]?RO\b", "TCE-RO"),
    (r"ICMBIO|^PARNA\b", "ICMBio"),
    (r"\bIPAM\b|IPAM[_-]|[_-]IPAM", "IPAM"),
    (r"\bINPA\b", "INPA"),
    (r"\bUFPA\b", "UFPA"),
    (r"\bUFRA\b|^UFRA[-_]", "UFRA"),
    (r"\bIFRO\b", "IFRO"),
    (r"\bUFMT\b", "UFMT"),
    (r"\bUNEMAT\b", "UNEMAT"),
    (r"\bIFMT\b", "IFMT"),
    (r"UFAC", "UFAC"),
    (r"CAPACREAM", "CAPACREAM"),
]


def dono_por_nome(nome) -> str | None:
    if not isinstance(nome, str):
        return None
    import re

    for padrao, dono in DONOS:
        m = re.search(padrao, nome, re.IGNORECASE)
        if m:
            return dono.replace("{1}", m.group(1).upper()) if "{1}" in dono else dono
    return None


def distancia_m(lon1, lat1, lon2, lat2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def f_para_c(f) -> float | None:
    return num((f - 32) * 5 / 9, 1) if isinstance(f, (int, float)) and not isinstance(f, bool) and math.isfinite(f) else None


def guardar_leitura(fonte: dict, k: str, t: float, pm, fl, extra: dict, agora: float) -> bool:
    """
    Junta uma leitura (t, pm corrigido, fl, bruto/tempo) do sensor `k` à fonte.
    A mais nova vira a `ultima`; a série guarda uma por horário de 5 min (a
    mais nova), dentro da janela de 48 h. Devolve True se entrou na série.
    """
    if t > agora + 600:
        return False
    ult = fonte.setdefault("ultimas", {})
    if k not in ult or t >= ult[k]["t"]:
        ult[k] = {"t": t, "pm": pm, "fl": fl, **extra}
    if pm is None or agora - t > JANELA_S:
        return False
    lista = fonte.setdefault("leituras", {}).setdefault(k, [])
    slot = round(t / PASSO_SENSOR_S)
    for i, p in enumerate(lista):
        if round(p[0] / PASSO_SENSOR_S) == slot:
            if t > p[0] + 0.5:
                lista[i] = [t, pm, fl]
                return True
            return False
    lista.append([t, pm, fl])
    return True


def podar(fonte: dict, agora: float) -> None:
    leituras = fonte.get("leituras", {})
    for k in list(leituras):
        leituras[k] = [p for p in leituras[k] if agora - p[0] <= JANELA_S + PASSO_SENSOR_S]
        if not leituras[k]:
            del leituras[k]
    # Sem leitura há mais de 7 dias e fora do cadastro: esquece.
    for k in list(fonte.get("ultimas", {})):
        if agora - fonte["ultimas"][k]["t"] > 7 * 86400 and k not in fonte.get("meta", {}):
            del fonte["ultimas"][k]


def vivo_ha(fonte: dict, k: str, agora: float, segundos: float) -> bool:
    u = fonte.get("ultimas", {}).get(k)
    return bool(u) and agora - u["t"] <= segundos


def item_publicado(fonte_nome: str, k: str, meta: dict, fonte: dict, t0: float, n: int) -> dict | None:
    """Um sensor de fonte nova no formato de `pontos.json` (o mesmo dos da UFAC, com campos a mais)."""
    x = fonte.get("ultimas", {}).get(k)
    if x is None or meta.get("lat") is None or meta.get("lon") is None:
        return None
    leituras = fonte.get("leituras", {}).get(k, [])
    base = ID_BASE.get(fonte_nome, 0) if not meta.get("pa") else 0
    item = {
        "id": base + int(k),
        "cod": None,
        "nome": meta.get("nome"),
        "mun": meta.get("mun"),
        "lat": meta["lat"],
        "lon": meta["lon"],
        "ult": {"t": ms(x["t"]), "pm": x["pm"], "fl": x["fl"]},
        "fl": sorted({p[2] for p in leituras if isinstance(p[2], int) and p[2] > 0}),
        "serie": serie_na_grade([(p[0], p[1]) for p in leituras], t0, PASSO_SENSOR_S, n, arredondar=True),
        "fonte": fonte_nome,
        "uf": meta.get("uf"),
        "dono": meta.get("dono"),
        "ref": int(k),
        "pa": bool(meta.get("pa")),
    }
    bruto = {c: x[c] for c in ("a", "b") if x.get(c) is not None}
    if bruto:
        # O bruto tem horário próprio: num sensor mesclado ele pode vir de outra fonte.
        item["ab"] = dict(bruto, t=ms(x["t"]), fl=x["fl"])
    met = {c: x[c] for c in ("temp", "ur") if x.get(c) is not None}
    if met:
        item["met"] = met
    if meta.get("tol"):
        item["tol"] = meta["tol"]
    if meta.get("corr"):
        item["corr"] = meta["corr"]
    return item


def mesclar(principal: dict, outro: dict) -> None:
    """O mesmo sensor em duas fontes: um ponto só, a série da principal com as lacunas preenchidas pela outra."""
    principal["serie"] = [a if a is not None else b for a, b in zip(principal["serie"], outro["serie"])]
    # A leitura mais recente com valor; uma descartada pela checagem A/B na
    # outra fonte não apaga o valor que a fonte principal publicou.
    if outro["ult"]["t"] > principal["ult"]["t"] and (outro["ult"]["pm"] is not None or principal["ult"]["pm"] is None):
        principal["ult"] = outro["ult"]
    principal["fl"] = sorted(set(principal["fl"]) | set(outro["fl"]))
    for c in ("ab", "met"):
        if c in outro and c not in principal:
            principal[c] = outro[c]
    for c in ("nome", "mun", "uf", "dono"):
        if not principal.get(c) and outro.get(c):
            principal[c] = outro[c]
    principal.setdefault("tambem", [])
    if outro["fonte"] not in principal["tambem"]:
        principal["tambem"].append(outro["fonte"])


def juntar_sensores(por_fonte: dict) -> list:
    """
    Todas as fontes numa lista só, sem dois símbolos no mesmo sensor:
    - pelo `sensor_index` da PurpleAir (UFAC, RedeAr, PurpleAir): fica a fonte
      de maior prioridade (`PRIORIDADE`), com a série completada pelas outras;
    - a OpenAQ a menos de 200 m de um sensor de outra fonte é o mesmo aparelho
      repassado (a OpenAQ agrega AirGradient, PurpleAir…): fica a fonte direta.
    """
    saida: list = []
    por_id: dict = {}
    for nome in PRIORIDADE:
        for item in por_fonte.get(nome, []):
            if nome == "openaq":
                perto = next(
                    (o for o in saida if distancia_m(o["lon"], o["lat"], item["lon"], item["lat"]) < MESMO_LUGAR_M), None
                )
                if perto is not None:
                    perto.setdefault("tambem", [])
                    if "openaq" not in perto["tambem"]:
                        perto["tambem"].append("openaq")
                    continue
            existente = por_id.get(item["id"])
            if existente is not None:
                mesclar(existente, item)
                continue
            por_id[item["id"]] = item
            saida.append(item)
    return saida


# ---------------------------------------------------------------------------
# RedeAr (IPAM/UFPA)


def leitura_redear(r: dict, purpleair: bool) -> tuple[float, float | None, int | None, dict] | None:
    """Uma leitura da RedeAr → (t, pm corrigido, fl, extra). T nos PurpleAir vem em °F."""
    t = iso_para_s(r.get("datetime"))
    if t is None:
        return None
    a, b = r.get("pms1_pm2_5_env"), r.get("pms2_pm2_5_env")
    pm, fl = corrigir_ab(a, b)
    temp = r.get("bme_temperature")
    extra = {
        "a": num(canal(a), 1),
        "b": num(canal(b), 1),
        "temp": f_para_c(temp) if purpleair else num(temp, 1),
        "ur": num(r.get("bme_humidity"), 0),
    }
    return t, pm, fl, extra


def ler_redear(cru, anel) -> dict:
    """/sensors → {chave: (meta, [leituras])} só da Amazônia Legal (pela coordenada, não pelo cadastro)."""
    out = {}
    for s in cru if isinstance(cru, list) else []:
        sid = s.get("sensor_id")
        coords = ((s.get("gps") or {}).get("coordinates")) or []
        if not isinstance(sid, int) or len(coords) < 2 or not all(isinstance(c, (int, float)) for c in coords[:2]):
            continue
        lon, lat = float(coords[0]), float(coords[1])
        if not no_poligono(lon, lat, anel):
            continue
        pa = s.get("source") != "RedeAr"
        meta = {"nome": s.get("name"), "lat": num(lat, 6), "lon": num(lon, 6), "pa": pa, "dono": dono_por_nome(s.get("name"))}
        out[str(sid)] = (meta, [x for x in (leitura_redear(r, pa) for r in s.get("readings") or []) if x])
    return out


def mais_nova_redear(cru) -> float:
    return max((iso_para_s(r.get("datetime")) or 0 for s in (cru if isinstance(cru, list) else []) for r in s.get("readings") or []), default=0)


def atualizar_redear(estado: dict, agora: float, anel) -> str:
    r = estado.setdefault("redear", {})
    base = r.get("base", REDEAR_HMG)
    outra = REDEAR_PROD if base == REDEAR_HMG else REDEAR_HMG
    try:
        cru = baixar_json(f"{base}/sensors", timeout=120)
    except Exception as e:  # noqa: BLE001 — a outra base pode estar de pé
        print(f"redear: {base} falhou ({e}); tentando {outra}", file=sys.stderr)
        cru = baixar_json(f"{outra}/sensors", timeout=120)
        base, outra = outra, base
    nova = mais_nova_redear(cru)
    # A produção parou em julho de 2026: de tempos em tempos, confere se voltou.
    if agora - r.get("conferido_em", 0) >= REDEAR_CONFERE_S:
        r["conferido_em"] = agora
        try:
            cru_outra = baixar_json(f"{outra}/sensors", timeout=120)
            if mais_nova_redear(cru_outra) > nova + 1800:
                print(f"redear: {outra} tem dado mais novo; passa a ser a base")
                cru, base, nova = cru_outra, outra, mais_nova_redear(cru_outra)
        except Exception as e:  # noqa: BLE001
            print(f"redear: conferência de {outra} falhou — {e}", file=sys.stderr)
    r["base"] = base
    lidos = ler_redear(cru, anel)
    meta_ant = r.get("meta", {})
    novas = 0
    for k, (meta, leituras) in lidos.items():
        ant = meta_ant.get(k, {})
        if ant.get("lat") == meta["lat"] and ant.get("lon") == meta["lon"] and "mun" in ant:
            meta["mun"], meta["uf"] = ant["mun"], ant["uf"]
        else:
            meta["mun"], meta["uf"] = municipio_em(meta["lon"], meta["lat"])
        meta_ant[k] = meta
        for t, pm, fl, extra in leituras:
            novas += guardar_leitura(r, k, t, pm, fl, extra, agora)
    # Sensor que saiu do cadastro da RedeAr sai do mapa.
    r["meta"] = {k: v for k, v in meta_ant.items() if k in lidos}
    # Os aparelhos próprios têm série na API: completa as lacunas (a cada 6 h).
    serie_em = r.setdefault("serie_em", {})
    for k, meta in r["meta"].items():
        if meta["pa"] or agora - serie_em.get(k, 0) < REDEAR_SERIE_S or not vivo_ha(r, k, agora, JANELA_S):
            continue
        try:
            novas += completar_serie_redear(r, base, k, agora)
            serie_em[k] = agora
        except Exception as e:  # noqa: BLE001
            print(f"redear: série do sensor {k} falhou — {e}", file=sys.stderr)
    podar(r, agora)
    vivos = sum(1 for k in r["meta"] if vivo_ha(r, k, agora, JANELA_S))
    return f"{len(r['meta'])} sensores na Amazônia Legal ({vivos} com dado em 48 h), {novas} leituras novas · {base}"


def completar_serie_redear(r: dict, base: str, k: str, agora: float) -> int:
    inicio = iso(agora - JANELA_S)
    fim = iso(agora + 60)
    novas = 0
    for offset in range(0, 5000, 1000):
        q = urllib.parse.urlencode({"startDate": inicio, "endDate": fim, "limit": 1000, "offset": offset})
        cru = baixar_json(f"{base}/sensors/{k}/readings?{q}", timeout=120)
        leituras = cru.get("readings") if isinstance(cru, dict) else None
        for x in leituras or []:
            lida = leitura_redear(x, False)
            if lida:
                novas += guardar_leitura(r, k, lida[0], lida[1], lida[2], lida[3], agora)
        total = ((cru.get("pagination") or {}).get("total")) if isinstance(cru, dict) else None
        if not leituras or not isinstance(total, int) or offset + 1000 >= total:
            break
    return novas


def publicar_redear(r: dict, agora: float) -> list:
    t0, n = grade_sensores(agora)
    out = []
    for k, meta in r.get("meta", {}).items():
        item = item_publicado("redear", k, meta, r, t0, n)
        if item:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# AirGradient (lista mundial pública, sem chave)


def leitura_airgradient(x: dict) -> tuple[float, float | None, int | None, dict] | None:
    t = iso_para_s(x.get("timestamp"))
    if t is None:
        return None
    pm02 = canal(x.get("pm02"))
    # Um PMS5003 só (O-1PST): sem par A/B para checar; a mesma correção.
    pm = num(lrapa(pm02)) if pm02 is not None else None
    return t, pm, None, {"a": num(pm02, 1), "temp": num(x.get("atmp"), 1), "ur": num(x.get("rhum"), 0)}


def atualizar_airgradient(estado: dict, agora: float, anel) -> str:
    g = estado.setdefault("airgradient", {})
    if agora - g.get("descoberto_em", 0) >= DESCOBERTA_S or "meta" not in g:
        # 1,5 MB com o mundo todo: uma vez por dia; depois, ponto a ponto.
        mundo = baixar_json(f"{AIRGRADIENT}/measures/current", timeout=120)
        meta = {}
        for x in mundo if isinstance(mundo, list) else []:
            lid, lat, lon = x.get("locationId"), x.get("latitude"), x.get("longitude")
            if not isinstance(lid, int) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            if not no_poligono(lon, lat, anel):
                continue
            mun, uf = municipio_em(lon, lat)
            meta[str(lid)] = {
                "nome": x.get("publicLocationName") or x.get("locationName"),
                "lat": num(lat, 6),
                "lon": num(lon, 6),
                "mun": mun,
                "uf": uf,
                "dono": (x.get("publicContributorName") or "").strip() or None,
                "pa": False,
            }
            lida = leitura_airgradient(x)
            if lida:
                guardar_leitura(g, str(lid), *lida, agora)
        g["meta"] = meta
        g["descoberto_em"] = agora
    novas = 0
    for k in g["meta"]:
        x = baixar_json(f"{AIRGRADIENT}/{k}/measures/current")
        lida = leitura_airgradient(x) if isinstance(x, dict) else None
        if lida:
            novas += guardar_leitura(g, k, *lida, agora)
    podar(g, agora)
    return f"{len(g['meta'])} pontos na Amazônia Legal, {novas} leituras novas"


def publicar_airgradient(g: dict, agora: float) -> list:
    t0, n = grade_sensores(agora)
    return [i for i in (item_publicado("airgradient", k, m, g, t0, n) for k, m in g.get("meta", {}).items()) if i]


# ---------------------------------------------------------------------------
# Chaves e configuração (PurpleAir e OpenAQ são opcionais)


def ler_chave(caminho: str) -> tuple[str | None, str | None]:
    """
    (chave, nota). Sem arquivo: (None, "sem chave") — a fonte fica de fora, em
    silêncio. Arquivo que o serviço não consegue ler: (None, aviso).
    """
    try:
        with open(caminho) as f:
            chave = f.read().strip()
    except FileNotFoundError:
        return None, "sem chave"
    except OSError:
        return None, f"sem permissão para ler {caminho} (deve ser 640 root:amaview-ar)"
    return (chave, None) if chave else (None, "sem chave")


def ler_conf() -> dict:
    conf = dict(CONF_PADRAO)
    try:
        with open(CONF) as f:
            for linha in f:
                linha = linha.split("#", 1)[0].strip()
                if "=" not in linha:
                    continue
                k, v = (x.strip() for x in linha.split("=", 1))
                if k in conf:
                    try:
                        conf[k] = max(1, int(float(v)))
                    except ValueError:
                        pass
    except OSError:
        pass
    return conf


def bbox_al(anel) -> tuple[float, float, float, float]:
    xs = [p[0] for p in anel]
    ys = [p[1] for p in anel]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# PurpleAir (API v1, com chave e saldo de pontos)

# Campos da consulta de cada rodada: o mínimo para a MESMA correção (LRAPA
# sobre o "atm" de A e B, com a checagem A/B) e o horário.
CAMPOS_PA = ["pm2.5_atm_a", "pm2.5_atm_b", "last_seen"]
CAMPOS_PA_DESCOBERTA = ["name", "latitude", "longitude", "last_seen"]
# Estimativa (a conta real vem do saldo antes e depois): base + campos × linhas.
CUSTO_PA_BASE = 5
CUSTO_PA_CAMPO = 2


def custo_pa(campos: int, linhas: int) -> int:
    return CUSTO_PA_BASE + CUSTO_PA_CAMPO * campos * linhas


def tabela_pa(cru) -> list[dict]:
    """Resposta de /v1/sensors (`fields` + `data`) → lista de dicionários."""
    if not isinstance(cru, dict) or not isinstance(cru.get("fields"), list) or not isinstance(cru.get("data"), list):
        raise ValueError("resposta da PurpleAir sem `fields`/`data`")
    campos = cru["fields"]
    return [dict(zip(campos, linha)) for linha in cru["data"] if isinstance(linha, list)]


def saldo_pa(chave: str) -> int | None:
    org = baixar_json(f"{PURPLEAIR}/organization", cabecalhos={"X-API-Key": chave}, tentativas=2)
    v = org.get("remaining_points") if isinstance(org, dict) else None
    return int(v) if isinstance(v, (int, float)) else None


def atualizar_purpleair(estado: dict, agora: float, anel, conf: dict, fontes: dict) -> str | None:
    p = estado.setdefault("purpleair", {})
    f = fontes.setdefault("purpleair", {"em": None, "erro": None})
    chave, nota = ler_chave(CHAVE_PURPLEAIR)
    intervalo = conf["PURPLEAIR_INTERVALO_MIN"] * 60
    f["intervaloMin"] = conf["PURPLEAIR_INTERVALO_MIN"]
    if not chave:
        f["nota"] = nota
        f["erro"] = None
        return None
    if agora - p.get("consultado_em", 0) < intervalo - 30 and p.get("meta") is not None:
        return None
    p["consultado_em"] = agora
    antes = saldo_pa(chave)
    f["saldo"] = antes
    minimo = conf["PURPLEAIR_SALDO_MIN"]
    if antes is not None and antes < minimo:
        f["nota"] = f"saldo de {antes} pontos, abaixo de {minimo}: consultas suspensas"
        return f"saldo {antes} < {minimo}: sem consulta"
    cab = {"X-API-Key": chave}
    gasto_previsto = 0
    if agora - p.get("descoberto_em", 0) >= DESCOBERTA_S or p.get("meta") is None:
        x0, y0, x1, y1 = bbox_al(anel)
        q = urllib.parse.urlencode(
            {
                "fields": ",".join(CAMPOS_PA_DESCOBERTA),
                "location_type": 0,
                "max_age": 3600,
                "nwlng": x0,
                "nwlat": y1,
                "selng": x1,
                "selat": y0,
            }
        )
        linhas = tabela_pa(baixar_json(f"{PURPLEAIR}/sensors?{q}", cabecalhos=cab, timeout=90))
        gasto_previsto += custo_pa(len(CAMPOS_PA_DESCOBERTA), len(linhas))
        meta_ant = p.get("meta") or {}
        meta = {}
        for x in linhas:
            idx, lat, lon = x.get("sensor_index"), x.get("latitude"), x.get("longitude")
            if not isinstance(idx, int) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            if not no_poligono(lon, lat, anel):
                continue
            k = str(idx)
            ant = meta_ant.get(k, {})
            if ant.get("lat") == num(lat, 6) and ant.get("lon") == num(lon, 6) and "mun" in ant:
                mun, uf = ant["mun"], ant["uf"]
            else:
                mun, uf = municipio_em(lon, lat)
            meta[k] = {"nome": x.get("name"), "lat": num(lat, 6), "lon": num(lon, 6), "mun": mun, "uf": uf,
                       "dono": dono_por_nome(x.get("name")), "pa": True}
        p["meta"] = meta
        p["descoberto_em"] = agora
    # Só o que as fontes sem chave não trazem (leitura nas últimas 2 h).
    ufac, redear = estado.get("ufac", {}), estado.get("redear", {})
    alvo = [k for k in p["meta"] if not vivo_ha(ufac, k, agora, 7200) and not vivo_ha(redear, k, agora, 7200)]
    p["alvo"] = len(alvo)
    novas = 0
    for i in range(0, len(alvo), 500):
        lote = alvo[i : i + 500]
        q = urllib.parse.urlencode({"fields": ",".join(CAMPOS_PA), "show_only": ",".join(lote)})
        linhas = tabela_pa(baixar_json(f"{PURPLEAIR}/sensors?{q}", cabecalhos=cab, timeout=90))
        gasto_previsto += custo_pa(len(CAMPOS_PA), len(linhas))
        for x in linhas:
            k = str(x.get("sensor_index"))
            t = x.get("last_seen")
            if k not in p["meta"] or not isinstance(t, (int, float)):
                continue
            pm, fl = corrigir_ab(x.get("pm2.5_atm_a"), x.get("pm2.5_atm_b"))
            extra = {"a": num(canal(x.get("pm2.5_atm_a")), 1), "b": num(canal(x.get("pm2.5_atm_b")), 1)}
            novas += guardar_leitura(p, k, float(t), pm, fl, extra, agora)
    # Quem não saiu na consulta desta hora não é mais alvo: fica só a série acumulada.
    for k in p["meta"]:
        p["meta"][k]["tol"] = conf["PURPLEAIR_INTERVALO_MIN"] + 15
    depois = saldo_pa(chave)
    f["saldo"] = depois if depois is not None else antes
    gasto = antes - depois if antes is not None and depois is not None and antes >= depois else gasto_previsto
    f["gastoRodada"] = gasto
    f["gastoDia"] = int(gasto * 86400 / intervalo)
    f["nota"] = f"{len(alvo)} sensores consultados a cada {conf['PURPLEAIR_INTERVALO_MIN']} min"
    podar(p, agora)
    return f"{len(p['meta'])} sensores na Amazônia Legal, {len(alvo)} consultados, {novas} leituras novas, gasto {gasto} pontos, saldo {f['saldo']}"


def publicar_purpleair(p: dict, agora: float) -> list:
    t0, n = grade_sensores(agora)
    return [i for i in (item_publicado("purpleair", k, m, p, t0, n) for k, m in (p.get("meta") or {}).items()) if i]


# ---------------------------------------------------------------------------
# OpenAQ v3 (com chave)


def atualizar_openaq(estado: dict, agora: float, anel, conf: dict, fontes: dict) -> str | None:
    o = estado.setdefault("openaq", {})
    f = fontes.setdefault("openaq", {"em": None, "erro": None})
    chave, nota = ler_chave(CHAVE_OPENAQ)
    intervalo = conf["OPENAQ_INTERVALO_MIN"] * 60
    if not chave:
        f["nota"] = nota
        f["erro"] = None
        return None
    if agora - o.get("consultado_em", 0) < intervalo - 30 and o.get("meta") is not None:
        return None
    o["consultado_em"] = agora
    cab = {"X-API-Key": chave}
    if agora - o.get("descoberto_em", 0) >= DESCOBERTA_S or o.get("meta") is None:
        x0, y0, x1, y1 = bbox_al(anel)
        meta = {}
        for pagina in range(1, 11):
            q = urllib.parse.urlencode({"bbox": f"{x0},{y0},{x1},{y1}", "limit": 1000, "page": pagina})
            cru = baixar_json(f"{OPENAQ}/locations?{q}", cabecalhos=cab, timeout=90)
            resultados = cru.get("results") if isinstance(cru, dict) else None
            for loc in resultados or []:
                lid = loc.get("id")
                c = loc.get("coordinates") or {}
                lat, lon = c.get("latitude"), c.get("longitude")
                if not isinstance(lid, int) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                    continue
                if not no_poligono(lon, lat, anel) or loc.get("isMobile"):
                    continue
                pm25 = [s.get("id") for s in loc.get("sensors") or [] if ((s.get("parameter") or {}).get("name")) == "pm25"]
                if not pm25:
                    continue
                mun, uf = municipio_em(lon, lat)
                provedor = (loc.get("provider") or {}).get("name")
                dono = (loc.get("owner") or {}).get("name")
                meta[str(lid)] = {
                    "nome": loc.get("name"),
                    "lat": num(lat, 6),
                    "lon": num(lon, 6),
                    "mun": mun,
                    "uf": uf,
                    "dono": " · ".join(x for x in (dono, provedor) if x) or None,
                    "pa": False,
                    "sensor": pm25[0],
                    # Monitor de referência: o valor vale como publicado; baixo custo: a mesma correção.
                    "monitor": bool(loc.get("isMonitor")),
                    "corr": None if loc.get("isMonitor") else "lrapa",
                    "tol": 90,
                }
            if not resultados or len(resultados) < 1000:
                break
        o["meta"] = meta
        o["descoberto_em"] = agora
    novas = 0
    for k, m in o["meta"].items():
        cru = baixar_json(f"{OPENAQ}/locations/{k}/latest", cabecalhos=cab)
        for x in (cru.get("results") if isinstance(cru, dict) else None) or []:
            if x.get("sensorsId") != m.get("sensor"):
                continue
            t = iso_para_s((x.get("datetime") or {}).get("utc"))
            v = canal(x.get("value"))
            if t is None:
                continue
            pm = num(v) if m.get("monitor") else (num(lrapa(v)) if v is not None else None)
            novas += guardar_leitura(o, k, t, pm, None, {"a": num(v, 1)}, agora)
        time.sleep(1.1)  # 60 pedidos por minuto
    podar(o, agora)
    f["nota"] = f"{len(o['meta'])} locais a cada {conf['OPENAQ_INTERVALO_MIN']} min"
    return f"{len(o['meta'])} locais na Amazônia Legal, {novas} leituras novas"


def publicar_openaq(o: dict, agora: float) -> list:
    t0, n = grade_sensores(agora)
    return [i for i in (item_publicado("openaq", k, m, o, t0, n) for k, m in (o.get("meta") or {}).items()) if i]


# ---------------------------------------------------------------------------
# MonitorAr (MMA)


def na_amazonia_legal(e: dict) -> bool:
    uf = ((e.get("municipio") or {}).get("sgUf")) or ""
    try:
        lon = float(e.get("nuLongitude"))
    except (TypeError, ValueError):
        return False
    return uf in UFS_AL and (uf != "MA" or lon <= MERIDIANO_MA)


def ler_estacao_monitorar(e: dict, agora: float, acumulado: dict | None) -> dict | None:
    try:
        lat, lon = float(e.get("nuLatitude")), float(e.get("nuLongitude"))
    except (TypeError, ValueError):
        return None
    ult = monitorar_para_s(e.get("dtUltimaAtualizacao"))
    # Data no futuro (há uma estação com 2066) não é "atualizada agora".
    if ult is not None and ult > agora + 3600:
        ult = None
    atual = e.get("indiceQualidadeArAtual") or {}
    med = atual.get("medicao") or {}
    t_atual = monitorar_para_s(med.get("dtMedicao"))
    if t_atual is not None and t_atual > agora + 3600:
        t_atual = None
    polu = {} if acumulado is None else {k: dict(v, med=dict(v["med"])) for k, v in acumulado.get("polu", {}).items()}
    for p in e.get("poluentes") or []:
        nome = p.get("noPoluente")
        if not isinstance(nome, str):
            continue
        alvo = polu.setdefault(nome, {"desc": p.get("dsPoluente"), "med": {}})
        for m in p.get("medicoes") or []:
            t = monitorar_para_s(m.get("dtMedicao"))
            v = num(m.get("indiceQualidadeAr"))
            if t is None or v is None or t > agora + 3600:
                continue
            cl = (m.get("classificacaoIqAr") or {}).get("id")
            alvo["med"][str(int(t))] = [v, cl if isinstance(cl, int) else None, m.get("stDadoValidado") is True]
    for alvo in polu.values():
        alvo["med"] = {t: v for t, v in alvo["med"].items() if agora - int(t) <= JANELA_S + 3600}
    # O "última atualização" do cadastro às vezes fica atrás das próprias medições.
    for alvo in polu.values():
        for t in alvo["med"]:
            ult = max(ult or 0, int(t))
    mun = e.get("municipio") or {}
    return {
        "id": e.get("idEstacao"),
        "nome": e.get("noEstacao"),
        "mun": mun.get("noMunicipio"),
        "uf": mun.get("sgUf"),
        "lat": lat,
        "lon": lon,
        "orgao": e.get("noFonteDados"),
        "ult": ult,
        "atual": None
        if t_atual is None or num(med.get("indiceQualidadeAr")) is None
        else {
            "t": t_atual,
            "iqar": num(med.get("indiceQualidadeAr")),
            "cl": (atual.get("classificacaoIqAr") or {}).get("id"),
            "nomeCl": (atual.get("classificacaoIqAr") or {}).get("noClassificacao"),
            "pol": (atual.get("poluente") or {}).get("noPoluente"),
        },
        "polu": polu,
    }


def atualizar_monitorar(estado: dict, agora: float) -> str | None:
    m = estado.setdefault("monitorar", {})
    if agora - m.get("em", 0) < MONITORAR_S and m.get("estacoes"):
        return None
    todas = baixar_json(f"{MONITORAR}/estacao/todas", timeout=90)
    al = [e for e in todas if isinstance(e, dict) and na_amazonia_legal(e)]
    ids = [str(e["idEstacao"]) for e in al if isinstance(e.get("idEstacao"), int)]
    detalhes = {}
    if ids:
        for e in baixar_json(f"{MONITORAR}/estacao/por-ids?ids={','.join(ids)}", timeout=90):
            if isinstance(e, dict) and isinstance(e.get("idEstacao"), int):
                detalhes[e["idEstacao"]] = e
    anteriores = m.get("estacoes", {})
    estacoes = {}
    for e in al:
        d = detalhes.get(e["idEstacao"], e)
        lida = ler_estacao_monitorar(d, agora, anteriores.get(str(e["idEstacao"])))
        if lida:
            estacoes[str(e["idEstacao"])] = lida
    m["estacoes"] = estacoes
    m["em"] = agora
    vivas = sum(1 for e in estacoes.values() if e["ult"] and agora - e["ult"] <= JANELA_S)
    return f"{len(estacoes)} estações na Amazônia Legal, {vivas} com dado nas últimas 48 h"


def publicar_monitorar(m: dict, agora: float) -> dict:
    h0, horas = grade_horaria(agora)
    lista = []
    for e in sorted(m.get("estacoes", {}).values(), key=lambda e: (e.get("uf") or "", e.get("mun") or "", e.get("nome") or "")):
        polu = []
        for nome, p in sorted(e["polu"].items()):
            pontos = [(int(t), v) for t, v in p["med"].items()]
            iqar = serie_na_grade([(t, v[0]) for t, v in pontos], h0, 3600, horas, arredondar=False)
            if all(v is None for v in iqar):
                continue
            cl = serie_na_grade([(t, v[1]) for t, v in pontos], h0, 3600, horas, arredondar=False)
            val = serie_na_grade([(t, 1 if v[2] else 0) for t, v in pontos], h0, 3600, horas, arredondar=False)
            polu.append({"nome": nome, "desc": p.get("desc"), "iqar": iqar, "cl": cl, "val": val})
        a = e.get("atual")
        lista.append(
            {
                "id": e["id"],
                "nome": e["nome"],
                "mun": e["mun"],
                "uf": e["uf"],
                "lat": e["lat"],
                "lon": e["lon"],
                "orgao": e["orgao"],
                "ult": ms(e["ult"]) if e.get("ult") else None,
                "atual": None if not a else dict(a, t=ms(a["t"])),
                "polu": polu,
            }
        )
    return {"h0": ms(h0), "horas": horas, "lista": lista}


# ---------------------------------------------------------------------------
# CAMS global (Open-Meteo)


def url_cams(celulas) -> str:
    q = urllib.parse.urlencode(
        {
            "latitude": ",".join(f"{c[1]:g}" for c in celulas),
            "longitude": ",".join(f"{c[0]:g}" for c in celulas),
            "hourly": ",".join(VARS_CAMS),
            "past_days": 2,
            "forecast_days": 1,
            "domains": "cams_global",
            "timezone": "GMT",
            "timeformat": "unixtime",
        }
    )
    return f"{OPEN_METEO}/v1/air-quality?{q}"


def baixar_grade_cams(celulas) -> dict:
    """Todas as células, em lotes; qualquer lote falhando derruba a grade inteira (fica a anterior)."""
    tempos = None
    vals = {v: [] for v in VARS_CAMS}
    unidades = {}
    for i in range(0, len(celulas), LOTE):
        lote = celulas[i : i + LOTE]
        cru = baixar_json(url_cams(lote), timeout=120)
        if isinstance(cru, dict):
            cru = [cru]
        if not isinstance(cru, list) or len(cru) != len(lote):
            raise ValueError(f"lote {i // LOTE}: {len(cru) if isinstance(cru, list) else '?'} respostas para {len(lote)} pontos")
        for r in cru:
            h = r.get("hourly") or {}
            t = h.get("time")
            if tempos is None:
                tempos = t
            elif t != tempos:
                raise ValueError("horários diferentes entre pontos")
            for v in VARS_CAMS:
                vals[v].append([num(x, 3) for x in h.get(v) or [None] * len(t)])
            unidades.update(r.get("hourly_units") or {})
        time.sleep(0.5)
    return {"tempos": tempos or [], "celulas": celulas, "vals": vals, "unidades": {v: unidades.get(v, "") for v in VARS_CAMS}}


def atualizar_cams(estado: dict, agora: float, anel) -> str | None:
    c = estado.setdefault("cams", {})
    run = c.get("run")
    if agora - c.get("meta_em", 0) >= META_S:
        try:
            meta = baixar_json(f"{OPEN_METEO}/data/cams_global/static/meta.json")
            c["meta_em"] = agora
            run = meta.get("last_run_initialisation_time")
            c["disponivel"] = meta.get("last_run_availability_time")
        except Exception as e:  # noqa: BLE001 — sem meta, vale o relógio de 6 h
            print(f"cams: meta.json falhou — {e}", file=sys.stderr)
    precisa = run != c.get("baixado_run") or agora - c.get("baixado_em", 0) >= CAMS_MAX_S or not os.path.exists(CAMS_BRUTO)
    c["run"] = run
    if not precisa:
        return None
    celulas = grade_cams(anel)
    t0 = time.time()
    bruto = baixar_grade_cams(celulas)
    bruto["run"] = run
    escrever_atomico(CAMS_BRUTO, json.dumps(bruto, separators=(",", ":")).encode())
    c["baixado_em"] = agora
    c["baixado_run"] = run
    return f"grade do CAMS baixada: {len(celulas)} células, {len(bruto['tempos'])} horas ({time.time() - t0:.0f} s)"


def publicar_cams(agora: float, run) -> tuple[dict, dict] | None:
    bruto = ler_json(CAMS_BRUTO, None)
    if not bruto or not bruto.get("tempos"):
        return None
    h0, horas = grade_horaria(agora)
    tempos = bruto["tempos"]
    # Só as 48 h até a hora corrente: a previsão para depois de agora não entra na camada.
    idx = [tempos.index(h0 + k * 3600) if (h0 + k * 3600) in tempos else None for k in range(horas)]

    def recorte(serie):
        return [serie[i] if i is not None and i < len(serie) else None for i in idx]

    series = {v: [recorte(s) for s in bruto["vals"][v]] for v in VARS_CAMS}
    base = {
        "versao": VERSAO,
        "gerado": iso(agora),
        "fonte": "CAMS global (ECMWF/Copernicus) via Open-Meteo — modelo, não medição",
        "atribuicao": "Contains modified Copernicus Atmosphere Monitoring Service information",
        "run": ms(bruto["run"]) if isinstance(bruto.get("run"), (int, float)) else None,
        "passo": PASSO_CAMS,
        "h0": ms(h0),
        "horas": horas,
        "celulas": bruto["celulas"],
    }
    mapa = dict(base, pm2_5=series["pm2_5"])
    completo = dict(base, vars=VARS_CAMS, unidades=bruto.get("unidades", {}), series=series)
    return mapa, completo


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


def escrever_json(nome: str, dado) -> int:
    corpo = json.dumps(dado, separators=(",", ":"), ensure_ascii=False).encode()
    escrever_atomico(os.path.join(PASTA, nome), corpo)
    return len(corpo)


def marcar(fontes: dict, nome: str, agora: float, erro: Exception | None) -> None:
    f = fontes.setdefault(nome, {"em": None, "erro": None})
    if erro is None:
        f["em"] = ms(agora)
        f["erro"] = None
    else:
        f["erro"] = str(erro)[:300]


def rodada(agora: float | None = None) -> dict:
    agora = agora if agora is not None else time.time()
    os.makedirs(PASTA, exist_ok=True)
    estado = ler_json(ESTADO, {})
    fontes = estado.setdefault("fontes", {})
    anel = carregar_anel()
    conf = ler_conf()
    falhas = 0
    tentadas = 0

    def tentar(nome: str, fn) -> None:
        nonlocal falhas, tentadas
        tentadas += 1
        try:
            r = fn()
            if r:
                print(f"{nome}: {r}")
                marcar(fontes, nome, agora, None)
        except Exception as e:  # noqa: BLE001 — uma fonte fora não derruba as outras
            falhas += 1
            marcar(fontes, nome, agora, e)
            print(f"{nome}: falhou — {e}", file=sys.stderr)

    tentar("ufac", lambda: atualizar_ufac(estado, agora))
    tentar("redear", lambda: atualizar_redear(estado, agora, anel))
    tentar("airgradient", lambda: atualizar_airgradient(estado, agora, anel))
    # As duas com chave: sem arquivo de chave, `nota` = "sem chave" e nada é pedido.
    tentar("purpleair", lambda: atualizar_purpleair(estado, agora, anel, conf, fontes))
    tentar("openaq", lambda: atualizar_openaq(estado, agora, anel, conf, fontes))
    tentar("monitorar", lambda: atualizar_monitorar(estado, agora))
    tentar("cams", lambda: atualizar_cams(estado, agora, anel))

    sensores, municipios = publicar_ufac(estado.get("ufac", {}), agora)
    sensores["lista"] = juntar_sensores(
        {
            "ufac": sensores["lista"],
            "redear": publicar_redear(estado.get("redear", {}), agora),
            "purpleair": publicar_purpleair(estado.get("purpleair", {}), agora),
            "airgradient": publicar_airgradient(estado.get("airgradient", {}), agora),
            "openaq": publicar_openaq(estado.get("openaq", {}), agora),
        }
    )
    estacoes = publicar_monitorar(estado.get("monitorar", {}), agora)
    tamanho = escrever_json(
        "pontos.json",
        {
            "versao": VERSAO,
            "gerado": iso(agora),
            "sensores": sensores,
            "municipios": municipios,
            "estacoes": estacoes,
            "fontes": fontes,
        },
    )
    publicado = f"pontos.json {tamanho // 1024} KB"

    # A grade do modelo só é reescrita quando a hora vira ou a grade muda.
    c = estado.get("cams", {})
    marca = [grade_horaria(agora)[0], c.get("baixado_em")]
    if c.get("publicado") != marca or not os.path.exists(os.path.join(PASTA, "cams.json")):
        cams = publicar_cams(agora, c.get("run"))
        if cams:
            publicado += f", cams.json {escrever_json('cams.json', cams[0]) // 1024} KB"
            publicado += f", cams-series.json {escrever_json('cams-series.json', cams[1]) // 1024} KB"
            c["publicado"] = marca

    escrever_atomico(ESTADO, json.dumps(estado, separators=(",", ":")).encode())
    vivos: dict = {}
    for s in sensores["lista"]:
        if agora * 1000 - s["ult"]["t"] <= JANELA_S * 1000:
            vivos[s["fonte"]] = vivos.get(s["fonte"], 0) + 1
    return {
        "falhas": falhas,
        "tentadas": tentadas,
        "sensores": len(sensores["lista"]),
        "vivos": sum(vivos.values()),
        "vivosPorFonte": vivos,
        "estacoes": len(estacoes["lista"]),
        "publicado": publicado,
    }


def main() -> None:
    r = rodada()
    por_fonte = ", ".join(f"{k} {v}" for k, v in sorted(r["vivosPorFonte"].items()))
    print(
        f"rodada: {r['sensores']} sensores ({r['vivos']} com dado em 48 h: {por_fonte}), {r['estacoes']} estações oficiais, "
        f"{r['falhas']} fontes falharam · {r['publicado']}"
    )
    sys.exit(1 if r["falhas"] == r["tentadas"] else 0)


if __name__ == "__main__":
    main()
