#!/usr/bin/env python3
"""
Fumaça do GOES-19 para o AmaView: a máscara `Smoke` do produto ABI-L2-ADPF da
NOAA (detecção de aerossóis, disco completo, 10 em 10 min) virada contorno.

- **Só o que a NOAA marcou.** `Smoke == 1` (fumaça presente) vira polígono;
  nada é filtrado, suavizado além do contorno ou reclassificado. O contorno é
  o de meio caminho entre pixel com e sem fumaça (marching squares), na grade
  de 2 km do próprio produto.
- **Leve.** O arquivo (~4 MB) é baixado para a MEMÓRIA, lido só nas linhas do
  setor NSA (o `Smoke` vem em blocos de 48 linhas) e descartado: nenhum netCDF
  toca o disco. Um quadro vira um GeoJSON de dezenas de KB (~0,2 s).
- **48 h, no máximo.** Os quadros mais velhos que isso são apagados a cada
  execução; o que falta na janela é preenchido do mais novo para o mais velho.
- Roda pelo `amaview-fumaca.timer` (a cada 2 min). A NOAA publica cada quadro
  ~14 min depois do início da varredura.

Saída (abaixo de FUMACA_RAIZ, servida pelo nginx em /fumaca/v1/):

  fumaca/v1/indice.json                 quadros disponíveis (o app lê a cada minuto)
  fumaca/v1/quadros/AAAADDDHHMM.geojson  um por horário (imutável)

  fumaca.py                  uma rodada (o que o timer chama)
  fumaca.py quadro ARQ.nc    processa um arquivo local e imprime o resumo (teste)
"""
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("FUMACA_RAIZ", "/var/cache/amaview-fumaca")
PASTA = os.path.join(RAIZ, "fumaca", f"v{VERSAO}")
QUADROS = os.path.join(PASTA, "quadros")
ESTADO = os.path.join(RAIZ, "estado.json")
BALDE = "https://noaa-goes19.s3.amazonaws.com"
PRODUTO = "ABI-L2-ADPF"
AGENTE = "AmaView-fumaca (+https://helvecioneto.github.io/AmaView/)"

JANELA = timedelta(hours=48)
# Por rodada: o quadro novo nunca espera o preenchimento do passado.
MAX_POR_RODADA = 36
# Hora com quadro faltando: listar de novo no S3 no máximo a cada 30 min
# (as duas horas mais recentes são listadas em toda rodada).
RELISTAR_S = 1800
# Arquivo que falhou (download ou leitura): tentar de novo só depois disto,
# para um arquivo quebrado não ser baixado a cada 2 min.
ESPERA_FALHA_S = 1800

# Setor NSA do STAR (ângulos de varredura das bordas; `src/config/georef.json`
# do AmaView). Recortar aqui é o que deixa a leitura barata: 2161 × 3437 de
# 5424 × 5424 pixels.
NSA = {"xMin": -0.040572, "xMax": 0.161028, "yMin": -0.06048, "yMax": 0.06048}
# E só a parte oeste dele: a leste de 30°W o setor mostra o Atlântico e a
# borda do disco sobre a África, onde o pixel tem dezenas de km e o ADP marca
# como fumaça a poeira do Saara. Medido em 23/09/2026 14:50 UTC: 303 das 315
# áreas (88 mil de 89 mil km²) estavam lá, e nenhuma na Amazônia que o
# AmaView mostra.
LON_LESTE = -30.0
# Passo da grade grossa em que a longitude é calculada (16 km: o corte cai no mar).
PASSO_GROSSO = 8

ARQUIVO = re.compile(r"OR_ABI-L2-ADPF-M\d_G19_s(\d{4})(\d{3})(\d{2})(\d{2})\d{3}_e\d{14}_c\d{14}\.nc$")


def carimbo_de(t: datetime) -> str:
    """AAAADDDHHMM (dia juliano, UTC), como nos arquivos do STAR."""
    return f"{t.year:04d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"


def instante(carimbo: str) -> datetime:
    ano, dia, hora, minuto = int(carimbo[:4]), int(carimbo[4:7]), int(carimbo[7:9]), int(carimbo[9:11])
    if not (1 <= dia <= 366 and hora < 24 and minuto < 60):
        raise ValueError(carimbo)
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def carimbo_do_arquivo(nome: str) -> str | None:
    """Início da varredura (s…) do nome do arquivo, no minuto: o mesmo horário do quadro do STAR."""
    m = ARQUIVO.search(nome)
    return "".join(m.groups()) if m else None


def prefixo_da_hora(t: datetime) -> str:
    return f"{PRODUTO}/{t.year:04d}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"


# ---------------------------------------------------------------------------
# Grade fixa do ABI → lon/lat (GOES-R PUG, vol. 4, 7.1.2.8.1)


