#!/usr/bin/env python3
"""
Focos de queimada do GOES-19 direto da NOAA, para o AmaView: o produto
ABI-L2-FDCF (Fire Detection and Characterization, disco completo, 10 em 10 min)
virado CSV, um arquivo por varredura.

Por que direto da NOAA (08/11/2026): o arquivo de 10 min do INPE chega ~20 min
depois do início da varredura e às vezes horas depois, em lotes; o FDC está no
S3 da NOAA ~10 min depois do início (1–2 min depois do fim da varredura).

- **Como a NOAA publica.** Cada pixel em que a máscara `Mask` marca fogo
  (códigos 10–15 e 30–35) vira uma linha, com o código (`mascara`) e a potência
  radiativa (`frp`, MW) do próprio produto. Nada é reclassificado; quem decide
  que categorias mostrar é o app (hoje: 10–13 e 30–33, as de foco — ficam de
  fora as de média e baixa probabilidade, 14/15 e 34/35).
- **Lat/lon do centro do pixel** pela grade fixa do ABI (a mesma conta da
  fumaça), 4 casas.
- **`data` é o início da varredura no minuto** (`s…` do nome do arquivo), o
  mesmo horário do quadro do STAR que o AmaView mostra.
- **Leve.** O netCDF (~1,7 MB) vem para a memória, só o recorte do setor NSA a
  oeste de 30°W é lido, e nada além do CSV (~20 KB) toca o disco. 72 h, como o
  INPE guarda os de 10 min.

Saída (abaixo de FOCOS_RAIZ, servida pelo nginx em /focos/v1/):

  focos/v1/indice.json                 varreduras disponíveis (o app lê a cada carga)
  focos/v1/goes19/AAAAMMDD_HHMM.csv     uma por varredura (imutável)

  focos.py                  uma rodada (o que o timer chama, a cada minuto)
  focos.py quadro ARQ.nc    processa um arquivo local e imprime o resumo (teste)
"""
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fumaca"))
from fumaca import NSA, LON_LESTE, baixar, escrever_atomico, geos_para_lonlat, ler_json  # noqa: E402

VERSAO = 1
RAIZ = os.environ.get("FOCOS_RAIZ", "/var/cache/amaview-focos")
PASTA = os.path.join(RAIZ, "focos", f"v{VERSAO}")
ARQS = os.path.join(PASTA, "goes19")
ESTADO = os.path.join(RAIZ, "estado.json")
BALDE = "https://noaa-goes19.s3.amazonaws.com"
PRODUTO = "ABI-L2-FDCF"

JANELA = timedelta(hours=72)
MAX_POR_RODADA = 24
RELISTAR_S = 1800
ESPERA_FALHA_S = 1800

# Códigos de fogo da máscara (FDC, GOES-R PUG vol. 5): 10 bom, 11 saturado,
# 12 com nuvem, 13 alta, 14 média e 15 baixa probabilidade; 30–35 os mesmos
# "filtrados no tempo" (o pixel já tinha fogo nas varreduras anteriores).
FOGO = (10, 11, 12, 13, 14, 15, 30, 31, 32, 33, 34, 35)

ARQUIVO = re.compile(r"OR_ABI-L2-FDCF-M\d_G19_s(\d{4})(\d{3})(\d{2})(\d{2})\d{3}_e\d{14}_c\d{14}\.nc$")


def instante_do_arquivo(nome: str) -> datetime | None:
    m = ARQUIVO.search(nome)
    if not m:
        return None
    ano, dia, hora, minuto = (int(g) for g in m.groups())
    return datetime(ano, 1, 1, hora, minuto, tzinfo=timezone.utc) + timedelta(days=dia - 1)


def nome_de(t: datetime) -> str:
    return t.strftime("%Y%m%d_%H%M")


