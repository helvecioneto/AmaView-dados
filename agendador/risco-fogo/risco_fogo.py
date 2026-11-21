#!/usr/bin/env python3
"""
Risco de fogo e dias sem chuva do INPE (Programa Queimadas), recortados na
Amazônia Legal, para a camada `src/riscoFogo` do AmaView.

O INPE publica todo dia, por volta das 3h30 de Brasília, o dia anterior
observado (a pasta `previsto/` parou em 2023):

- `risco_fogo/INPE_FireRiskModel_2.2_FireRisk_AAAAMMDD.nc` — NetCDF4 (HDF5),
  `rf` de 0 a 1, grade de 0,01° (~12 MB, a América do Sul inteira);
- `numero_dias_sem_chuva/INPE_FireRiskModel_2.2_NDWR_AAAAMMDD.nc` — NetCDF3,
  `ndsc120`, dias seguidos com chuva ≤ 1 mm (IMERG), de 0 a 120, grade de 0,1°.

O arquivo é baixado para a MEMÓRIA, recortado na caixa da Amazônia Legal (com
folga de 0,5°) e descartado — nenhum NetCDF toca o disco. Cada dia vira um PNG
em tons de cinza com o valor no byte, sem reamostrar nem corrigir nada:

- risco: byte = round(rf × 200) (0–200);
- dias:  byte = round(ndsc120) (0–120);
- 255 = sem dado (o −999 do INPE, ou NaN).

Ficam só os 3 dias publicados mais recentes; o resto é apagado. Dia que já está
no índice não é baixado de novo: a rodada das 7h30 (nova tentativa, se o dia
ainda não tinha saído às 4h30) não pede nada ao INPE quando o dia já está lá.

Saída (abaixo de RISCO_RAIZ, servida pelo nginx em /risco-fogo/v1/):

  risco-fogo/v1/indice.json
  risco-fogo/v1/risco/AAAA-MM-DD.png
  risco-fogo/v1/dias/AAAA-MM-DD.png

  risco_fogo.py                 uma rodada (o que o timer chama)
  risco_fogo.py dia PROD ARQ.nc recorta um arquivo local e grava o PNG ao lado (teste)
"""
import io
import json
import os
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("RISCO_RAIZ", "/var/cache/amaview-risco-fogo")
PASTA = os.path.join(RAIZ, "risco-fogo", f"v{VERSAO}")
BASE = "https://dataserver-coids.inpe.br/queimadas/queimadas/riscofogo_meteorologia/observado"
AGENTE = "AmaView-risco-fogo (+https://amaview.com.br/)"
FONTE = "INPE — Programa Queimadas (FireRiskModel 2.2), observado"
SEM_DADO = 255
# Quantos dias publicados ficam, e quantos dias para trás procurar por eles.
DIAS = int(os.environ.get("RISCO_DIAS", "3"))
PROCURAR = 10
# Caixa da Amazônia Legal (oeste, sul, leste, norte; `AMAZONIA_LEGAL_BBOX_LONLAT`
# em `src/map/projection.ts` do AmaView) e a folga do recorte, em graus.
AMAZONIA_LEGAL = (-74.0, -18.1, -43.9, 5.3)
FOLGA_GRAUS = 0.5
# Horário de Brasília (sem horário de verão desde 2019).
BRASILIA = timezone(timedelta(hours=-3))

PRODUTOS = {
    # produto: (pasta no INPE, prefixo do arquivo, variável, escala do byte, máximo)
    "risco": ("risco_fogo", "INPE_FireRiskModel_2.2_FireRisk", "rf", 200, 1.0),
    "dias": ("numero_dias_sem_chuva", "INPE_FireRiskModel_2.2_NDWR", "ndsc120", 1, 120.0),
}


# ---------------------------------------------------------------------------
# NetCDF: o 4 é HDF5 (h5py); o 3 clássico é lido aqui mesmo (formato simples,
# e a Oracle não tem o netCDF4 nem o scipy).


