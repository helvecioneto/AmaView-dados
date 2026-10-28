#!/usr/bin/env python3
"""
Uso do solo e situação fundiária no ponto de um foco de queimada (AmaView).

  GET /terreno/v1?lat=-8.7612&lon=-63.9012[&uf=RO]
  GET /terreno/saude

Responde, para o círculo de 375 m em volta do ponto (o pixel do VIIRS, o
menor dos satélites de focos):

- `mapbiomas`: a composição da cobertura e uso da terra em cada ano de 1985 a
  2025 (MapBiomas Coleção 11, 30 m), contada nos recortes anuais que o
  `preparar.py` deixa em disco. Ler o pixel direto do bucket custaria ~4 MB e
  ~4 s por ano (os arquivos não têm visões reduzidas e o índice de blocos é
  grande), e o bucket não manda CORS.
- `car`: os imóveis do CAR que contêm o ponto, pelo WFS público do SICAR (só
  dados públicos: código, situação, área, tipo; o CAR não publica nome nem
  CPF). O CAR é autodeclaratório e os imóveis se sobrepõem: vão todos.

A resposta de um ponto não muda (o MapBiomas é anual; o CAR muda devagar): o
nginx guarda em cache (X-Accel-Expires) e limita a taxa por IP.
"""
from __future__ import annotations

import http.server
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

PORTA = int(os.environ.get("TERRENO_PORTA", "8096"))
DADOS = os.environ.get("TERRENO_DADOS", "/var/lib/amaview-terreno/mapbiomas-c11")
ANOS = list(range(1985, 2026))
COLECAO = "11"
RAIO_M = 375.0
M_POR_GRAU = 111_320.0

# Caixa da Amazônia Legal (anel do IBGE, com folga): fora dela não há recorte.
CAIXA = (-74.2, -18.2, -43.8, 5.5)
UFS = {"AC", "AM", "AP", "MA", "MT", "PA", "RO", "RR", "TO"}

CAR_WFS = "https://geoserver.car.gov.br/geoserver/sicar/wfs"
CAR_CAMPOS = "cod_imovel,status_imovel,condicao,area,m_fiscal,tipo_imovel,municipio,uf"
CAR_TEMPO = 10
CAR_MAX = 10
AGENTE = "AmaView-terreno (+https://amaview.com.br/)"
# O GeoServer do SICAR só oferece cifras abaixo do nível 2 do OpenSSL 3 (o
# 3.6 recusa no aperto de mão; o 3.0 do Ubuntu 24.04 ainda aceita): nível 1
# só para ele, com o certificado verificado como sempre.
TLS_CAR = ssl.create_default_context()
TLS_CAR.set_ciphers("DEFAULT:@SECLEVEL=1")

# Quanto o nginx guarda a resposta: o MapBiomas não muda; o CAR, devagar. Sem
# o CAR (fora do ar), pouco, para tentar de novo logo.
CACHE_OK = 7 * 24 * 3600
CACHE_SEM_CAR = 120


# --- MapBiomas ---------------------------------------------------------------

