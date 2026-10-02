#!/usr/bin/env python3
"""
Blocos do GOES-19 para o AmaView: o JPEG do STAR cortado sem recompressão.

O AmaView mostra o setor NSA do NOAA/STAR em até 7200 px (7,3 MB por quadro,
124 MB decodificado). De perto, quase nada disso aparece na tela — no zoom 6,
0,8% da imagem. Aqui cada quadro de 3600 e 7200 px vira blocos de 512 px, e o
navegador baixa e decodifica só os da área visível.

- **Sem recompressão.** O corte é feito nos coeficientes do JPEG
  (`tjTransform`, o mesmo do `jpegtran -crop`): os pixels são os do arquivo da
  NOAA. Continua "só reunir e exibir".
- **Sobra de 16 px.** Cada bloco leva 16 px do vizinho de cada lado (onde há
  vizinho). O navegador desenha só o miolo de 512 px, e a suavização ao ampliar
  lê a sobra — sem emendas visíveis entre blocos; e a cor da borda, que a
  decodificação suaviza olhando o vizinho, sai igual à do arquivo inteiro.
- **Progressivo (v2).** Os blocos do v2 saem em JPEG progressivo, também nos
  coeficientes (`TJXOPT_PROGRESSIVE`, o `jpegtran -progressive`): os mesmos
  pixels, ~24% menos bytes (a codificação de Huffman do progressivo é
  otimizada). O v1, igual ao arquivo do STAR, só é cortado sob demanda, para
  quem ainda tem o app antigo, e fica no disco só 12 h.
- **Pré-corte.** A cada minuto, um HEAD no arquivo de 450 px de cada horário
  que falta nas últimas 6 h diz se o STAR o publicou — inclusive atrasado,
  depois de uma pane; publicado, todos os produtos dele são cortados na hora.
  Na última hora a sonda é por produto: as bandas saem minutos antes do
  GEOCOLOR e são cortadas assim que aparecem. Os horários seguem a grade de
  10 min (sem baixar a listagem de 1,1 MB), e o resto das últimas 48 h é
  preenchido do mais novo para o mais velho.
- `/blocos/v{1,2}/ultimos`: o horário mais recente já cortado (v2) de cada
  produto — o app pergunta a cada minuto e recarrega quando há quadro novo.
- **Sob demanda.** O nginx serve o bloco do disco; se ele ainda não existe, o
  pedido cai aqui, o quadro é cortado na hora (~0,2 s depois do download) e o
  bloco volta na mesma resposta. Arquivo do STAR truncado (ainda sendo
  publicado): baixa de novo uma vez; se continuar, 503 com Retry-After.
- **Espelho da base.** Os arquivos inteiros de 450, 900 e 1800 px do STAR
  (a base do AmaView) ficam aqui também, byte a byte iguais aos da NOAA, em
  `/blocos/v2/nsa/{produto}/{AAAADDDHHMM}/{largura}.jpg`: o STAR responde em
  ~0,2 s com cauda de vários segundos (1% acima de 1 s, medido em 02/10/2026)
  e travava o play do app; daqui, ~50 ms, na mesma conexão HTTP/2 dos blocos.
  Baixados assim que o quadro de 7200 é cortado (o horário está publicado),
  depois o resto das 48 h, do mais novo para o mais velho, 3 por vez. Ainda
  não espelhado: 404 com cache curto, e o app pede ao STAR.
- Só produtos, larguras e horários conhecidos (últimas 49 h); nada além do CDN
  do STAR é buscado.

  blocos.py                  serviço (porta 8090, só local; o nginx fica na frente)
  blocos.py cortar P C L     corta um quadro e sai (teste)

URL: /blocos/v2/nsa/{produto}/{AAAADDDHHMM}/{largura}/{linha}_{coluna}.jpg — é
também o caminho no disco, abaixo de BLOCOS_RAIZ (v1: o mesmo, sem progressivo).
Base inteira: /blocos/v2/nsa/{produto}/{AAAADDDHHMM}/{largura}.jpg.
"""
import contextlib
import ctypes
import ctypes.util
import http.client
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

# Versão pré-cortada (progressiva). A 1 (baseline, igual ao STAR) só sob demanda.
VERSAO = 2
VERSOES = (1, 2)
RAIZ = os.environ.get("BLOCOS_RAIZ", "/var/cache/amaview-blocos")
PASTAS = {v: os.path.join(RAIZ, "blocos", f"v{v}", "nsa") for v in VERSOES}
PORTA = int(os.environ.get("BLOCOS_PORTA", "8090"))
STAR = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/nsa"
AGENTE = "AmaView-blocos (+https://helvecioneto.github.io/AmaView/)"