def instante_de(nome: str) -> datetime:
    return datetime.strptime(nome, "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)


def prefixo_da_hora(t: datetime) -> str:
    return f"{PRODUTO}/{t.year:04d}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"


def processar(nc: bytes | str, t: datetime) -> tuple[str, dict]:
    """netCDF do FDCF → (CSV, resumo {n, nc: contagem por código})."""
    import h5py
    import numpy as np

    with h5py.File(io.BytesIO(nc) if isinstance(nc, bytes) else nc, "r") as f:
        def eixo(nome):
            v = f[nome]
            return float(v.attrs["scale_factor"][0]), float(v.attrs["add_offset"][0]), v.shape[0]

        xs, xo, nx = eixo("x")
        ys, yo, ny = eixo("y")
        # O mesmo recorte da fumaça (x cresce com a coluna; y decresce com a linha).
        c0 = max(0, math.floor((NSA["xMin"] - xo) / xs))
        c1 = min(nx, math.ceil((NSA["xMax"] - xo) / xs) + 1)
        l0 = max(0, math.floor((NSA["yMax"] - yo) / ys))
        l1 = min(ny, math.ceil((NSA["yMin"] - yo) / ys) + 1)
        mascara = f["Mask"][l0:l1, c0:c1]
        lin, col = np.nonzero(np.isin(mascara, FOGO))
        potencia = f["Power"][l0:l1, c0:c1][lin, col] if len(lin) else np.array([])
        p = f["goes_imager_projection"].attrs
        proj = {k: float(p[k][0]) for k in ("semi_major_axis", "semi_minor_axis", "perspective_point_height", "longitude_of_projection_origin")}

    codigos = mascara[lin, col]
    lon, lat = geos_para_lonlat(xo + (col + c0) * xs, yo + (lin + l0) * ys, proj)
    ok = np.isfinite(lon) & np.isfinite(lat) & (lon <= LON_LESTE)
    data = t.strftime("%Y-%m-%d %H:%M:%S")
    linhas = ["lat,lon,satelite,data,mascara,frp"]
    contagem: dict[str, int] = {}
    for la, lo, cod, frp in zip(lat[ok], lon[ok], codigos[ok], potencia[ok]):
        frp_txt = f"{frp:.1f}" if frp >= 0 else ""
        linhas.append(f"{la:.4f},{lo:.4f},GOES-19,{data},{int(cod)},{frp_txt}")
        contagem[str(int(cod))] = contagem.get(str(int(cod)), 0) + 1
    return "\n".join(linhas) + "\n", {"n": len(linhas) - 1, "nc": contagem}


def listar(prefixo: str) -> dict[str, str]:
    """nome (AAAAMMDD_HHMM) → chave no S3 das varreduras de uma hora."""
    xml = baixar(f"{BALDE}/?list-type=2&prefix={prefixo}").decode()
    out = {}
    for chave in re.findall(r"<Key>([^<]+)</Key>", xml):
        t = instante_do_arquivo(chave)
        if t:
            out[nome_de(t)] = chave
    return out


def no_disco() -> set[str]:
    return {n[:-4] for n in os.listdir(ARQS) if n.endswith(".csv") and ".parcial-" not in n}


def limpar(agora: datetime) -> int:
    removidos = 0
    for nome in os.listdir(ARQS):
        caminho = os.path.join(ARQS, nome)
        try:
            velho = instante_de(nome[:13]) < agora - JANELA
        except ValueError:
            velho = True
        if velho or (".parcial-" in nome and time.time() - os.path.getmtime(caminho) > 600):
            os.remove(caminho)
            removidos += 1
    return removidos


def rodada(agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    os.makedirs(ARQS, exist_ok=True)
    estado = ler_json(ESTADO, {})
    listadas: dict[str, float] = estado.get("listadas", {})
    resumos: dict[str, dict] = estado.get("resumos", {})
    removidos = limpar(agora)
    tem = no_disco()
    limite = agora - JANELA

    pendentes: dict[str, str] = dict(estado.get("pendentes", {}))
    base = agora.replace(minute=0, second=0, microsecond=0)
    for k in range(int(JANELA.total_seconds() // 3600) + 1):
        hora = base - timedelta(hours=k)
        prefixo = prefixo_da_hora(hora)
        esperados = {nome_de(hora + timedelta(minutes=m)) for m in range(0, 60, 10)}
        falta = any(n not in tem and limite <= instante_de(n) <= agora - timedelta(minutes=12) for n in esperados)
        if k >= 2 and (not falta or time.time() - listadas.get(prefixo, 0) < RELISTAR_S):
            continue
        try:
            pendentes.update(listar(prefixo))
            listadas[prefixo] = time.time()
        except Exception as e:  # noqa: BLE001
            print(f"falha ao listar {prefixo}: {e}", file=sys.stderr)

    falhados = {n: t for n, t in estado.get("falhados", {}).items() if time.time() - t < ESPERA_FALHA_S}
    fila = sorted((n for n in pendentes if n not in tem and instante_de(n) >= limite), reverse=True)
    feitos_agora: set[str] = set()
    falhas = 0
    for n in [n for n in fila if n not in falhados][:MAX_POR_RODADA]:
        t0 = time.time()
        try:
            nc = baixar(f"{BALDE}/{pendentes[n]}")
            csv, resumo = processar(nc, instante_de(n))
            del nc
            escrever_atomico(os.path.join(ARQS, f"{n}.csv"), csv.encode())
            resumos[n] = resumo
            feitos_agora.add(n)
            print(f"{n}: {resumo['n']} pixels de fogo ({time.time() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            falhas += 1
            falhados[n] = time.time()
            print(f"{n}: falhou — {e}", file=sys.stderr)

    tem = no_disco()
    arquivos = []
    for n in sorted(tem):
        r = resumos.get(n)
        if r is None:
            with open(os.path.join(ARQS, f"{n}.csv")) as f:
                r = {"n": max(0, sum(1 for _ in f) - 1), "nc": None}
            resumos[n] = r
        arquivos.append({"n": n, "focos": r["n"]})
    indice = {
        "versao": VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "NOAA/NESDIS — GOES-19 ABI L2 Fire Detection and Characterization (FDCF), máscara Mask",
        "satelite": "GOES-19",
        "cadencia_min": 10,
        "janela_h": int(JANELA.total_seconds() // 3600),
        "arquivos": arquivos,
    }
    escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(indice, separators=(",", ":"), ensure_ascii=False).encode())
    corte = time.time() - JANELA.total_seconds() - 3600
    estado = {
        "listadas": {p: t for p, t in listadas.items() if t > corte},
        "resumos": {n: r for n, r in resumos.items() if n in tem},
        "pendentes": {n: pendentes[n] for n in fila if n not in feitos_agora},
        "falhados": falhados,
    }
    escrever_atomico(ESTADO, json.dumps(estado).encode())
    return {"novos": len(feitos_agora), "falhas": falhas, "fila": len(fila) - len(feitos_agora), "removidos": removidos, "arquivos": len(arquivos)}


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "quadro":
        t0 = time.time()
        t = instante_do_arquivo(os.path.basename(sys.argv[2])) or datetime.now(timezone.utc)
        csv, resumo = processar(sys.argv[2], t)
        print(f"{resumo} · {len(csv) / 1024:.0f} KB · {time.time() - t0:.2f} s")
        return
    r = rodada()
    if r["novos"] or r["falhas"] or r["removidos"]:
        print(f"rodada: {r['novos']} novos, {r['falhas']} falhas, {r['fila']} na fila, {r['removidos']} apagados, {r['arquivos']} em 72 h")
    sys.exit(1 if r["falhas"] and not r["novos"] else 0)


if __name__ == "__main__":
    main()
