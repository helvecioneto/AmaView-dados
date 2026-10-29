#!/usr/bin/env python3
"""
Raios do GOES-19 para o AmaView: os flashes do GLM (Geostationary Lightning
Mapper, produto GLM-L2-LCFA da NOAA), em quadros de 10 min para o mapa e numa
consulta por ponto para a ficha do foco e a denúncia.

- **O que a NOAA publica.** Cada flash do arquivo, com a posição e o instante
  do primeiro evento, como vem: nada é filtrado pela qualidade (os flashes
  marcados como "degradados" são raios grandes ou longos, não falsos) nem
  corrigido. O GLM vê raios nuvem-solo e intranuvem, de dia e de noite, com
  pixel de ~8 km; a NOAA garante detecção acima de 70% na média do disco.
- **Leve.** Um arquivo a cada 20 s (~200 KB), baixado para a MEMÓRIA, lido e
  descartado: só os flashes do setor (a América do Sul ao norte de 25°S)
  ficam, em centésimos de grau (~1 km).
- **7 dias.** Os quadros mais velhos são apagados; o que falta na janela é
  preenchido do mais novo para o mais velho, um pouco a cada ciclo.

Serviço (amaview-raios, 127.0.0.1:8097, atrás do nginx):

  GET /raios/v1/ponto?lat=-6.62&lon=-51.98&t=202610291030[&h=72][&r=25]
      Raios a até `r` km do ponto nas `h` horas antes de `t` (UTC, AAAAMMDDHHMM):
      quantos, o mais perto, o mais recente, e a cobertura (fração da janela com
      dado). É o que diz "nenhum raio perto antes do foco".
  GET /raios/saude

Arquivos (abaixo de RAIOS_RAIZ, servidos direto pelo nginx em /raios/v1/):

  raios/v1/indice.json                  quadros disponíveis
  raios/v1/quadros/AAAADDDHHMM.json     {"c", "a": arquivos lidos, "v": [[lon*100, lat*100, s], …]}
                                        s = segundos desde o início do quadro

  raios.py                 o serviço
  raios.py arquivo ARQ.nc  lê um arquivo local e imprime o resumo (teste)
"""
from __future__ import annotations

import http.server
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

VERSAO = 1
PORTA = int(os.environ.get("RAIOS_PORTA", "8097"))
RAIZ = os.environ.get("RAIOS_RAIZ", "/var/cache/amaview-raios")
PASTA = os.path.join(RAIZ, "raios", f"v{VERSAO}")
QUADROS = os.path.join(PASTA, "quadros")
BALDE = "https://noaa-goes19.s3.amazonaws.com"
PRODUTO = "GLM-L2-LCFA"
AGENTE = "AmaView-raios (+https://amaview.com.br/)"

JANELA = timedelta(days=7)
QUADRO = timedelta(minutes=10)
# Um arquivo a cada 20 s: 30 por quadro.
ARQUIVOS_POR_QUADRO = 30
CICLO_S = 20
# Preenchimento do passado: arquivos por ciclo (~200 KB cada; ~1 min de rede por hora de dado).
PASSADO_POR_CICLO = 60
RELISTAR_S = 1800
ESPERA_FALHA_S = 1800

# Setor guardado: a América do Sul e o Caribe ao norte de 25°S (cobre o setor NSA do mapa).
CAIXA = (-85.0, -25.0, -30.0, 15.0)  # oeste, sul, leste, norte