def geos_para_lonlat(x, y, proj):
    """Ângulos de varredura (rad) → (lon, lat) em graus; fora do disco sai NaN."""
    import numpy as np

    req, rpol = proj["semi_major_axis"], proj["semi_minor_axis"]
    H = proj["perspective_point_height"] + req
    lon0 = math.radians(proj["longitude_of_projection_origin"])
    sx, cx, sy, cy = np.sin(x), np.cos(x), np.sin(y), np.cos(y)
    a = sx**2 + cx**2 * (cy**2 + (req**2 / rpol**2) * sy**2)
    b = -2 * H * cx * cy
    c = H**2 - req**2
    with np.errstate(invalid="ignore"):
        rs = (-b - np.sqrt(b**2 - 4 * a * c)) / (2 * a)
    px, py, pz = rs * cx * cy, -rs * sx, rs * cx * sy
    lat = np.degrees(np.arctan((req**2 / rpol**2) * pz / np.sqrt((H - px) ** 2 + py**2)))
    lon = np.degrees(lon0 - np.arctan(py / (H - px)))
    return lon, lat


# ---------------------------------------------------------------------------
# Máscara → polígonos


def enxugar(anel):
    """Tira os vértices colineares (o contorno da grade vem cheio deles); mantém o anel fechado."""
    import numpy as np

    p = anel[:-1] if len(anel) > 1 and np.array_equal(anel[0], anel[-1]) else anel
    if len(p) < 3:
        return p
    ant, seg = np.roll(p, 1, axis=0), np.roll(p, -1, axis=0)
    cruz = (p[:, 0] - ant[:, 0]) * (seg[:, 1] - p[:, 1]) - (p[:, 1] - ant[:, 1]) * (seg[:, 0] - p[:, 0])
    p = p[np.abs(cruz) > 1e-9]
    return np.vstack([p, p[:1]]) if len(p) >= 3 else p[:0]


def area_assinada(anel) -> float:
    """Área (graus², com sinal: + anti-horário) — só para a orientação."""
    x, y = anel[:, 0], anel[:, 1]
    return 0.5 * float((x[:-1] * y[1:] - x[1:] * y[:-1]).sum())


def area_km2(anel) -> float:
    """Área aproximada (km²) de um anel lon/lat: equirretangular local, erro < 1% no tamanho de uma pluma."""
    import numpy as np

    lat0 = math.radians(float(anel[:, 1].mean()))
    x = anel[:, 0] * 111.320 * math.cos(lat0)
    y = anel[:, 1] * 110.574
    return abs(0.5 * float((x[:-1] * y[1:] - x[1:] * y[:-1]).sum()))


def poligonos(mascara, col0: int, lin0: int, eixo_x, eixo_y, proj, casas: int = 3):
    """
    Máscara booleana (recorte começando em col0, lin0 da grade do arquivo) →
    lista de polígonos [[anel externo, buracos…], …] em lon/lat, cada um com a
    área em km². `eixo_x(col)`/`eixo_y(lin)` dão o ângulo de varredura.
    """
    import contourpy
    import numpy as np

    if not mascara.any():
        return []
    # Borda de zeros: toda mancha vira anel fechado, inclusive a que toca o recorte.
    z = np.pad(mascara.astype(np.float32), 1)
    gerador = contourpy.contour_generator(z=z, fill_type=contourpy.FillType.OuterOffset, name="serial")
    pontos, offsets = gerador.filled(0.5, 2.0)
    saida = []
    for pts, offs in zip(pontos, offsets):
        aneis = []
        for i in range(len(offs) - 1):
            anel = enxugar(pts[offs[i] : offs[i + 1]])
            if len(anel) < 4:
                continue
            lon, lat = geos_para_lonlat(eixo_x(anel[:, 0] - 1 + col0), eixo_y(anel[:, 1] - 1 + lin0), proj)
            ll = np.round(np.column_stack([lon, lat]), casas)
            if not np.isfinite(ll).all():
                continue  # no limbo do disco (não acontece sobre a América do Sul)
            # O arredondamento pode colar vértices vizinhos.
            manter = np.ones(len(ll), bool)
            manter[1:] = np.any(ll[1:] != ll[:-1], axis=1)
            ll = ll[manter]
            if len(ll) < 4:
                continue
            # RFC 7946: externo anti-horário, buracos horário.
            if (area_assinada(ll) > 0) != (not aneis):
                ll = ll[::-1]
            aneis.append(ll)
        if aneis and len(aneis[0]):
            km2 = area_km2(aneis[0]) - sum(area_km2(b) for b in aneis[1:])
            saida.append((aneis, max(km2, 0.0)))
    return saida


