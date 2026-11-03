#!/usr/bin/env python3
"""
Rastreio da chuva por satélite para o AmaView: o pyForTraCC sobre o GSMaP_NOW
que o `amaview-gsmap` já guarda (recorte NSA, 442 × 594 pontos de 0,1°),
acompanhando cada área contínua com chuva de pelo menos 0,1 mm/h de um
horário para o outro.

- **Sem download.** Lê os quadros de `gsmap/v1/quadros/` (um byte por ponto;
  `q > 0` ⇔ chuva ≥ 0,1 mm/h, exato: ver `codificar` no gsmap.py). Roda sem
  rede nenhuma (`docker run --network none`).
- **Sem imagem nova.** Roda na imagem do rastreio de nuvens (`pyfortracc_ir`,
  a mesma biblioteca), com este arquivo montado só para leitura: o disco não
  ganha nada além dos quadros publicados (~20 MB nas 48 h).
- **Só quando há horário novo.** O `amaview-rastreio-chuva.timer` olha a cada
  5 min; o `ExecCondition` (`rastreio.py precisa`, só biblioteca padrão, no
  Python da máquina) compara o índice do GSMaP com o publicado e só então o
  container sobe. Medido em 03/11/2026: ~4,5 s de processador por horário
  novo, < 650 MB de memória.
- **O dado da JAXA, sem retoque.** A taxa de chuva é a do GSMaP (mm/h, média
  da hora); o contorno é a borda dos pontos de 0,1°, como a grade é. Sem
  previsão: só o que foi observado.
- Um horário que chega depois de um mais novo já rastreado (preenchimento
  atrasado) fica de fora: o rastreio só anda para a frente.

Saída (abaixo de RASTREIO_PUBLICA, servida pelo nginx em /rastreio-chuva/vN/):

  rastreio-chuva/v1/indice.json                     quadros (o app lê a cada minuto)
  rastreio-chuva/v1/quadros/AAAAMMDDHHNN.geojson    contornos (k: "c") e trajetórias (k: "t")

AAAAMMDDHHNN é o início da hora do GSMaP (UTC), o mesmo nome do quadro da
chuva. Um quadro publicado nunca muda; o índice é refeito do disco.

Dados abertos (QGIS, scripts; documentados em amaview.com.br/dados), refeitos a
cada rodada a partir dos quadros já publicados, com nomes de campo legíveis:

  rastreio-chuva/v1/latest.geojson                  as áreas do horário mais novo
  rastreio-chuva/v1/last-24h.geojson                as das últimas 24 h (campo quadro_inicio)

  rastreio.py              uma rodada (o que o container roda)
  rastreio.py precisa      sai 0 se há horário novo para rastrear, 1 se não (ExecCondition)
"""
import gzip
import json
import math
import os
import pathlib
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# Sobe quando o conteúdo dos quadros muda (cache imutável de 1 ano no navegador).
VERSAO = 1
GSMAP = pathlib.Path(os.environ.get("RASTREIO_GSMAP", "/gsmap"))
DADOS = pathlib.Path(os.environ.get("RASTREIO_DADOS", "/dados"))
PUBLICA = pathlib.Path(os.environ.get("RASTREIO_PUBLICA", "/publica")) / "rastreio-chuva" / f"v{VERSAO}"

JANELA = timedelta(hours=48)
# História de cada sistema na ficha: até 24 pontos (12 h de 30 em 30 min).
PONTOS_DA_HISTORIA = 24
# Tabelas do pyForTraCC guardadas: a história e o horário anterior bastam.
GUARDAR_TABELAS = timedelta(hours=13)
# Entrada do pyForTraCC: os últimos horários (o anterior liga o novo).
JANELA_DE_ENTRADA = 4
# Primeira rodada (sem estado): as 48 h inteiras, em blocos.
LOTE = 24

