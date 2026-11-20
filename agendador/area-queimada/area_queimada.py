#!/usr/bin/env python3
"""
Área atingida pelo fogo na Amazônia Legal, ESTIMADA a partir dos focos do
INPE, para o AmaView: os focos dos satélites polares de um período (3, 7, 15,
30 e 60 dias) agrupados por vizinhança e contornados.

- **Estimativa do AmaView, não o mapeamento oficial de área queimada** (o INPE
  mapeia a cicatriz por imagem, em outro produto). O que entra é o que o INPE
  publica — posição, satélite, horário e município de cada foco —, sem
  correção; o contorno é a conta abaixo, e o app diz isso.
- **Só satélites polares**: VIIRS (NOAA-20, NOAA-21, NPP; pixel de 375 m) e
  MODIS (AQUA e TERRA; 1 km). O GOES-19 e o MSG têm pixel de 2 a 4 km na
  região: desenhariam a grade deles, não a forma do fogo (`--com-goes` liga o
  GOES-19, para comparar). O METOP (AVHRR) fica de fora.
- **Como a área é montada.** Numa projeção de área igual (senoidal centrada
  em 58° O), cada foco vira o pixel do satélite como um disco de mesma área
  (375 m → raio de 212 m; 1 km → 564 m). Focos cujos pixels ficam a até LIGA
  (500 m) um do outro são a mesma área: os discos crescem LIGA/2, se unem e
  encolhem LIGA/2 (um "fechamento", que preenche o vão entre pixels vizinhos
  sem inchar a borda de fora). A área de cada contorno é medida nessa
  projeção, antes da simplificação (SIMPLIFICA, 40 m) usada só no desenho.
- **Fontes.** Diário do INPE (~1 mês publicado) e, para os dias mais velhos, o
  mensal (lido em fluxo, sem tocar o disco). Os dias fechados ficam guardados
  só com a Amazônia Legal (~230 KB/dia): depois do preenchimento, uma rodada
  baixa só os diários ainda abertos (hoje e ontem), e só se mudaram.
- **Uma vez por dia, às 3h de Brasília** (`amaview-area-queimada.timer`),
  quando quase ninguém usa o AmaView. Medido na Oracle (20/11/2026): ~170 s de
  um núcleo para os 5 períodos (105 s nos 60 dias) e 42 s para o
  preenchimento inicial (359 MB do INPE).

Saída (abaixo de AREA_RAIZ, servida pelo nginx em /area-queimada/v1/):

  area-queimada/v1/indice.json                    o que há (o app lê na abertura)
  area-queimada/v1/AAAAMMDD_HHMM/dN.json          um por período (imutável; formato em CAMPOS)

  area_queimada.py                  uma rodada (o que o timer chama, às 3h)
  area_queimada.py --raiz DIR       idem, guardando em DIR (teste local)
  area_queimada.py --com-goes       inclui o GOES-19 no contorno (comparação)
"""
import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("AREA_RAIZ", "/var/cache/amaview-area-queimada")
INPE = "https://dataserver-coids.inpe.br/queimadas/queimadas/focos/csv"
AGENTE = "AmaView-area-queimada (+https://amaview.com.br/)"
AQUI = os.path.dirname(os.path.abspath(__file__))

PERIODOS = (3, 7, 15, 30, 60)
GUARDA_DIAS = max(PERIODOS) + 1
# Gerações que continuam no disco: quem abriu o app antes das 3h ainda pede a de ontem.
GERACOES_GUARDADAS = 2

# Satélite → (código no arquivo do dia, classe). Classe: 1 VIIRS, 2 MODIS,
# 3 GOES-19, 0 os demais (guardados para contar, nunca no contorno).
SATELITES = (
    "NOAA-20", "NOAA-21", "NPP-375", "NPP-375D",
    "AQUA_M-T", "AQUA_M-M", "TERRA_M-T", "TERRA_M-M",
    "GOES-19", "MSG-03", "METOP-B", "METOP-C",
)
CLASSE = {"NOAA-20": 1, "NOAA-21": 1, "NPP-375": 1, "NPP-375D": 1,
          "AQUA_M-T": 2, "AQUA_M-M": 2, "TERRA_M-T": 2, "TERRA_M-M": 2,
          "GOES-19": 3}
OUTRO = 255
# Lado nominal do pixel (m) por classe.
PIXEL = {1: 375.0, 2: 1000.0, 3: 2000.0}
NOME_CLASSE = {1: "viirs", 2: "modis", 3: "goes"}

