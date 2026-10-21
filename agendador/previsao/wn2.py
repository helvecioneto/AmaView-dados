#!/usr/bin/env python3
"""
Cenários do Google WeatherNext 2 para o AmaView — ACESSO RESTRITO.

O WeatherNext 2 é um ensemble de 64 cenários (0,25°, de 6 em 6 h até +360 h,
rodadas a cada 6 h). Este coletor lê a cópia do dynamical.org
(`google-weathernext2-forecast-operational-virtual`, Icechunk/Zarr virtual) e
publica, recortados na Amazônia Legal e na MESMA grade do IFS de `previsao.py`,
só produtos derivados dos 64 cenários:

  chance    % de cenários com ≥ 1 mm de chuva nas 6 h que terminam no passo
  forte     % de cenários com ≥ 10 mm nas mesmas 6 h
  chance24  % de cenários com ≥ 1 mm nas 24 h que terminam no passo
  t10, t90  temperatura a 2 m (°C): 10% e 90% dos cenários abaixo do valor

**Licença.** A previsão do futuro do WeatherNext 2 segue os termos do Google
para dados experimentais em tempo real
(https://storage.googleapis.com/weathernext-public/terms-of-use.pdf): uso
interno e, para fins educacionais, distribuição controlada a pessoas
identificadas. Por isso a pasta `wn2/` fica atrás de senha no nginx
(`/etc/amaview/wn2.htpasswd`, fora do repositório) e não entra no
`indice.json` público. Junto da rodada vão a cópia dos termos e o aviso que
eles pedem (`termos.txt`).

**Download.** Cada pedaço do Zarr é o globo inteiro de um cenário, passo e
variável (721 × 1440 float32, blosc/lz4 em 8 blocos de 512 KiB). A faixa da
Amazônia cai em 2 desses blocos: pede-se o cabeçalho do blosc (onde cada bloco
começa) e depois só os 2 blocos, ~25% dos bytes (~0,6 MB na temperatura,
~0,9 MB na chuva). Uma rodada (2 variáveis × 64 cenários × 60 passos) são
~7.700 pedaços, ~6 GB e ~7 min na máquina Oracle.

Roda com o Python do venv `/opt/amaview/venv-wn2` (icechunk, zarr, numcodecs,
numpy), criado pelo `amaview instalar`; reaproveita a escrita de `previsao.py`
(mesmo formato dos .bin e dos blocos da série).

  wn2.py                 uma rodada (o que o timer chama)
  wn2.py AAAAMMDDHH      força uma rodada (teste)
"""
import asyncio
import json
import os
import struct
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import previsao as P  # noqa: E402

MODELO = "wn2"
PASTA = os.path.join(P.PASTA, MODELO)
ESTADO = os.path.join(P.RAIZ, "estado-wn2.json")

REPO_URL = "https://google-weathernext2.r2.dynamical.org/google-weathernext2-forecast-operational-virtual/v0.1.0.icechunk"
CONTEINER = "https://wn.dynamical.org/chunks/"
TERMOS_URL = "https://storage.googleapis.com/weathernext-public/terms-of-use.pdf"

# As rodadas de 00 e 12 UTC (as do IFS): ~6 GB cada. As de 06 e 18 UTC dobrariam
# o download sem mudar muito um produto de 15 dias.
HORAS_RODADA = (0, 12)
MEMBROS = 64
# Passos publicados: o 0 (sem dado: o WeatherNext 2 começa em +6 h) mantém o
# começo da rodada igual ao do IFS no app; depois, de 6 em 6 h até +360 h.
PASSOS = list(range(0, 361, 6))
# Pedidos simultâneos ao dynamical.org (medido: 32 → ~12 MB/s na máquina Oracle).
PARALELO = 32
TENTATIVAS = 4

LIMIAR_CHANCE_MM = 1.0
LIMIAR_FORTE_MM = 10.0

P.VARS[MODELO] = {
    "chance": ("%", 0.0, 100.0 / MEMBROS, "cenários com ≥ 1 mm de chuva nas 6 h que terminam no passo"),
    "forte": ("%", 0.0, 100.0 / MEMBROS, "cenários com ≥ 10 mm de chuva nas 6 h que terminam no passo"),
    "chance24": ("%", 0.0, 100.0 / MEMBROS, "cenários com ≥ 1 mm de chuva nas 24 h que terminam no passo"),
    "t10": ("°C", -60.0, 0.01, "temperatura a 2 m: 10% dos cenários abaixo"),
    "t90": ("°C", -60.0, 0.01, "temperatura a 2 m: 90% dos cenários abaixo"),
}
P.PASSO_GRADE[MODELO] = 0.25