PRODUTOS = (
    "GEOCOLOR", "FireTemperature", "Sandwich", "AirMass", "Dust", "DayNightCloudMicroCombo",
    "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16",
)
LARGURAS = (3600, 7200)
BLOCO = 512
# Múltiplo do MCU (16 px no 4:2:0 do STAR): o corte continua sem perda.
SOBRA = 16
JANELA = timedelta(hours=49)
# O v1 só serve o app antigo (sob demanda): 12 h bastam, e o disco não leva as
# duas versões inteiras ao mesmo tempo (~93 GB + ~70 GB num disco de 193 GB).
JANELA_V1 = timedelta(hours=float(os.environ.get("BLOCOS_JANELA_V1_H", "12")))
# Transição: as últimas horas também são pré-cortadas no v1, para o app antigo
# (em cache nos navegadores) não cair no corte sob demanda a cada quadro novo.
# O v2 sai do v1 sem baixar de novo, então custa só ~0,4 s de CPU por quadro.
# BLOCOS_PRECORTE_V1_H=0 desliga, quando ninguém mais pedir o v1.
PRECORTE_V1 = timedelta(hours=float(os.environ.get("BLOCOS_PRECORTE_V1_H", "3")))
# Pré-corte ligado por padrão; BLOCOS_AQUECER=0 deixa só o sob demanda.
AQUECER = os.environ.get("BLOCOS_AQUECER", "1") != "0"
# Espelho da base (ver espelhar_para_sempre); BLOCOS_ESPELHAR=0 desliga.
ESPELHAR = AQUECER and os.environ.get("BLOCOS_ESPELHAR", "1") != "0"
TRABALHADORES_AQUECER = 6
# Cortes simultâneos no total (cada um segura ~100 MB de coeficientes).
CORTES = threading.BoundedSemaphore(8)

# Larguras da base espelhadas inteiras (o STAR publica 450/900/1800/3600/7200).
INTEIROS = tuple(int(w) for w in os.environ.get("BLOCOS_INTEIROS", "450,900,1800").split(",") if w.strip())
TRABALHADORES_ESPELHO = 3

ROTA = re.compile(
    r"^/blocos/v(?P<versao>[12])/nsa/(?P<produto>[A-Za-z0-9]{2,24})/(?P<carimbo>\d{11})/"
    r"(?P<largura>\d{4,5})/(?P<linha>\d{1,2})_(?P<coluna>\d{1,2})\.jpg$"
)


ROTA_INTEIRO = re.compile(r"^/blocos/v2/nsa/(?P<produto>[A-Za-z0-9]{2,24})/(?P<carimbo>\d{11})/(?P<largura>\d{3,4})\.jpg$")


def altura(largura: int) -> int:
    return largura * 3 // 5


def grade(largura: int) -> tuple[int, int]:
    """(linhas, colunas) de blocos de 512 px; os da borda direita e de baixo são menores."""
    return math.ceil(altura(largura) / BLOCO), math.ceil(largura / BLOCO)


