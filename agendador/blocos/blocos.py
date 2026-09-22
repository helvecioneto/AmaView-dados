#!/usr/bin/env python3
"""
Blocos do GOES-19 para o AmaView: o JPEG do STAR cortado sem recompressão.

O AmaView mostra o setor NSA do NOAA/STAR em até 7200 px (7,3 MB por quadro,
124 MB decodificado). De perto, quase nada disso aparece na tela — no zoom 6,
0,8% da imagem. Aqui cada quadro de 3600 e 7200 px vira blocos de 512 px, e o
navegador baixa e decodifica só os da área visível.

- **Sem recompressão.** O corte é feito nos coeficientes do JPEG
  (`tjTransform`, o mesmo do `jpegtran -crop`): o miolo de cada bloco é
  idêntico pixel a pixel ao arquivo da NOAA. Só a borda de 1 px varia um pouco
  (a suavização da cor olha o vizinho). Continua "só reunir e exibir".
- **Pré-corte.** A cada ciclo, os quadros novos dos 22 produtos são cortados
  assim que o STAR os publica (horários previstos na grade de 10 min, sem
  baixar a listagem de 1,1 MB), e o resto das últimas 48 h é preenchido do mais
  novo para o mais velho.
- **Sob demanda.** O nginx serve o bloco do disco; se ele ainda não existe, o
  pedido cai aqui, o quadro é cortado na hora (~0,2 s depois do download) e o
  bloco volta na mesma resposta.
- Só produtos, larguras e horários conhecidos (últimas 49 h); nada além do CDN
  do STAR é buscado.

  blocos.py                  serviço (porta 8090, só local; o nginx fica na frente)
  blocos.py cortar P C L     corta um quadro e sai (teste)

URL: /blocos/v1/nsa/{produto}/{AAAADDDHHMM}/{largura}/{linha}_{coluna}.jpg — é
também o caminho no disco, abaixo de BLOCOS_RAIZ.
"""
import ctypes
import ctypes.util
import http.server
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("BLOCOS_RAIZ", "/var/cache/amaview-blocos")
PASTA = os.path.join(RAIZ, "blocos", f"v{VERSAO}", "nsa")
PORTA = int(os.environ.get("BLOCOS_PORTA", "8090"))
STAR = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/nsa"
AGENTE = "AmaView-blocos (+https://helvecioneto.github.io/AmaView/)"

PRODUTOS = (
    "GEOCOLOR", "FireTemperature", "Sandwich", "AirMass", "Dust", "DayNightCloudMicroCombo",
    "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16",
)
LARGURAS = (3600, 7200)
BLOCO = 512
JANELA = timedelta(hours=49)
# Pré-corte ligado por padrão; BLOCOS_AQUECER=0 deixa só o sob demanda.
AQUECER = os.environ.get("BLOCOS_AQUECER", "1") != "0"
TRABALHADORES_AQUECER = 3
# Cortes simultâneos no total (cada um segura ~100 MB de coeficientes).
CORTES = threading.BoundedSemaphore(6)

ROTA = re.compile(
    r"^/blocos/v1/nsa/(?P<produto>[A-Za-z0-9]{2,24})/(?P<carimbo>\d{11})/"
    r"(?P<largura>\d{4})/(?P<linha>\d{1,2})_(?P<coluna>\d{1,2})\.jpg$"
)


def altura(largura: int) -> int:
    return largura * 3 // 5


def grade(largura: int) -> tuple[int, int]:
    """(linhas, colunas) de blocos de 512 px; os da borda direita e de baixo são menores."""
    return math.ceil(altura(largura) / BLOCO), math.ceil(largura / BLOCO)


def instante(carimbo: str) -> datetime:
    """AAAADDDHHMM (dia juliano, UTC) → datetime."""
    ano, dia, hora, minuto = int(carimbo[:4]), int(carimbo[4:7]), int(carimbo[7:9]), int(carimbo[9:11])
    if not (1 <= dia <= 366 and hora < 24 and minuto < 60):
        raise ValueError(carimbo)
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def carimbo_de(t: datetime) -> str:
    return f"{t.year:04d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"


def pasta_do_quadro(produto: str, carimbo: str, largura: int) -> str:
    return os.path.join(PASTA, produto, carimbo, str(largura))


def url_star(produto: str, carimbo: str, largura: int) -> str:
    return f"{STAR}/{produto}/{carimbo}_GOES19-ABI-nsa-{produto}-{largura}x{altura(largura)}.jpg"


