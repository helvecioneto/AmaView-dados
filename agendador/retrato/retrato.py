#!/usr/bin/env python3
"""
Retrato da denúncia (AmaView): os dados públicos em volta de um foco, guardados
no dia em que alguém abre a página de denúncia, para o link curto
(`amaview.com.br/d/<código>`) continuar mostrando o que foi reunido depois que
as fontes apagam o dado (descargas atmosféricas: 7 dias; INMET e fumaça: 48 h;
chuva do CEMADEN: 24 h). Pedido do usuário em 29/10/2026.

- **Só dado público, montado aqui.** O navegador manda só o código do foco
  (o mesmo do link curto: ponto, horário e satélite) e a sigla do estado; o
  serviço busca cada fonte sozinho e guarda o recorte bruto em volta do ponto
  e do horário, no formato que a fonte publica. Nada que a pessoa escreveu
  (nome, e-mail, relato) passa por aqui. Sem a imagem do mapa: o fundo vem do
  Google, ao vivo, e não pode ser guardado.
- **Sem registro de quem pediu.** O nginx não registra os acessos deste
  endereço (um IP ao lado de um foco identificaria quem denunciou), e o serviço
  não escreve IP em lugar nenhum.
- **O primeiro retrato fica.** É o mais perto do fogo; um pedido depois só
  completa as partes que faltaram (fonte fora do ar).

Serviço (amaview-retrato, 127.0.0.1:8098, atrás do nginx):

  POST /retrato/v1/<código>[?uf=PA]   monta (ou completa) o retrato
  GET  /retrato/v1/<código>           o retrato guardado (404 se não houver)
  GET  /retrato/saude

Arquivos: RETRATO_RAIZ/retrato/v1/<código>.json (~20 KB cada).
"""
from __future__ import annotations

import http.server
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSAO = 1
PORTA = int(os.environ.get("RETRATO_PORTA", "8098"))
RAIZ = os.environ.get("RETRATO_RAIZ", "/var/lib/amaview-retrato")
PASTA = os.path.join(RAIZ, "retrato", f"v{VERSAO}")
# Teto de retratos novos por dia: o endereço é público, e o disco é finito.
MAX_POR_DIA = int(os.environ.get("RETRATO_MAX_POR_DIA", "3000"))

# Pelo nginx local, para aproveitar o cache: a página da denúncia já pediu o mesmo ponto.
NGINX = os.environ.get("RETRATO_NGINX", "http://127.0.0.1")
NGINX_HOST = os.environ.get("RETRATO_NGINX_HOST", "147.15.84.134")
TERRENO = f"{NGINX}/terreno/v1"
RAIOS = f"{NGINX}/raios/v1/ponto"
METEO = os.environ.get("RETRATO_METEO", "/var/cache/amaview-meteo/meteo/v1")
FUMACA = os.environ.get("RETRATO_FUMACA", "/var/cache/amaview-fumaca/fumaca/v2")
CEMADEN = os.environ.get("RETRATO_CEMADEN", "https://helvecioneto.github.io/AmaView-dados/cemaden")
AGENTE = "AmaView-retrato/1 (+https://amaview.com.br)"

# Os mesmos números da página (src/denuncia/evidencias.ts e src/raios/api.ts).
RAIO_INMET_KM = 150
RAIO_CEMADEN_KM = 50
TOLERANCIA_FUMACA = timedelta(minutes=30)
RAIO_RAIOS_KM = 25
HORAS_RAIOS = 72
UFS = {"AC", "AM", "AP", "MA", "MT", "PA", "RO", "RR", "TO"}

# A tabela de satélites do link curto (src/denuncia/linkCurto.ts): só cresce.
SATELITES = [
    "GOES-19", "NOAA-20", "NOAA-21", "NPP-375", "NPP-375D", "AQUA_M-T", "AQUA_M-M", "TERRA_M-T", "TERRA_M-M",
    "METOP-B", "METOP-C", "MSG-03", "NOAA-18", "NOAA-19", "NOAA-18D", "NOAA-19D", "GOES-16", "METOP-A",
]
T0 = datetime(2000, 1, 1, tzinfo=timezone.utc)
# Só os códigos da tabela (sem o "~nome" dos satélites fora dela): o nome vira arquivo.
CODIGO = re.compile(r"[0-9a-z]{1,8}\.[0-9a-z]{1,8}\.[0-9a-z]{1,8}\.[0-9a-z]{1,2}")
PARTES = ("terreno", "raios", "meteo", "fumaca", "cemaden")


