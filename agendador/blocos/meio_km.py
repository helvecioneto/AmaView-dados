#!/usr/bin/env python3
"""
Banda 02 (vermelho visível) a 0,5 km para o AmaView: o nível 14400 dos blocos.

O STAR publica o setor NSA em até 7200 px — a grade de 1 km do ABI. Só a banda
02 é medida a 0,5 km; aqui ela vem do produto da NOAA (ABI-L2-CMIPF, canal 02,
no balde público `noaa-goes19`) e vira blocos de 14400 × 8640 px, no MESMO
esquema dos do STAR (512 px de miolo + 16 de sobra, JPEG progressivo, cache
imutável), servidos pelo nginx ao lado deles:

  /blocos/v2/nsa/02/{AAAADDDHHMM}/14400/{linha}_{coluna}.jpg

- **Mesma extensão do 7200, 2×2 px por px.** O setor do STAR começa na coluna
  3975 e na linha 3264 da grade de 1 km (src/config/georef.json do AmaView);
  na de 0,5 km, 7950 e 6528. Medido: reduzido 2×2, o 14400 cai sobre o 7200 do
  STAR com deslocamento abaixo de ¼ px de 7200 (~0,2 km).
- **O mesmo brilho do STAR.** A refletância vira cinza por uma tabela
  (`lut_02.json`, 4096 valores do CMI) ajustada contra o próprio 7200 do STAR
  em vários horários (`meio_km.py calibrar`), monotônica.
- **O que é desenho do STAR vem do STAR.** As linhas de fronteira e costa
  (brancas, 255), o logotipo e o rodapé, e o espaço fora do disco, saem do
  7200 do STAR ampliado 2×: de perto, nada some nem muda de lugar.
- **Leve na leitura.** O netCDF (~410 MB) é lido por byte-range, só nas linhas
  do setor (o CMI vem em blocos de 6 linhas inteiras: ~40% do arquivo), direto
  na memória; nada toca o disco além dos blocos prontos.
- **Só de dia.** Quadro com o sol abaixo do horizonte em todo o setor não é
  gerado (404; o AmaView fica no 7200).
- Isto é renderização do AmaView (a única do satélite): JPEG q92, 1 canal.

Roda pelo `amaview-meio-km.timer` (a cada 2 min), como usuário amaview-blocos,
no mesmo /var/cache/amaview-blocos. O `/blocos/saude` anuncia o nível
(`niveis`) enquanto o estado (`meio-km.json`) estiver fresco.

  meio_km.py                        uma rodada (o que o timer chama)
  meio_km.py quadro AAAADDDHHMM     gera um quadro (teste)
  meio_km.py calibrar CARIMBO...    ajusta a tabela de cinza contra o STAR
"""
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tj  # noqa: E402

RAIZ = os.environ.get("BLOCOS_RAIZ", "/var/cache/amaview-blocos")
PASTA = os.path.join(RAIZ, "blocos", "v2", "nsa", "02")
ESTADO = os.path.join(RAIZ, "meio-km.json")
BALDE = "https://noaa-goes19.s3.amazonaws.com"
PRODUTO_S3 = "ABI-L2-CMIPF"
STAR = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/nsa/02"
AGENTE = "AmaView-meio-km (+https://helvecioneto.github.io/AmaView/)"
LUT_ARQUIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lut_02.json")

LARGURA, ALTURA = 14400, 8640
BLOCO, SOBRA = 512, 16
QUALIDADE = int(os.environ.get("MEIOKM_QUALIDADE", "92"))
RETENCAO = timedelta(hours=float(os.environ.get("MEIOKM_RETENCAO_H", "48")))
# Por rodada: o quadro novo primeiro; o passado, no tempo que sobrar.
TEMPO_RODADA_S = float(os.environ.get("MEIOKM_TEMPO_RODADA_S", "100"))
TRABALHADORES_JPEG = 2