LIGA = 500.0          # vão máximo entre pixels de uma mesma área (m)
SIMPLIFICA = 40.0     # tolerância do desenho (m)
LON0 = -58.0          # meridiano central da senoidal
R_TERRA = 6371007.2   # raio autálico (m)

DIARIO = re.compile(r"focos_diario_br_(\d{8})\.csv")


# ---------------------------------------------------------------------------
# Projeção de área igual
# ---------------------------------------------------------------------------

def projetar(lon, lat):
    import numpy as np

    phi = np.radians(lat)
    return R_TERRA * np.radians(np.asarray(lon) - LON0) * np.cos(phi), R_TERRA * phi


def desprojetar(xy):
    """(N, 2) senoidal → (N, 2) lon/lat."""
    import numpy as np

    phi = xy[:, 1] / R_TERRA
    lon = LON0 + np.degrees(xy[:, 0] / (R_TERRA * np.cos(phi)))
    return np.column_stack([lon, np.degrees(phi)])


# ---------------------------------------------------------------------------
# Leitura do CSV do INPE (diário e mensal têm as mesmas colunas)
# ---------------------------------------------------------------------------

def ler_focos(linhas, codigos: set[str], nomes: dict) -> dict[str, dict]:
    """
    Focos da Amazônia Legal (por `municipio_id`) separados por dia UTC.
    `linhas` é qualquer iterável de linhas de texto (arquivo inteiro ou fluxo).
    `nomes` recebe `municipio_id → [município, UF]` como o INPE publica.
    """
    leitor = csv.reader(linhas)
    cab = [c.strip().lstrip("﻿") for c in next(leitor)]
    i_lat, i_lon, i_t, i_sat = (cab.index(c) for c in ("lat", "lon", "data_hora_gmt", "satelite"))
    i_mun, i_frp = cab.index("municipio_id"), cab.index("frp") if "frp" in cab else -1
    i_nome, i_uf = cab.index("municipio"), cab.index("estado")
    codigo_sat = {s: i for i, s in enumerate(SATELITES)}
    dias: dict[str, dict] = {}
    for linha in leitor:
        if len(linha) <= max(i_lat, i_lon, i_t, i_sat, i_mun):
            continue
        mun = linha[i_mun].strip()
        if mun not in codigos:
            continue
        try:
            lat, lon = float(linha[i_lat]), float(linha[i_lon])
            data = linha[i_t].strip()
            t = datetime.strptime(data[:19], "%Y-%m-%d %H:%M:%S") if len(data) > 10 else datetime.strptime(data, "%Y-%m-%d")
        except ValueError:
            continue
        frp = math.nan
        if 0 <= i_frp < len(linha) and linha[i_frp].strip():
            try:
                frp = float(linha[i_frp])
            except ValueError:
                pass
        if frp < 0:  # −999 = sem dado
            frp = math.nan
        dia = t.strftime("%Y%m%d")
        d = dias.setdefault(dia, {"lat": [], "lon": [], "t": [], "sat": [], "frp": [], "mun": []})
        d["lat"].append(lat)
        d["lon"].append(lon)
        d["t"].append(int(t.replace(tzinfo=timezone.utc).timestamp()))
        d["sat"].append(codigo_sat.get(linha[i_sat].strip(), OUTRO))
        d["frp"].append(frp)
        d["mun"].append(int(mun))
        if mun not in nomes:
            nomes[mun] = [linha[i_nome].strip(), linha[i_uf].strip()]
    return dias


def para_numpy(d: dict) -> dict:
    import numpy as np

    return {
        "lat": np.asarray(d["lat"], np.float64),
        "lon": np.asarray(d["lon"], np.float64),
        "t": np.asarray(d["t"], np.int64),
        "sat": np.asarray(d["sat"], np.uint8),
        "frp": np.asarray(d["frp"], np.float32),
        "mun": np.asarray(d["mun"], np.int32),
    }


VAZIO = {"lat": [], "lon": [], "t": [], "sat": [], "frp": [], "mun": []}


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------

def pedir(url: str, metodo: str = "GET", tentativas: int = 3):
    pedido = urllib.request.Request(url, method=metodo, headers={"User-Agent": AGENTE})
    for i in range(tentativas):
        try:
            return urllib.request.urlopen(pedido, timeout=120)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
            time.sleep(2 + 3 * i)
    raise RuntimeError("inalcançável")


