#!/usr/bin/env python3
"""
Medições do GOES-19 para o AmaView (camada "Medições", `src/medicoes`): os
produtos L2 da NOAA em PNG, um por quadro, com o valor no byte.

    produto  NOAA           grade   cadência      variável           recorte
    aod      ABI-L2-AODF    2 km    10 min        AOD (adimensional) Amazônia Legal (+1°)
    lst      ABI-L2-LSTF    10 km   1 h           LST (K → °C)       setor NSA
    tpw      ABI-L2-TPWF    10 km   10 min        TPW (mm)           setor NSA
    cape     ABI-L2-DSIF    10 km   10 min        CAPE (J/kg)        setor NSA

- **Na grade da NOAA, sem reamostrar.** A grade fixa do ABI é a projeção do
  mapa do AmaView: o PNG cai no mapa pela caixa (metros na projeção) do índice.
  R = round((valor − deslocamento) / escala), de 0 a 250, e 255 = sem valor (o
  preenchimento da NOAA); G = qualidade unificada (0 alta, 1 média, 2 baixa ou
  degradada, 3 inválida; o `DQF` 0–3 do AOD e do LST como vem, e no TPW e no
  CAPE o `DQF_Overall` 0 → 0, 2–3 → 2, o resto → 3); B = 0. Nada é corrigido.
- **AOD só na Amazônia Legal.** O AOD é de 2 km: o setor NSA inteiro dava
  3600 × 2160 px (~2 MB por quadro). A caixa da Amazônia Legal com 1° de folga
  é ~1/3 disso; o app lê a caixa do índice, então o recorte não muda nada lá.
- **Leve.** O NetCDF (até ~42 MB, o AOD de dia) é baixado para a MEMÓRIA,
  lido só no recorte (os blocos que o cobrem) e descartado: nenhum NetCDF toca
  o disco.
- **48 h, no máximo** (a janela mais longa do loop do app). O que sai da
  janela é apagado a cada rodada; o que falta nela é preenchido do mais novo
  para o mais velho, com teto por rodada para o quadro novo nunca esperar.

Saída (abaixo de GOESL2_RAIZ, servida pelo nginx em /goes-l2/v1/):

  goes-l2/v1/<produto>/indice.json        quadros, caixa, escala (o app lê a cada minuto)
  goes-l2/v1/<produto>/AAAADDDHHMM.png    um por quadro (imutável; o carimbo é o do STAR)

  goes_l2.py                    uma rodada (o que o timer chama)
  goes_l2.py quadro PROD ARQ.nc processa um arquivo local e grava o PNG ao lado (teste)
"""
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

# Sobe quando o conteúdo dos quadros muda (cache imutável de 1 ano no navegador).
VERSAO = 1
RAIZ = os.environ.get("GOESL2_RAIZ", "/var/cache/amaview-goes-l2")
PASTA = os.path.join(RAIZ, "goes-l2", f"v{VERSAO}")
ESTADO = os.path.join(RAIZ, "estado.json")
BALDE = "https://noaa-goes19.s3.amazonaws.com"
AGENTE = "AmaView-goes-l2 (+https://amaview.com.br/)"
SEM_VALOR = 255

JANELA = timedelta(hours=48)
# Hora com quadro faltando: relistar no S3 no máximo a cada 30 min (as duas
# horas mais novas são listadas em toda rodada).
RELISTAR_S = 1800
# Arquivo que falhou: tentar de novo só depois disto.
ESPERA_FALHA_S = 1800
# Tempo de trabalho por rodada (o timer é de 10 min): passado disto, o que
# falta fica para a próxima.
TEMPO_RODADA_S = float(os.environ.get("GOESL2_TEMPO_RODADA_S", "330"))