# Setor NSA (georef.json do AmaView): bordas em ângulo de varredura (rad).
NSA_X_MIN, NSA_Y_MAX = -0.040572, 0.06048
PASSO = 14e-6  # 0,5 km no nadir
# Decoração do 7200 do STAR (georef.json, decorationsBySize["7200"]).
RODAPE_7200 = 44
LOGO_7200 = (12, 3925, 362, 4275)  # x0, y0, x1, y1
# Linhas do mapa do STAR (fronteiras e costa): opacas, ~230 de cinza, sempre
# no mesmo lugar. Num quadro de noite (o dado é ~0) elas são tudo o que passa
# deste limiar; o mapa sai dali e fica em LINHAS por uma semana.
LIMIAR_LINHA_NOITE = 50
LINHAS = os.path.join(RAIZ, "meio-km-linhas.npz")
LINHAS_VALIDADE_S = 7 * 86400
# Sol a menos de 2° acima do horizonte em todo o setor: noite.
ZENITE_NOITE = 88.0

ARQUIVO = re.compile(r"OR_ABI-L2-CMIPF-M\d+C02_G19_s(\d{4})(\d{3})(\d{2})(\d{2})\d{3}_e\d{14}_c\d{14}\.nc$")


def carimbo_de(t: datetime) -> str:
    return f"{t.year:04d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"


def instante(carimbo: str) -> datetime:
    ano, dia, hora, minuto = int(carimbo[:4]), int(carimbo[4:7]), int(carimbo[7:9]), int(carimbo[9:11])
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def carimbo_do_arquivo(nome: str) -> str | None:
    m = ARQUIVO.search(nome)
    return "".join(m.groups()) if m else None


def grade() -> tuple[int, int]:
    return math.ceil(ALTURA / BLOCO), math.ceil(LARGURA / BLOCO)