def cabecalhos(url: str) -> tuple[str, str]:
    with pedir(url, "HEAD") as r:
        return r.headers.get("Last-Modified", ""), r.headers.get("Content-Length", "")


def instante_http(valor: str) -> datetime | None:
    try:
        return datetime.strptime(valor, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def listar_diarios() -> set[str]:
    with pedir(f"{INPE}/diario/Brasil/") as r:
        return set(DIARIO.findall(r.read().decode("utf-8", "replace")))


# ---------------------------------------------------------------------------
# Arquivo dos dias (só Amazônia Legal)
# ---------------------------------------------------------------------------

def pasta_dias(raiz: str) -> str:
    return os.path.join(raiz, "arquivo")


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


def guardar_dia(raiz: str, dia: str, d: dict) -> None:
    import numpy as np

    buf = io.BytesIO()
    np.savez_compressed(buf, **para_numpy(d))
    escrever_atomico(os.path.join(pasta_dias(raiz), f"{dia}.npz"), buf.getvalue())


def abrir_dia(raiz: str, dia: str) -> dict | None:
    import numpy as np

    try:
        with np.load(os.path.join(pasta_dias(raiz), f"{dia}.npz")) as z:
            return {k: z[k] for k in z.files}
    except (FileNotFoundError, ValueError, OSError):
        return None


def atualizar_arquivo(raiz: str, agora: datetime, log=print) -> dict:
    """
    Garante no arquivo os dias UTC da janela (GUARDA_DIAS), do diário quando
    ele ainda os publica e do mensal quando não. Devolve o resumo da rodada.
    """
    os.makedirs(pasta_dias(raiz), exist_ok=True)
    estado_arq = os.path.join(raiz, "estado.json")
    estado = ler_json(estado_arq, {})
    nomes_arq = os.path.join(pasta_dias(raiz), "municipios.json")
    nomes = ler_json(nomes_arq, {})
    with open(os.path.join(AQUI, "municipios_amazonia_legal.json")) as f:
        codigos = set(json.load(f))

    hoje = agora.date()
    dias = [(hoje - timedelta(days=k)).strftime("%Y%m%d") for k in range(GUARDA_DIAS)]
    publicados = listar_diarios()
    baixados, bytes_ = 0, 0
    faltam_mensal: dict[str, list[str]] = {}

    for dia in dias:
        e = estado.get(dia, {})
        no_disco = os.path.exists(os.path.join(pasta_dias(raiz), f"{dia}.npz"))
        if e.get("fechado") and no_disco:
            continue
        if dia in publicados:
            url = f"{INPE}/diario/Brasil/focos_diario_br_{dia}.csv"
            lm, cl = cabecalhos(url)
            if no_disco and e.get("lm") == lm and e.get("cl") == cl:
                continue
            with pedir(url) as r:
                corpo = r.read()
            bytes_ += len(corpo)
            baixados += 1
            por_dia = ler_focos(io.StringIO(corpo.decode("utf-8-sig", "replace")), codigos, nomes)
            # O diário de D traz só D; o que vier fora é ignorado.
            guardar_dia(raiz, dia, por_dia.get(dia, VAZIO))
            t_lm = instante_http(lm)
            fim_do_dia = datetime.strptime(dia, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1, hours=12)
            estado[dia] = {"fonte": "diario", "lm": lm, "cl": cl, "fechado": bool(t_lm and t_lm >= fim_do_dia)}
        elif not no_disco or not e.get("fechado"):
            faltam_mensal.setdefault(dia[:6], []).append(dia)

    for mes, dias_mes in sorted(faltam_mensal.items()):
        url = f"{INPE}/mensal/Brasil/focos_mensal_br_{mes}.csv"
        try:
            lm, cl = cabecalhos(url)
        except urllib.error.HTTPError as erro:
            log(f"mensal {mes}: HTTP {erro.code}; {len(dias_mes)} dia(s) sem dado")
            continue
        log(f"mensal {mes}: {int(cl or 0) / 1e6:.0f} MB para {len(dias_mes)} dia(s)")
        with pedir(url) as r:
            por_dia = ler_focos(io.TextIOWrapper(r, encoding="utf-8-sig", errors="replace", newline=""), codigos, nomes)
        bytes_ += int(cl or 0)
        baixados += 1
        t_lm = instante_http(lm)
        for dia in dias_mes:
            guardar_dia(raiz, dia, por_dia.get(dia, VAZIO))
            # Como no diário: o dia está completo no mensal atualizado depois das
            # 12h UTC do dia seguinte (o mensal do mês corrente cresce todo dia,
            # mas os dias passados não mudam mais).
            fim_do_dia = datetime.strptime(dia, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1, hours=12)
            estado[dia] = {"fonte": "mensal", "lm": lm, "cl": cl, "fechado": bool(t_lm and t_lm >= fim_do_dia)}

    # Fora da janela: apaga.
    manter = set(dias)
    for nome in os.listdir(pasta_dias(raiz)):
        if nome.endswith(".npz") and nome[:8] not in manter:
            os.remove(os.path.join(pasta_dias(raiz), nome))
    estado = {k: v for k, v in estado.items() if k in manter}
    escrever_atomico(estado_arq, json.dumps(estado, indent=1, sort_keys=True).encode())
    escrever_atomico(nomes_arq, json.dumps(nomes, ensure_ascii=False, sort_keys=True).encode())
    return {"baixados": baixados, "bytes": bytes_, "dias": dias}


def juntar(raiz: str, dias: list[str]) -> dict:
    import numpy as np

    partes = [d for d in (abrir_dia(raiz, dia) for dia in dias) if d is not None]
    if not partes:
        return {k: np.asarray(v) for k, v in para_numpy(VAZIO).items()}
    return {k: np.concatenate([p[k] for p in partes]) for k in partes[0]}


# ---------------------------------------------------------------------------
# Áreas
# ---------------------------------------------------------------------------

def grupos(x, y, raio, liga: float, lote: int = 50_000):
    """
    Rótulo do grupo de cada pixel: dois pixels são do mesmo grupo quando as
    bordas ficam a até `liga` (encadeado). Pares consultados em lotes (a lista
    inteira de 60 dias passava de 2 GB); componentes por propagação do menor
    rótulo com salto de ponteiro.
    """
    import numpy as np
    import shapely

    n = len(x)
    pontos = shapely.points(x, y)
    arvore = shapely.STRtree(pontos)
    rmax = float(raio.max())
    pares_i, pares_j = [], []
    for a in range(0, n, lote):
        b = min(n, a + lote)
        i, j = arvore.query(pontos[a:b], predicate="dwithin", distance=raio[a:b] + rmax + liga)
        i = i + a
        m = i < j
        i, j = i[m], j[m]
        perto = np.hypot(x[i] - x[j], y[i] - y[j]) <= raio[i] + raio[j] + liga
        pares_i.append(i[perto].astype(np.int32))
        pares_j.append(j[perto].astype(np.int32))
    i = np.concatenate(pares_i) if pares_i else np.zeros(0, np.int32)
    j = np.concatenate(pares_j) if pares_j else np.zeros(0, np.int32)
    rot = np.arange(n, dtype=np.int32)
    while True:
        menor = np.minimum(rot[i], rot[j])
        novo = rot.copy()
        np.minimum.at(novo, i, menor)
        np.minimum.at(novo, j, menor)
        while True:
            pulo = novo[novo]
            if np.array_equal(pulo, novo):
                break
            novo = pulo
        if np.array_equal(novo, rot):
            break
        rot = novo
    return np.unique(rot, return_inverse=True)[1].ravel()


def areas(focos: dict, desde: int, ate: int, classes=(1, 2), nomes: dict | None = None,
          liga: float = LIGA, simplifica: float = SIMPLIFICA, min_focos: int = 1) -> dict:
    """
    Áreas dos focos com `desde < t ≤ ate` das classes dadas, da maior para a
    menor: contorno (lon/lat), área medida, contagens, horários e municípios.
    """
    import numpy as np
    import shapely

    nomes = nomes or {}
    sat = focos["sat"]
    classe = np.zeros(len(sat), np.uint8)
    for nome, c in CLASSE.items():
        classe[sat == SATELITES.index(nome)] = c
    sel = (focos["t"] > desde) & (focos["t"] <= ate) & np.isin(classe, classes)
    if not sel.any():
        return {"areas": [], "municipios": []}
    lat, lon, t = focos["lat"][sel], focos["lon"][sel], focos["t"][sel]
    cl, frp, mun = classe[sel], focos["frp"][sel], focos["mun"][sel]
    x, y = projetar(lon, lat)

    # O mesmo pixel em várias passagens vira um disco só (grade de 25 m por classe).
    chave = np.column_stack([np.round(x / 25.0), np.round(y / 25.0), cl.astype(np.float64)])
    unicos, inverso = np.unique(chave, axis=0, return_inverse=True)
    inverso = inverso.ravel()
    ux, uy, ucl = unicos[:, 0] * 25.0, unicos[:, 1] * 25.0, unicos[:, 2].astype(np.uint8)
    raio = np.array([PIXEL.get(c, 375.0) for c in range(4)])[ucl] / math.sqrt(math.pi)

    rot_u = grupos(ux, uy, raio, liga)
    n = int(rot_u.max()) + 1
    # Contorno de cada grupo: o pixel isolado é o próprio disco (o fechamento
    # não o muda), vetorizado; os demais, união dos discos crescidos de liga/2,
    # encolhida de liga/2. (Um buffer de MultiPoint por grupo, vetorizado,
    # mediu 5× mais lento e 3× mais memória nos 60 dias.)
    pontos = shapely.points(ux, uy)
    ordem_u = np.argsort(rot_u, kind="stable")
    lim_u = np.searchsorted(rot_u[ordem_u], np.arange(n + 1))
    sozinho = np.diff(lim_u) == 1
    contornos = np.empty(n, dtype=object)
    um = ordem_u[lim_u[:-1][sozinho]]
    contornos[sozinho] = shapely.buffer(pontos[um], raio[um], quad_segs=4)
    for k in np.flatnonzero(~sozinho):
        ids = ordem_u[lim_u[k]:lim_u[k + 1]]
        uniao = shapely.union_all(shapely.buffer(pontos[ids], raio[ids] + liga / 2, quad_segs=4))
        contornos[k] = shapely.buffer(uniao, -liga / 2, quad_segs=4)
    area_km2 = shapely.area(contornos) / 1e6
    desenho = shapely.transform(shapely.simplify(contornos, simplifica, preserve_topology=True), desprojetar)

    rotulo = rot_u[inverso]
    ordem_t = np.argsort(rotulo, kind="stable")
    limites = np.searchsorted(rotulo[ordem_t], np.arange(n + 1))
    mun_indice: dict[int, int] = {}
    saida = []
    for k in np.argsort(-area_km2):
        idx = ordem_t[limites[k]:limites[k + 1]]
        if len(idx) < min_focos or shapely.is_empty(desenho[k]):
            continue
        geom = codificar(desenho[k])
        if not geom:
            continue
        cont_cl = np.bincount(cl[idx], minlength=4)
        mun_k, mun_n = np.unique(mun[idx], return_counts=True)
        topo = np.argsort(-mun_n, kind="stable")[:3]
        lista_mun = []
        for j in topo:
            lista_mun += [mun_indice.setdefault(int(mun_k[j]), len(mun_indice)), int(mun_n[j])]
        frp_k = frp[idx]
        saida.append([
            round(float(area_km2[k]), 2),
            int(len(idx)),
            [int(cont_cl[c]) for c in classes],
            int((t[idx].min() - desde) // 60),
            int((t[idx].max() - desde) // 60),
            None if np.all(np.isnan(frp_k)) else round(float(np.nanmax(frp_k)), 1),
            int(len(mun_k)),
            lista_mun,
            geom,
        ])
    # [nome como o INPE publica, código IBGE] (a UF são os 2 primeiros dígitos).
    municipios = [None] * len(mun_indice)
    for cod, i in mun_indice.items():
        municipios[i] = [nomes.get(str(cod), [""])[0], cod]
    return {"areas": saida, "municipios": municipios}


def codificar(geom) -> list:
    """
    Polígono/multipolígono em lon/lat → [[anel, …], …] (um item por polígono,
    o 1º anel é o de fora). Anel: inteiros em 1e-4° (~11 m), o 1º vértice
    absoluto e os outros como diferença do anterior, sem repetir o 1º no fim.
    """
    import numpy as np
    import shapely

    poligonos = []
    for parte in shapely.get_parts(geom):
        aneis = []
        for anel in shapely.get_rings(parte):
            q = np.round(shapely.get_coordinates(anel)[:-1] * 1e4).astype(np.int64)
            if len(q) == 0:
                continue
            manter = np.ones(len(q), bool)
            manter[1:] = np.any(np.diff(q, axis=0) != 0, axis=1)
            q = q[manter]
            if len(q) < 3:
                continue
            d = np.diff(q, axis=0, prepend=np.zeros((1, 2), np.int64))
            aneis.append(d.ravel().tolist())
        if aneis and len(aneis[0]) >= 6:
            poligonos.append(aneis)
    return poligonos


CAMPOS = ["area", "focos", "satelites", "ini", "fim", "frp", "nmun", "mun", "geom"]


def escrever_periodo(caminho: str, dias: int, desde: int, ate: int, classes, resultado: dict) -> int:
    dado = json.dumps({
        "versao": VERSAO,
        "dias": dias,
        "desde": desde * 1000,
        "ate": ate * 1000,
        "satelites": [NOME_CLASSE[c] for c in classes],
        "campos": CAMPOS,
        "municipios": resultado["municipios"],
        "areas": resultado["areas"],
    }, ensure_ascii=False, separators=(",", ":")).encode()
    escrever_atomico(caminho, dado)
    return len(dado)


# ---------------------------------------------------------------------------
# Rodada
# ---------------------------------------------------------------------------

def rodada(raiz: str = RAIZ, agora: datetime | None = None, classes=(1, 2), min_focos: int = 1, log=print) -> dict:
    agora = agora or datetime.now(timezone.utc)
    t0 = time.monotonic()
    resumo_arq = atualizar_arquivo(raiz, agora, log)
    t_arq = time.monotonic() - t0
    focos = juntar(raiz, resumo_arq["dias"])
    nomes = ler_json(os.path.join(pasta_dias(raiz), "municipios.json"), {})
    ate = int(agora.timestamp())

    carimbo = agora.strftime("%Y%m%d_%H%M")
    saida = os.path.join(raiz, "area-queimada", f"v{VERSAO}")
    geracao = os.path.join(saida, carimbo)
    os.makedirs(geracao, exist_ok=True)
    periodos = []
    for dias in PERIODOS:
        t1 = time.monotonic()
        desde = ate - dias * 86400
        resultado = areas(focos, desde, ate, classes, nomes, min_focos=min_focos)
        tam = escrever_periodo(os.path.join(geracao, f"d{dias}.json"), dias, desde, ate, classes, resultado)
        lista = resultado["areas"]
        periodos.append({
            "dias": dias,
            "arquivo": f"{carimbo}/d{dias}.json",
            "areas": len(lista),
            "focos": sum(a[1] for a in lista),
            "km2": round(sum(a[0] for a in lista), 1),
            "bytes": tam,
            "segundos": round(time.monotonic() - t1, 2),
        })
        log(f"{dias:>2} dias: {periodos[-1]['focos']:>7} focos → {len(lista):>6} áreas, "
            f"{periodos[-1]['km2']:>9.1f} km², {tam / 1e6:5.2f} MB, {periodos[-1]['segundos']:.1f} s")

    presentes = focos["t"][focos["t"] <= ate]
    indice = {
        "versao": VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dadosAte": int(presentes.max()) * 1000 if len(presentes) else None,
        "fonte": "INPE — Programa Queimadas (focos); contorno estimado pelo AmaView",
        "satelites": [NOME_CLASSE[c] for c in classes],
        "parametros": {"ligaM": LIGA, "simplificaM": SIMPLIFICA, "minFocos": min_focos},
        "periodos": periodos,
    }
    escrever_atomico(os.path.join(saida, "indice.json"), json.dumps(indice, ensure_ascii=False, indent=1).encode())

    geracoes = sorted(d for d in os.listdir(saida) if re.fullmatch(r"\d{8}_\d{4}", d))
    for velha in geracoes[:-GERACOES_GUARDADAS]:
        shutil.rmtree(os.path.join(saida, velha), ignore_errors=True)
    total = time.monotonic() - t0
    log(f"arquivo: {resumo_arq['baixados']} download(s), {resumo_arq['bytes'] / 1e6:.1f} MB, {t_arq:.1f} s; "
        f"{len(focos['t'])} focos guardados; total {total:.1f} s")
    return indice


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raiz", default=RAIZ)
    ap.add_argument("--com-goes", action="store_true", help="inclui o GOES-19 no contorno")
    ap.add_argument("--agora", help="instante de referência (AAAA-MM-DDTHH:MMZ), para teste")
    ap.add_argument("--min-focos", type=int, default=1, help="focos mínimos para uma área (padrão 1)")
    args = ap.parse_args()
    agora = datetime.strptime(args.agora, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) if args.agora else None
    rodada(args.raiz, agora, (1, 2, 3) if args.com_goes else (1, 2), args.min_focos)


if __name__ == "__main__":
    sys.exit(main())