class Recortes:
    """Os 41 recortes anuais, abertos uma vez. O GDAL não é seguro entre
    threads no mesmo dataset: a leitura (milissegundos) passa por um cadeado."""

    def __init__(self, pasta: str) -> None:
        self.pasta = pasta
        self.cadeado = threading.Lock()
        self.ds: dict[int, gdal.Dataset] = {}
        self.gt: tuple[float, ...] | None = None
        self.tam = (0, 0)
        self.visto = 0.0
        self.abrir()

    def completar(self) -> None:
        """Enquanto o preparar.py ainda baixa anos, olha a pasta de novo (no
        máximo uma vez por minuto)."""
        if len(self.ds) == len(ANOS) or time.time() - self.visto < 60:
            return
        with self.cadeado:
            try:
                self.abrir()
            except Exception as e:  # noqa: BLE001 — segue com os anos que já tinha
                print(f"recortes: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def abrir(self) -> None:
        self.visto = time.time()
        for ano in ANOS:
            if ano in self.ds:
                continue
            arq = os.path.join(self.pasta, f"{ano}.tif")
            if not os.path.exists(arq):
                continue
            try:
                ds = gdal.Open(arq)
            except RuntimeError as e:  # um ano ilegível não derruba o serviço
                print(f"{arq}: {e}", file=sys.stderr, flush=True)
                continue
            gt = ds.GetGeoTransform()
            if self.gt is None:
                self.gt, self.tam = gt, (ds.RasterXSize, ds.RasterYSize)
            elif any(abs(a - b) > 1e-9 for a, b in zip(gt, self.gt)) or (ds.RasterXSize, ds.RasterYSize) != self.tam:
                print(f"{arq}: grade diferente da dos outros anos; fica de fora", file=sys.stderr, flush=True)
                continue
            self.ds[ano] = ds

    @property
    def anos(self) -> list[int]:
        return sorted(self.ds)

    def composicao(self, lat: float, lon: float) -> list[list[list[int]]] | None:
        """Por ano, [[classe, pixels], ...] (mais pixels primeiro) no círculo de
        RAIO_M; None se o ponto cai fora do recorte."""
        if self.gt is None:
            return None
        janela, mascara = janela_circulo(self.gt, self.tam, lat, lon, RAIO_M)
        if janela is None:
            return None
        x, y, w, h = janela
        saida = []
        with self.cadeado:
            for ano in self.anos:
                a = self.ds[ano].GetRasterBand(1).ReadAsArray(x, y, w, h)
                saida.append(contar(a[mascara]))
        return saida

    def composicao_segura(self, lat: float, lon: float) -> tuple[list | None, bool]:
        """A composição e se a leitura deu certo (um bloco ruim no disco não
        pode levar junto o CAR da mesma resposta)."""
        try:
            return self.composicao(lat, lon), True
        except RuntimeError as e:
            print(f"MapBiomas {lat},{lon}: {e}", file=sys.stderr, flush=True)
            return None, False


def janela_circulo(gt, tam, lat: float, lon: float, raio_m: float):
    """Janela (x, y, w, h) em pixels que cobre o círculo, e a máscara dos pixels
    cujo centro está dentro dele. (None, None) se sair da grade."""
    x0, dx, _, y0, _, dy = gt
    px = (lon - x0) / dx
    py = (lat - y0) / dy
    rx = raio_m / (M_POR_GRAU * math.cos(math.radians(lat))) / dx
    ry = raio_m / M_POR_GRAU / abs(dy)
    xa, xb = math.floor(px - rx), math.ceil(px + rx)
    ya, yb = math.floor(py - ry), math.ceil(py + ry)
    if xa < 0 or ya < 0 or xb > tam[0] or yb > tam[1]:
        return None, None
    cx = np.arange(xa, xb) + 0.5
    cy = np.arange(ya, yb) + 0.5
    mascara = ((cx[None, :] - px) / rx) ** 2 + ((cy[:, None] - py) / ry) ** 2 <= 1.0
    return (xa, ya, xb - xa, yb - ya), mascara


def contar(valores: np.ndarray) -> list[list[int]]:
    """[[classe, pixels], ...] sem o 0 (fora do mapa), mais pixels primeiro."""
    cont = np.bincount(valores.ravel(), minlength=1)
    pares = [[int(c), int(n)] for c, n in enumerate(cont) if c and n]
    pares.sort(key=lambda p: (-p[1], p[0]))
    return pares


# --- CAR ---------------------------------------------------------------------

# Situação antes de tudo (ativo, pendente, suspenso; cancelado por último).
ORDEM_STATUS = {"AT": 0, "PE": 1, "SU": 2, "CA": 3}


class CacheCar:
    """Cache em memória das consultas ao SICAR (o nginx guarda a resposta
    inteira; este evita repetir o WFS quando só a precisão do ponto muda)."""

    def __init__(self, maximo: int = 5000, validade: float = 24 * 3600) -> None:
        self.maximo, self.validade = maximo, validade
        self.itens: OrderedDict[tuple, tuple[float, list]] = OrderedDict()
        self.cadeado = threading.Lock()

    def pegar(self, chave: tuple):
        with self.cadeado:
            item = self.itens.get(chave)
            if item and time.time() - item[0] < self.validade:
                self.itens.move_to_end(chave)
                return item[1]
        return None

    def guardar(self, chave: tuple, valor: list) -> None:
        with self.cadeado:
            self.itens[chave] = (time.time(), valor)
            self.itens.move_to_end(chave)
            while len(self.itens) > self.maximo:
                self.itens.popitem(last=False)


CACHE_CAR = CacheCar()

# SICAR fora do ar: depois de uma falha, um minuto sem tentar (senão cada clique
# espera o tempo-limite inteiro), e no máximo 4 consultas ao mesmo tempo.
PAUSA_CAR = 60
SEMAFORO_CAR = threading.BoundedSemaphore(4)
_falha_car = [0.0]


def url_car(uf: str, lat: float, lon: float) -> str:
    # Em EPSG:4326 o WFS 2.0 do GeoServer usa a ordem latitude, longitude.
    q = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"sicar:sicar_imoveis_{uf.lower()}",
        "outputFormat": "application/json",
        "count": str(CAR_MAX),
        "propertyName": CAR_CAMPOS,
        "cql_filter": f"INTERSECTS(geo_area_imovel,POINT({lat} {lon}))",
    }
    return f"{CAR_WFS}?{urllib.parse.urlencode(q)}"