# Setor NSA do STAR (ângulos de varredura das bordas; `src/config/georef.json`).
NSA = {"xMin": -0.040572, "xMax": 0.161028, "yMin": -0.06048, "yMax": 0.06048}
# Caixa da Amazônia Legal (oeste, sul, leste, norte; `AMAZONIA_LEGAL_BBOX_LONLAT`
# em `src/map/projection.ts`) e a folga do recorte, em graus.
AMAZONIA_LEGAL = (-74.0, -18.1, -43.9, 5.3)
FOLGA_GRAUS = 1.0
# Grade fixa do ABI (GOES-19, GRS80), a mesma dos arquivos.
GEOS = {
    "semi_major_axis": 6378137.0,
    "semi_minor_axis": 6356752.31414,
    "perspective_point_height": 35786023.0,
    "longitude_of_projection_origin": -75.0,
}


class Produto:
    def __init__(self, id, noaa, var, dqf, unidade, escala, deslocamento=0.0, kelvin=False,
                 dqf_geral=False, recorte="nsa", cadencia_min=10, max_por_rodada=24):
        self.id, self.noaa, self.var, self.dqf, self.unidade = id, noaa, var, dqf, unidade
        self.escala, self.deslocamento, self.kelvin, self.dqf_geral = escala, deslocamento, kelvin, dqf_geral
        self.recorte, self.cadencia_min, self.max_por_rodada = recorte, cadencia_min, max_por_rodada


PRODUTOS = {
    # O AOD de dia pesa ~42 MB: 12 por rodada preenchem as 48 h em ~4 h.
    "aod": Produto("aod", "ABI-L2-AODF", "AOD", "DQF", "", 0.02, recorte="amazonia", max_por_rodada=12),
    "lst": Produto("lst", "ABI-L2-LSTF", "LST", "DQF", "°C", 0.25, -10.0, kelvin=True, cadencia_min=60),
    "tpw": Produto("tpw", "ABI-L2-TPWF", "TPW", "DQF_Overall", "mm", 0.4, dqf_geral=True),
    "cape": Produto("cape", "ABI-L2-DSIF", "CAPE", "DQF_Overall", "J/kg", 20.0, dqf_geral=True),
}

ARQUIVO = re.compile(r"OR_(ABI-L2-[A-Z]+)-M\d_G19_s(\d{4})(\d{3})(\d{2})(\d{2})\d{3}_e\d{14}_c\d{14}\.nc$")


def carimbo_de(t: datetime) -> str:
    """AAAADDDHHMM (dia juliano, UTC), como nos arquivos do STAR."""
    return f"{t.year:04d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"


def instante(carimbo: str) -> datetime:
    if not re.fullmatch(r"\d{11}", carimbo):
        raise ValueError(carimbo)
    ano, dia, hora, minuto = int(carimbo[:4]), int(carimbo[4:7]), int(carimbo[7:9]), int(carimbo[9:11])
    if not (1 <= dia <= 366 and hora < 24 and minuto < 60):
        raise ValueError(carimbo)
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def carimbo_do_arquivo(nome: str, noaa: str) -> str | None:
    """Início da varredura (s…) no minuto, se o arquivo é do produto."""
    m = ARQUIVO.search(nome)
    return "".join(m.groups()[1:]) if m and m.group(1) == noaa else None


def prefixo_da_hora(p: Produto, t: datetime) -> str:
    return f"{p.noaa}/{t.year:04d}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"


# ---------------------------------------------------------------------------
# Grade fixa do ABI (GOES-R PUG, vol. 4, 7.1.2.8.1)