def ler_netcdf3(dado: bytes, nomes: list[str]) -> dict:
    """
    NetCDF3 clássico (CDF1/CDF2) em memória → {nome: (array, atributos)} das
    variáveis pedidas. Variável com a dimensão de registro vem só no 1º registro.
    """
    import numpy as np

    if dado[:3] != b"CDF" or dado[3] not in (1, 2):
        raise ValueError("não é NetCDF3 clássico")
    largo = dado[3] == 2  # CDF2: deslocamentos de 64 bits
    pos = 8  # magia + numrecs
    tipos = {1: ">i1", 2: "S1", 3: ">i2", 4: ">i4", 5: ">f4", 6: ">f8"}

    def inteiro():
        nonlocal pos
        v = struct.unpack_from(">i", dado, pos)[0]
        pos += 4
        return v

    def nome():
        nonlocal pos
        n = inteiro()
        s = dado[pos : pos + n].decode("utf-8", "replace")
        pos += (n + 3) // 4 * 4
        return s

    def atributos():
        nonlocal pos
        marca, n = inteiro(), inteiro()
        if marca == 0 and n == 0:
            return {}
        out = {}
        for _ in range(n):
            k, tipo, cont = nome(), inteiro(), inteiro()
            dt = np.dtype(tipos[tipo])
            bruto = dado[pos : pos + cont * dt.itemsize]
            pos += (cont * dt.itemsize + 3) // 4 * 4
            out[k] = bruto.decode("utf-8", "replace") if tipo == 2 else np.frombuffer(bruto, dt)
        return out

    marca, ndims = inteiro(), inteiro()
    dims = []
    for _ in range(ndims if marca else 0):
        dims.append((nome(), inteiro()))
    atributos()  # globais
    marca, nvars = inteiro(), inteiro()
    out = {}
    for _ in range(nvars if marca else 0):
        n = nome()
        ids = [inteiro() for _ in range(inteiro())]
        attrs = atributos()
        tipo, _vsize = inteiro(), inteiro()
        inicio = struct.unpack_from(">q" if largo else ">i", dado, pos)[0]
        pos += 8 if largo else 4
        if n not in nomes:
            continue
        # Dimensão de tamanho 0 = a de registro: só o 1º registro.
        forma = [dims[i][1] if dims[i][1] else 1 for i in ids]
        dt = np.dtype(tipos[tipo])
        total = int(np.prod(forma)) if forma else 1
        arr = np.frombuffer(dado, dt, count=total, offset=inicio).reshape(forma)
        out[n] = (arr, attrs)
    falta = [n for n in nomes if n not in out]
    if falta:
        raise ValueError(f"variáveis ausentes: {falta}")
    return out


def ler_recorte(dado: bytes, variavel: str, caixa: tuple[float, float, float, float]):
    """(lon, lat, valores do 1º tempo no retângulo que cobre a caixa, índices, preenchimento)."""
    import numpy as np

    oeste, sul, leste, norte = caixa

    def indices(lon, lat):
        ii = np.nonzero((lon >= oeste) & (lon <= leste))[0]
        jj = np.nonzero((lat >= sul) & (lat <= norte))[0]
        if ii.size < 2 or jj.size < 2:
            raise ValueError("a caixa não cobre a grade")
        return int(ii[0]), int(ii[-1]), int(jj[0]), int(jj[-1])

    def preenchimento(fill):
        return None if fill is None else float(np.ravel(fill)[0])

    if dado[:4] == b"\x89HDF":
        import h5py

        with h5py.File(io.BytesIO(dado), "r") as f:
            lon = np.asarray(f["lon"][:], np.float64)
            lat = np.asarray(f["lat"][:], np.float64)
            i0, i1, j0, j1 = indices(lon, lat)
            v = f[variavel]
            # O `rf` vem em blocos de uma linha: só as linhas da caixa são lidas.
            bruto = np.asarray(v[0, j0 : j1 + 1, i0 : i1 + 1], np.float32)
            return lon, lat, bruto, (i0, i1, j0, j1), preenchimento(v.attrs.get("_FillValue"))
    vs = ler_netcdf3(dado, ["lon", "lat", variavel])
    lon, lat = np.asarray(vs["lon"][0], np.float64), np.asarray(vs["lat"][0], np.float64)
    i0, i1, j0, j1 = indices(lon, lat)
    arr, attrs = vs[variavel]
    bruto = np.asarray(arr[0, j0 : j1 + 1, i0 : i1 + 1], np.float32)
    return lon, lat, bruto, (i0, i1, j0, j1), preenchimento(attrs.get("_FillValue"))