LIMIAR = 0.1  # mm/h
# Áreas menores que 5 pontos (~600 km²) não viram sistema: chuva isolada de um ou dois pontos.
MIN_PONTOS = 5
CADENCIA = 30  # min entre horários do GSMaP_NOW
DURACAO = 60   # min de cada taxa (média da hora)

# Grade do recorte (gsmap.py): centro do primeiro ponto em (22,05°N; 89,35°W),
# linha a linha para o sul. O pyForTraCC quer as BORDAS e a linha 0 ao sul.
NI, NJ, PASSO = 442, 594, 0.1
LAT_N, LON_O = 22.05 + PASSO / 2, -89.35 - PASSO / 2
FATOR, SEM_DADO = 20, 255

# Coordenadas nas bordas dos pontos de 0,1°: duas casas são exatas.
CASAS = 2
KM_POR_GRAU_LAT = 110.574
KM_POR_GRAU_LON = 111.320
FMT_PYF = "%Y%m%d_%H%M"
FMT_C = "%Y%m%d%H%M"


def agora():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ horários
def horarios_do_gsmap():
    """{carimbo: arquivo} dos horários no índice do GSMaP (o ajustado por pluviômetros quando há)."""
    try:
        with open(GSMAP / "indice.json") as f:
            indice = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for q in indice.get("quadros", []):
        c, p = q.get("c"), q.get("p")
        if not isinstance(c, str) or len(c) != 12 or p not in ("g", "s"):
            continue
        arq = GSMAP / "quadros" / f"{c}-{p}.bin"
        if arq.is_file():
            out[c] = (arq, p)
    return out


def ultimo_publicado():
    """Carimbo do quadro mais novo publicado, ou None."""
    pasta = PUBLICA / "quadros"
    nomes = [f.stem for f in pasta.glob("*.geojson")] if pasta.is_dir() else []
    return max(nomes) if nomes else None


def ultimo_rastreado():
    """Último horário que o pyForTraCC ligou (o estado dele), ou None."""
    try:
        with open(DADOS / "saida/track/state/state.json") as f:
            return datetime.strptime(json.load(f)["last_stamp"], "%Y-%m-%d %H:%M:%S").strftime(FMT_C)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def novos(horarios, ultimo, limite):
    """Carimbos depois de `ultimo` e dentro da janela, em ordem."""
    return sorted(c for c in horarios if (ultimo is None or c > ultimo) and c >= limite)


def limite_da_janela():
    return (agora() - JANELA).strftime(FMT_C)


def precisa():
    """Há horário novo do GSMaP para rastrear (ou publicar)?"""
    horarios = horarios_do_gsmap()
    if not horarios:
        return False
    return max(horarios) > (ultimo_publicado() or "")


# ------------------------------------------------------------------ pyForTraCC
def ler(path):
    """Quadro do GSMaP → mm/h (NaN sem dado), com a linha 0 ao sul, como o pyForTraCC conta."""
    import numpy as np

    q = np.fromfile(path, dtype=np.uint8).reshape(NI, NJ)
    v = (q.astype(np.float32) / FATOR) ** 2
    v[q == SEM_DADO] = np.nan
    return v[::-1]


def name_list(entrada, saida, tmp):
    return {
        "input_path": str(entrada) + "/",
        "output_path": str(saida) + "/",
        "temp_folder": str(tmp) + "/",
        "timestamp_pattern": "%Y%m%d%H%M",
        "pattern_position": [0, 12],
        "thresholds": [LIMIAR],
        "min_cluster_size": [MIN_PONTOS],
        "operator": ">=",
        "delta_time": CADENCIA,
        "delta_tolerance": 0,
        "lat_min": LAT_N - NI * PASSO,
        "lat_max": LAT_N,
        "lon_min": LON_O,
        "lon_max": LON_O + NJ * PASSO,
        "y_dim": NI,
        "x_dim": NJ,
        "y_res": PASSO,
        "x_res": PASSO,
        "spl_correction": True,
        "mrg_correction": True,
        "inc_correction": True,
        "opt_correction": True,
        "elp_correction": True,
        "new_correction": True,
        "new_neighbors": 3,
        "validation": True,
        "n_jobs": 1,
        "persist_uid": True,
        # A correção pelo fluxo óptico e a validação dos vetores leem os pontos
        # de cada sistema na tabela (o pyForTraCC recusa sem eles). Ela só fica 13 h.
        "save_arrays": True,
    }


