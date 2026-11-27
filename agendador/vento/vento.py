#!/usr/bin/env python3
"""
Vento do ECMWF para a camada "Vento" do AmaView (a parte observada, não a
previsão): a análise do IFS de 6 em 6 h e, entre elas e nas horas mais novas,
a previsão curta da rodada mais recente, de 3 em 3 h, em três níveis.

- Fonte: IFS do ECMWF, dados abertos (`data.ecmwf.int`, sem conta, CC BY 4.0).
  Rodadas de 00, 06, 12 e 18 UTC, grade de 0,25°. O servidor guarda só ~4 dias;
  aqui ficam 7 (`RETENCAO_H`).
- Níveis: superfície (10 m: `10u`/`10v`), médios (500 hPa) e altos (200 hPa).
  Só esses 6 campos são baixados, por intervalo de bytes (~3,5 MB por passo).
- Cada horário (múltiplo de 3 h) vem da rodada mais nova que já o alcança:
  passo 0 (a análise) quando há, senão o menor passo. Quando sai uma rodada
  nova, os horários dela trocam a previsão mais longa pela mais curta. A
  rodada sai ~7,5 h depois da hora dela: os passos até +18 h cobrem o "agora"
  até a seguinte chegar.
- Recorte: 15°N–25°S, 85°W–30°W, a imagem inteira do setor do GOES-19 sobre a
  Amazônia (a camada não termina na borda da Amazônia Legal).

Saída (abaixo de VENTO_RAIZ, servida pelo nginx em /vento/vN/):

  vento/v1/indice.json                         grade, níveis, horários (no-cache)
  vento/v1/<nivel>/<AAAAMMDDHH>_<rodada>.bin   u e v de um horário (imutável)

O .bin é uint8: as `ni·nj` posições do u (leste), depois as do v (norte);
valor em m/s = (q − 127) × escala do nível; 255 = sem dado. Linha i →
lat = LAT0 − i·passo; coluna j → lon = LON0 + j·passo (bordas inclusas).
A rodada vai no nome: quando um horário melhora (passo menor), o arquivo é
outro e o antigo sai depois de uma hora — o cache do navegador pode ser
imutável.

  vento.py                 uma execução (o que o timer chama)
  vento.py AAAAMMDDHH      força os horários de uma rodada (teste)
"""
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "previsao"))
import previsao as P  # noqa: E402 — GRIB pela libeccodes, downloads com espera do 429

VERSAO = 1
RAIZ = os.environ.get("VENTO_RAIZ", "/var/cache/amaview-vento")
PASTA = os.path.join(RAIZ, "vento", f"v{VERSAO}")
ESTADO = os.path.join(RAIZ, "estado.json")

LAT0, LAT1 = 15.0, -25.0
LON0, LON1 = -85.0, -30.0
PASSO = 0.25
NI = round((LAT0 - LAT1) / PASSO) + 1
NJ = round((LON1 - LON0) / PASSO) + 1
SEM_DADO = 255
ZERO = 127

# id → (nome, escala em m/s por unidade, (levtype, levelist, u), (levtype, levelist, v))
NIVEIS = {
    "10m": ("Superfície (10 m)", 0.2, ("sfc", None, "10u"), ("sfc", None, "10v")),
    "500": ("Médios níveis (500 hPa, ~5,5 km)", 0.4, ("pl", "500", "u"), ("pl", "500", "v")),
    "200": ("Altos níveis (200 hPa, ~12 km)", 0.8, ("pl", "200", "u"), ("pl", "200", "v")),
}

HORA_S = 3600
INTERVALO_H = 3
RODADA_H = 6
PASSO_MAX = 18
RETENCAO_H = 7 * 24
# O servidor aberto guarda ~3,5 dias (em 27/11/2026, a rodada mais velha tinha 84 h);
# 3 dias para trás bastam para as 48 h do app sem pedir o que já saiu de lá.
ALCANCE_FONTE_H = 3 * 24
# Arquivo trocado por um melhor: fica no disco esta folga (quem já baixou o índice ainda o pede).
FOLGA_APOSENTADO_S = 3600
# Rodada ainda não publicada (404): não perguntar de novo antes disto.
ESPERA_404_S = 15 * 60
FONTE = "ECMWF IFS (dados abertos), CC BY 4.0"


def nome(t: datetime) -> str:
    return t.strftime("%Y%m%d%H")