def recortar(dado: bytes, variavel: str, escala: int, maximo: float):
    """Recorte na caixa, norte para cima, codificado em bytes; e a caixa das bordas dos pixels."""
    import numpy as np

    oeste, sul, leste, norte = AMAZONIA_LEGAL
    caixa = (oeste - FOLGA_GRAUS, sul - FOLGA_GRAUS, leste + FOLGA_GRAUS, norte + FOLGA_GRAUS)
    lon, lat, bruto, (i0, i1, j0, j1), preenchimento = ler_recorte(dado, variavel, caixa)
    dx = float(np.mean(np.diff(lon)))
    dy = float(np.mean(np.diff(lat)))
    sem = ~np.isfinite(bruto) | (bruto < 0)
    if preenchimento is not None:
        sem |= bruto == np.float32(preenchimento)
    valores = np.clip(np.where(sem, 0, bruto), 0, maximo)
    codigo = np.rint(valores * escala).astype(np.uint8)
    codigo[sem] = SEM_DADO
    # A latitude cresce para o norte no NetCDF; a linha 0 do PNG é a de cima.
    if dy > 0:
        codigo = codigo[::-1, :]
    s = min(lat[j0], lat[j1]) - abs(dy) / 2
    n = max(lat[j0], lat[j1]) + abs(dy) / 2
    w = lon[i0] - abs(dx) / 2
    e = lon[i1] + abs(dx) / 2
    return np.ascontiguousarray(codigo), [round(float(w), 5), round(float(s), 5), round(float(e), 5), round(float(n), 5)]


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
    # Em faixas de linhas: a memória fica em poucos MB mesmo no risco (~3100 × 2440).
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
# INPE e disco