def ler_codigo(codigo: str) -> dict | None:
    """O foco do código do link curto, ou None."""
    if not CODIGO.fullmatch(codigo):
        return None
    a, b, c, d = codigo.split(".")
    lat = round(int(a, 36) / 1e5 - 90, 5)
    lon = round(int(b, 36) / 1e5 - 180, 5)
    n = int(d, 36)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or n >> 1 >= len(SATELITES):
        return None
    t = T0 + timedelta(minutes=int(c, 36))
    return {"lat": lat, "lon": lon, "t": t, "satelite": SATELITES[n >> 1], "fonte": "goes10min" if n & 1 else "diario"}


def distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine, como o `distanciaKm` da página."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def compacto(t: datetime) -> str:
    return t.strftime("%Y%m%d%H%M")


def baixar_json(url: str, tempo: float = 30, host: str | None = None) -> object:
    cabecalhos = {"User-Agent": AGENTE, "Accept": "application/json"}
    if host:
        cabecalhos["Host"] = host
    pedido = urllib.request.Request(url, headers=cabecalhos)
    with urllib.request.urlopen(pedido, timeout=tempo) as r:
        return json.load(r)


_cemaden: dict = {"t": 0.0, "dado": None}
_cemaden_trava = threading.Lock()


def cemaden_publicado() -> tuple[dict, dict]:
    """O cadastro e a grade do CEMADEN, guardados 10 min na memória (o pipeline publica a cada 15 min)."""
    with _cemaden_trava:
        if _cemaden["dado"] is None or time.time() - _cemaden["t"] > 600:
            _cemaden["dado"] = (baixar_json(f"{CEMADEN}/estacoes.json"), baixar_json(f"{CEMADEN}/serie.json"))
            _cemaden["t"] = time.time()
        return _cemaden["dado"]


def ler_json(caminho: str) -> object:
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# As partes: cada uma no formato da fonte, recortada em volta do ponto
# ---------------------------------------------------------------------------

def parte_terreno(foco: dict, uf: str | None) -> object:
    q = {"lat": f"{foco['lat']:.4f}", "lon": f"{foco['lon']:.4f}"}
    if uf:
        q["uf"] = uf
    return baixar_json(f"{TERRENO}?{urllib.parse.urlencode(q)}", 40, NGINX_HOST)


def parte_raios(foco: dict) -> object:
    q = {"lat": f"{foco['lat']:.4f}", "lon": f"{foco['lon']:.4f}", "t": compacto(foco["t"]), "h": HORAS_RAIOS, "r": RAIO_RAIOS_KM}
    return baixar_json(f"{RAIOS}?{urllib.parse.urlencode(q)}", 20, NGINX_HOST)


def recorte_meteo(estacoes: dict, serie: dict, lat: float, lon: float) -> dict:
    """As estações automáticas do INMET a até 150 km, com a série inteira (48 h) de cada uma."""
    perto = [e for e in estacoes.get("estacoes", []) if e.get("k") == "i" and isinstance(e.get("x"), (int, float))
             and isinstance(e.get("y"), (int, float)) and distancia_km(lat, lon, e["y"], e["x"]) <= RAIO_INMET_KM]
    cods = {str(e.get("c")) for e in perto}
    return {
        "estacoes": {**{k: v for k, v in estacoes.items() if k != "estacoes"}, "estacoes": perto},
        "serie": {**{k: v for k, v in serie.items() if k not in ("inmet", "metar")},
                  "inmet": {c: v for c, v in (serie.get("inmet") or {}).items() if c in cods}, "metar": {}},
    }


def parte_meteo(foco: dict) -> object:
    return recorte_meteo(ler_json(os.path.join(METEO, "estacoes.json")), ler_json(os.path.join(METEO, "serie.json")), foco["lat"], foco["lon"])