# Consulta por ponto: padrões e limites.
HORAS_PADRAO = 72
RAIO_PADRAO_KM = 25.0
HORAS_MAX = int(JANELA.total_seconds() // 3600)
RAIO_MAX_KM = 100.0

ARQUIVO = re.compile(r"OR_GLM-L2-LCFA_G19_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_e\d{14}_c\d{14}\.nc$")


def carimbo_de(t: datetime) -> str:
    """AAAADDDHHMM (dia juliano, UTC), como nos quadros do STAR."""
    return f"{t.year:04d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"


def instante(carimbo: str) -> datetime:
    ano, dia, hora, minuto = int(carimbo[:4]), int(carimbo[4:7]), int(carimbo[7:9]), int(carimbo[9:11])
    if not (1 <= dia <= 366 and hora < 24 and minuto < 60):
        raise ValueError(carimbo)
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def inicio_do_arquivo(nome: str) -> datetime | None:
    m = ARQUIVO.search(nome)
    if not m:
        return None
    ano, dia, h, mi, s, _ = m.groups()
    return datetime(int(ano), 1, 1, int(h), int(mi), int(s), tzinfo=timezone.utc) + timedelta(days=int(dia) - 1)


def quadro_de(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)


def prefixo_da_hora(t: datetime) -> str:
    return f"{PRODUTO}/{t.year:04d}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"


# ---------------------------------------------------------------------------
# Um arquivo do GLM → flashes do setor


def ler_flashes(nc: bytes | str) -> tuple[datetime, np.ndarray]:
    """netCDF do LCFA → (início do arquivo, matriz N×3 [lon, lat, s desde o início]) só do setor."""
    import h5py

    with h5py.File(io.BytesIO(nc) if isinstance(nc, bytes) else nc, "r") as f:
        inicio = datetime.strptime(f.attrs["time_coverage_start"].decode()[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        if "flash_lat" not in f or f["flash_lat"].shape[0] == 0:
            return inicio, np.zeros((0, 3), np.float64)
        lat = f["flash_lat"][:].astype(np.float64)
        lon = f["flash_lon"][:].astype(np.float64)
        v = f["flash_time_offset_of_first_event"]
        bruto = v[:].astype(np.float64)
        escala = float(v.attrs.get("scale_factor", [1.0])[0])
        desvio = float(v.attrs.get("add_offset", [0.0])[0])
        # `_Unsigned`: o int16 guarda um número sem sinal.
        if v.attrs.get("_Unsigned", b"false") in (b"true", "true"):
            bruto = np.where(bruto < 0, bruto + 65536, bruto)
        seg = bruto * escala + desvio
    o, s, l, n = CAIXA
    m = (lon >= o) & (lon <= l) & (lat >= s) & (lat <= n) & np.isfinite(seg)
    return inicio, np.column_stack([lon[m], lat[m], np.clip(seg[m], 0, 60)])


# ---------------------------------------------------------------------------
# Memória e disco


class Raios:
    """Os quadros da janela: na memória (para a consulta) e em disco (para o mapa)."""

    def __init__(self) -> None:
        self.trava = threading.Lock()
        # carimbo → {"a": [inícios dos arquivos lidos, "SSS"], "v": matriz N×3 [lon, lat, epoch s]}
        self.quadros: dict[str, dict] = {}
        self.alterados: set[str] = set()
        self.listadas: dict[str, float] = {}
        self.falhados: dict[str, float] = {}
        self.ultimo_ciclo: float | None = None
        self.ultimo_erro: str | None = None

    # -- disco ---------------------------------------------------------------

    def carregar(self) -> None:
        os.makedirs(QUADROS, exist_ok=True)
        for nome in os.listdir(QUADROS):
            if not nome.endswith(".json"):
                continue
            c = nome[:-5]
            try:
                t0 = instante(c).timestamp()
                with open(os.path.join(QUADROS, nome)) as f:
                    d = json.load(f)
                v = np.asarray(d["v"], np.float64).reshape(-1, 3)
                v[:, :2] /= 100
                v[:, 2] += t0
                self.quadros[c] = {"a": list(d["a"]), "v": v}
            except (ValueError, KeyError, json.JSONDecodeError, OSError):
                continue

    def gravar(self, c: str) -> None:
        q = self.quadros[c]
        t0 = instante(c).timestamp()
        v = q["v"]
        linhas = np.column_stack([np.round(v[:, 0] * 100), np.round(v[:, 1] * 100), np.round(v[:, 2] - t0)]).astype(int).tolist()
        corpo = json.dumps({"c": c, "a": sorted(q["a"]), "v": linhas}, separators=(",", ":")).encode()
        escrever_atomico(os.path.join(QUADROS, f"{c}.json"), corpo)

    def gravar_indice(self, agora: datetime) -> None:
        quadros = [{"c": c, "n": int(len(q["v"])), "a": len(q["a"])} for c, q in sorted(self.quadros.items())]
        indice = {
            "versao": VERSAO,
            "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fonte": "NOAA GOES-19 GLM L2 LCFA (flashes)",
            "cadencia_min": 10,
            "arquivos_por_quadro": ARQUIVOS_POR_QUADRO,
            "quadros": quadros,
        }
        escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(indice, separators=(",", ":")).encode())

    def limpar(self, agora: datetime) -> None:
        limite = agora - JANELA
        for c in [c for c in self.quadros if instante(c) < limite]:
            del self.quadros[c]
            try:
                os.remove(os.path.join(QUADROS, f"{c}.json"))
            except FileNotFoundError:
                pass

    # -- arquivos lidos ------------------------------------------------------

    def ja_lido(self, inicio: datetime) -> bool:
        q = self.quadros.get(carimbo_de(quadro_de(inicio)))
        return q is not None and f"{inicio.minute % 10}{inicio.second:02d}" in q["a"]

    def juntar(self, inicio: datetime, flashes: np.ndarray) -> None:
        c = carimbo_de(quadro_de(inicio))
        marca = f"{inicio.minute % 10}{inicio.second:02d}"
        with self.trava:
            q = self.quadros.setdefault(c, {"a": [], "v": np.zeros((0, 3), np.float64)})
            if marca in q["a"]:
                return
            v = flashes.copy()
            v[:, 2] += inicio.timestamp()
            q["v"] = np.vstack([q["v"], v])
            q["a"].append(marca)
            self.alterados.add(c)

    # -- consulta ------------------------------------------------------------

    def consultar(self, lat: float, lon: float, t: datetime, horas: float, raio_km: float) -> dict:
        de = t - timedelta(hours=horas)
        ini, fim = de.timestamp(), t.timestamp()
        n = 0
        perto: float | None = None
        ultimo: float | None = None
        arquivos = 0
        esperados = 0
        dlat = raio_km / 111.0
        dlon = raio_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
        with self.trava:
            c = quadro_de(de)
            while c <= t:
                q = self.quadros.get(carimbo_de(c))
                lidos = set(q["a"]) if q else set()
                # Cobertura: as fatias de 20 s que tocam a janela, e quantas delas foram lidas.
                for k in range(ARQUIVOS_POR_QUADRO):
                    s0 = c + timedelta(seconds=20 * k)
                    if s0 + timedelta(seconds=20) > de and s0 <= t:
                        esperados += 1
                        arquivos += f"{s0.minute % 10}{s0.second:02d}" in lidos
                c += QUADRO
                if q is None:
                    continue
                v = q["v"]
                if not len(v):
                    continue
                m = (np.abs(v[:, 1] - lat) <= dlat) & (np.abs(v[:, 0] - lon) <= dlon) & (v[:, 2] >= ini) & (v[:, 2] <= fim)
                if not m.any():
                    continue
                sel = v[m]
                d = distancia_km(lat, lon, sel[:, 1], sel[:, 0])
                dentro = d <= raio_km
                if not dentro.any():
                    continue
                n += int(dentro.sum())
                dmin = float(d[dentro].min())
                perto = dmin if perto is None else min(perto, dmin)
                tmax = float(sel[dentro, 2].max())
                ultimo = tmax if ultimo is None else max(ultimo, tmax)
        iso = lambda s: datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        return {
            "raio_km": raio_km,
            "horas": horas,
            "de": iso(ini),
            "ate": iso(fim),
            "cobertura": round(arquivos / esperados, 3) if esperados else 0.0,
            "n": n,
            "mais_perto_km": round(perto, 1) if perto is not None else None,
            "ultimo": iso(ultimo) if ultimo is not None else None,
            "fonte": "NOAA GOES-19 GLM L2 LCFA",
        }


def distancia_km(lat1: float, lon1: float, lat2, lon2):
    r = math.pi / 180
    a = np.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * np.cos(lat2 * r) * np.sin((lon2 - lon1) * r / 2) ** 2
    return 2 * 6371 * np.arcsin(np.minimum(1, np.sqrt(a)))


def escrever_atomico(caminho: str, dado: bytes) -> None:
    temp = f"{caminho}.parcial-{os.getpid()}"
    with open(temp, "wb") as f:
        f.write(dado)
    os.replace(temp, caminho)


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


def listar(prefixo: str) -> list[str]:
    """Chaves dos arquivos de uma hora (180, no normal; a listagem do S3 vem inteira até 1000)."""
    xml = baixar(f"{BALDE}/?list-type=2&prefix={prefixo}").decode()
    return [k for k in re.findall(r"<Key>([^<]+)</Key>", xml) if ARQUIVO.search(k)]


def horas_da_janela(agora: datetime) -> list[datetime]:
    base = agora.replace(minute=0, second=0, microsecond=0)
    n = int(JANELA.total_seconds() // 3600)
    return [base - timedelta(hours=k) for k in range(n + 1)]


def ciclo(r: Raios, agora: datetime | None = None) -> dict:
    """Lê o que é novo (as duas horas mais recentes) e um pouco do passado que falta."""
    agora = agora or datetime.now(timezone.utc)
    limite = agora - JANELA
    novos: list[tuple[datetime, str]] = []
    passado: list[tuple[datetime, str]] = []
    for k, hora in enumerate(horas_da_janela(agora)):
        prefixo = prefixo_da_hora(hora)
        if k >= 2:
            # Hora antiga: só se falta quadro e a última olhada foi há mais de 30 min.
            completa = all(
                len(r.quadros.get(carimbo_de(hora + timedelta(minutes=m)), {"a": []})["a"]) >= ARQUIVOS_POR_QUADRO for m in range(0, 60, 10)
            )
            if completa or time.time() - r.listadas.get(prefixo, 0) < RELISTAR_S:
                continue
        try:
            chaves = listar(prefixo)
            r.listadas[prefixo] = time.time()
        except Exception as e:  # noqa: BLE001 — uma hora falhando não para as outras
            print(f"falha ao listar {prefixo}: {e}", file=sys.stderr)
            continue
        for chave in chaves:
            ini = inicio_do_arquivo(chave)
            if ini is None or ini < limite or r.ja_lido(ini) or time.time() - r.falhados.get(chave, 0) < ESPERA_FALHA_S:
                continue
            (novos if k < 2 else passado).append((ini, chave))
        if len(passado) >= PASSADO_POR_CICLO:
            break
    passado.sort(reverse=True)
    lidos, falhas, flashes = 0, 0, 0
    for ini, chave in sorted(novos) + passado[:PASSADO_POR_CICLO]:
        try:
            _, v = ler_flashes(baixar(f"{BALDE}/{chave}"))
            r.juntar(ini, v)
            lidos += 1
            flashes += len(v)
        except Exception as e:  # noqa: BLE001
            falhas += 1
            r.falhados[chave] = time.time()
            print(f"{chave}: falhou — {e}", file=sys.stderr)
    with r.trava:
        r.limpar(agora)
        for c in r.alterados:
            if c in r.quadros:
                r.gravar(c)
        r.alterados.clear()
        r.gravar_indice(agora)
    r.falhados = {k: t for k, t in r.falhados.items() if time.time() - t < ESPERA_FALHA_S}
    r.listadas = {p: t for p, t in r.listadas.items() if time.time() - t < JANELA.total_seconds()}
    return {"lidos": lidos, "falhas": falhas, "flashes": flashes, "quadros": len(r.quadros)}


def laco(r: Raios) -> None:
    while True:
        t0 = time.time()
        try:
            res = ciclo(r)
            r.ultimo_ciclo, r.ultimo_erro = time.time(), None
            if res["lidos"] or res["falhas"]:
                print(f"ciclo: {res['lidos']} arquivos, {res['flashes']} flashes, {res['falhas']} falhas, {res['quadros']} quadros ({time.time() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            r.ultimo_erro = str(e)
            print(f"ciclo falhou: {e}", file=sys.stderr)
        time.sleep(max(1.0, CICLO_S - (time.time() - t0)))


# ---------------------------------------------------------------------------
# HTTP


def ler_instante(texto: str | None) -> datetime | None:
    if not texto:
        return None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})", texto)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


def ler_pedido(qs: dict) -> tuple[float, float, datetime, float, float]:
    def num(k: str, padrao: float | None = None) -> float:
        v = qs.get(k, [None])[0]
        if v is None:
            if padrao is None:
                raise ValueError(k)
            return padrao
        x = float(v)
        if not math.isfinite(x):
            raise ValueError(k)
        return x

    lat, lon = num("lat"), num("lon")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon")
    t = ler_instante(qs.get("t", [None])[0])
    if t is None:
        raise ValueError("t")
    horas = min(HORAS_MAX, max(1.0, num("h", HORAS_PADRAO)))
    raio = min(RAIO_MAX_KM, max(1.0, num("r", RAIO_PADRAO_KM)))
    return round(lat, 4), round(lon, 4), t, horas, raio


class Pedido(http.server.BaseHTTPRequestHandler):
    raios: Raios

    def enviar(self, codigo: int, corpo: dict, validade: int = 0) -> None:
        dado = json.dumps(corpo, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dado)))
        self.send_header("Cache-Control", f"public, max-age={validade}" if validade else "no-store")
        self.send_header("X-Accel-Expires", str(validade))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(dado)

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/raios/saude":
            r = self.raios
            idade = time.time() - r.ultimo_ciclo if r.ultimo_ciclo else None
            ultimo = max(r.quadros) if r.quadros else None
            self.enviar(200, {"ok": idade is not None and idade < 300, "ciclo_ha_s": round(idade) if idade else None, "quadros": len(r.quadros), "ultimo": ultimo, "erro": r.ultimo_erro})
            return
        if url.path != "/raios/v1/ponto":
            self.enviar(404, {"erro": "não encontrado"})
            return
        try:
            lat, lon, t, horas, raio = ler_pedido(urllib.parse.parse_qs(url.query))
        except (ValueError, TypeError) as e:
            self.enviar(400, {"erro": f"parâmetro inválido: {e}"})
            return
        resp = self.raios.consultar(lat, lon, t, horas, raio)
        # Janela já inteira no passado e com dado completo: a resposta não muda mais.
        completa = resp["cobertura"] >= 0.999 and t < datetime.now(timezone.utc) - timedelta(minutes=20)
        self.enviar(200, resp, 86400 if completa else 60)

    def log_message(self, fmt: str, *args) -> None:  # o nginx já registra os pedidos
        pass


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "arquivo":
        t0 = time.time()
        inicio, v = ler_flashes(sys.argv[2])
        print(f"{inicio:%Y-%m-%d %H:%M:%S} · {len(v)} flashes no setor · {time.time() - t0:.2f} s")
        return
    r = Raios()
    r.carregar()
    print(f"raios em 127.0.0.1:{PORTA}, {len(r.quadros)} quadros em disco", flush=True)
    threading.Thread(target=laco, args=(r,), daemon=True).start()
    Pedido.raios = r
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    servidor.daemon_threads = True
    servidor.serve_forever()


if __name__ == "__main__":
    main()