def rastrear(horarios, carimbos):
    """Rastreia `carimbos` (em ordem), com os anteriores na entrada para a ligação."""
    import pyfortracc

    entrada, saida, tmp = DADOS / "entrada", DADOS / "saida", DADOS / "tmp"
    for d in (entrada, saida, tmp):
        d.mkdir(parents=True, exist_ok=True)
    for c in carimbos:
        destino = entrada / f"{c}.bin"
        destino.unlink(missing_ok=True)
        destino.symlink_to(horarios[c][0])
    # O resto da rodada anterior; o estado (track/state) fica.
    shutil.rmtree(saida / "track/processing", ignore_errors=True)
    pyfortracc.track(name_list(entrada, saida, tmp), ler, parallel=False)
    # Ficam os últimos: o pyForTraCC lê o anterior para ligar o próximo.
    ligados = sorted(entrada.glob("*.bin"))
    for f in ligados[:-JANELA_DE_ENTRADA]:
        f.unlink(missing_ok=True)


# ------------------------------------------------------------------ publicação
def num(v, casas):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    v = round(v, casas)
    return int(v) if casas == 0 else v


def area_km2(pontos, lat):
    """Pontos × área de um ponto de 0,1° na latitude do sistema."""
    p = num(pontos, 0)
    if p is None:
        return None
    return round(p * PASSO * KM_POR_GRAU_LAT * PASSO * KM_POR_GRAU_LON * math.cos(math.radians(lat)))


def movimento(u, v, lat):
    """Velocidade (km/h) e rumo (graus do norte, para onde vai) do vetor do pyForTraCC (graus por intervalo)."""
    u, v = num(u, 6), num(v, 6)
    if u is None or v is None:
        return {"vel": None, "rumo": None}
    dx = u * KM_POR_GRAU_LON * math.cos(math.radians(lat))
    dy = v * KM_POR_GRAU_LAT
    vel = math.hypot(dx, dy) * 60 / CADENCIA
    rumo = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return {"vel": round(vel), "rumo": round(rumo) % 360}


def arredondar(geo):
    def andar(c):
        if isinstance(c[0], (int, float)):
            return [round(x, CASAS) for x in c]
        return [andar(x) for x in c]
    return {"type": geo["type"], "coordinates": andar(geo["coordinates"])}


def tabela(c):
    return DADOS / "saida/track/trackingtable" / (datetime.strptime(c, FMT_C).strftime(FMT_PYF) + ".parquet")


def ler_tabela(c, colunas=None):
    import pandas as pd

    return pd.read_parquet(tabela(c), columns=colunas)


def pontos_da_historia(c, memoria):
    """{uid: (km2, pmax, pmed)} da tabela de `c`, lida uma vez por rodada."""
    from shapely import wkt

    if c not in memoria:
        pts = {}
        if tabela(c).is_file():
            for _, r in ler_tabela(c, ["uid", "size", "mean", "max", "geometry"]).iterrows():
                uid = num(r["uid"], 0)
                if uid is None or r["geometry"] is None:
                    continue
                lat = wkt.loads(r["geometry"]).centroid.y
                pts[uid] = (area_km2(r["size"], lat), num(r["max"], 1), num(r["mean"], 2))
        memoria[c] = pts
    return memoria[c]