def imoveis_car(geojson: dict) -> list[dict]:
    """Só os campos públicos que a ficha mostra, na ordem da situação."""
    saida = []
    for f in geojson.get("features") or []:
        p = f.get("properties") or {}
        cod = p.get("cod_imovel")
        if not cod:
            continue
        saida.append({
            "cod": cod,
            "status": p.get("status_imovel"),
            "condicao": p.get("condicao"),
            "areaHa": round(p["area"], 2) if isinstance(p.get("area"), (int, float)) else None,
            "modulos": round(p["m_fiscal"], 2) if isinstance(p.get("m_fiscal"), (int, float)) else None,
            "tipo": p.get("tipo_imovel"),
            "municipio": p.get("municipio"),
            "uf": p.get("uf"),
        })
    saida.sort(key=lambda i: (ORDEM_STATUS.get(i["status"], 9), i["areaHa"] or 0))
    return saida


def abrir_car(pedido, timeout):
    return urllib.request.urlopen(pedido, timeout=timeout, context=TLS_CAR)


def consultar_car(uf: str | None, lat: float, lon: float, abrir=abrir_car) -> dict:
    if not uf:
        return {"situacao": "sem_uf", "imoveis": []}
    chave = (uf, round(lat, 4), round(lon, 4))
    guardado = CACHE_CAR.pegar(chave)
    if guardado is not None:
        return {"situacao": "ok", "imoveis": guardado}
    if time.time() - _falha_car[0] < PAUSA_CAR or not SEMAFORO_CAR.acquire(timeout=CAR_TEMPO):
        return {"situacao": "indisponivel", "imoveis": []}
    pedido = urllib.request.Request(url_car(uf, lat, lon), headers={"User-Agent": AGENTE})
    inicio = time.time()
    try:
        with abrir(pedido, timeout=CAR_TEMPO) as r:
            dado = json.loads(r.read())
        if time.time() - inicio > 2 * CAR_TEMPO:  # respondeu, mas devagar demais: pausa também
            _falha_car[0] = time.time()
    except Exception as e:  # noqa: BLE001 — qualquer falha do SICAR vira "indisponível"
        _falha_car[0] = time.time()
        print(f"CAR {uf} {lat},{lon}: {type(e).__name__}: {e}", file=sys.stderr)
        return {"situacao": "indisponivel", "imoveis": []}
    finally:
        SEMAFORO_CAR.release()
    imoveis = imoveis_car(dado)
    CACHE_CAR.guardar(chave, imoveis)
    return {"situacao": "ok", "imoveis": imoveis}