def processar(nc: bytes | str) -> tuple[dict, dict]:
    """netCDF do ADPF (bytes ou caminho) → (GeoJSON, resumo {n, km2, cob})."""
    import h5py
    import numpy as np

    with h5py.File(io.BytesIO(nc) if isinstance(nc, bytes) else nc, "r") as f:
        def eixo(nome):
            v = f[nome]
            return float(v.attrs["scale_factor"][0]), float(v.attrs["add_offset"][0]), v.shape[0]

        xs, xo, nx = eixo("x")
        ys, yo, ny = eixo("y")
        # Índices do recorte NSA (x cresce com a coluna; y decresce com a linha).
        c0 = max(0, math.floor((NSA["xMin"] - xo) / xs))
        c1 = min(nx, math.ceil((NSA["xMax"] - xo) / xs) + 1)
        l0 = max(0, math.floor((NSA["yMax"] - yo) / ys))
        l1 = min(ny, math.ceil((NSA["yMin"] - yo) / ys) + 1)
        fumaca = f["Smoke"][l0:l1, c0:c1]
        fill = int(f["Smoke"].attrs["_FillValue"][0])
        p = f["goes_imager_projection"].attrs
        proj = {k: float(p[k][0]) for k in ("semi_major_axis", "semi_minor_axis", "perspective_point_height", "longitude_of_projection_origin")}

    eixo_x = lambda col: xo + col * xs  # noqa: E731
    eixo_y = lambda lin: yo + lin * ys  # noqa: E731
    # Área de interesse numa grade grossa: no disco e a oeste de LON_LESTE.
    g = PASSO_GROSSO
    gx, gy = np.meshgrid(eixo_x(np.arange(c0, c1, g) + g / 2), eixo_y(np.arange(l0, l1, g) + g / 2))
    with np.errstate(invalid="ignore"):
        lon = geos_para_lonlat(gx, gy, proj)[0]
        area = np.isfinite(lon) & (lon <= LON_LESTE)
    # Cobertura: fração da área em que a NOAA tentou detectar fumaça. De noite
    # (e fora do ângulo de sol do algoritmo) o `Smoke` vem todo vazio: "sem
    # fumaça" e "sem dado" não são a mesma coisa.
    validos = (fumaca[::g, ::g][: area.shape[0], : area.shape[1]] != fill) & area
    cob = float(validos.sum() / max(1, area.sum()))
    dentro = np.repeat(np.repeat(area, g, axis=0), g, axis=1)[: fumaca.shape[0], : fumaca.shape[1]]

    polis = poligonos((fumaca == 1) & dentro, c0, l0, eixo_x, eixo_y, proj)
    feicoes = [
        {
            "type": "Feature",
            "properties": {"km2": round(km2)},
            "geometry": {"type": "Polygon", "coordinates": [a.tolist() for a in aneis]},
        }
        for aneis, km2 in polis
    ]
    total = round(sum(k for _, k in polis))
    return {"type": "FeatureCollection", "features": feicoes}, {"n": len(feicoes), "km2": total, "cob": round(cob, 3)}


# ---------------------------------------------------------------------------
# S3 da NOAA (balde público, sem credencial)