# ---------------------------------------------------------------------------
# TurboJPEG (API 2.x, presente na 2.1 do Ubuntu e ainda na 3.x): tjTransform
# com vários recortes numa chamada lê o arquivo UMA vez — 135 blocos em ~0,2 s,
# contra ~12 s com um `jpegtran` por bloco.


class _Regiao(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("w", ctypes.c_int), ("h", ctypes.c_int)]


_FILTRO = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(ctypes.c_short), _Regiao, _Regiao, ctypes.c_int, ctypes.c_int, ctypes.c_void_p
)


class _Transformacao(ctypes.Structure):
    _fields_ = [
        ("r", _Regiao),
        ("op", ctypes.c_int),
        ("options", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("customFilter", _FILTRO),
    ]


TJXOPT_CROP = 4
TJXOPT_COPYNONE = 64


def _carregar_turbojpeg():
    caminho = os.environ.get("BLOCOS_LIBTURBOJPEG") or ctypes.util.find_library("turbojpeg") or "libturbojpeg.so.0"
    lib = ctypes.CDLL(caminho)
    lib.tjInitTransform.restype = ctypes.c_void_p
    lib.tjTransform.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(_Transformacao),
        ctypes.c_int,
    ]
    lib.tjTransform.restype = ctypes.c_int
    lib.tjFree.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
    lib.tjDestroy.argtypes = [ctypes.c_void_p]
    lib.tjGetErrorStr2.argtypes = [ctypes.c_void_p]
    lib.tjGetErrorStr2.restype = ctypes.c_char_p
    return lib


_tj = None


def recortar(jpeg: bytes, regioes: list[tuple[int, int, int, int]]) -> list[bytes]:
    """Recortes sem perda (x e y múltiplos do MCU; 512 serve para 4:4:4 a 4:2:0)."""
    global _tj
    if _tj is None:
        _tj = _carregar_turbojpeg()
    n = len(regioes)
    trans = (_Transformacao * n)()
    for i, (x, y, w, h) in enumerate(regioes):
        trans[i].r = _Regiao(x, y, w, h)
        trans[i].options = TJXOPT_CROP | TJXOPT_COPYNONE
    saidas = (ctypes.POINTER(ctypes.c_ubyte) * n)()
    tamanhos = (ctypes.c_ulong * n)()
    origem = (ctypes.c_ubyte * len(jpeg)).from_buffer_copy(jpeg)
    h = _tj.tjInitTransform()
    try:
        if _tj.tjTransform(h, origem, len(jpeg), n, saidas, tamanhos, trans, 0) != 0:
            raise RuntimeError(_tj.tjGetErrorStr2(h).decode(errors="replace"))
        return [ctypes.string_at(saidas[i], tamanhos[i]) for i in range(n)]
    finally:
        for i in range(n):
            if saidas[i]:
                _tj.tjFree(saidas[i])
        _tj.tjDestroy(h)


def dimensoes(jpeg: bytes) -> tuple[int, int]:
    """(largura, altura) do SOF; confere que o arquivo é o tamanho esperado."""
    i = 2
    while i + 9 < len(jpeg) and jpeg[i] == 0xFF:
        marca = jpeg[i + 1]
        tamanho = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        if marca in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(jpeg[i + 7 : i + 9], "big"), int.from_bytes(jpeg[i + 5 : i + 7], "big")
        if marca == 0xDA:
            break
        i += 2 + tamanho
    raise ValueError("JPEG sem SOF")


# ---------------------------------------------------------------------------
# Corte de um quadro (compartilhado entre o sob demanda e o pré-corte)


class Ausente(Exception):
    """O STAR não tem (ainda) este quadro."""


_travas: dict[tuple, threading.Lock] = {}
_travas_lock = threading.Lock()
_estado = {
    "inicio": time.time(),
    "cortes": deque(maxlen=2000),  # (instante, segundos, origem)
    "falhas": 0,
    "ultimo_erro": None,
}


def _trava(chave: tuple) -> threading.Lock:
    with _travas_lock:
        t = _travas.get(chave)
        if t is None:
            t = _travas[chave] = threading.Lock()
        return t