def historia(c, anteriores, memoria):
    """{uid: {dt, km2, pmax, pmed}} dos horários anteriores (até PONTOS_DA_HISTORIA), o próprio por último."""
    origem = datetime.strptime(c, FMT_C)
    mais_antigo = (origem - timedelta(minutes=CADENCIA * (PONTOS_DA_HISTORIA - 1))).strftime(FMT_C)
    out = {}
    for ant in [a for a in anteriores[-(PONTOS_DA_HISTORIA - 1):] if a >= mais_antigo] + [c]:
        dt = int(round((datetime.strptime(ant, FMT_C) - origem).total_seconds() / 60))
        for uid, (km2, pmax, pmed) in pontos_da_historia(ant, memoria).items():
            h = out.setdefault(uid, {"dt": [], "km2": [], "pmax": [], "pmed": []})
            h["dt"].append(dt)
            h["km2"].append(km2)
            h["pmax"].append(pmax)
            h["pmed"].append(pmed)
    return out


def quadro(c, anteriores, memoria=None):
    """FeatureCollection do horário `c`: contornos com a história, e as trajetórias."""
    from shapely import wkt
    from shapely.geometry import mapping

    t = ler_tabela(c, ["uid", "status", "size", "lifetime", "expansion", "mean", "max", "std", "u_", "v_", "geometry", "trajectory"])
    hist = historia(c, anteriores, {} if memoria is None else memoria)
    contornos, trajetorias = [], []
    for _, r in t.iterrows():
        if r["geometry"] is None:
            continue
        geo = wkt.loads(r["geometry"]).simplify(0)
        if geo.is_empty:
            continue
        lat = geo.centroid.y
        uid = num(r["uid"], 0)
        props = {
            "k": "c",
            "uid": uid,
            "st": r["status"] if isinstance(r["status"], str) else None,
            "vida": num(r["lifetime"], 0),
            "km2": area_km2(r["size"], lat),
            "pmax": num(r["max"], 1),
            "pmed": num(r["mean"], 2),
            "dp": num(r["std"], 2),
            "exp": num(r["expansion"], 1),
        }
        props.update(movimento(r["u_"], r["v_"], lat))
        h = hist.get(uid)
        # Um ponto só (o próprio horário) não é história.
        props["hist"] = h if h and len(h["dt"]) > 1 else None
        contornos.append({"type": "Feature", "properties": props, "geometry": arredondar(mapping(geo))})
        traj = r["trajectory"]
        if isinstance(traj, str) and traj and uid is not None:
            linha = wkt.loads(traj)
            for parte in getattr(linha, "geoms", [linha]):
                if parte.geom_type == "LineString" and len(parte.coords) >= 2:
                    # Centroides: 3 casas (~100 m), como no rastreio de nuvens.
                    g = mapping(parte)
                    trajetorias.append({"type": "Feature", "properties": {"k": "t", "uid": uid},
                                        "geometry": {"type": "LineString",
                                                     "coordinates": [[round(x, 3), round(y, 3)] for x, y in g["coordinates"]]}})
    # Do maior para o menor: o navegador desenha e lista nessa ordem.
    contornos.sort(key=lambda f: -(f["properties"]["km2"] or 0))
    return {"type": "FeatureCollection", "features": contornos + trajetorias}