def regiao(linha: int, coluna: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) do bloco: miolo de 512 px mais a sobra, cortada na borda (como no blocos.py)."""
    x0, y0 = max(0, coluna * BLOCO - SOBRA), max(0, linha * BLOCO - SOBRA)
    x1, y1 = min(LARGURA, (coluna + 1) * BLOCO + SOBRA), min(ALTURA, (linha + 1) * BLOCO + SOBRA)
    return x0, y0, x1 - x0, y1 - y0


def origem_na_grade(x_offset: float, y_offset: float, passo: float = PASSO) -> tuple[int, int]:
    """
    (coluna, linha) do canto superior esquerdo do setor na grade do arquivo.
    `x_offset`/`y_offset` são os do netCDF (centro do 1º pixel).
    """
    col = (NSA_X_MIN - (x_offset - passo / 2)) / passo
    lin = ((y_offset + passo / 2) - NSA_Y_MAX) / passo
    c, l = round(col), round(lin)
    if abs(col - c) > 0.01 or abs(lin - l) > 0.01:
        raise ValueError(f"setor fora da grade de 0,5 km: coluna {col:.3f}, linha {lin:.3f}")
    return c, l


# ---------------------------------------------------------------------------
# Rede


def _pedir(url: str, inicio: int | None = None, fim: int | None = None, tentativas: int = 3) -> bytes:
    cab = {"User-Agent": AGENTE}
    if inicio is not None:
        cab["Range"] = f"bytes={inicio}-{fim - 1}"
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
            time.sleep(1 + 2 * i)
    raise RuntimeError("inalcançável")


def baixar_paralelo(url: str, tamanho: int, partes: int = 8) -> bytearray:
    """
    O arquivo inteiro na memória, em `partes` byte-ranges simultâneos. Medido
    na máquina (02/10/2026, ~120 ms de ida e volta até o S3): 410 MB em 3,6 s.
    Ler só as linhas do setor por byte-range custava ~40 s: o índice de chunks
    do HDF5 fica espalhado pelo arquivo, e cada salto é um pedido novo.
    """
    buf = bytearray(tamanho)
    passo = -(-tamanho // partes)

    def parte(i: int) -> None:
        a, b = i * passo, min(tamanho, (i + 1) * passo)
        dado = _pedir(url, a, b)
        if len(dado) != b - a:
            raise ConnectionError(f"{url}: {len(dado)} de {b - a} bytes na parte {i}")
        buf[a:b] = dado

    with ThreadPoolExecutor(partes) as pool:
        list(pool.map(parte, range(partes)))
    return buf


def listar(prefixo: str) -> dict[str, tuple[str, int]]:
    """carimbo → (chave, tamanho) dos arquivos do canal 02 de uma hora."""
    xml = _pedir(f"{BALDE}/?list-type=2&prefix={prefixo}").decode()
    out = {}
    for chave, tam in re.findall(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", xml):
        c = carimbo_do_arquivo(chave)
        if c:
            out[c] = (chave, int(tam))
    return out


def prefixo_da_hora(t: datetime) -> str:
    return f"{PRODUTO_S3}/{t.year:04d}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/OR_{PRODUTO_S3}-M6C02"


def star_7200(carimbo: str) -> np.ndarray | None:
    """O 7200 do STAR em cinza, ou None se ainda não saiu (ou veio truncado)."""
    url = f"{STAR}/{carimbo}_GOES19-ABI-nsa-02-7200x4320.jpg"
    try:
        jpeg = _pedir(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if not jpeg.rstrip(b"\0").endswith(b"\xff\xd9"):
        return None
    return tj.decodificar_cinza(jpeg)


def ler_setor(chave: str, tamanho: int) -> tuple[np.ndarray, dict]:
    """CMI (int16, cru) do setor: ALTURA linhas a partir da origem, da coluna da origem ao fim do disco."""
    import h5py

    buf = baixar_paralelo(f"{BALDE}/{chave}", tamanho)
    with h5py.File(io.BytesIO(buf), "r") as f:
        ds = f["CMI"]
        col0, lin0 = origem_na_grade(float(f["x"].attrs["add_offset"][0]), float(f["y"].attrs["add_offset"][0]))
        # Só os chunks dessas linhas são descomprimidos (~1,7 s no ARM).
        cmi = ds[lin0 : lin0 + ALTURA, col0:]
    del buf
    return cmi, {"baixado_mb": round(tamanho / 1e6, 1)}


# ---------------------------------------------------------------------------
# Sol


def geos_para_lonlat(x, y, proj):
    """Ângulos de varredura (rad) → (lon, lat) em graus; fora do disco sai NaN (fumaca.py)."""
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


PROJ_GOES19 = {
    "semi_major_axis": 6378137.0,
    "semi_minor_axis": 6356752.31414,
    "perspective_point_height": 35786023.0,
    "longitude_of_projection_origin": -75.0,
}


def zenite_minimo(t: datetime, proj: dict = PROJ_GOES19) -> float:
    """Menor ângulo zenital do sol (graus) numa grade grossa do setor (parte no disco)."""
    xs = np.linspace(NSA_X_MIN, NSA_X_MIN + LARGURA * PASSO, 25)
    ys = np.linspace(NSA_Y_MAX, NSA_Y_MAX - ALTURA * PASSO, 15)
    lon, lat = geos_para_lonlat(*np.meshgrid(xs, ys), proj)
    dia = t.timetuple().tm_yday + (t.hour + t.minute / 60) / 24
    g = 2 * math.pi / 365 * (dia - 1)
    decl = 0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    hora_solar = (t.hour * 60 + t.minute + eqt + 4 * lon) / 60
    ha = np.radians(15 * (hora_solar - 12))
    la = np.radians(lat)
    cosz = np.sin(la) * math.sin(decl) + np.cos(la) * math.cos(decl) * np.cos(ha)
    return float(np.degrees(np.arccos(np.clip(np.nanmax(cosz), -1, 1))))


# ---------------------------------------------------------------------------
# Imagem


def carregar_lut() -> np.ndarray:
    with open(LUT_ARQUIVO) as f:
        return np.array(json.load(f)["cinza"], np.uint8)


def reduzir(cmi: np.ndarray) -> np.ndarray:
    """
    CMI cru (ALTURA × n) → média 2×2 em float32 (4320 × 7200), NaN onde algum
    dos 4 pixels de 0,5 km está fora do disco (ou sem dado).
    """
    n = cmi.shape[1] - cmi.shape[1] % 2
    q = [cmi[i::2, j:n:2] for i in (0, 1) for j in (0, 1)]
    soma = q[0].astype(np.int32)
    validos = q[0] >= 0
    for v in q[1:]:
        soma += v
        validos &= v >= 0
    out = np.full((ALTURA // 2, LARGURA // 2), np.nan, np.float32)
    out[:, : n // 2] = np.where(validos, soma / np.float32(4), np.nan)
    return out


def linhas_de_noite(noite: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Quadro de noite do STAR → (máscara, cinza) das linhas do mapa (o rodapé fica de fora)."""
    m = noite >= LIMIAR_LINHA_NOITE
    m[noite.shape[0] - RODAPE_7200 :] = False
    return m, noite