def regiao(largura: int, linha: int, coluna: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) do bloco no quadro: o miolo de 512 px mais a sobra, cortada na borda da imagem."""
    alt = altura(largura)
    x0, y0 = max(0, coluna * BLOCO - SOBRA), max(0, linha * BLOCO - SOBRA)
    x1, y1 = min(largura, (coluna + 1) * BLOCO + SOBRA), min(alt, (linha + 1) * BLOCO + SOBRA)
    return x0, y0, x1 - x0, y1 - y0


def instante(carimbo: str) -> datetime:
    """AAAADDDHHMM (dia juliano, UTC) → datetime."""
    ano, dia, hora, minuto = int(carimbo[:4]), int(carimbo[4:7]), int(carimbo[7:9]), int(carimbo[9:11])
    if not (1 <= dia <= 366 and hora < 24 and minuto < 60):
        raise ValueError(carimbo)
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def carimbo_de(t: datetime) -> str:
    return f"{t.year:04d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"


def pasta_do_quadro(produto: str, carimbo: str, largura: int, versao: int = VERSAO) -> str:
    return os.path.join(PASTAS[versao], produto, carimbo, str(largura))


def url_star(produto: str, carimbo: str, largura: int) -> str:
    return f"{STAR}/{produto}/{carimbo}_GOES19-ABI-nsa-{produto}-{largura}x{altura(largura)}.jpg"


def arquivo_inteiro(produto: str, carimbo: str, largura: int) -> str:
    """Base inteira espelhada: ao lado das pastas de blocos do mesmo horário."""
    return os.path.join(PASTAS[VERSAO], produto, carimbo, f"{largura}.jpg")


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
TJXOPT_PROGRESSIVE = 32
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


def recortar(jpeg: bytes, regioes: list[tuple[int, int, int, int]], progressivo: bool = False) -> list[bytes]:
    """
    Recortes sem perda (x e y múltiplos do MCU; 512 serve para 4:4:4 a 4:2:0).
    `progressivo`: reescreve os mesmos coeficientes em JPEG progressivo.
    """
    global _tj
    if _tj is None:
        _tj = _carregar_turbojpeg()
    n = len(regioes)
    trans = (_Transformacao * n)()
    opcoes = TJXOPT_CROP | TJXOPT_COPYNONE | (TJXOPT_PROGRESSIVE if progressivo else 0)
    for i, (x, y, w, h) in enumerate(regioes):
        trans[i].r = _Regiao(x, y, w, h)
        trans[i].options = opcoes
    saidas = (ctypes.POINTER(ctypes.c_ubyte) * n)()
    tamanhos = (ctypes.c_ulong * n)()
    # Aponta para o próprio buffer do `bytes` (só leitura para a TurboJPEG), sem
    # copiar os ~17 MB do quadro; `jpeg` segue vivo até o fim da chamada.
    origem = ctypes.cast(ctypes.c_char_p(jpeg), ctypes.POINTER(ctypes.c_ubyte))
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


class Truncado(Exception):
    """O arquivo do STAR veio incompleto duas vezes (provavelmente ainda sendo publicado)."""


# Trava por quadro, com contagem de quem a usa: some quando ninguém mais espera
# (antes, uma por quadro cortado em 48 h ficava para sempre no dicionário).
_travas: dict[tuple, list] = {}  # chave → [Lock, usuários]
_travas_lock = threading.Lock()
_estado = {
    "inicio": time.time(),
    "cortes": deque(maxlen=2000),  # (instante, segundos, origem)
    "falhas": 0,
    "ultimo_erro": None,
}


@contextlib.contextmanager
def _travado(chave: tuple):
    with _travas_lock:
        t = _travas.get(chave)
        if t is None:
            t = _travas[chave] = [threading.Lock(), 0]
        t[1] += 1
    try:
        with t[0]:
            yield
    finally:
        with _travas_lock:
            t[1] -= 1
            if t[1] == 0 and _travas.get(chave) is t:
                del _travas[chave]


def completo(jpeg: bytes) -> bool:
    """Termina no marcador EOI? (o STAR às vezes serve o arquivo ainda sendo escrito)"""
    return jpeg.rstrip(b"\0")[-2:] == b"\xff\xd9"


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


# Espera antes de baixar de novo um arquivo do STAR que veio truncado.
ESPERA_TRUNCADO = 3


def baixar_inteiro(url: str) -> bytes:
    """Baixa o JPEG; truncado (sem EOI), tenta mais uma vez e depois desiste (Truncado)."""
    for tentativa in range(2):
        jpeg = baixar(url)
        if completo(jpeg):
            return jpeg
        if tentativa == 0:
            time.sleep(ESPERA_TRUNCADO)
    raise Truncado(f"{url}: {len(jpeg)} bytes, sem o fim do JPEG")


def _gravar(destino: str, blocos: dict[str, bytes]) -> None:
    """Escreve numa pasta temporária e renomeia: a pasta final só existe completa."""
    temp = f"{destino}.parcial-{os.getpid()}-{threading.get_ident()}"
    os.makedirs(temp, mode=0o755)
    for nome, corpo in blocos.items():
        with open(os.path.join(temp, nome), "wb") as f:
            f.write(corpo)
    os.rename(temp, destino)


def _de_v1(anterior: str, destino: str, origem: str) -> str:
    """
    Transição v1 → v2: o quadro já cortado no v1 vira progressivo bloco a bloco,
    sem baixar de novo do STAR (os coeficientes são os mesmos). Se o quadro já
    passou da janela do v1, o v1 dele sai do disco na hora.
    """
    with CORTES:
        t0 = time.time()
        saida = {}
        for nome in os.listdir(anterior):
            if not nome.endswith(".jpg"):
                continue
            with open(os.path.join(anterior, nome), "rb") as f:
                bloco = f.read()
            w, h = dimensoes(bloco)
            saida[nome] = recortar(bloco, [(0, 0, w, h)], progressivo=True)[0]
        _gravar(destino, saida)
        _estado["cortes"].append((time.time(), time.time() - t0, f"{origem}-v1"))
    carimbo = os.path.basename(os.path.dirname(anterior))
    with contextlib.suppress(ValueError):
        if instante(carimbo) < datetime.now(timezone.utc) - JANELA_V1:
            shutil.rmtree(anterior, ignore_errors=True)
    return destino


def cortar(produto: str, carimbo: str, largura: int, origem: str = "pedido", versao: int = VERSAO) -> str:
    """Garante os blocos do quadro no disco e devolve a pasta. Ausente se o STAR não tem."""
    destino = pasta_do_quadro(produto, carimbo, largura, versao)
    if os.path.isdir(destino):
        return destino
    with _travado((versao, produto, carimbo, largura)):
        if os.path.isdir(destino):
            return destino
        anterior = pasta_do_quadro(produto, carimbo, largura, 1)
        if versao >= 2 and os.path.isdir(anterior):
            return _de_v1(anterior, destino, origem)
        with CORTES:
            t0 = time.time()
            jpeg = baixar_inteiro(url_star(produto, carimbo, largura))
            if dimensoes(jpeg) != (largura, altura(largura)):
                raise ValueError(f"tamanho inesperado em {produto} {carimbo} {largura}: {dimensoes(jpeg)}")
            linhas, colunas = grade(largura)
            regioes = [regiao(largura, r, c) for r in range(linhas) for c in range(colunas)]
            try:
                blocos = recortar(jpeg, regioes, progressivo=versao >= 2)
            except RuntimeError as e:
                # A TurboJPEG ainda achou o arquivo curto (EOI no lugar mas dados faltando).
                if "Premature end" in str(e):
                    raise Truncado(str(e)) from None
                raise
            del jpeg
            nomes = (f"{r}_{c}.jpg" for r in range(linhas) for c in range(colunas))
            _gravar(destino, dict(zip(nomes, blocos)))
            _estado["cortes"].append((time.time(), time.time() - t0, origem))
        if versao == VERSAO and largura == max(LARGURAS):
            _acordar_espelho.set()  # horário publicado: a base dele pode vir já
        return destino


# ---------------------------------------------------------------------------
# Pré-corte: os quadros novos assim que saem, depois o resto das últimas 48 h

_ausentes: dict[tuple, float] = {}  # (produto, carimbo, largura) → quando tentar de novo
# Horários que a NOAA já publicou (vistos no STAR), com quando foram vistos.
_publicados: dict[str, float] = {}
# Produtos já vistos no STAR num horário ainda sem o produto padrão (última hora).
_publicados_produto: dict[tuple[str, str], float] = {}
# Última sonda de cada horário (ou (produto, horário)) ainda não publicado.
_sondados: dict = {}

# Sem publicação confirmada, quando tentar de novo um horário que o STAR não
# tem: 5 min até 1 h de idade, 30 min até 6 h, depois 3 h (lacuna de verdade).
ESPERA_AUSENTE = ((timedelta(hours=1), 300), (timedelta(hours=6), 1800))
ESPERA_LACUNA = 3 * 3600
# Horário já publicado, produto que ainda não chegou: os produtos saem com
# minutos de diferença (em 22/09/2026 o GEOCOLOR voltou antes das bandas).
ESPERA_PUBLICADO = 60
# O mesmo, na última hora: o arquivo de 450 px (o da sonda) sai segundos antes
# do de 7200, e esperar 60 s deixava o quadro novo ~90 s atrás do STAR.
ESPERA_PUBLICADO_RECENTE = 15
# Até onde procurar horários publicados atrasados, e de quanto em quanto.
JANELA_ATRASADOS = timedelta(hours=6)
SONDA_A_CADA = 60
# Na última hora (sonda por produto, um HEAD de 450 px): de 20 em 20 s, para o
# quadro novo ficar pronto ~30 s depois de o STAR publicar.
SONDA_RECENTE_A_CADA = 20
# O arquivo que diz se um horário saiu: o menor do produto padrão.
PRODUTO_SONDA = "GEOCOLOR"


def espera_ausente(idade: timedelta, publicado: bool = False) -> int:
    """Segundos até tentar de novo um horário ausente dessa idade."""
    if publicado:
        return ESPERA_PUBLICADO_RECENTE if idade < JANELA_SONDA_PRODUTO else ESPERA_PUBLICADO
    for ate, segundos in ESPERA_AUSENTE:
        if idade < ate:
            return segundos
    return ESPERA_LACUNA


# Na última hora a sonda é por produto: o GEOCOLOR é o último que o STAR solta
# (medido em 02/10/2026: banda 13 às 14:01, GEOCOLOR às 14:05), e esperar por
# ele segurava as bandas 3–5 min. ~22 HEADs por horário faltante por minuto.
JANELA_SONDA_PRODUTO = timedelta(hours=1)


def _existe_no_star(carimbo: str, produto: str = PRODUTO_SONDA) -> bool:
    """O STAR tem esse horário? Um HEAD no arquivo de 450 px do produto."""
    url = f"{STAR}/{produto}/{carimbo}_GOES19-ABI-nsa-{produto}-450x270.jpg"
    pedido = urllib.request.Request(url, method="HEAD", headers={"User-Agent": AGENTE})
    try:
        with urllib.request.urlopen(pedido, timeout=15) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — 404 ou rede: não publicado (ainda)
        return False


def sondar_publicados(agora: datetime) -> list[str]:
    """
    Horários da grade (últimas 6 h) que o STAR passou a ter desde a última
    olhada: todos os produtos deles voltam para a fila já. Devolve os horários
    (ou "produto/horário", na última hora) que acabaram de aparecer.

    Em 22/09/2026 o GOES-19 parou às 09:50 UTC (manutenção no solo da NOAA) e
    voltou às 13h soltando os quadros das 12:00–12:20 de uma vez — sem mexer
    no `latest.jpg`, que ficou em 09:55. Sondar o próprio horário é o único
    sinal que enxerga quadro atrasado. Custo: um HEAD por horário faltante por
    minuto (1–2 em regime, ~36 durante uma pane de 6 h); na última hora, um por
    produto ainda faltante (as bandas saem antes do GEOCOLOR).
    """
    base = agora.replace(minute=agora.minute - agora.minute % 10, second=0, microsecond=0)
    novos = []
    sondas: list[tuple[str, str | None]] = []  # (horário, produto | None = o horário todo)
    passos = int(JANELA_ATRASADOS.total_seconds() // 600)
    for k in range(passos + 1):
        t = base - timedelta(minutes=10 * k)
        c = carimbo_de(t)
        if c in _publicados or os.path.isdir(pasta_do_quadro(PRODUTO_SONDA, c, 7200)):
            continue
        if agora - t < JANELA_SONDA_PRODUTO:
            for p in PRODUTOS:
                if (p, c) in _publicados_produto or os.path.isdir(pasta_do_quadro(p, c, 7200)):
                    continue
                if time.time() - _sondados.get((p, c), 0) < SONDA_RECENTE_A_CADA:
                    continue
                _sondados[(p, c)] = time.time()
                sondas.append((c, p))
        elif time.time() - _sondados.get(c, 0) >= SONDA_A_CADA:
            _sondados[c] = time.time()
            sondas.append((c, None))
    if not sondas:
        return novos
    with ThreadPoolExecutor(8, thread_name_prefix="sonda") as pool:
        achados = list(pool.map(lambda s: _existe_no_star(s[0]) if s[1] is None else _existe_no_star(s[0], s[1]), sondas))
    for (c, p), existe in zip(sondas, achados):
        if not existe:
            continue
        if p is None or p == PRODUTO_SONDA:
            # O produto padrão (o último a sair) apareceu: o horário todo está publicado.
            _publicados[c] = time.time()
            _sondados.pop(c, None)
            novos.append(c)
            for chave in [k2 for k2 in _ausentes if k2[1] == c]:
                _ausentes.pop(chave, None)
        else:
            _publicados_produto[(p, c)] = time.time()
            _sondados.pop((p, c), None)
            novos.append(f"{p}/{c}")
            for chave in [k2 for k2 in _ausentes if k2[0] == p and k2[1] == c]:
                _ausentes.pop(chave, None)
    return novos


def ultimos() -> dict[str, str]:
    """
    Horário mais recente já cortado de cada produto (o app usa para saber que
    há quadro novo). Conta o v2 (pré-cortado); o v1 só entra na transição, para
    produto que ainda não tenha nada no v2.
    """
    out = {}
    for p in PRODUTOS:
        for v in sorted(VERSOES, reverse=True):
            pasta = os.path.join(PASTAS[v], p)
            try:
                prontos = [c for c in os.listdir(pasta) if len(c) == 11 and c.isdigit() and os.path.isdir(os.path.join(pasta, c, "7200"))]
            except FileNotFoundError:
                continue
            if prontos:
                out[p] = max(prontos)
                break
    return out


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
        if datetime.now(timezone.utc) - instante(c) < PRECORTE_V1:
            cortar(p, c, w, origem="pre", versao=1)
        cortar(p, c, w, origem="pre")
        _ausentes.pop(chave, None)
    except Ausente:
        # O sinal principal é a sonda do horário (sondar_publicados); isto é a rede de segurança.
        publicado = c in _publicados or (p, c) in _publicados_produto
        _ausentes[chave] = time.time() + espera_ausente(datetime.now(timezone.utc) - instante(c), publicado)
    except Truncado as e:
        # O STAR ainda está escrevendo o arquivo: daqui a um minuto ele está inteiro.
        _estado["ultimo_erro"] = f"{datetime.now(timezone.utc):%H:%M:%S} {p} {c} {w}: {e}"
        _ausentes[chave] = time.time() + ESPERA_PUBLICADO
    except Exception as e:  # noqa: BLE001 — o laço não pode morrer
        _estado["falhas"] += 1
        _estado["ultimo_erro"] = f"{datetime.now(timezone.utc):%H:%M:%S} {p} {c} {w}: {e}"
        _ausentes[chave] = time.time() + 300


# ---------------------------------------------------------------------------
# Espelho da base: os arquivos inteiros do STAR, sem tocar num byte

_acordar_espelho = threading.Event()
_espelho = {"arquivos": deque(maxlen=5000), "falhas": 0, "ultimo_erro": None, "fila": None}
_conexao_star = threading.local()


def _get_star(url: str) -> tuple[int, dict[str, str], bytes]:
    """
    GET numa conexão HTTPS mantida por thread (keep-alive): o preenchimento das
    48 h são ~19 mil arquivos pequenos, e um TLS novo a cada um custaria mais
    que o próprio download. Conexão caída: abre outra e tenta uma vez.
    """
    host, caminho = url.split("://", 1)[1].split("/", 1)
    for tentativa in range(2):
        con = getattr(_conexao_star, "c", None)
        if con is None or con.host != host:
            con = _conexao_star.c = http.client.HTTPSConnection(host, timeout=60)
        try:
            con.request("GET", "/" + caminho, headers={"User-Agent": AGENTE})
            r = con.getresponse()
            corpo = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, corpo
        except (http.client.HTTPException, OSError):
            con.close()
            _conexao_star.c = None
            if tentativa:
                raise
    raise RuntimeError("inalcançável")


def _tamanho_do_etag(etag: str | None) -> int | None:
    """O nginx do STAR põe o tamanho do arquivo no ETag ("mtime-tamanho", hex)."""
    m = re.fullmatch(r'(?:W/)?"[0-9a-f]+-([0-9a-f]+)"', (etag or "").strip())
    return int(m[1], 16) if m else None


def baixar_validado(produto: str, carimbo: str, largura: int) -> bytes:
    """
    O arquivo inteiro do STAR, só se veio inteiro: tamanho igual ao do
    Content-Length e ao do ETag, termina no EOI e tem as dimensões da largura.
    """
    status, cab, corpo = _get_star(url_star(produto, carimbo, largura))
    if status == 404:
        raise Ausente(carimbo)
    if status != 200:
        raise RuntimeError(f"STAR respondeu {status}")
    declarado = cab.get("content-length")
    if declarado is not None and int(declarado) != len(corpo):
        raise Truncado(f"{len(corpo)} de {declarado} bytes")
    no_etag = _tamanho_do_etag(cab.get("etag"))
    if no_etag is not None and no_etag != len(corpo):
        raise Truncado(f"{len(corpo)} bytes, o ETag diz {no_etag}")
    if not completo(corpo):
        raise Truncado(f"{len(corpo)} bytes, sem o fim do JPEG")
    if dimensoes(corpo) != (largura, altura(largura)):
        raise ValueError(f"tamanho inesperado: {dimensoes(corpo)}")
    return corpo


def espelhar(produto: str, carimbo: str, largura: int) -> str:
    """Grava a base inteira (temporário + rename: o arquivo final só existe completo)."""
    destino = arquivo_inteiro(produto, carimbo, largura)
    if os.path.exists(destino):
        return destino
    corpo = baixar_validado(produto, carimbo, largura)
    os.makedirs(os.path.dirname(destino), mode=0o755, exist_ok=True)
    temp = f"{destino}.parcial-{os.getpid()}-{threading.get_ident()}"
    with open(temp, "wb") as f:
        f.write(corpo)
    os.rename(temp, destino)
    _espelho["arquivos"].append((time.time(), len(corpo)))
    return destino


def _pendentes_inteiros(agora: datetime) -> list[tuple[str, str, int]]:
    """Bases que faltam, do horário mais novo ao mais velho; só de horário já cortado (publicado)."""
    base = agora.replace(minute=agora.minute - agora.minute % 10, second=0, microsecond=0)
    fila = []
    passos = int(JANELA.total_seconds() // 600) - 6  # 48 h
    for k in range(passos + 1):
        c = carimbo_de(base - timedelta(minutes=10 * k))
        for p in PRODUTOS:
            if not os.path.isdir(pasta_do_quadro(p, c, max(LARGURAS))):
                continue
            for w in INTEIROS:
                chave = ("inteiro", p, c, w)
                if _ausentes.get(chave, 0) > time.time():
                    continue
                if not os.path.exists(arquivo_inteiro(p, c, w)):
                    fila.append((p, c, w))
    return fila


def _espelhar_um(item: tuple[str, str, int]) -> None:
    p, c, w = item
    chave = ("inteiro", p, c, w)
    try:
        espelhar(p, c, w)
        _ausentes.pop(chave, None)
    except Ausente:
        # O 7200 do horário existe, então o STAR publicou; essa largura deve vir já.
        _ausentes[chave] = time.time() + espera_ausente(datetime.now(timezone.utc) - instante(c), True)
    except Truncado as e:
        _espelho["ultimo_erro"] = f"{datetime.now(timezone.utc):%H:%M:%S} {p} {c} {w}: {e}"
        _ausentes[chave] = time.time() + ESPERA_PUBLICADO_RECENTE
    except Exception as e:  # noqa: BLE001 — o laço não pode morrer
        _espelho["falhas"] += 1
        _espelho["ultimo_erro"] = f"{datetime.now(timezone.utc):%H:%M:%S} {p} {c} {w}: {e}"
        _ausentes[chave] = time.time() + 300


def espelhar_para_sempre() -> None:
    # Poucos por vez: o STAR é um servidor de origem só (sem CDN na frente).
    with ThreadPoolExecutor(TRABALHADORES_ESPELHO, thread_name_prefix="espelho") as pool:
        while True:
            fila = _pendentes_inteiros(datetime.now(timezone.utc))
            _espelho["fila"] = len(fila)
            # Lote curto: um horário novo (acordado pelo corte) passa na frente do passado.
            list(pool.map(_espelhar_um, fila[: TRABALHADORES_ESPELHO * 4]))
            if not fila:
                _acordar_espelho.wait(10)
            _acordar_espelho.clear()


def inteiros_anunciados(agora: float) -> list[int]:
    """Larguras que o app pode pedir aqui: as espelhadas, se o espelho gravou na última meia hora."""
    if not ESPELHAR or not INTEIROS:
        return []
    feitos = _espelho["arquivos"]
    return list(INTEIROS) if feitos and agora - feitos[-1][0] < 1800 else []


def limpar(agora: datetime) -> int:
    """
    Apaga quadros fora da janela (v2: 49 h; v1: 12 h, desde que o v2 do mesmo
    quadro já exista) e restos de cortes interrompidos.
    """
    removidos = 0
    limite = agora - JANELA
    limite_v1 = agora - JANELA_V1
    for v in VERSOES:
        raiz = PASTAS[v]
        if not os.path.isdir(raiz):
            continue
        for p in os.listdir(raiz):
            pp = os.path.join(raiz, p)
            for c in os.listdir(pp):
                cp = os.path.join(pp, c)
                try:
                    t = instante(c)
                    velho = t < limite
                except ValueError:
                    velho = True
                if velho:
                    shutil.rmtree(cp, ignore_errors=True)
                    removidos += 1
                    continue
                for w in os.listdir(cp):
                    wp = os.path.join(cp, w)
                    if ".parcial-" in w:
                        if time.time() - os.path.getmtime(wp) > 600:
                            if os.path.isdir(wp):
                                shutil.rmtree(wp, ignore_errors=True)
                            else:
                                with contextlib.suppress(OSError):
                                    os.remove(wp)
                    elif v < VERSAO and t < limite_v1 and w.isdigit() and os.path.isdir(pasta_do_quadro(p, c, int(w))):
                        shutil.rmtree(wp, ignore_errors=True)
                with contextlib.suppress(OSError):
                    os.rmdir(cp)  # só se ficou vazia
    # Chaves do corte: (produto, carimbo, largura); do espelho: ("inteiro", produto, carimbo, largura).
    for chave in [k for k in _ausentes if instante(k[-2]) < limite]:
        _ausentes.pop(chave, None)
    for c in [c for c in _publicados if instante(c) < limite]:
        _publicados.pop(c, None)
    for chave in [k for k in _publicados_produto if instante(k[1]) < limite]:
        _publicados_produto.pop(chave, None)
    for chave in [k for k in _sondados if instante(k[1] if isinstance(k, tuple) else k) < limite]:
        _sondados.pop(chave, None)
    return removidos


def aquecer_para_sempre() -> None:
    with ThreadPoolExecutor(TRABALHADORES_AQUECER, thread_name_prefix="pre") as pool:
        ultima_limpeza = 0.0
        while True:
            agora = datetime.now(timezone.utc)
            # A cada 10 min: na transição v1 → v2, o v1 já convertido sai logo do disco.
            if time.time() - ultima_limpeza > 600:
                limpar(agora)
                ultima_limpeza = time.time()
            sondar_publicados(agora)
            fila = _pendentes(agora)
            _estado["fila"] = len(fila)
            # Lote pequeno: o quadro novo nunca espera o preenchimento do passado.
            # 24 = 4 por trabalhador: com o passado ainda por preencher (ou a
            # transição v1 → v2), a volta leva ~15 s e não ~1 min (com 66, o
            # GEOCOLOR novo esperava ~60 s em vez de ~15 s).
            list(pool.map(_aquecer_um, fila[:24]))
            time.sleep(2 if fila else 10)


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
        # Bases inteiras que o app pode pedir aqui (/{produto}/{carimbo}/{largura}.jpg).
        "inteiros": inteiros_anunciados(agora),
        "espelho": {
            "arquivos_ultima_hora": sum(1 for t, _ in _espelho["arquivos"] if agora - t < 3600),
            "fila": _espelho["fila"],
            "falhas": _espelho["falhas"],
            "ultimo_erro": _espelho["ultimo_erro"],
        },
        **nivel_meio_km(agora),
    }


# Banda 02 a 0,5 km (meio_km.py, timer próprio): o nível 14400 é anunciado
# enquanto a rodada dele estiver em dia. Parada há mais de 30 min, o AmaView
# volta a parar no 7200 (o que já existe no disco continua servido).
ESTADO_MEIO_KM = os.path.join(RAIZ, "meio-km.json")
NIVEIS_MEIO_KM = {"02": [3600, 7200, 14400]}


def nivel_meio_km(agora: float) -> dict:
    try:
        with open(ESTADO_MEIO_KM) as f:
            e = json.load(f)
        fresco = agora - os.path.getmtime(ESTADO_MEIO_KM) < 1800 and e.get("quadros", 0) > 0
    except (OSError, ValueError):
        return {"niveis": {}}
    resumo = {k: e.get(k) for k in ("atualizado", "ultimo", "quadros", "ultimo_feito")}
    return {"niveis": NIVEIS_MEIO_KM if fresco else {}, "meio_km": resumo}


class Pedido(http.server.BaseHTTPRequestHandler):
    server_version = "AmaView-blocos"

    def _responder(self, codigo: int, corpo: bytes, tipo: str, cache: str, extras: dict | None = None) -> None:
        try:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", cache)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Timing-Allow-Origin", "*")
            for nome, valor in (extras or {}).items():
                self.send_header(nome, valor)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corpo)
        except (BrokenPipeError, ConnectionResetError):
            # O navegador desistiu (mexeu no mapa) enquanto o quadro era cortado: normal.
            self.close_connection = True
            sys.stderr.write(f"{self.address_string()} \"{self.requestline}\" cancelado pelo cliente\n")

    def _erro(self, codigo: int, texto: str, cache: str = "no-store", extras: dict | None = None) -> None:
        self._responder(codigo, texto.encode(), "text/plain; charset=utf-8", cache, extras)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        caminho = self.path.split("?", 1)[0]
        if caminho == "/blocos/saude":
            return self._responder(200, json.dumps(saude()).encode(), "application/json", "no-store")
        if caminho in ("/blocos/v1/ultimos", "/blocos/v2/ultimos"):
            # O app pergunta a cada minuto se há quadro novo: vale mais que o latest.jpg do STAR.
            return self._responder(200, json.dumps(ultimos()).encode(), "application/json", "no-store")
        m = ROTA_INTEIRO.match(caminho)
        if m:
            # O nginx serve do disco; aqui só cai a base ainda não espelhada (o app pede ao STAR).
            if m["produto"] in PRODUTOS and int(m["largura"]) in INTEIROS:
                return self._erro(404, "base ainda não espelhada", "public, max-age=60")
            return self._erro(404, "produto ou largura desconhecidos")
        m = ROTA.match(caminho)
        if not m:
            return self._erro(404, "rota desconhecida")
        produto, carimbo, versao = m["produto"], m["carimbo"], int(m["versao"])
        largura, linha, coluna = int(m["largura"]), int(m["linha"]), int(m["coluna"])
        if produto == "02" and largura == 14400:
            # Gerado pelo meio_km.py (o nginx serve do disco); aqui só o que falta:
            # quadro ainda por gerar, ou de noite (o app fica no 7200).
            return self._erro(404, "0,5 km ainda não gerado para este horário", "public, max-age=60")
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
            pasta = cortar(produto, carimbo, largura, versao=versao)
        except Ausente:
            # Pode sair daqui a pouco: cache curto, para o navegador tentar de novo.
            return self._erro(404, "o STAR ainda não publicou este quadro", "public, max-age=60")
        except Truncado as e:
            # O STAR ainda está escrevendo o arquivo: não é falha deste servidor.
            _estado["ultimo_erro"] = f"{agora:%H:%M:%S} {produto} {carimbo} {largura}: {e}"
            return self._erro(503, "o STAR ainda está publicando este quadro", "no-store", {"Retry-After": "30"})
        except Exception as e:  # noqa: BLE001
            _estado["falhas"] += 1
            _estado["ultimo_erro"] = f"{agora:%H:%M:%S} {produto} {carimbo} {largura}: {e}"
            return self._erro(502, "falha ao cortar o quadro")
        with open(os.path.join(pasta, f"{linha}_{coluna}.jpg"), "rb") as f:
            corpo = f.read()
        self._responder(200, corpo, "image/jpeg", "public, max-age=31536000, immutable")

    def handle(self) -> None:
        # A conexão caída depois da resposta (no flush final) também não vira traceback.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            super().handle()

    def log_message(self, formato: str, *args) -> None:
        # Só o que passou pelo corte sob demanda chega aqui; uma linha curta basta.
        sys.stderr.write(f"{self.address_string()} {formato % args}\n")


def main() -> None:
    if len(sys.argv) == 5 and sys.argv[1] == "cortar":
        t0 = time.time()
        pasta = cortar(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        print(f"{pasta}: {len(os.listdir(pasta))} blocos em {time.time() - t0:.2f} s")
        return
    os.makedirs(PASTAS[VERSAO], exist_ok=True)
    if AQUECER:
        threading.Thread(target=aquecer_para_sempre, name="pre-corte", daemon=True).start()
    if ESPELHAR:
        threading.Thread(target=espelhar_para_sempre, name="espelho", daemon=True).start()
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    servidor.daemon_threads = True
    print(
        f"blocos v{VERSAO} em 127.0.0.1:{PORTA}, disco em {PASTAS[VERSAO]}, "
        f"pré-corte {'ligado' if AQUECER else 'desligado'}, espelho {list(INTEIROS) if ESPELHAR else 'desligado'}"
    )
    servidor.serve_forever()


if __name__ == "__main__":
    main()