def baixar(url: str, tentativas: int = 3) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(pedido, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
            time.sleep(1 + 2 * i)
    raise RuntimeError("inalcançável")


def listar(prefixo: str) -> dict[str, str]:
    """carimbo → chave no S3 dos arquivos de uma hora."""
    xml = baixar(f"{BALDE}/?list-type=2&prefix={prefixo}").decode()
    out = {}
    for chave in re.findall(r"<Key>([^<]+)</Key>", xml):
        c = carimbo_do_arquivo(chave)
        if c:
            out[c] = chave
    return out


# ---------------------------------------------------------------------------
# Disco: quadros, índice e limpeza


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


def limpar(agora: datetime) -> int:
    """Apaga quadros fora das 48 h e restos de escritas interrompidas."""
    removidos = 0
    limite = agora - JANELA
    for nome in os.listdir(QUADROS):
        caminho = os.path.join(QUADROS, nome)
        carimbo = nome.split(".", 1)[0]
        try:
            velho = instante(carimbo) < limite
        except ValueError:
            velho = True
        if velho or (".parcial-" in nome and time.time() - os.path.getmtime(caminho) > 600):
            os.remove(caminho)
            removidos += 1
    return removidos


def montar_indice(resumos: dict[str, dict], agora: datetime) -> dict:
    """Índice só com o que está no disco (a fonte da verdade são os arquivos)."""
    no_disco = {n.split(".", 1)[0] for n in os.listdir(QUADROS) if n.endswith(".geojson")}
    quadros = []
    for c in sorted(no_disco):
        r = resumos.get(c)
        if r is None:  # estado perdido: o resumo sai do próprio arquivo
            gj = ler_json(os.path.join(QUADROS, f"{c}.geojson"), {"features": []})
            r = {"n": len(gj["features"]), "km2": sum(f["properties"].get("km2", 0) for f in gj["features"]), "cob": None}
            resumos[c] = r
        quadros.append({"c": c, **r})
    return {
        "versao": VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "NOAA GOES-19 ABI L2 Aerosol Detection (ADPF), máscara Smoke",
        "cadencia_min": 10,
        "quadros": quadros,
    }


def horas_da_janela(agora: datetime) -> list[datetime]:
    base = agora.replace(minute=0, second=0, microsecond=0)
    n = int(JANELA.total_seconds() // 3600)
    return [base - timedelta(hours=k) for k in range(n + 1)]


def rodada(agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    os.makedirs(QUADROS, exist_ok=True)
    estado = ler_json(ESTADO, {})
    listadas: dict[str, float] = estado.get("listadas", {})
    resumos: dict[str, dict] = estado.get("resumos", {})
    removidos = limpar(agora)
    no_disco = {n.split(".", 1)[0] for n in os.listdir(QUADROS) if n.endswith(".geojson")}
    limite = agora - JANELA

    # O que já foi listado e ainda não processado (passou do limite da rodada,
    # ou falhou) continua na fila: sem isto, a hora listada esperaria 30 min
    # para ser vista de novo.
    pendentes: dict[str, str] = dict(estado.get("pendentes", {}))
    # Quais horas listar: as duas mais novas sempre; as outras só se falta
    # quadro e a última olhada foi há mais de 30 min.
    for k, hora in enumerate(horas_da_janela(agora)):
        prefixo = prefixo_da_hora(hora)
        esperados = {carimbo_de(hora + timedelta(minutes=m)) for m in range(0, 60, 10)}
        falta = any(c not in no_disco and instante(c) >= limite and instante(c) <= agora for c in esperados)
        if k >= 2 and (not falta or time.time() - listadas.get(prefixo, 0) < RELISTAR_S):
            continue
        try:
            pendentes.update(listar(prefixo))
            listadas[prefixo] = time.time()
        except Exception as e:  # noqa: BLE001 — uma hora falhando não para as outras
            print(f"falha ao listar {prefixo}: {e}", file=sys.stderr)

    falhados: dict[str, float] = {c: t for c, t in estado.get("falhados", {}).items() if time.time() - t < ESPERA_FALHA_S}
    fila = sorted((c for c in pendentes if c not in no_disco and instante(c) >= limite), reverse=True)
    feitos_agora: set[str] = set()
    feitos, falhas = 0, 0
    for c in [c for c in fila if c not in falhados][:MAX_POR_RODADA]:
        t0 = time.time()
        try:
            nc = baixar(f"{BALDE}/{pendentes[c]}")
            gj, resumo = processar(nc)
            del nc  # 4 MB: nunca vai para o disco
            escrever_atomico(os.path.join(QUADROS, f"{c}.geojson"), json.dumps(gj, separators=(",", ":")).encode())
            resumos[c] = resumo
            feitos_agora.add(c)
            feitos += 1
            print(f"{c}: {resumo['n']} áreas, {resumo['km2']} km², cobertura {resumo['cob']:.0%} ({time.time() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            falhas += 1
            falhados[c] = time.time()
            print(f"{c}: falhou — {e}", file=sys.stderr)

    indice = montar_indice(resumos, agora)
    escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(indice, separators=(",", ":"), ensure_ascii=False).encode())
    vivos = {q["c"] for q in indice["quadros"]}
    corte = time.time() - JANELA.total_seconds() - 3600
    estado = {
        "listadas": {p: t for p, t in listadas.items() if t > corte},
        "resumos": {c: r for c, r in resumos.items() if c in vivos},
        "pendentes": {c: pendentes[c] for c in fila if c not in feitos_agora},
        "falhados": falhados,
    }
    escrever_atomico(ESTADO, json.dumps(estado).encode())
    return {"novos": feitos, "falhas": falhas, "fila": len(fila) - feitos, "removidos": removidos, "quadros": len(vivos)}


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "quadro":
        t0 = time.time()
        gj, resumo = processar(sys.argv[2])
        corpo = json.dumps(gj, separators=(",", ":"))
        print(f"{resumo} · {len(corpo) / 1024:.0f} KB · {time.time() - t0:.2f} s")
        return
    r = rodada()
    print(f"rodada: {r['novos']} novos, {r['falhas']} falhas, {r['fila']} na fila, {r['removidos']} apagados, {r['quadros']} quadros em 48 h")
    sys.exit(1 if r["falhas"] and not r["novos"] else 0)


if __name__ == "__main__":
    main()