def instante(n: str) -> datetime:
    return datetime.strptime(n, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def url_base(rodada: datetime, passo: int) -> str:
    return f"{P.IFS_URL}/{rodada:%Y%m%d}/{rodada:%H}z/ifs/0p25/oper/{rodada:%Y%m%d%H}0000-{passo}h-oper-fc"


def chao(t: datetime, horas: int) -> datetime:
    t = t.replace(minute=0, second=0, microsecond=0)
    return t - timedelta(hours=t.hour % horas)


def candidato(horario: datetime, ultima_rodada: datetime) -> tuple[datetime, int] | None:
    """A rodada mais nova (≤ `ultima_rodada`) que alcança o horário, e o passo: o menor possível."""
    r = min(chao(horario, RODADA_H), ultima_rodada)
    passo = int((horario - r).total_seconds() // HORA_S)
    return (r, passo) if 0 <= passo <= PASSO_MAX else None


def horarios_desejados(agora: datetime, ultima_rodada: datetime) -> list[datetime]:
    """De 3 em 3 h: do mais velho que a fonte ainda tem até o último passo da rodada mais nova."""
    ini = chao(agora - timedelta(hours=ALCANCE_FONTE_H - RODADA_H), INTERVALO_H)
    fim = ultima_rodada + timedelta(hours=PASSO_MAX)
    out, t = [], ini
    while t <= fim:
        out.append(t)
        t += timedelta(hours=INTERVALO_H)
    return out


def quantizar(valores, escala: float):
    import numpy as np

    q = np.round(np.asarray(valores, dtype=np.float64) / escala) + ZERO
    q = np.where(np.isfinite(q), np.clip(q, 0, SEM_DADO - 1), SEM_DADO)
    return q.astype(np.uint8)


def recortar(campo: dict):
    """Grade global do ECMWF (norte em cima, lon −180…180) → recorte (NI × NJ, float32)."""
    import numpy as np

    lats_q = LAT0 - np.arange(NI) * PASSO
    lons_q = LON0 + np.arange(NJ) * PASSO
    li = np.array([int(np.argmin(np.abs(campo["lats"] - la))) for la in lats_q])
    lj = np.array([int(np.argmin(np.abs(campo["lons"] - lo))) for lo in lons_q])
    if np.abs(campo["lats"][li] - lats_q).max() > PASSO / 4 or np.abs(campo["lons"][lj] - lons_q).max() > PASSO / 4:
        raise ValueError("a grade do IFS não cobre o recorte")
    return campo["valores"][np.ix_(li, lj)].astype(np.float32)


def codificar(u, v, escala: float) -> bytes:
    import numpy as np

    return np.concatenate([quantizar(u, escala).ravel(), quantizar(v, escala).ravel()]).tobytes()


def baixar_passo(rodada: datetime, passo: int, ec) -> dict[str, bytes]:
    """Os 6 campos de um passo → {nível: bytes do .bin}. 404 sobe como HTTPError."""
    base = url_base(rodada, passo)
    indice = [json.loads(l) for l in P.baixar(base + ".index", tentativas=2).decode().splitlines() if l.strip()]
    quer = {c for _, _, cu, cv in NIVEIS.values() for c in (cu, cv)}
    faixas = {}
    for e in indice:
        k = (e.get("levtype"), e.get("levelist"), e.get("param"))
        if k in quer and k not in faixas:
            faixas[k] = (int(e["_offset"]), int(e["_length"]))
    if set(faixas) != quer:
        raise ValueError(f"{nome(rodada)} +{passo}h sem {sorted(map(str, quer - set(faixas)))} no índice")
    # Downloads em paralelo (o servidor anda a ~0,3 MB/s por pedido); a leitura do GRIB numa
    # linha só (a libeccodes não promete ser segura entre threads). O mesmo limite da previsão.
    from concurrent.futures import ThreadPoolExecutor

    pedir = lambda ini_n: P.baixar(base + ".grib2", {"Range": f"bytes={ini_n[0]}-{sum(ini_n) - 1}"})  # noqa: E731
    with ThreadPoolExecutor(max_workers=P.IFS_PARALELO) as pool:
        msgs = dict(zip(faixas, pool.map(pedir, faixas.values())))
    campos = {k: recortar(ec.ler(m)) for k, m in msgs.items()}
    return {nv: codificar(campos[cu], campos[cv], esc) for nv, (_, esc, cu, cv) in NIVEIS.items()}


def arquivo(nivel: str, horario: str, rodada: str) -> str:
    return os.path.join(PASTA, nivel, f"{horario}_{rodada}.bin")


def publicados(estado: dict) -> dict:
    return estado.setdefault("horarios", {})


def rodada_mais_nova(estado: dict, agora: datetime) -> datetime | None:
    """A rodada mais nova já no servidor (o passo 0 dela responde). Pergunta só pelas que faltam."""
    conhecida = estado.get("ultimaRodada")
    pendentes = estado.setdefault("ausentes", {})
    r = chao(agora, RODADA_H)
    limite = instante(conhecida) if conhecida else chao(agora - timedelta(hours=2 * RODADA_H), RODADA_H)
    while r > limite:
        n = nome(r)
        if time.time() - pendentes.get(n, 0) > ESPERA_404_S:
            try:
                P.baixar(url_base(r, PASSO_MAX) + ".index", metodo="HEAD", tentativas=1)
                estado["ultimaRodada"] = n
                pendentes.pop(n, None)
                return r
            except urllib.error.HTTPError as e:
                if e.code not in (403, 404):
                    raise
                pendentes[n] = time.time()
        r -= timedelta(hours=RODADA_H)
    estado["ausentes"] = {k: v for k, v in pendentes.items() if time.time() - v < 86400}
    return instante(conhecida) if conhecida else limite


def aposentar(estado: dict, caminhos: list[str]) -> None:
    fila = estado.setdefault("aposentados", {})
    for c in caminhos:
        fila[c] = time.time()


def limpar(estado: dict, agora: datetime) -> None:
    """Some o que passou da retenção e o que foi trocado há mais de uma hora."""
    corte = nome(agora - timedelta(hours=RETENCAO_H))
    hs = publicados(estado)
    for h in [h for h in hs if h < corte]:
        aposentar(estado, [arquivo(nv, h, hs[h]["r"]) for nv in NIVEIS])
        del hs[h]
    fila = estado.setdefault("aposentados", {})
    for c, t in list(fila.items()):
        if time.time() - t > FOLGA_APOSENTADO_S:
            try:
                os.remove(c)
            except FileNotFoundError:
                pass
            del fila[c]


def montar_indice(estado: dict, agora: datetime) -> dict:
    hs = publicados(estado)
    return {
        "versao": VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": FONTE,
        "grade": {"lat0": LAT0, "lon0": LON0, "passo": PASSO, "ni": NI, "nj": NJ},
        "semDado": SEM_DADO,
        "zero": ZERO,
        "niveis": {nv: {"nome": n, "esc": esc} for nv, (n, esc, _, _) in NIVEIS.items()},
        # t = horário (UTC), r = rodada do ECMWF de onde veio, p = passo (0 = análise).
        "horarios": [{"t": h, "r": hs[h]["r"], "p": hs[h]["p"]} for h in sorted(hs)],
        "erro": estado.get("erro"),
    }


def executar(agora: datetime | None = None, forcar: str | None = None, ec=None) -> str:
    agora = agora or datetime.now(timezone.utc)
    os.makedirs(PASTA, exist_ok=True)
    for nv in NIVEIS:
        os.makedirs(os.path.join(PASTA, nv), exist_ok=True)
    estado = P.ler_json(ESTADO, {})
    hs = publicados(estado)
    ec = ec or P.Eccodes()
    feitos, erro = 0, None
    t0 = time.time()
    try:
        ultima = instante(forcar) if forcar else rodada_mais_nova(estado, agora)
        if forcar:
            alvos = [(ultima + timedelta(hours=s), ultima, s) for s in range(0, PASSO_MAX + 1, INTERVALO_H)]
        else:
            alvos = []
            for h in horarios_desejados(agora, ultima):
                c = candidato(h, ultima)
                if c is None:
                    continue
                atual = hs.get(nome(h))
                if atual is None or c[1] < atual["p"]:
                    alvos.append((h, *c))
        # Os mais novos primeiro: o "agora" do mapa é o que mais importa.
        alvos.sort(key=lambda a: a[0], reverse=True)
        for h, r, s in alvos:
            nh, nr = nome(h), nome(r)
            try:
                bins = baixar_passo(r, s, ec)
            except urllib.error.HTTPError as e:
                # A rodada mais velha já saiu do servidor, ou o passo ainda não foi publicado.
                if e.code in (403, 404):
                    continue
                raise
            for nv, dado in bins.items():
                P.escrever_atomico(arquivo(nv, nh, nr), dado)
            antigo = hs.get(nh)
            if antigo and antigo["r"] != nr:
                aposentar(estado, [arquivo(nv, nh, antigo["r"]) for nv in NIVEIS])
            hs[nh] = {"r": nr, "p": s}
            feitos += 1
            # Progresso salvo a cada horário: uma execução morta não perde o que já baixou.
            P.escrever_atomico(ESTADO, json.dumps(estado).encode())
            P.escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(montar_indice(estado, agora), separators=(",", ":"), ensure_ascii=False).encode())
        estado["erro"] = None
    except Exception as e:  # noqa: BLE001 — o índice sai mesmo assim, com o erro
        erro = f"{type(e).__name__}: {e}"
        estado["erro"] = erro
    limpar(estado, agora)
    P.escrever_atomico(ESTADO, json.dumps(estado).encode())
    P.escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(montar_indice(estado, agora), separators=(",", ":"), ensure_ascii=False).encode())
    resumo = f"vento: {feitos} horários novos em {time.time() - t0:.0f} s · {len(hs)} no ar · rodada {estado.get('ultimaRodada')}"
    if erro:
        raise RuntimeError(f"{resumo} · falhou — {erro}")
    return resumo


def main() -> None:
    forcar = sys.argv[1] if len(sys.argv) == 2 else None
    try:
        print(executar(forcar=forcar))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