def lonlat_para_geos(lon, lat, proj=GEOS):
    """(lon, lat) em graus → ângulos de varredura (x, y) em rad (sweep x)."""
    import numpy as np

    req, rpol = proj["semi_major_axis"], proj["semi_minor_axis"]
    H = proj["perspective_point_height"] + req
    lon0 = math.radians(proj["longitude_of_projection_origin"])
    e2 = (req**2 - rpol**2) / req**2
    lam, phi = np.radians(lon), np.radians(lat)
    phic = np.arctan((rpol**2 / req**2) * np.tan(phi))
    rc = rpol / np.sqrt(1 - e2 * np.cos(phic) ** 2)
    sx = H - rc * np.cos(phic) * np.cos(lam - lon0)
    sy = -rc * np.cos(phic) * np.sin(lam - lon0)
    sz = rc * np.sin(phic)
    return np.arcsin(-sy / np.sqrt(sx**2 + sy**2 + sz**2)), np.arctan(sz / sx)


def angulos_do_recorte(recorte: str) -> dict:
    """Caixa do recorte em ângulos de varredura: o setor NSA, ou a Amazônia Legal com folga."""
    if recorte == "nsa":
        return NSA
    import numpy as np

    o, s, l, n = AMAZONIA_LEGAL
    o, s, l, n = o - FOLGA_GRAUS, s - FOLGA_GRAUS, l + FOLGA_GRAUS, n + FOLGA_GRAUS
    # As bordas da caixa lon/lat viram curvas na projeção: amostra densa delas.
    t = np.linspace(0, 1, 65)
    lon = np.concatenate([o + (l - o) * t, np.full_like(t, l), l - (l - o) * t, np.full_like(t, o)])
    lat = np.concatenate([np.full_like(t, s), s + (n - s) * t, np.full_like(t, n), n - (n - s) * t])
    x, y = lonlat_para_geos(lon, lat)
    caixa = {"xMin": float(x.min()), "xMax": float(x.max()), "yMin": float(y.min()), "yMax": float(y.max())}
    # Nunca além do setor NSA (o mapa do AmaView para nele).
    return {
        "xMin": max(caixa["xMin"], NSA["xMin"]),
        "xMax": min(caixa["xMax"], NSA["xMax"]),
        "yMin": max(caixa["yMin"], NSA["yMin"]),
        "yMax": min(caixa["yMax"], NSA["yMax"]),
    }


# ---------------------------------------------------------------------------
# PNG (só a biblioteca padrão e o numpy: a Oracle não tem o Pillow)