def mapa_de_linhas(agora: datetime | None = None) -> tuple[np.ndarray, np.ndarray]:
    """As linhas do STAR, do arquivo (até uma semana) ou do quadro mais recente todo de noite."""
    try:
        if time.time() - os.path.getmtime(LINHAS) < LINHAS_VALIDADE_S:
            with np.load(LINHAS) as z:
                return z["mascara"], z["cinza"]
    except (OSError, ValueError, KeyError):
        pass
    agora = agora or datetime.now(timezone.utc)
    t = agora.replace(minute=agora.minute - agora.minute % 10, second=0, microsecond=0) - timedelta(minutes=30)
    for _ in range(48 * 6):
        if zenite_minimo(t + timedelta(minutes=5)) > 100:
            noite = star_7200(carimbo_de(t))
            if noite is not None:
                m, cinza = linhas_de_noite(noite)
                temp = f"{LINHAS}.parcial-{os.getpid()}.npz"
                np.savez_compressed(temp, mascara=m, cinza=cinza)
                os.replace(temp, LINHAS)
                return m, cinza
        t -= timedelta(minutes=10)
    raise RuntimeError("nenhum quadro de noite do STAR nas últimas 48 h para tirar as linhas do mapa")


def mascara_do_star(previsto: np.ndarray, linhas: np.ndarray) -> np.ndarray:
    """
    Pixels de 7200 que são desenho do STAR, não dado: linhas do mapa, logotipo,
    rodapé e o que fica fora do disco (ou sem dado).
    """
    m = linhas | ~np.isfinite(previsto)
    m[m.shape[0] - RODAPE_7200 :] = True
    x0, y0, x1, y1 = LOGO_7200
    m[y0:y1, x0:x1] = True
    return m