FONTE = "Google WeatherNext 2 (64 cenários), via dynamical.org — dados experimentais, termos do Google"
COPYRIGHT = "Copyright 2024-6 Google LLC"
AVISO_TERMOS = f"By using this information, you agree to the Terms of Use found at {TERMOS_URL}"
CITACAO = (
    "© 2024-6 Google LLC, whose machine learning models were used to create the experimental data made available "
    f"under the following licence terms {TERMOS_URL}. This data is intended for experimental modelling only and is "
    "not intended, validated, or approved for real world use."
)
MODIFICACOES = (
    "Modificações do AmaView: recorte na Amazônia Legal (8°N–20°S, 76°W–42°W); dos 64 cenários, a fração com chuva "
    "≥ 1 mm e ≥ 10 mm em 6 h e ≥ 1 mm em 24 h, e os percentis 10 e 90 da temperatura a 2 m; valores quantizados "
    "(uint16). Leitura pela cópia do dynamical.org, que converte as unidades (°C, mm)."
)

# Grade global do Zarr: y de −90 a 90 (sul → norte), x de 0 a 359,75.
NY, NX = 721, 1440
VAR_ZARR = {"t2m": "temperature_2m", "tp": "total_precipitation_surface"}


def linhas_colunas() -> tuple[int, int, int, int]:
    """Linhas [i0, i1) e colunas [j0, j1) do recorte na grade global (y crescente)."""
    ni, nj, _ = P.dimensoes(MODELO)
    i0 = round((P.LAT1 + 90.0) / 0.25) + 1  # −19,75°: a linha de baixo do recorte
    j0 = round((P.LON0 % 360.0) / 0.25)
    return i0, i0 + ni, j0, j0 + nj


# ---------------------------------------------------------------------------
# Blosc parcial


def blocos_do_recorte(cabecalho: bytes, i0: int, i1: int) -> tuple[bytes, int, list[tuple[int, int]]]:
    """
    Do cabeçalho do blosc (16 bytes + início de cada bloco), os blocos que cobrem
    as linhas [i0, i1): (cabeçalho fixo, primeiro bloco, bytes [ini, fim) de cada
    bloco, na ordem dos blocos). Os blocos NÃO estão em ordem no arquivo (o blosc
    comprime em paralelo): o fim de um é o início seguinte em posição, não em número.
    """
    ver, verlz, flags, typesize, nbytes, bs, cbytes = struct.unpack("<BBBBiii", cabecalho[:16])
    if typesize != 4 or nbytes != NY * NX * 4:
        raise ValueError(f"pedaço inesperado: typesize {typesize}, {nbytes} bytes")
    if flags & 0x02:
        raise ValueError("pedaço sem compressão (memcpy): não previsto")
    nb = (nbytes + bs - 1) // bs
    inicios = struct.unpack(f"<{nb}i", cabecalho[16 : 16 + 4 * nb])
    b0 = (i0 * NX * 4) // bs
    b1 = (i1 * NX * 4 - 1) // bs
    posicoes = sorted(inicios) + [cbytes]
    trechos = [(inicios[b], posicoes[posicoes.index(inicios[b]) + 1]) for b in range(b0, b1 + 1)]
    return cabecalho[:16], b0, trechos