def instante_quadro(c: str) -> datetime | None:
    try:
        return datetime.strptime(c, "%Y%j%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def caixa(coords) -> tuple[float, float, float, float]:
    xs, ys = [], []

    def andar(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                andar(x)

    andar(coords)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, -1, -1)


def recorte_quadro(geojson: dict, lat: float, lon: float) -> dict:
    """As áreas de fumaça cuja caixa chega a 1 grau do ponto (a página olha até 30 km)."""
    def perto(f) -> bool:
        g = f.get("geometry") or {}
        x0, y0, x1, y1 = caixa(g.get("coordinates") or [])
        return x0 - 1 <= lon <= x1 + 1 and y0 - 1 <= lat <= y1 + 1

    return {**geojson, "features": [f for f in geojson.get("features", []) if perto(f)]}


def parte_fumaca(foco: dict) -> object:
    """Os quadros da fumaça a até 30 min do foco: a entrada do índice e as áreas perto do ponto."""
    indice = ler_json(os.path.join(FUMACA, "indice.json"))
    quadros = [q for q in indice.get("quadros", []) if (t := instante_quadro(str(q.get("c")))) and abs(t - foco["t"]) <= TOLERANCIA_FUMACA]
    geo = {}
    for q in quadros:
        if q.get("n"):
            geo[q["c"]] = recorte_quadro(ler_json(os.path.join(FUMACA, "quadros", f"{q['c']}.geojson")), foco["lat"], foco["lon"])
    return {"indice": {**{k: v for k, v in indice.items() if k != "quadros"}, "quadros": quadros}, "quadros": geo}


def recorte_cemaden(estacoes: dict, serie: dict, lat: float, lon: float) -> dict:
    """Os pluviômetros a até 50 km: o cadastro e as linhas da grade de 24 h."""
    perto = [e for e in estacoes.get("estacoes", []) if isinstance(e.get("x"), (int, float)) and isinstance(e.get("y"), (int, float))
             and distancia_km(lat, lon, e["y"], e["x"]) <= RAIO_CEMADEN_KM]
    cods = {str(e.get("c")) for e in perto}
    linhas = [i for i, c in enumerate(serie.get("estacoes", [])) if str(c) in cods]
    return {
        "estacoes": {**{k: v for k, v in estacoes.items() if k != "estacoes"}, "estacoes": perto},
        "serie": {**{k: v for k, v in serie.items() if k not in ("estacoes", "v")},
                  "estacoes": [serie["estacoes"][i] for i in linhas], "v": [serie["v"][i] for i in linhas]},
    }


def parte_cemaden(foco: dict) -> object:
    return recorte_cemaden(*cemaden_publicado(), foco["lat"], foco["lon"])


def montar(foco: dict, uf: str | None, antes: dict | None) -> dict:
    """O retrato: as partes que já havia ficam; as que faltam são buscadas agora."""
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    partes = dict((antes or {}).get("partes") or {})
    fazer = {
        "terreno": lambda: parte_terreno(foco, uf),
        "raios": lambda: parte_raios(foco),
        "meteo": lambda: parte_meteo(foco),
        "fumaca": lambda: parte_fumaca(foco),
        "cemaden": lambda: parte_cemaden(foco),
    }
    quando = dict((antes or {}).get("quando") or {})
    for nome in PARTES:
        if partes.get(nome) is not None:
            continue
        try:
            partes[nome] = fazer[nome]()
            quando[nome] = agora
        except Exception as e:  # noqa: BLE001 — a parte fica para o próximo pedido
            partes[nome] = None
            print(f"retrato: {nome}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return {
        "versao": VERSAO,
        "criado": (antes or {}).get("criado") or agora,
        "foco": {**foco, "t": foco["t"].strftime("%Y-%m-%dT%H:%M:%SZ")},
        "uf": uf or (antes or {}).get("uf"),
        "partes": partes,
        "quando": quando,
    }


# ---------------------------------------------------------------------------
# Disco e serviço
# ---------------------------------------------------------------------------

def caminho(codigo: str) -> str:
    return os.path.join(PASTA, f"{codigo}.json")


def gravar(codigo: str, retrato: dict) -> None:
    os.makedirs(PASTA, exist_ok=True)
    tmp = f"{caminho(codigo)}.tmp{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(retrato, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, caminho(codigo))


def ler(codigo: str) -> dict | None:
    try:
        return ler_json(caminho(codigo))  # type: ignore[return-value]
    except (OSError, ValueError):
        return None


class Guarda:
    """Um retrato por vez para cada código, e o teto do dia."""

    def __init__(self) -> None:
        self.trava = threading.Lock()
        self.travas: dict[str, threading.Lock] = {}
        self.dia = ""
        self.novos = 0

    def do_codigo(self, codigo: str) -> threading.Lock:
        with self.trava:
            if len(self.travas) > 10000:
                self.travas.clear()
            return self.travas.setdefault(codigo, threading.Lock())

    def cabe_mais_um(self) -> bool:
        with self.trava:
            hoje = time.strftime("%Y-%m-%d")
            if hoje != self.dia:
                self.dia, self.novos = hoje, 0
            if self.novos >= MAX_POR_DIA:
                return False
            self.novos += 1
            return True


GUARDA = Guarda()


def pedir_retrato(codigo: str, uf: str | None) -> tuple[int, dict]:
    foco = ler_codigo(codigo)
    if not foco:
        return 400, {"erro": "código inválido"}
    with GUARDA.do_codigo(codigo):
        antes = ler(codigo)
        if antes and all(antes.get("partes", {}).get(p) is not None for p in PARTES):
            return 200, {"codigo": codigo, "criado": antes.get("criado"), "novo": False}
        if not antes and not GUARDA.cabe_mais_um():
            return 429, {"erro": "limite de retratos do dia"}
        retrato = montar(foco, uf, antes)
        gravar(codigo, retrato)
        faltam = [p for p in PARTES if retrato["partes"].get(p) is None]
        print(f"retrato {codigo}: {'completado' if antes else 'criado'}{f' (faltam {faltam})' if faltam else ''}", flush=True)
        return 200, {"codigo": codigo, "criado": retrato["criado"], "novo": not antes, "faltam": faltam}


class Pedido(http.server.BaseHTTPRequestHandler):
    def enviar(self, codigo: int, corpo: dict | bytes, validade: int = 0) -> None:
        dado = corpo if isinstance(corpo, bytes) else json.dumps(corpo, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dado)))
        self.send_header("Cache-Control", f"public, max-age={validade}" if validade else "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(dado)

    def codigo(self) -> str | None:
        m = re.fullmatch(r"/retrato/v1/([^/]+)", urllib.parse.urlsplit(self.path).path)
        return m.group(1) if m else None

    def do_GET(self) -> None:  # noqa: N802
        if urllib.parse.urlsplit(self.path).path == "/retrato/saude":
            n = len([x for x in os.listdir(PASTA) if x.endswith(".json")]) if os.path.isdir(PASTA) else 0
            self.enviar(200, {"ok": True, "retratos": n, "novos_hoje": GUARDA.novos})
            return
        c = self.codigo()
        if not c or not CODIGO.fullmatch(c):
            self.enviar(404, {"erro": "não encontrado"})
            return
        try:
            with open(caminho(c), "rb") as f:
                dado = f.read()
        except OSError:
            self.enviar(404, {"erro": "sem retrato"}, 60)
            return
        self.enviar(200, dado, 300)

    def do_POST(self) -> None:  # noqa: N802
        c = self.codigo()
        if not c:
            self.enviar(404, {"erro": "não encontrado"})
            return
        uf = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("uf") or [""])[0].upper()
        codigo, corpo = pedir_retrato(c, uf if uf in UFS else None)
        self.enviar(codigo, corpo)

    def log_message(self, fmt: str, *args) -> None:
        # Nada de IP: quem pede um retrato é quem está denunciando.
        pass


def main() -> None:
    os.makedirs(PASTA, exist_ok=True)
    print(f"retrato em 127.0.0.1:{PORTA}, pasta {PASTA}", flush=True)
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    servidor.daemon_threads = True
    servidor.serve_forever()


if __name__ == "__main__":
    main()