def gravar(path, corpo):
    """Gravação atômica, legível pelo nginx (644): ninguém lê meio arquivo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(corpo)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def gravar_json(path, dados, comprimido=False):
    """O JSON e, com `comprimido`, o .gz ao lado (gzip_static do nginx: comprime uma vez, não por pedido)."""
    corpo = json.dumps(dados, separators=(",", ":"), ensure_ascii=False).encode()
    if comprimido:
        # Primeiro o .gz: quando o .geojson aparece (é ele que marca o quadro como publicado), o par está completo.
        gravar(path.with_name(path.name + ".gz"), gzip.compress(corpo, 9, mtime=0))
    gravar(path, corpo)


def publicar(horarios):
    """Grava os quadros que têm tabela e ainda não foram publicados."""
    pasta = PUBLICA / "quadros"
    pasta.mkdir(parents=True, exist_ok=True)
    tabelas = sorted(datetime.strptime(f.stem, FMT_PYF).strftime(FMT_C)
                     for f in (DADOS / "saida/track/trackingtable").glob("*.parquet"))
    limite = limite_da_janela()
    feitos, memoria = 0, {}
    for i, c in enumerate(tabelas):
        out = pasta / f"{c}.geojson"
        if c < limite or out.exists():
            continue
        try:
            fc = quadro(c, tabelas[:i], memoria)
        except Exception as e:  # um quadro ruim não segura os seguintes
            log(f"quadro {c} não publicado: {e!r}")
            continue
        gravar_json(out, fc, comprimido=True)
        feitos += 1
    return feitos


def indice(horarios):
    quadros = []
    for f in sorted((PUBLICA / "quadros").glob("*.geojson")):
        try:
            with open(f) as src:
                fc = json.load(src)
        except (OSError, ValueError):
            continue
        sis = [x["properties"] for x in fc["features"] if x["properties"].get("k") == "c"]
        pmax = [p["pmax"] for p in sis if p.get("pmax") is not None]
        quadros.append({
            "c": f.stem,
            # O produto do GSMaP rastreado: o arquivo que existia na hora (o ajustado pode ter chegado depois).
            "p": horarios.get(f.stem, (None, None))[1],
            "n": len(sis),
            "km2": sum(p.get("km2") or 0 for p in sis),
            "pmax": max(pmax) if pmax else None,
        })
    gravar_json(PUBLICA / "indice.json", {
        "versao": VERSAO,
        "gerado": agora().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "GSMaP_NOW (JAXA) · pyForTraCC",
        "credito": "GSMaP data by Japan Aerospace Exploration Agency (JAXA)",
        "cadencia_min": CADENCIA,
        "duracao_min": DURACAO,
        "limiar_mmh": LIMIAR,
        "min_pontos": MIN_PONTOS,
        "grade": {"passo": PASSO},
        "quadros": quadros,
    })


def limpar():
    """Fora da janela: quadros publicados (48 h) e tabelas do pyForTraCC (13 h)."""
    limite = limite_da_janela()
    for f in (PUBLICA / "quadros").glob("*.geojson*"):
        if f.name[:12] < limite:
            f.unlink(missing_ok=True)
    corte = (agora() - GUARDAR_TABELAS).strftime(FMT_PYF)
    for sub in ("trackingtable", "geometry"):
        for f in (DADOS / "saida/track" / sub).rglob("*"):
            if f.is_file() and f.stem[:13] < corte:
                f.unlink(missing_ok=True)


# ------------------------------------------------------------------ dados abertos
# Os mesmos contornos, para quem usa fora do AmaView (QGIS, Python, R). Pedido do
# usuário em 03/11/2026. A JAXA permite redistribuir produto derivado do GSMaP
# com o crédito dela (earth.jaxa.jp/en/data/policy).
ABERTO_JANELA = timedelta(hours=24)
SITUACOES = {"NEW": "nova", "CON": "continua", "SPL": "divisão", "MRG": "fusão"}
PRODUTOS = {"g": "GSMaP_Gauge_NOW", "s": "GSMaP_NOW"}
CREDITO = "GSMaP data by Japan Aerospace Exploration Agency (JAXA); rastreio: pyForTraCC; AmaView (amaview.com.br)"
LICENCA = "CC BY 4.0, com o crédito acima"
DOCUMENTACAO = "https://amaview.com.br/dados"
FMT_ISO = "%Y-%m-%dT%H:%M:%SZ"


def situacao(st):
    """"SPL/MRG" → "divisão e fusão"; sigla desconhecida fica como veio (o mesmo do app)."""
    if not isinstance(st, str) or not st:
        return None
    return " e ".join(SITUACOES.get(x.strip(), x.strip()) for x in st.split("/"))


def iso(c, minutos=0):
    return (datetime.strptime(c, FMT_C) + timedelta(minutes=minutos)).strftime(FMT_ISO)


def feicoes_abertas(c, fc, produto):
    """Os contornos (sem as trajetórias nem a história) de um quadro publicado, com os campos por extenso."""
    out = []
    for f in fc.get("features", []):
        p = f.get("properties") or {}
        if p.get("k") != "c":
            continue
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": {
            "id_area": p.get("uid"),
            "quadro_inicio": iso(c),
            "quadro_fim": iso(c, DURACAO),
            "situacao": situacao(p.get("st")),
            "situacao_codigo": p.get("st"),
            "vida_min": p.get("vida"),
            "area_km2": p.get("km2"),
            "chuva_max_mm_h": p.get("pmax"),
            "chuva_media_mm_h": p.get("pmed"),
            "chuva_dp_mm_h": p.get("dp"),
            "expansao_1e6_s": p.get("exp"),
            "velocidade_km_h": p.get("vel"),
            "rumo_graus": p.get("rumo"),
            "produto": PRODUTOS.get(produto),
        }})
    return out


def colecao(nome, feicoes, de, ate):
    """FeatureCollection com os metadados por fora (membros a mais do GeoJSON: o QGIS ignora, o script lê)."""
    return {
        "type": "FeatureCollection",
        "name": nome,
        "gerado": agora().strftime(FMT_ISO),
        "quadros": {"de": de, "ate": ate},
        "credito": CREDITO,
        "licenca": LICENCA,
        "documentacao": DOCUMENTACAO,
        "limiar_mmh": LIMIAR,
        "features": feicoes,
    }


def dados_abertos(horarios):
    """Refaz o latest.geojson e o last-24h.geojson a partir dos quadros publicados."""
    arquivos = sorted((PUBLICA / "quadros").glob("*.geojson"))
    if not arquivos:
        return
    mais_novo = arquivos[-1].stem
    corte = (datetime.strptime(mais_novo, FMT_C) - ABERTO_JANELA).strftime(FMT_C)
    ultimo, dia = [], []
    for f in arquivos:
        c = f.stem
        if c <= corte:
            continue
        try:
            with open(f) as src:
                fc = json.load(src)
        except (OSError, ValueError):
            continue
        feicoes = feicoes_abertas(c, fc, horarios.get(c, (None, None))[1])
        dia.extend(feicoes)
        if c == mais_novo:
            ultimo = feicoes
    primeiro = next(f.stem for f in arquivos if f.stem > corte)
    gravar_json(PUBLICA / "latest.geojson",
                colecao("AmaView: rastreio da chuva, horário mais novo", ultimo, iso(mais_novo), iso(mais_novo)), comprimido=True)
    gravar_json(PUBLICA / "last-24h.geojson",
                colecao("AmaView: rastreio da chuva, últimas 24 h", dia, iso(primeiro), iso(mais_novo)), comprimido=True)


def rodada():
    t0 = time.time()
    horarios = horarios_do_gsmap()
    if not horarios:
        log("sem índice do GSMaP: nada a fazer")
        return
    ultimo = ultimo_rastreado()
    fila = novos(horarios, ultimo, limite_da_janela())
    if ultimo is None and fila:
        log(f"primeira rodada: {len(fila)} horários")
    for i in range(0, len(fila), LOTE):
        lote = fila[i:i + LOTE]
        rastrear(horarios, lote)
    feitos = publicar(horarios)
    limpar()
    indice(horarios)
    try:
        dados_abertos(horarios)
    except Exception as e:  # o app não depende deles: um erro aqui não derruba a rodada
        log(f"dados abertos não refeitos: {e!r}")
    log(f"rodada: {len(fila)} rastreados, {feitos} publicados; mais novo {ultimo_publicado()} ({time.time() - t0:.1f} s)")


if __name__ == "__main__":
    if sys.argv[1:] == ["precisa"]:
        sys.exit(0 if precisa() else 1)
    rodada()