def pedidos(trechos: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Os intervalos a pedir: blocos vizinhos no arquivo vão num pedido só."""
    out: list[tuple[int, int]] = []
    for ini, fim in sorted(trechos):
        if out and out[-1][1] == ini:
            out[-1] = (out[-1][0], fim)
        else:
            out.append((ini, fim))
    return out


def montar_quadro(fixo: bytes, trechos: list[tuple[int, int]], baixados: dict[tuple[int, int], bytes]) -> bytes:
    """Um blosc válido só com os blocos do recorte (cada bloco é independente: o embaralhamento é por bloco)."""
    ver, verlz, flags, typesize, _, bs, _ = struct.unpack("<BBBBiii", fixo)
    corpos = []
    for ini, fim in trechos:
        (a, b), dado = next(((a, b), d) for (a, b), d in baixados.items() if a <= ini and fim <= b)
        corpos.append(dado[ini - a : fim - a])
    n = len(trechos)
    novos, pos = [], 16 + 4 * n
    for c in corpos:
        novos.append(pos)
        pos += len(c)
    return struct.pack("<BBBBiii", ver, verlz, flags, typesize, n * bs, bs, pos) + struct.pack(f"<{n}i", *novos) + b"".join(corpos)


def recorte_do_quadro(quadro: bytes, b0: int, bs: int, i0: int, i1: int, j0: int, j1: int, escala: float, offset: float):
    """Descomprime e devolve o recorte [norte → sul][oeste → leste] em float32, já nas unidades (scale_offset do Zarr)."""
    import numpy as np
    from numcodecs import Blosc

    bruto = np.frombuffer(Blosc().decode(quadro), dtype="<f4")
    l0 = b0 * bs // 4
    faixa = bruto[i0 * NX - l0 : i1 * NX - l0].reshape(i1 - i0, NX)[:, j0:j1]
    # scale_offset do Zarr v3: decodificado = guardado / escala + offset.
    return (faixa[::-1].astype(np.float64) / escala + offset).astype(np.float32)


# ---------------------------------------------------------------------------
# Produtos


def produtos_do_passo(t: "np.ndarray", tp: "np.ndarray", tp_24: "np.ndarray | None") -> dict:
    """De [membro][lat][lon] (°C; mm em 6 h; mm em 24 h ou None), os campos publicados."""
    import numpy as np

    tp = np.maximum(tp, 0.0)  # negativos pequenos do modelo (o dynamical.org avisa)
    ok_t = np.isfinite(t).all(axis=0)
    ok_p = np.isfinite(tp).all(axis=0)
    out = {
        "chance": np.where(ok_p, (tp >= LIMIAR_CHANCE_MM).mean(axis=0) * 100.0, np.nan),
        "forte": np.where(ok_p, (tp >= LIMIAR_FORTE_MM).mean(axis=0) * 100.0, np.nan),
        "t10": np.where(ok_t, np.percentile(t, 10, axis=0), np.nan),
        "t90": np.where(ok_t, np.percentile(t, 90, axis=0), np.nan),
    }
    if tp_24 is None:
        out["chance24"] = np.full(tp.shape[1:], np.nan)
    else:
        ok24 = np.isfinite(tp_24).all(axis=0)
        out["chance24"] = np.where(ok24, (np.maximum(tp_24, 0.0) >= LIMIAR_CHANCE_MM).mean(axis=0) * 100.0, np.nan)
    return {k: v.astype(np.float32) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Leitura


def abrir():
    import icechunk

    acesso = getattr(getattr(icechunk, "credentials", None), "HttpAccess", None)
    repo = icechunk.Repository.open(icechunk.http_storage(REPO_URL), authorize_virtual_chunk_access={CONTEINER: acesso})
    return repo.readonly_session("main").store


def coordenadas(store) -> tuple[list[datetime], list[int], dict]:
    """Rodadas no repositório, antecedências (h) e o scale_offset de cada variável."""
    import zarr

    g = zarr.open_group(store, mode="r")
    inits = [datetime.fromtimestamp(int(s), timezone.utc) for s in g["init_time"][:]]
    leads = [int(round(float(s) / 3600)) for s in g["lead_time"][:]]
    if [float(v) for v in g["y"][[0, -1]]] != [-90.0, 90.0] or float(g["x"][0]) != 0.0:
        raise ValueError("grade do WeatherNext 2 mudou de orientação")
    escalas = {}
    for nome, zv in VAR_ZARR.items():
        cfg = next((c.get("configuration", {}) for c in g[zv].metadata.to_dict()["codecs"] if c["name"] == "scale_offset"), {})
        escalas[nome] = (float(cfg.get("scale", 1.0)), float(cfg.get("offset", 0.0)))
    return inits, leads, escalas


async def rodada_completa(store, n_init: int, n_lead: int) -> bool:
    """Todos os cenários do último passo das duas variáveis já estão no repositório."""
    for zv in VAR_ZARR.values():
        for m in range(MEMBROS):
            if not await store.exists(f"{zv}/c/{n_init}/{m}/{n_lead - 1}/0/0"):
                return False
    return True


async def ler_recortes(store, i_init: int, leads: list[int], escalas: dict, ao_terminar_passo) -> None:
    """Para cada passo (em ordem), os 64 cenários das duas variáveis; `ao_terminar_passo(k, {var: [membro][lat][lon]})`."""
    import numpy as np
    from zarr.abc.store import RangeByteRequest
    from zarr.core.buffer import default_buffer_prototype

    prot = default_buffer_prototype()
    i0, i1, j0, j1 = linhas_colunas()
    sem = asyncio.Semaphore(PARALELO)
    bytes_lidos = 0

    async def um(zv: str, nome: str, k: int, m: int):
        nonlocal bytes_lidos
        chave = f"{zv}/c/{i_init}/{m}/{k}/0/0"
        for tentativa in range(TENTATIVAS):
            try:
                async with sem:
                    cab = await store.get(chave, prototype=prot, byte_range=RangeByteRequest(0, 16 + 4 * 16))
                    if cab is None:
                        raise FileNotFoundError(chave)
                    cab = cab.to_bytes()
                    fixo, b0, trechos = blocos_do_recorte(cab, i0, i1)
                    baixados = {}
                    for ini, fim in pedidos(trechos):
                        corpo = await store.get(chave, prototype=prot, byte_range=RangeByteRequest(ini, fim))
                        if corpo is None:
                            raise FileNotFoundError(chave)
                        baixados[(ini, fim)] = corpo.to_bytes()
                bytes_lidos += len(cab) + sum(len(d) for d in baixados.values())
                bs = struct.unpack("<i", fixo[8:12])[0]
                esc, off = escalas[nome]
                return recorte_do_quadro(montar_quadro(fixo, trechos, baixados), b0, bs, i0, i1, j0, j1, esc, off)
            except FileNotFoundError:
                raise
            except Exception:  # noqa: BLE001 — rede: tenta de novo
                if tentativa == TENTATIVAS - 1:
                    raise
                await asyncio.sleep(2 + 4 * tentativa)

    inicio = time.time()
    for k in range(len(leads)):
        nomes = list(VAR_ZARR)
        todos = await asyncio.gather(*(um(VAR_ZARR[nome], nome, k, m) for nome in nomes for m in range(MEMBROS)))
        lidos = {nome: np.stack(todos[n * MEMBROS : (n + 1) * MEMBROS]) for n, nome in enumerate(nomes)}
        ao_terminar_passo(k, lidos)
        if k % 10 == 9:
            print(f"wn2: {k + 1}/{len(leads)} passos, {bytes_lidos / 1e9:.1f} GB, {time.time() - inicio:.0f} s", flush=True)


def baixar_rodada(store, i_init: int, leads: list[int], escalas: dict) -> dict:
    """{var: [passo][lat][lon]} nos PASSOS (o passo 0 sem dado)."""
    import numpy as np

    ni, nj, _ = P.dimensoes(MODELO)
    if [0] + leads != PASSOS:
        raise ValueError(f"passos do WeatherNext 2 mudaram: {leads[:3]}…{leads[-1]}")
    out = {v: np.full((len(PASSOS), ni, nj), np.nan, np.float32) for v in P.VARS[MODELO]}
    chuvas: list = []  # os 4 últimos passos de chuva [membro][lat][lon], para as 24 h

    def passo(k: int, lidos: dict) -> None:
        chuvas.append(lidos["tp"])
        del chuvas[:-4]
        tp_24 = np.sum(chuvas, axis=0) if len(chuvas) == 4 else None
        for v, campo in produtos_do_passo(lidos["t2m"], lidos["tp"], tp_24).items():
            out[v][k + 1] = campo

    asyncio.run(ler_recortes(store, i_init, leads, escalas, passo))
    return out


# ---------------------------------------------------------------------------
# Publicação


def escrever_termos() -> None:
    """A cópia dos termos e o aviso que eles pedem para acompanhar o dado (seção 4a)."""
    os.makedirs(PASTA, exist_ok=True)
    texto = "\n\n".join(["Legally Binding Terms of Use", AVISO_TERMOS, COPYRIGHT, CITACAO, MODIFICACOES]) + "\n"
    P.escrever_atomico(os.path.join(PASTA, "termos.txt"), texto.encode())
    caminho = os.path.join(PASTA, "terms-of-use.pdf")
    if os.path.exists(caminho) and time.time() - os.path.getmtime(caminho) < 86400:
        return
    try:
        P.escrever_atomico(caminho, P.baixar(TERMOS_URL))
    except Exception as e:  # noqa: BLE001 — fica a cópia anterior
        print(f"wn2: termos não baixados — {e}", file=sys.stderr)


def montar_indice(estado: dict, agora: datetime) -> dict:
    rodadas = sorted((n for n in os.listdir(PASTA) if n.isdigit()), reverse=True) if os.path.isdir(PASTA) else []
    ni, nj, b = P.dimensoes(MODELO)
    return {
        "versao": P.VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "semDado": P.SEM_DADO,
        "termos": {"url": TERMOS_URL, "aviso": AVISO_TERMOS, "copyright": COPYRIGHT, "citacao": CITACAO, "modificacoes": MODIFICACOES},
        "modelos": {
            MODELO: {
                "fonte": FONTE,
                "rodadas": rodadas,
                "passos": PASSOS,
                "grade": {"lat0": P.LAT0, "lon0": P.LON0, "passo": P.PASSO_GRADE[MODELO], "ni": ni, "nj": nj, "bloco": b},
                "vars": {v: {"u": u, "off": off, "esc": esc, "desc": d} for v, (u, off, esc, d) in P.VARS[MODELO].items()},
                "faixas": {r: estado.get("faixas", {}).get(r) for r in rodadas},
                "erro": estado.get("erro"),
            }
        },
    }


def rodadas_alvo(inits: list[datetime], feitas: set, falhas: dict, forcar: str | None) -> list[tuple[int, datetime]]:
    """As rodadas de 00/12 UTC do repositório ainda não publicadas, da mais nova para a mais velha (até 2 dias)."""
    if forcar:
        alvo = P.instante_rodada(forcar)
        return [(i, t) for i, t in enumerate(inits) if t == alvo]
    limite = datetime.now(timezone.utc) - timedelta(days=2)
    ultima = max(feitas) if feitas else ""
    return [
        (i, t)
        for i, t in sorted(enumerate(inits), key=lambda x: x[1], reverse=True)
        if t.hour in HORAS_RODADA and t >= limite and P.nome_rodada(t) > ultima and time.time() - falhas.get(P.nome_rodada(t), 0) > P.ESPERA_FALHA_S
    ]


def rodada(forcar: str | None = None) -> str:
    agora = datetime.now(timezone.utc)
    os.makedirs(PASTA, exist_ok=True)
    estado = P.ler_json(ESTADO, {})
    feitas = set(estado.get("feitas", []))
    falhas = estado.get("falhas", {})
    resultado = "em dia"
    try:
        escrever_termos()
        store = abrir()
        inits, leads, escalas = coordenadas(store)
        for i, t in rodadas_alvo(inits, feitas, falhas, forcar):
            if not asyncio.run(rodada_completa(store, i, len(leads))):
                continue  # ainda chegando: a próxima execução do timer vê de novo
            n = P.nome_rodada(t)
            inicio = time.time()
            try:
                dados = baixar_rodada(store, i, leads, escalas)
                faixa = P.publicar(MODELO, n, PASSOS, dados)
            except Exception as e:  # noqa: BLE001
                falhas[n] = time.time()
                estado["falhas"] = {k: v for k, v in falhas.items() if time.time() - v < 86400}
                estado["erro"] = f"{n}: {e}"
                raise
            estado["feitas"] = sorted(feitas | {n})[-P.RODADAS_GUARDADAS :]
            estado.setdefault("faixas", {})[n] = faixa
            estado["erro"] = None
            resultado = f"{n} publicada"
            print(f"wn2 {n}: publicada em {(time.time() - inicio) / 60:.0f} min · chance {faixa['chance']} · t10 {faixa['t10']} · t90 {faixa['t90']}")
            break
    finally:
        estado["faixas"] = {k: v for k, v in estado.get("faixas", {}).items() if k in estado.get("feitas", [])}
        P.escrever_atomico(ESTADO, json.dumps(estado).encode())
        P.limpar(MODELO)
        P.escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(montar_indice(estado, agora), separators=(",", ":"), ensure_ascii=False).encode())
    return resultado


def main() -> None:
    forcar = sys.argv[1] if len(sys.argv) == 2 else None
    try:
        print(f"wn2: {rodada(forcar)}")
    except Exception as e:  # noqa: BLE001
        print(f"wn2: falhou — {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