# --- HTTP --------------------------------------------------------------------

def ler_ponto(qs: dict) -> tuple[float, float, str | None]:
    """Valida lat, lon (4 casas, ~11 m) e a UF. ValueError se inválido."""
    lat = round(float(qs["lat"][0]), 4)
    lon = round(float(qs["lon"][0]), 4)
    if not (CAIXA[1] <= lat <= CAIXA[3] and CAIXA[0] <= lon <= CAIXA[2]):  # também recusa nan
        raise ValueError("fora da Amazônia Legal")
    # UF de fora (foco na folga do recorte): sem CAR, mas com o MapBiomas.
    uf = (qs.get("uf") or [""])[0].upper()
    return lat, lon, uf if uf in UFS else None


def responder_ponto(recortes: Recortes, lat: float, lon: float, uf: str | None, car=consultar_car) -> tuple[dict, int]:
    """O corpo da resposta e por quanto tempo o nginx pode guardá-la."""
    comp, leu = recortes.composicao_segura(lat, lon)
    resultado_car = car(uf, lat, lon)
    corpo = {
        "v": 1,
        "lat": lat,
        "lon": lon,
        "raioM": int(RAIO_M),
        "mapbiomas": None if comp is None else {
            "colecao": COLECAO,
            "resolucaoM": 30,
            "anos": recortes.anos,
            "comp": comp,
        },
        "car": resultado_car,
    }
    validade = CACHE_SEM_CAR if resultado_car["situacao"] == "indisponivel" or not leu else CACHE_OK
    return corpo, validade


class Pedido(http.server.BaseHTTPRequestHandler):
    recortes: Recortes
    server_version = "amaview-terreno"

    def enviar(self, codigo: int, corpo: dict, validade: int = 0) -> None:
        dados = json.dumps(corpo, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", f"public, max-age={min(validade, 86400)}" if validade else "no-store")
        self.send_header("X-Accel-Expires", str(validade))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/terreno/saude":
            self.recortes.completar()
            return self.enviar(200, {"ok": bool(self.recortes.anos), "anos": self.recortes.anos})
        if u.path != "/terreno/v1":
            return self.enviar(404, {"erro": "caminho desconhecido"})
        try:
            lat, lon, uf = ler_ponto(urllib.parse.parse_qs(u.query))
        except KeyError:
            return self.enviar(400, {"erro": "lat e lon são obrigatórios"}, 3600)
        except ValueError as e:
            return self.enviar(400, {"erro": str(e)}, 3600)
        self.recortes.completar()
        corpo, validade = responder_ponto(self.recortes, lat, lon, uf)
        if len(self.recortes.anos) < len(ANOS):
            validade = min(validade, CACHE_SEM_CAR)  # série incompleta: não guardar
        self.enviar(200, corpo, validade)

    def log_message(self, fmt: str, *args) -> None:  # o nginx já registra os pedidos
        pass


class Servidor(http.server.ThreadingHTTPServer):
    # A fila padrão (5) recusava conexões com 32 clientes ao mesmo tempo.
    request_queue_size = 128


def main() -> None:
    recortes = Recortes(DADOS)
    Pedido.recortes = recortes
    servidor = Servidor(("127.0.0.1", PORTA), Pedido)
    servidor.daemon_threads = True
    print(f"terreno em 127.0.0.1:{PORTA}, {len(recortes.anos)} anos do MapBiomas em {DADOS}", flush=True)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
