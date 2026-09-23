#!/usr/bin/env python3
"""
Qualidade do ar para o AmaView: um espelho de três fontes, sem reprocessar.

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

Nada é filtrado, suavizado ou reclassificado: os números saem como as fontes
publicam (o PM2,5 dos sensores só perde os resíduos de ponto flutuante, com
duas casas). A faixa de qualidade do ar dos sensores e do modelo é calculada
no navegador, com a tabela documentada no AmaView.

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

# Amazônia Legal: os nove estados; o Maranhão só a oeste do meridiano de 44°W.
UFS_AL = {"AC", "AM", "AP", "PA", "RO", "RR", "TO", "MT", "MA"}
MERIDIANO_MA = -44.0
BRT = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------------
# Rede


def baixar(url: str, tentativas: int = 3, timeout: int = 60) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE, "Accept": "application/json"})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(pedido, timeout=timeout) as r:
                return r.read()
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
    """'2026-09-23T18:53:46Z' → epoch s; None se não servir."""
    if not isinstance(texto, str):
        return None
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    falhas = 0

    try:
        print(f"ufac: {atualizar_ufac(estado, agora)}")
        marcar(fontes, "ufac", agora, None)
    except Exception as e:  # noqa: BLE001 — uma fonte fora não derruba as outras
        falhas += 1
        marcar(fontes, "ufac", agora, e)
        print(f"ufac: falhou — {e}", file=sys.stderr)

    try:
        r = atualizar_monitorar(estado, agora)
        if r:
            print(f"monitorar: {r}")
            marcar(fontes, "monitorar", agora, None)
    except Exception as e:  # noqa: BLE001
        falhas += 1
        marcar(fontes, "monitorar", agora, e)
        print(f"monitorar: falhou — {e}", file=sys.stderr)

    try:
        r = atualizar_cams(estado, agora, carregar_anel())
        if r:
            print(f"cams: {r}")
            marcar(fontes, "cams", agora, None)
    except Exception as e:  # noqa: BLE001
        falhas += 1
        marcar(fontes, "cams", agora, e)
        print(f"cams: falhou — {e}", file=sys.stderr)

    sensores, municipios = publicar_ufac(estado.get("ufac", {}), agora)
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
    vivos = sum(1 for s in sensores["lista"] if agora * 1000 - s["ult"]["t"] <= JANELA_S * 1000)
    return {"falhas": falhas, "sensores": len(sensores["lista"]), "vivos": vivos, "estacoes": len(estacoes["lista"]), "publicado": publicado}


def main() -> None:
    r = rodada()
    print(
        f"rodada: {r['sensores']} sensores ({r['vivos']} com dado em 48 h), {r['estacoes']} estações oficiais, "
        f"{r['falhas']} fontes falharam · {r['publicado']}"
    )
    sys.exit(1 if r["falhas"] == 3 else 0)


if __name__ == "__main__":
    main()