def baixar(url: str) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(pedido, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Ausente(url) from None
            if tentativa == 2:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if tentativa == 2:
                raise
        time.sleep(1 + tentativa * 2)
    raise RuntimeError("inalcançável")


def cortar(produto: str, carimbo: str, largura: int, origem: str = "pedido") -> str:
    """Garante os blocos do quadro no disco e devolve a pasta. Ausente se o STAR não tem."""
    destino = pasta_do_quadro(produto, carimbo, largura)
    if os.path.isdir(destino):
        return destino
    with _trava((produto, carimbo, largura)):
        if os.path.isdir(destino):
            return destino
        with CORTES:
            t0 = time.time()
            jpeg = baixar(url_star(produto, carimbo, largura))
            if dimensoes(jpeg) != (largura, altura(largura)):
                raise ValueError(f"tamanho inesperado em {produto} {carimbo} {largura}: {dimensoes(jpeg)}")
            linhas, colunas = grade(largura)
            regioes = []
            for r in range(linhas):
                for c in range(colunas):
                    x, y = c * BLOCO, r * BLOCO
                    regioes.append((x, y, min(BLOCO, largura - x), min(BLOCO, altura(largura) - y)))
            blocos = recortar(jpeg, regioes)
            # Escreve numa pasta temporária e renomeia: a pasta final só existe completa.
            temp = f"{destino}.parcial-{os.getpid()}-{threading.get_ident()}"
            os.makedirs(temp, mode=0o755)
            i = 0
            for r in range(linhas):
                for c in range(colunas):
                    with open(os.path.join(temp, f"{r}_{c}.jpg"), "wb") as f:
                        f.write(blocos[i])
                    i += 1
            os.rename(temp, destino)
            _estado["cortes"].append((time.time(), time.time() - t0, origem))
        return destino


# ---------------------------------------------------------------------------
# Pré-corte: os quadros novos assim que saem, depois o resto das últimas 48 h

_ausentes: dict[tuple, float] = {}  # (produto, carimbo, largura) → quando tentar de novo


def _pendentes(agora: datetime) -> list[tuple[str, str, int]]:
    base = agora.replace(minute=agora.minute - agora.minute % 10, second=0, microsecond=0)
    fila = []
    passos = int(JANELA.total_seconds() // 600) - 6  # 48 h
    for k in range(passos + 1):
        t = base - timedelta(minutes=10 * k)
        c = carimbo_de(t)
        for p in PRODUTOS:
            for w in LARGURAS:
                chave = (p, c, w)
                if _ausentes.get(chave, 0) > time.time():
                    continue
                if not os.path.isdir(pasta_do_quadro(p, c, w)):
                    fila.append(chave)
    return fila


def _aquecer_um(chave: tuple[str, str, int]) -> None:
    p, c, w = chave
    try:
        cortar(p, c, w, origem="pre")
        _ausentes.pop(chave, None)
    except Ausente:
        idade = datetime.now(timezone.utc) - instante(c)
        # Recente: o STAR publica ~15–20 min depois; tenta de novo logo. Velho: lacuna real.
        _ausentes[chave] = time.time() + (120 if idade < timedelta(hours=1) else 3 * 3600)
    except Exception as e:  # noqa: BLE001 — o laço não pode morrer
        _estado["falhas"] += 1
        _estado["ultimo_erro"] = f"{datetime.now(timezone.utc):%H:%M:%S} {p} {c} {w}: {e}"
        _ausentes[chave] = time.time() + 300


def limpar(agora: datetime) -> int:
    """Apaga quadros fora da janela e restos de cortes interrompidos."""
    removidos = 0
    limite = agora - JANELA
    if not os.path.isdir(PASTA):
        return 0
    for p in os.listdir(PASTA):
        pp = os.path.join(PASTA, p)
        for c in os.listdir(pp):
            cp = os.path.join(pp, c)
            try:
                velho = instante(c) < limite
            except ValueError:
                velho = True
            if velho:
                shutil.rmtree(cp, ignore_errors=True)
                removidos += 1
                continue
            for w in os.listdir(cp):
                if ".parcial-" in w and time.time() - os.path.getmtime(os.path.join(cp, w)) > 600:
                    shutil.rmtree(os.path.join(cp, w), ignore_errors=True)
    for chave in [k for k in _ausentes if instante(k[1]) < limite]:
        _ausentes.pop(chave, None)
    return removidos


def aquecer_para_sempre() -> None:
    with ThreadPoolExecutor(TRABALHADORES_AQUECER, thread_name_prefix="pre") as pool:
        ultima_limpeza = 0.0
        while True:
            agora = datetime.now(timezone.utc)
            if time.time() - ultima_limpeza > 1800:
                limpar(agora)
                ultima_limpeza = time.time()
            fila = _pendentes(agora)
            _estado["fila"] = len(fila)
            # Lote pequeno: o quadro novo nunca espera o preenchimento do passado.
            list(pool.map(_aquecer_um, fila[:44]))
            time.sleep(20 if fila else 60)


# ---------------------------------------------------------------------------
# HTTP (atrás do nginx)


def saude() -> dict:
    agora = time.time()
    ultima_hora = [c for c in _estado["cortes"] if agora - c[0] < 3600]
    uso = shutil.disk_usage(RAIZ) if os.path.isdir(RAIZ) else None
    return {
        "ok": True,
        "versao": VERSAO,
        "no_ar_ha_min": round((agora - _estado["inicio"]) / 60),
        "cortes_ultima_hora": len(ultima_hora),
        "segundos_por_corte": round(sum(c[1] for c in ultima_hora) / len(ultima_hora), 2) if ultima_hora else None,
        "fila_pre_corte": _estado.get("fila"),
        "falhas": _estado["falhas"],
        "ultimo_erro": _estado["ultimo_erro"],
        "disco_livre_gb": round(uso.free / 1e9, 1) if uso else None,
    }


class Pedido(http.server.BaseHTTPRequestHandler):
    server_version = "AmaView-blocos"

    def _responder(self, codigo: int, corpo: bytes, tipo: str, cache: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Timing-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _erro(self, codigo: int, texto: str, cache: str = "no-store") -> None:
        self._responder(codigo, texto.encode(), "text/plain; charset=utf-8", cache)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        caminho = self.path.split("?", 1)[0]
        if caminho == "/blocos/saude":
            return self._responder(200, json.dumps(saude()).encode(), "application/json", "no-store")
        m = ROTA.match(caminho)
        if not m:
            return self._erro(404, "rota desconhecida")
        produto, carimbo = m["produto"], m["carimbo"]
        largura, linha, coluna = int(m["largura"]), int(m["linha"]), int(m["coluna"])
        if produto not in PRODUTOS or largura not in LARGURAS:
            return self._erro(404, "produto ou largura desconhecidos")
        linhas, colunas = grade(largura)
        if linha >= linhas or coluna >= colunas:
            return self._erro(404, "bloco fora da grade")
        try:
            t = instante(carimbo)
        except ValueError:
            return self._erro(404, "horário inválido")
        agora = datetime.now(timezone.utc)
        if not (agora - JANELA <= t <= agora + timedelta(minutes=10)):
            return self._erro(404, "fora das últimas 48 h")
        try:
            pasta = cortar(produto, carimbo, largura)
        except Ausente:
            # Pode sair daqui a pouco: cache curto, para o navegador tentar de novo.
            return self._erro(404, "o STAR ainda não publicou este quadro", "public, max-age=60")
        except Exception as e:  # noqa: BLE001
            _estado["falhas"] += 1
            _estado["ultimo_erro"] = f"{agora:%H:%M:%S} {produto} {carimbo} {largura}: {e}"
            return self._erro(502, "falha ao cortar o quadro")
        with open(os.path.join(pasta, f"{linha}_{coluna}.jpg"), "rb") as f:
            corpo = f.read()
        self._responder(200, corpo, "image/jpeg", "public, max-age=31536000, immutable")

    def log_message(self, formato: str, *args) -> None:
        # Só o que passou pelo corte sob demanda chega aqui; uma linha curta basta.
        sys.stderr.write(f"{self.address_string()} {formato % args}\n")


def main() -> None:
    if len(sys.argv) == 5 and sys.argv[1] == "cortar":
        t0 = time.time()
        pasta = cortar(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        print(f"{pasta}: {len(os.listdir(pasta))} blocos em {time.time() - t0:.2f} s")
        return
    os.makedirs(PASTA, exist_ok=True)
    if AQUECER:
        threading.Thread(target=aquecer_para_sempre, name="pre-corte", daemon=True).start()
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    servidor.daemon_threads = True
    print(f"blocos v{VERSAO} em 127.0.0.1:{PORTA}, disco em {PASTA}, pré-corte {'ligado' if AQUECER else 'desligado'}")
    servidor.serve_forever()


if __name__ == "__main__":
    main()