def png(img) -> bytes:
    """uint8 H×W (cinza) ou H×W×3 (RGB) → PNG sem perda, filtro escolhido por linha."""
    import numpy as np

    img = np.ascontiguousarray(img, dtype=np.uint8)
    if img.ndim == 2:
        tipo, c = 0, 1
    elif img.ndim == 3 and img.shape[2] == 3:
        tipo, c = 2, 3
    else:
        raise ValueError(f"forma {img.shape}")
    h, w = img.shape[:2]
    tudo = img.reshape(h, w * c)
    comp = zlib.compressobj(9)
    partes = []
    # Em faixas de linhas: a memória fica em poucos MB mesmo no AOD (~1650 × 1370 × 3).
    for r0 in range(0, h, 128):
        x = tudo[r0 : r0 + 128].astype(np.int16)
        acima = tudo[r0 - 1 : r0].astype(np.int16) if r0 else np.zeros((1, w * c), np.int16)
        esq = np.zeros_like(x)
        esq[:, c:] = x[:, :-c]
        cima = np.vstack([acima, x[:-1]])
        diag = np.zeros_like(x)
        diag[:, c:] = cima[:, :-c]
        p = esq + cima - diag
        pa, pb, pc = np.abs(p - esq), np.abs(p - cima), np.abs(p - diag)
        paeth = np.where((pa <= pb) & (pa <= pc), esq, np.where(pb <= pc, cima, diag))
        # 0 nenhum, 1 Sub, 2 Up, 3 Average, 4 Paeth (PNG, seção 9)
        filtros = np.stack([x, x - esq, x - cima, x - (esq + cima) // 2, x - paeth]).astype(np.uint8)
        # A heurística de sempre: o filtro com a menor soma dos valores como bytes com sinal.
        custo = np.minimum(filtros, 256 - filtros.astype(np.int16)).sum(axis=2)
        escolha = custo.argmin(axis=0)
        linhas = filtros[escolha, np.arange(len(x))]
        partes.append(comp.compress(np.column_stack([escolha.astype(np.uint8), linhas]).tobytes()))
    partes.append(comp.flush())

    def bloco(nome: bytes, dado: bytes) -> bytes:
        return struct.pack(">I", len(dado)) + nome + dado + struct.pack(">I", zlib.crc32(nome + dado) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + bloco(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, tipo, 0, 0, 0))
        + bloco(b"IDAT", b"".join(partes))
        + bloco(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# NetCDF (HDF5) → PNG


def qualidade(p: Produto, dqf):
    import numpy as np

    if not p.dqf_geral:
        return np.clip(dqf, 0, 3).astype(np.uint8)
    q = np.full(dqf.shape, 3, np.uint8)
    q[dqf == 0] = 0
    q[(dqf == 2) | (dqf == 3)] = 2
    return q


def processar(p: Produto, nc) -> tuple[bytes, dict, float]:
    """NetCDF do produto (bytes ou caminho) → (PNG, grade {caixa, largura, altura}, cobertura)."""
    import h5py
    import numpy as np

    angulos = angulos_do_recorte(p.recorte)
    with h5py.File(io.BytesIO(nc) if isinstance(nc, bytes) else nc, "r") as f:
        def coord(nome):
            v = f[nome]
            return v[:].astype(np.float64) * float(v.attrs["scale_factor"][0]) + float(v.attrs["add_offset"][0])

        x, y = coord("x"), coord("y")
        h = float(f["goes_imager_projection"].attrs["perspective_point_height"][0])
        cols = np.nonzero((x >= angulos["xMin"]) & (x <= angulos["xMax"]))[0]
        lins = np.nonzero((y >= angulos["yMin"]) & (y <= angulos["yMax"]))[0]
        if cols.size < 2 or lins.size < 2:
            raise ValueError("o recorte não cobre a grade do arquivo")
        sl = (slice(int(lins[0]), int(lins[-1]) + 1), slice(int(cols[0]), int(cols[-1]) + 1))
        v = f[p.var]
        cru = v[sl]
        preenchimento = v.attrs["_FillValue"][0]
        escala, desloc = float(v.attrs["scale_factor"][0]), float(v.attrs["add_offset"][0])
        dqf = f[p.dqf][sl]
    sem = cru == preenchimento
    valor = cru.astype(np.float64) * escala + desloc
    if p.kelvin:
        valor -= 273.15
    r = np.where(sem, SEM_VALOR, np.clip(np.round((valor - p.deslocamento) / p.escala), 0, 250)).astype(np.uint8)
    g = np.where(sem, SEM_VALOR, qualidade(p, dqf)).astype(np.uint8)
    imagem = png(np.dstack([r, g, np.zeros_like(r)]))
    xs, ys = x[cols], y[lins]
    dx, dy = abs(xs[1] - xs[0]), abs(ys[1] - ys[0])
    caixa = [(xs.min() - dx / 2) * h, (ys.min() - dy / 2) * h, (xs.max() + dx / 2) * h, (ys.max() + dy / 2) * h]
    grade = {"caixa": [round(float(c), 1) for c in caixa], "largura": int(r.shape[1]), "altura": int(r.shape[0])}
    return imagem, grade, round(float((~sem).mean()), 4)


# ---------------------------------------------------------------------------
# S3 da NOAA (balde público, sem credencial)


def baixar(url: str, tentativas: int = 3) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(pedido, timeout=90) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
            time.sleep(1 + 2 * i)
    raise RuntimeError("inalcançável")


def listar(p: Produto, prefixo: str) -> dict[str, str]:
    """carimbo → chave no S3 dos arquivos de uma hora."""
    xml = baixar(f"{BALDE}/?list-type=2&prefix={prefixo}").decode()
    out = {}
    for chave in re.findall(r"<Key>([^<]+)</Key>", xml):
        c = carimbo_do_arquivo(chave, p.noaa)
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


def pasta_do(p: Produto) -> str:
    return os.path.join(PASTA, p.id)


def no_disco(p: Produto) -> set[str]:
    return {n[:-4] for n in os.listdir(pasta_do(p)) if n.endswith(".png") and re.fullmatch(r"\d{11}", n[:-4])}


def limpar(p: Produto, agora: datetime, conhecidos: set[str]) -> int:
    """Apaga quadros fora das 48 h, os que o índice não conhece e restos de escritas interrompidas."""
    removidos = 0
    limite = agora - JANELA
    for nome in os.listdir(pasta_do(p)):
        caminho = os.path.join(pasta_do(p), nome)
        if nome == "indice.json":
            continue
        if ".parcial-" in nome:
            apagar = time.time() - os.path.getmtime(caminho) > 600
        else:
            c = nome[:-4] if nome.endswith(".png") else ""
            try:
                apagar = instante(c) < limite or c not in conhecidos
            except ValueError:
                apagar = True
        if apagar:
            os.remove(caminho)
            removidos += 1
    return removidos


def montar_indice(p: Produto, quadros: dict[str, dict], grade: dict | None, agora: datetime) -> dict:
    return {
        "versao": VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": f"NOAA GOES-19 {p.noaa}",
        "produto": p.id,
        "unidade": p.unidade,
        "escala": p.escala,
        "deslocamento": p.deslocamento,
        "semValor": SEM_VALOR,
        "cadencia_min": p.cadencia_min,
        "grade": grade,
        "quadros": [quadros[c] for c in sorted(quadros)],
    }


def horas_da_janela(agora: datetime) -> list[datetime]:
    base = agora.replace(minute=0, second=0, microsecond=0)
    n = int(JANELA.total_seconds() // 3600)
    return [base - timedelta(hours=k) for k in range(n + 1)]


def rodada_produto(p: Produto, agora: datetime, estado: dict, prazo: float) -> dict:
    os.makedirs(pasta_do(p), exist_ok=True)
    caminho_indice = os.path.join(pasta_do(p), "indice.json")
    anterior = ler_json(caminho_indice, {})
    grade = anterior.get("grade")
    # O índice publicado guarda o resumo (cobertura) de cada quadro; PNG que ele
    # não conhece (estado perdido no meio de uma rodada) sai e volta pela fila.
    quadros = {q["c"]: q for q in anterior.get("quadros", []) if isinstance(q, dict) and "c" in q}
    removidos = limpar(p, agora, set(quadros))
    disco = no_disco(p)
    quadros = {c: q for c, q in quadros.items() if c in disco}
    limite = agora - JANELA

    listadas: dict[str, float] = estado.setdefault("listadas", {})
    pendentes: dict[str, str] = estado.setdefault("pendentes", {}).setdefault(p.id, {})
    falhados: dict[str, float] = estado.setdefault("falhados", {}).setdefault(p.id, {})
    for c in [c for c, t in falhados.items() if time.time() - t >= ESPERA_FALHA_S]:
        del falhados[c]

    for k, hora in enumerate(horas_da_janela(agora)):
        prefixo = prefixo_da_hora(p, hora)
        esperados = {carimbo_de(hora + timedelta(minutes=m)) for m in range(0, 60, p.cadencia_min)}
        falta = any(c not in disco and limite <= instante(c) <= agora for c in esperados)
        if k >= 2 and (not falta or time.time() - listadas.get(prefixo, 0) < RELISTAR_S):
            continue
        try:
            pendentes.update(listar(p, prefixo))
            listadas[prefixo] = time.time()
        except Exception as e:  # noqa: BLE001 — uma hora falhando não para as outras
            print(f"{p.id}: falha ao listar {prefixo}: {e}", file=sys.stderr)

    for c in list(pendentes):
        if c in disco or instante(c) < limite:
            del pendentes[c]
    fila = sorted((c for c in pendentes if c not in falhados), reverse=True)
    feitos = falhas = 0
    for c in fila[: p.max_por_rodada]:
        if time.time() > prazo:
            break
        t0 = time.time()
        try:
            nc = baixar(f"{BALDE}/{pendentes[c]}")
            mb = len(nc) / 1e6
            imagem, g, cob = processar(p, nc)
            del nc  # nunca vai para o disco
            if grade is not None and g != grade:
                # Recorte ou grade nova: os quadros antigos não cabem na caixa nova.
                print(f"{p.id}: grade mudou ({grade} → {g}); apagando os {len(quadros)} quadros antigos")
                for velho in quadros:
                    os.remove(os.path.join(pasta_do(p), f"{velho}.png"))
                quadros = {}
            grade = g
            escrever_atomico(os.path.join(pasta_do(p), f"{c}.png"), imagem)
            quadros[c] = {"c": c, "arquivo": f"{c}.png", "cob": cob}
            del pendentes[c]
            feitos += 1
            print(f"{p.id} {c}: {len(imagem) / 1e3:.0f} KB, cobertura {cob:.0%} (NetCDF {mb:.1f} MB, {time.time() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            falhas += 1
            falhados[c] = time.time()
            print(f"{p.id} {c}: falhou — {e}", file=sys.stderr)

    if quadros or grade:
        escrever_atomico(caminho_indice, json.dumps(montar_indice(p, quadros, grade, agora), separators=(",", ":"), ensure_ascii=False).encode())
    tamanho = sum(os.path.getsize(os.path.join(pasta_do(p), f"{c}.png")) for c in quadros)
    return {"novos": feitos, "falhas": falhas, "fila": len([c for c in pendentes if c not in falhados]),
            "removidos": removidos, "quadros": len(quadros), "mb": tamanho / 1e6}


def rodada(agora: datetime | None = None, produtos=None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    prazo = time.time() + TEMPO_RODADA_S
    os.makedirs(PASTA, exist_ok=True)
    # Versões antigas do formato: o app já não as pede.
    for nome in os.listdir(os.path.dirname(PASTA)):
        if nome != os.path.basename(PASTA):
            shutil.rmtree(os.path.join(os.path.dirname(PASTA), nome), ignore_errors=True)
    estado = ler_json(ESTADO, {})
    resultado = {}
    # Primeiro o quadro novo de cada produto; o preenchimento do passado vem depois.
    for p in produtos or PRODUTOS.values():
        resultado[p.id] = rodada_produto(p, agora, estado, prazo)
    corte = time.time() - JANELA.total_seconds() - 3600
    estado["listadas"] = {k: t for k, t in estado.get("listadas", {}).items() if t > corte}
    escrever_atomico(ESTADO, json.dumps(estado).encode())
    return resultado


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "quadro":
        p = PRODUTOS[sys.argv[2]]
        t0 = time.time()
        imagem, grade, cob = processar(p, sys.argv[3])
        destino = os.path.splitext(sys.argv[3])[0] + ".png"
        with open(destino, "wb") as f:
            f.write(imagem)
        print(f"{destino}: {grade['largura']}×{grade['altura']}, {len(imagem) / 1e3:.0f} KB, cobertura {cob:.0%}, {time.time() - t0:.2f} s")
        return
    r = rodada()
    for pid, x in r.items():
        print(f"{pid}: {x['novos']} novos, {x['falhas']} falhas, {x['fila']} na fila, {x['removidos']} apagados, {x['quadros']} quadros em 48 h ({x['mb']:.1f} MB)")
    novos, falhas = sum(x["novos"] for x in r.values()), sum(x["falhas"] for x in r.values())
    sys.exit(1 if falhas and not novos else 0)


if __name__ == "__main__":
    main()