def compor(cmi: np.ndarray, star: np.ndarray, lut: np.ndarray, linhas: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """O quadro de 14400 × 8640 em cinza: o dado a 0,5 km pela tabela, e o desenho do STAR por cima."""
    out = np.zeros((ALTURA, LARGURA), np.uint8)
    n = cmi.shape[1]
    for y in range(0, ALTURA, 1024):  # em faixas: o índice da tabela não dobra a memória
        out[y : y + 1024, :n] = lut[np.clip(cmi[y : y + 1024], 0, 4095)]
    previsto = np.interp(reduzir(cmi), np.arange(4096), lut.astype(np.float32)).astype(np.float32)
    mascara, cinza = linhas
    m = mascara_do_star(previsto, mascara)
    # As linhas no cinza delas (sem o ruído do JPEG do dia); o resto, do quadro do STAR.
    fonte = np.where(mascara, cinza, star)
    # Cada pixel de 7200 cobre 2×2 do 14400: escreve pela vista 4D, sem ampliar nada na memória.
    np.copyto(out.reshape(ALTURA // 2, 2, LARGURA // 2, 2), fonte[:, None, :, None], where=m[:, None, :, None])
    return out


def blocos_do_quadro(img: np.ndarray) -> dict[str, bytes]:
    linhas, colunas = grade()
    tarefas = [(l, c) for l in range(linhas) for c in range(colunas)]

    def um(lc):
        x, y, w, h = regiao(*lc)
        return f"{lc[0]}_{lc[1]}.jpg", tj.codificar_cinza(img[y : y + h, x : x + w], QUALIDADE, True)

    with ThreadPoolExecutor(TRABALHADORES_JPEG) as pool:
        return dict(pool.map(um, tarefas))


def gravar(destino: str, blocos: dict[str, bytes]) -> None:
    """Pasta temporária e rename (o `limpar` do blocos.py apaga `.parcial-` esquecido)."""
    temp = f"{destino}.parcial-{os.getpid()}"
    shutil.rmtree(temp, ignore_errors=True)
    os.makedirs(temp, mode=0o755)
    for nome, corpo in blocos.items():
        with open(os.path.join(temp, nome), "wb") as f:
            f.write(corpo)
    os.rename(temp, destino)


def pasta_do_quadro(carimbo: str) -> str:
    return os.path.join(PASTA, carimbo, str(LARGURA))


def gerar(carimbo: str, chave: str, tamanho: int, lut: np.ndarray, linhas: tuple[np.ndarray, np.ndarray]) -> dict | None:
    """Gera o quadro; None se o 7200 do STAR ainda não saiu (tentar na próxima rodada)."""
    t0, c0 = time.time(), time.process_time()
    star = star_7200(carimbo)
    if star is None:
        return None
    if star.shape != (ALTURA // 2, LARGURA // 2):
        raise ValueError(f"7200 do STAR com tamanho inesperado: {star.shape}")
    cmi, meta = ler_setor(chave, tamanho)
    t_leitura = time.time() - t0
    img = compor(cmi, star, lut, linhas)
    del cmi
    blocos = blocos_do_quadro(img)
    gravar(pasta_do_quadro(carimbo), blocos)
    return {
        "carimbo": carimbo,
        "s": round(time.time() - t0, 1),
        "s_leitura": round(t_leitura, 1),
        "cpu_s": round(time.process_time() - c0, 1),
        "s3_mb": meta["baixado_mb"],
        "disco_mb": round(sum(len(b) for b in blocos.values()) / 1e6, 1),
    }


# ---------------------------------------------------------------------------
# Rodada (timer)


def ler_json(caminho: str, padrao):
    try:
        with open(caminho) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def escrever_estado(estado: dict) -> None:
    temp = f"{ESTADO}.parcial-{os.getpid()}"
    with open(temp, "w") as f:
        json.dump(estado, f)
    os.replace(temp, ESTADO)


def limpar(agora: datetime) -> int:
    removidos = 0
    if not os.path.isdir(PASTA):
        return 0
    for c in os.listdir(PASTA):
        p = os.path.join(PASTA, c, str(LARGURA))
        try:
            velho = instante(c) < agora - RETENCAO
        except ValueError:
            continue
        if velho and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            removidos += 1
    return removidos


def rodada(agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    inicio = time.time()
    estado = ler_json(ESTADO, {})
    noites = estado.get("noites", {})
    lut = carregar_lut()
    linhas = None
    limpar(agora)
    # Horários de dia dentro da retenção, do mais novo para o mais velho.
    candidatos = []
    t = agora.replace(minute=agora.minute - agora.minute % 10, second=0, microsecond=0)
    while t >= agora - RETENCAO:
        c = carimbo_de(t)
        if not os.path.isdir(pasta_do_quadro(c)) and c not in noites:
            if zenite_minimo(t + timedelta(minutes=5)) > ZENITE_NOITE:
                noites[c] = 1
            else:
                candidatos.append(t)
        t -= timedelta(minutes=10)
    listagens: dict[str, dict] = {}
    feitos, erros = [], []
    for t in candidatos:
        if time.time() - inicio > TEMPO_RODADA_S:
            break
        pref = prefixo_da_hora(t)
        if pref not in listagens:
            try:
                listagens[pref] = listar(pref)
            except Exception as e:  # noqa: BLE001
                erros.append(f"listagem {pref}: {e}")
                listagens[pref] = {}
        c = carimbo_de(t)
        arq = listagens[pref].get(c)
        if not arq:
            continue
        try:
            linhas = linhas or mapa_de_linhas(agora)
            r = gerar(c, *arq, lut, linhas)
        except Exception as e:  # noqa: BLE001
            erros.append(f"{c}: {e}")
            continue
        if r:
            feitos.append(r)
            print(json.dumps(r), flush=True)
    limite = carimbo_de(agora - RETENCAO)
    noites = {c: 1 for c in noites if c >= limite}
    no_disco = sorted(c for c in os.listdir(PASTA) if os.path.isdir(pasta_do_quadro(c))) if os.path.isdir(PASTA) else []
    estado = {
        "atualizado": agora.isoformat(timespec="seconds"),
        "ultimo": no_disco[-1] if no_disco else None,
        "quadros": len(no_disco),
        "feitos": len(feitos),
        "ultimo_feito": feitos[0] if feitos else estado.get("ultimo_feito"),
        "erros": erros[-5:],
        "noites": noites,
    }
    escrever_estado(estado)
    for e in erros:
        print(e, file=sys.stderr, flush=True)
    return estado


# ---------------------------------------------------------------------------
# Calibração da tabela


def _pav(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Regressão isotônica (crescente), pool-adjacent-violators."""
    blocos = []  # [média, peso, n]
    for yi, wi in zip(y, w):
        blocos.append([yi, wi, 1])
        while len(blocos) > 1 and blocos[-2][0] > blocos[-1][0]:
            m2, w2, n2 = blocos.pop()
            m1, w1, n1 = blocos.pop()
            blocos.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    return np.concatenate([np.full(n, m) for m, _, n in blocos])


def ajustar_lut(pares: list[tuple[np.ndarray, np.ndarray]], linhas: np.ndarray | None = None) -> np.ndarray:
    """
    (CMI reduzido 2×2, 7200 do STAR) de vários quadros → tabela de 4096 cinzas.
    Mediana do STAR por faixa de 4 valores do CMI, longe das linhas e do rodapé,
    e depois monotônica.
    """
    soma_b, soma_s = [], []
    for red, star in pares:
        ok = np.isfinite(red)
        if linhas is not None:
            ok &= ~_dilatar(linhas)
        ok[-RODAPE_7200 - 4 :] = False
        soma_b.append(np.clip(np.round(red[ok] / 4).astype(np.int32), 0, 1023))
        soma_s.append(star[ok])
    b, s = np.concatenate(soma_b), np.concatenate(soma_s)
    ordem = np.argsort(b, kind="stable")
    b, s = b[ordem], s[ordem]
    lim = np.searchsorted(b, np.arange(1025))
    xs, ys, ws = [], [], []
    for k in range(1024):
        n = lim[k + 1] - lim[k]
        if n >= 50:
            xs.append(k * 4)
            ys.append(float(np.median(s[lim[k] : lim[k + 1]])))
            ws.append(n)
    yi = _pav(np.array(ys), np.array(ws, float))
    return np.clip(np.round(np.interp(np.arange(4096), xs, yi)), 0, 255).astype(np.uint8)


def _dilatar(m: np.ndarray) -> np.ndarray:
    d = m.copy()
    d[1:] |= m[:-1]
    d[:-1] |= m[1:]
    e = d.copy()
    e[:, 1:] |= d[:, :-1]
    e[:, :-1] |= d[:, 1:]
    return e


def erro_contra_star(red: np.ndarray, star: np.ndarray, lut: np.ndarray, linhas: np.ndarray) -> dict:
    prev = np.interp(red, np.arange(4096), lut.astype(np.float32))
    m = mascara_do_star(prev, _dilatar(linhas))
    ok = ~m & np.isfinite(prev)
    e = star.astype(np.float32) - prev
    # Brilho (o que a tabela controla): médias de 8×8 px, sem a textura nem o ruído do JPEG.
    h, w = (star.shape[0] // 8) * 8, (star.shape[1] // 8) * 8
    def media8(a):
        return a[:h, :w].reshape(h // 8, 8, w // 8, 8).mean(axis=(1, 3))
    ok8 = media8(ok.astype(np.float32)) == 1
    e8 = media8(np.where(ok, e, 0))
    return {
        "erro_px": round(float(np.abs(e[ok]).mean()), 2),
        "vies": round(float(e[ok].mean()), 2),
        "erro_8x8": round(float(np.abs(e8[ok8]).mean()), 2),
        "mascara_pct": round(float(m.mean() * 100), 2),
    }


def calibrar(carimbos: list[str]) -> None:
    pares = []
    for c in carimbos:
        t = instante(c)
        arq = listar(prefixo_da_hora(t)).get(c)
        star = star_7200(c)
        if not arq or star is None:
            print(f"{c}: sem arquivo ou sem 7200 do STAR", file=sys.stderr)
            continue
        cmi, _ = ler_setor(*arq)
        pares.append((c, reduzir(cmi), star))
        print(f"{c}: lido", file=sys.stderr, flush=True)
    linhas, _ = mapa_de_linhas()
    lut = ajustar_lut([(r, s) for _, r, s in pares], linhas)
    for c, red, star in pares:
        print(c, erro_contra_star(red, star, lut, linhas), file=sys.stderr)
    with open(LUT_ARQUIVO, "w") as f:
        json.dump({"ajustada_com": carimbos, "cinza": lut.tolist()}, f, separators=(",", ":"))
        f.write("\n")


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "calibrar":
        return calibrar(sys.argv[2:])
    if len(sys.argv) == 3 and sys.argv[1] == "quadro":
        c = sys.argv[2]
        arq = listar(prefixo_da_hora(instante(c))).get(c)
        if not arq:
            sys.exit(f"{c}: sem arquivo no S3")
        print(json.dumps(gerar(c, *arq, carregar_lut(), mapa_de_linhas())))
        return
    rodada()


if __name__ == "__main__":
    main()