def baixar(url: str, tentativas: int = 3) -> bytes | None:
    """O arquivo em memória; None se o INPE ainda não publicou (404)."""
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(pedido, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tentativas - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tentativas - 1:
                raise
        time.sleep(2 + 3 * i)
    raise RuntimeError("inalcançável")


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


def dia_mais_novo_esperado(agora: datetime) -> date:
    """O INPE publica de madrugada o dia anterior (de Brasília)."""
    return agora.astimezone(BRASILIA).date() - timedelta(days=1)


def rodada_produto(produto: str, anterior: dict | None, agora: datetime) -> tuple[dict | None, dict]:
    pasta_inpe, prefixo, variavel, escala, maximo = PRODUTOS[produto]
    pasta = os.path.join(PASTA, produto)
    os.makedirs(pasta, exist_ok=True)
    # O que já está publicado (e no disco) não é baixado de novo.
    grade = (anterior or {}).get("grade")
    prontos = {
        d["data"]: d for d in (anterior or {}).get("dias", [])
        if isinstance(d, dict) and os.path.exists(os.path.join(PASTA, str(d.get("arquivo", ""))))
    }
    dias: list[dict] = []
    baixados = ausentes = 0
    mais_novo = dia_mais_novo_esperado(agora)
    for k in range(PROCURAR):
        if len(dias) >= DIAS:
            break
        data = mais_novo - timedelta(days=k)
        chave = f"{data:%Y-%m-%d}"
        if chave in prontos and grade:
            dias.append(prontos[chave])
            continue
        nome = f"{prefixo}_{data:%Y%m%d}.nc"
        dado = baixar(f"{BASE}/{pasta_inpe}/{nome}")
        if dado is None:
            ausentes += 1
            continue
        baixados += 1
        codigo, borda = recortar(dado, variavel, escala, maximo)
        del dado  # nunca vai para o disco
        g = {"caixa": borda, "largura": int(codigo.shape[1]), "altura": int(codigo.shape[0])}
        if grade and g != grade:
            if dias:
                # Grade nova num dia mais velho que os já guardados: fica de fora.
                print(f"{produto} {chave}: grade diferente da dos dias mais novos — fora", file=sys.stderr)
                continue
            # O dia mais novo manda: os antigos (outra grade) saem.
            print(f"{produto}: grade mudou ({grade} → {g})")
            prontos = {}
        grade = g
        rel = f"{produto}/{chave}.png"
        imagem = png(codigo)
        escrever_atomico(os.path.join(PASTA, rel), imagem)
        dias.append({"data": chave, "arquivo": rel})
        print(f"{produto} {chave}: {g['largura']}×{g['altura']}, {len(imagem) / 1e3:.0f} KB")
    dias.sort(key=lambda d: d["data"])
    # Só os dias do índice ficam na pasta.
    guardados = {os.path.basename(d["arquivo"]) for d in dias}
    removidos = 0
    for nome in os.listdir(pasta):
        if nome not in guardados:
            os.remove(os.path.join(pasta, nome))
            removidos += 1
    resumo = {"baixados": baixados, "ausentes": ausentes, "removidos": removidos, "dias": [d["data"] for d in dias]}
    if not dias:
        return None, resumo
    return {"grade": grade, "escala": escala, "semDado": SEM_DADO, "maximo": maximo, "dias": dias}, resumo


def rodada(agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    os.makedirs(PASTA, exist_ok=True)
    # Versões antigas do formato: o app já não as pede.
    for nome in os.listdir(os.path.dirname(PASTA)):
        if nome != os.path.basename(PASTA):
            shutil.rmtree(os.path.join(os.path.dirname(PASTA), nome), ignore_errors=True)
    caminho = os.path.join(PASTA, "indice.json")
    anterior = ler_json(caminho, {}).get("produtos", {})
    esperado = f"{dia_mais_novo_esperado(agora):%Y-%m-%d}"
    em_dia = all(
        any(d.get("data") == esperado for d in (anterior.get(p) or {}).get("dias", []))
        and os.path.exists(os.path.join(PASTA, p, f"{esperado}.png"))
        for p in PRODUTOS
    )
    if em_dia:
        return {"em_dia": True, "esperado": esperado, "produtos": {}}
    indice = {"versao": VERSAO, "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"), "fonte": FONTE, "produtos": {}}
    resumos = {}
    for produto in PRODUTOS:
        try:
            publicado, resumos[produto] = rodada_produto(produto, anterior.get(produto), agora)
        except Exception as e:  # noqa: BLE001 — um produto falhando não derruba o outro
            print(f"{produto}: falhou — {e}", file=sys.stderr)
            resumos[produto] = {"erro": str(e)}
            publicado = anterior.get(produto)  # o que já estava no ar continua
        if publicado:
            indice["produtos"][produto] = publicado
    if indice["produtos"]:
        escrever_atomico(caminho, json.dumps(indice, ensure_ascii=False, separators=(",", ":")).encode())
    return {"em_dia": False, "esperado": esperado, "produtos": resumos}


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "dia":
        _, _, variavel, escala, maximo = PRODUTOS[sys.argv[2]]
        with open(sys.argv[3], "rb") as f:
            codigo, borda = recortar(f.read(), variavel, escala, maximo)
        destino = os.path.splitext(sys.argv[3])[0] + ".png"
        imagem = png(codigo)
        with open(destino, "wb") as f:
            f.write(imagem)
        print(f"{destino}: {codigo.shape[1]}×{codigo.shape[0]}, caixa {borda}, {len(imagem) / 1e3:.0f} KB")
        return
    r = rodada()
    if r["em_dia"]:
        print(f"em dia: {r['esperado']} já publicado — nada a pedir ao INPE")
        return
    for produto, x in r["produtos"].items():
        if "erro" in x:
            print(f"{produto}: erro — {x['erro']}")
        else:
            print(f"{produto}: {x['baixados']} baixados, {x['ausentes']} ainda não publicados, {x['removidos']} apagados; no ar: {', '.join(x['dias']) or 'nada'}")
    falhou = any("erro" in x for x in r["produtos"].values())
    atrasado = all(r["esperado"] not in x.get("dias", []) for x in r["produtos"].values())
    if atrasado:
        print(f"o INPE ainda não publicou {r['esperado']}")
    sys.exit(1 if falhou else 0)


if __name__ == "__main__":
    main()
