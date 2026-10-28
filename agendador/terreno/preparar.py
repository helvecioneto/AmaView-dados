#!/usr/bin/env python3
"""
Recortes anuais do MapBiomas (Coleção 11, 30 m) na Amazônia Legal, para o
terreno.py.

Um GeoTIFF por ano em TERRENO_DADOS (`1985.tif` … `2025.tif`), baixado
inteiro do bucket público (~0,76 GB; cortar direto pelo /vsicurl/ leva ~6 min
por ano, aos pedaços) e cortado do disco, sem reamostrar: a mesma grade do
MapBiomas, só a caixa da Amazônia Legal (a do anel do IBGE, com folga de 0,15°,
porque o anel é simplificado em 0,1°). ~0,27 GB por ano, ~1 min de download
cada no Oracle. Zerar o lado de fora do anel (gdal.Warp com cutline) pouparia
~20 % de disco, mas levava ~16 min por ano no Oracle; o serviço já recusa
pontos fora da caixa.

Ano que já existe é pulado: rodar de novo só completa o que faltou. O MapBiomas
publica uma coleção por ano; trocar de coleção é trocar COLECAO_URL e a pasta.

  python3 preparar.py            # todos os anos que faltam
  python3 preparar.py 2024 2025  # só esses
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
import time
import urllib.request

from osgeo import gdal, ogr

gdal.UseExceptions()

DADOS = os.environ.get("TERRENO_DADOS", "/var/lib/amaview-terreno/mapbiomas-c11")
ANEL = os.environ.get(
    "TERRENO_ANEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ar", "amazonia_legal.json")
)
COLECAO_URL = (
    "https://storage.googleapis.com/mapbiomas-public/initiatives/brasil/collection11/"
    "lulc/coverage/brazil_coverage/brazil_coverage-col11_{ano}.tif"
)
ANOS = list(range(1985, 2026))
FOLGA_GRAUS = 0.15
# Um ano pede ~1 GB enquanto é feito (o do Brasil inteiro + o recorte); a
# máquina tem outros serviços em /var: abaixo disto, para.
LIVRE_MINIMO = 3 * 1024**3
TRABALHADORES = os.environ.get("TERRENO_THREADS", "2")

AGENTE = "AmaView-terreno (+https://amaview.com.br/)"
TENTATIVAS = 4


def baixar(url: str, destino: str) -> None:
    """O ano inteiro para o disco, com novas tentativas (o bucket é estável;
    a rede da máquina, nem sempre)."""
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE})
            with urllib.request.urlopen(pedido, timeout=60) as r, open(destino, "wb") as f:
                esperado = int(r.headers.get("Content-Length") or 0)
                shutil.copyfileobj(r, f, 4 * 1024 * 1024)
            if esperado and os.path.getsize(destino) != esperado:
                raise OSError(f"baixou {os.path.getsize(destino)} de {esperado} bytes")
            return
        except OSError as e:
            if tentativa == TENTATIVAS:
                raise
            print(f"  {url.rsplit('/', 1)[-1]}: {e}; de novo em {30 * tentativa} s", file=sys.stderr, flush=True)
            time.sleep(30 * tentativa)


def caixa_da_amazonia() -> tuple[float, float, float, float]:
    """A caixa (oeste, sul, leste, norte) do anel do IBGE com folga."""
    with open(ANEL) as f:
        anel = json.load(f)["anel"]
    if anel[0] != anel[-1]:
        anel = anel + [anel[0]]
    wkt = "POLYGON((" + ",".join(f"{x} {y}" for x, y in anel) + "))"
    oeste, leste, sul, norte = ogr.CreateGeometryFromWkt(wkt).Buffer(FOLGA_GRAUS, 8).GetEnvelope()
    return oeste, sul, leste, norte


def recortar(ano: int, caixa: tuple[float, float, float, float]) -> None:
    final = os.path.join(DADOS, f"{ano}.tif")
    if os.path.exists(final):
        return
    t = time.time()
    oeste, sul, leste, norte = caixa
    livre = shutil.disk_usage(DADOS).free
    if livre < LIVRE_MINIMO:
        raise OSError(f"só {livre / 1024**3:.1f} GB livres em {DADOS}; precisa de {LIVRE_MINIMO / 1024**3:.0f}")
    with tempfile.TemporaryDirectory(dir=DADOS) as tmp:
        origem = os.path.join(tmp, "brasil.tif")
        baixar(COLECAO_URL.format(ano=ano), origem)
        # 1) Caixa na grade do próprio MapBiomas (VRT: nada é copiado ainda).
        vrt = os.path.join(tmp, "caixa.vrt")
        gdal.Translate(vrt, origem, format="VRT", projWin=[oeste, norte, leste, sul])
        ds = gdal.Open(vrt)
        x0, dx, _, y0, _, dy = ds.GetGeoTransform()
        lim = [x0, y0 + dy * ds.RasterYSize, x0 + dx * ds.RasterXSize, y0]
        ds = None
        # 2) Copia a caixa, pixel a pixel, em blocos de 512 px (o serviço lê
        #    janelas de ~30 px; os blocos de 256 do MapBiomas pedem índice 4× maior).
        parcial = os.path.join(tmp, f"{ano}.tif")
        saida = gdal.Translate(
            parcial, vrt,
            creationOptions=["COMPRESS=ZSTD", "ZSTD_LEVEL=9", "TILED=YES", "BLOCKXSIZE=512",
                             "BLOCKYSIZE=512", f"NUM_THREADS={TRABALHADORES}"],
        )
        # Fechar explicitamente: no destrutor, um erro de escrita (disco cheio)
        # passaria calado e o arquivo truncado entraria no lugar.
        saida.FlushCache()
        saida.Close()
        conferir(parcial, lim, dx)
        os.replace(parcial, final)
    print(f"{ano}: {os.path.getsize(final) / 1e6:.0f} MB em {time.time() - t:.0f} s", flush=True)


def conferir(arq: str, lim: list[float], dx: float) -> None:
    """Reabre e lê o arquivo inteiro por blocos antes de publicá-lo."""
    ds = gdal.Open(arq)
    x0, rx, _, y0, _, _ = ds.GetGeoTransform()
    if abs(x0 - lim[0]) > 1e-9 or abs(y0 - lim[3]) > 1e-9 or abs(rx - dx) > 1e-12:
        raise RuntimeError(f"{arq}: grade fora da do MapBiomas")
    banda = ds.GetRasterBand(1)
    passo = 4096
    for y in range(0, ds.RasterYSize, passo):
        banda.ReadAsArray(0, y, ds.RasterXSize, min(passo, ds.RasterYSize - y))


def main(argv: list[str]) -> int:
    os.makedirs(DADOS, exist_ok=True)
    # Sobras de uma rodada interrompida (o serviço só roda um de cada vez).
    for nome in os.listdir(DADOS):
        if nome.startswith("tmp") and os.path.isdir(os.path.join(DADOS, nome)):
            shutil.rmtree(os.path.join(DADOS, nome), ignore_errors=True)
    anos = [int(a) for a in argv] or ANOS
    caixa = caixa_da_amazonia()
    falhas = 0
    for ano in anos:
        try:
            recortar(ano, caixa)
        except Exception as e:  # noqa: BLE001 — um ano ruim não para os outros
            falhas += 1
            print(f"{ano}: falhou ({type(e).__name__}: {e})", file=sys.stderr, flush=True)
    prontos = sorted(int(n[:4]) for n in os.listdir(DADOS) if n.endswith(".tif") and n[:4].isdigit())
    print(f"{len(prontos)} de {len(ANOS)} anos em {DADOS}", flush=True)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
