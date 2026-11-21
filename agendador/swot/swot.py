#!/usr/bin/env python3
"""
Nível dos rios grandes da Amazônia Legal medido pelo satélite SWOT, para a
camada `src/swot` do AmaView.

Fonte: Hydrocron (NASA PO.DAAC), coleção `SWOT_L2_HR_RiverSP_reach_D`. Cada
trecho de rio (~10 km, ids do SWORD) tem a série das passagens do satélite:
`wse` é a cota da água em metros sobre o geoide EGM2008 — não é a cota da
régua da ANA. −999999999999 = sem dado.

**Percurso (uma vez).** Sem estado, o script percorre a rede pelo próprio
Hydrocron, como o protótipo do app (`scripts/swot_rios.py`): parte de um
trecho de cada rio grande (`SEMENTES`), anda rio abaixo e rio acima pelos
vizinhos (`rch_id_up`, `rch_id_dn`) enquanto o centro do trecho está na
Amazônia Legal (com folga de 0,3°) e a largura prevista é ≥ 400 m, e pula os
trechos que o Hydrocron não conhece pela numeração do SWORD. São ~2.700
pedidos, ~1 h com 2 de cada vez. `swot.py percurso` refaz (as séries que
já estão no estado continuam).

**Dia a dia (incremental).** O estado guarda, por trecho, o traçado, a série e
o horário da última passagem de cada órbita (`pass_id`) que o cobre. A órbita
do SWOT se repete a cada 20,86 dias, então a próxima passagem de cada órbita é
conhecida: um trecho só é pedido quando uma órbita que já trouxe cota válida
no ano passou por ele há mais de 5 dias (o Hydrocron recebe a passagem ~4–5
dias depois dela), e o pedido cobre só desde essa passagem. Se a passagem não
chega em 16 dias, desiste dela até o ciclo seguinte; com duas faltas seguidas
a órbita para de disparar pedidos (há órbitas que o Hydrocron deixou de trazer
para um trecho, e elas fariam pedidos inúteis todo dia). Cada trecho também é
revisto a cada dois ciclos (~42 dias, escalonado: ~1/42 dos trechos por dia),
para pegar o que chegou atrasado, órbita nova e reprocessamento. Resultado:
algumas centenas de pedidos por dia, 2 de cada vez, no lugar dos ~2.700.

Nada é corrigido: cada passagem guarda a cota, a incerteza e a qualidade do
SWOT (`reach_q`: 0 boa, 1 suspeita, 2 degradada, 3 ruim). Passagens com
`reach_q` = 3, `partial_f` = 1 ou cota sem dado ficam de fora da série. A
série de cada trecho é de 12 meses: o que fica mais velho que isso sai.

Chave opcional (`x-hydrocron-key`): a PO.DAAC não exige, mas pede que uso
contínuo com muitos pedidos por dia peça uma (issue no GitHub do Hydrocron).
Com o arquivo SWOT_CHAVE (padrão /etc/amaview/hydrocron-key, gravado pelo
`amaview chave hydrocron`), ela vai no cabeçalho; sem ele, tudo funciona igual.

Saída (abaixo de SWOT_RAIZ, servida pelo nginx em /swot/v1/):

  swot/v1/trechos.json (+ .gz)  traçado simplificado, nome, largura e série de cada trecho
  estado.json                   o estado (fora do que o nginx serve)

  swot.py            uma rodada (o que o timer chama): percurso se não há estado, senão incremental
  swot.py percurso   refaz o percurso da rede
"""
import concurrent.futures as cf
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSAO = 1
RAIZ = os.environ.get("SWOT_RAIZ", "/var/cache/amaview-swot")
PASTA = os.path.join(RAIZ, "swot", f"v{VERSAO}")
ESTADO = os.path.join(RAIZ, "estado.json")
CHAVE = os.environ.get("SWOT_CHAVE", "/etc/amaview/hydrocron-key")
POLIGONO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amazonia_legal.json")
HYDROCRON = "https://soto.podaac.earthdatacloud.nasa.gov/hydrocron/v1/timeseries"
COLECAO = "SWOT_L2_HR_RiverSP_reach_D"
CAMPOS_SERIE = "reach_id,time_str,wse,wse_u,reach_q,partial_f,pass_id,range_start_time"
CAMPOS_PERCURSO = CAMPOS_SERIE + ",rch_id_up,rch_id_dn,p_width,p_lat,p_lon,river_name"
SEM_DADO = -999999999999.0
AGENTE = "AmaView-swot (+https://amaview.com.br/; projeto academico)"

# Pedidos ao mesmo tempo e teto por rodada (o percurso tem o seu).
PARALELO = int(os.environ.get("SWOT_PARALELO", "2"))
LIMITE_RODADA = int(os.environ.get("SWOT_LIMITE", "1500"))
LIMITE_PERCURSO = 4000
LARGURA_MIN = 400.0  # m, largura prevista no SWORD
FOLGA_GRAUS = 0.3
SIMPLIFICAR = 0.002  # graus (~200 m)

DIA = 86400.0
JANELA_S = 365 * DIA  # série de 12 meses
CICLO_S = 20.86462 * DIA  # repetição da órbita científica do SWOT
LATENCIA_S = 5 * DIA  # a passagem chega ao Hydrocron ~4–5 dias depois (medido: 3,6 a 5,2 d; às vezes 14)
DESISTIR_S = 16 * DIA  # passagem que não chegou nisso fica para o ciclo seguinte
REVISAO_S = 2 * CICLO_S  # revisão de cada trecho, escalonada
FALTAS_MAX = 2  # passagens seguidas que não chegaram: a órbita para de disparar pedidos
MARGEM_S = 2 * DIA

#: Um trecho de cada rio grande, de onde o percurso parte (ids conferidos no
#: Hydrocron em 21/11/2026; os de fora do Amazonas vieram do FTS da PO.DAAC).
#: Rio abaixo, o Teles Pires e o Juruena levam ao Tapajós; o Araguaia, ao
#: Tocantins. O Juruá e o Japurá não foram achados no FTS.
SEMENTES = [
    "62293900091",  # Solimões em Coari (AM)
    "62410300061",  # Tocantins, abaixo de Tucuruí (PA)
    "62451100011",  # Araguaia, perto da foz no Tocantins
    "62244100011",  # Teles Pires
    "62244300021",  # Juruena
    "62268100011",  # Guaporé (RO)
    "62297400411",  # Putumayo / Içá
]


# ---------------------------------------------------------------------------
# Hydrocron


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(s) -> float | None:
    if not s or s == "no_data":
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f <= SEM_DADO / 2 else f


def ids(v) -> list[str]:
    out = []
    for s in v if isinstance(v, list) else [v]:
        for x in str(s or "").split(","):
            x = x.strip()
            if x and x != "no_data" and x not in out:
                out.append(x)
    return out


def ler_chave() -> str | None:
    try:
        with open(CHAVE) as f:
            return f.read().strip() or None
    except OSError:
        return None


class Hydrocron:
    """Pedidos ao Hydrocron, contados, com a chave opcional e recuo no 429/5xx."""

    def __init__(self, chave: str | None = None):
        self.chave = chave
        self.pedidos = 0
        self.recuos = 0  # 429, 5xx ou rede: o pedido esperou e foi repetido
        self._trava = threading.Lock()

    def abrir(self, url: str) -> bytes:  # separado para o teste trocar
        cab = {"User-Agent": AGENTE, "Accept": "application/geo+json"}
        if self.chave:
            cab["x-hydrocron-key"] = self.chave
        with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=60) as r:
            return r.read()

    def serie(self, reach_id: str, inicio: float, fim: float, campos: str = CAMPOS_SERIE, tentativas: int = 4) -> dict | None:
        """Um trecho no período. None = nada no período (ou trecho que o Hydrocron não conhece): o 400 dele."""
        q = urllib.parse.urlencode({
            "feature": "Reach", "feature_id": reach_id, "start_time": iso(inicio), "end_time": iso(fim),
            "fields": campos, "collection_name": COLECAO, "compact": "true",
        })
        for k in range(tentativas):
            with self._trava:
                self.pedidos += 1
            try:
                fc = json.loads(self.abrir(f"{HYDROCRON}?{q}").decode())
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    return None
                if k == tentativas - 1:
                    raise
                # 429 ou 5xx: o serviço pediu calma.
                espera = e.headers.get("Retry-After") if e.headers else None
                self._recuo(reach_id, f"HTTP {e.code}")
                time.sleep(min(120.0, float(espera)) if espera and espera.isdigit() else 5.0 * 2**k)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if k == tentativas - 1:
                    raise
                self._recuo(reach_id, type(e).__name__)
                time.sleep(5.0 * 2**k)
        return ler_resposta(reach_id, fc)

    def _recuo(self, reach_id: str, motivo: str) -> None:
        with self._trava:
            self.recuos += 1
            n = self.recuos
        if n <= 20 or n % 100 == 0:  # o diário não enche
            print(f"  Hydrocron: {motivo} em {reach_id}, repetindo ({n} até agora)", file=sys.stderr, flush=True)


def ler_resposta(reach_id: str, fc: dict) -> dict | None:
    """GeoJSON compacto do Hydrocron → {id, up, dn, larg, centro, rio, geom, obs}."""
    if isinstance(fc.get("results"), dict):  # resposta embrulhada (sem o Accept: application/geo+json)
        fc = fc["results"].get("geojson") or {}
    feats = fc.get("features") or []
    if not feats:
        return None
    p = feats[0].get("properties") or {}
    tempos = p.get("time_str") or []
    if not isinstance(tempos, list):
        tempos = [tempos]
    n = len(tempos)

    def col(nome):
        v = p.get(nome)
        return v if isinstance(v, list) else [v] * n

    wse, wse_u, rq, parcial = col("wse"), col("wse_u"), col("reach_q"), col("partial_f")
    passes, inicios = col("pass_id"), col("range_start_time")
    obs = []
    for i in range(n):
        obs.append({
            "t": epoch(tempos[i]),
            "rst": epoch(inicios[i]),
            "pass": str(passes[i]) if passes[i] not in (None, "", "no_data") else None,
            "wse": num(wse[i]),
            "u": num(wse_u[i]),
            "q": num(rq[i]),
            "parcial": num(parcial[i]) or 0,
        })
    larguras = [w for w in (num(x) for x in col("p_width")) if w and w > 0]
    lats = [v for v in (num(x) for x in col("p_lat")) if v is not None]
    lons = [v for v in (num(x) for x in col("p_lon")) if v is not None]
    nomes = [x for x in col("river_name") if x and x not in ("no_data", "NODATA")]
    return {
        "id": reach_id,
        "up": ids(col("rch_id_up")),
        "dn": ids(col("rch_id_dn")),
        "larg": max(larguras) if larguras else 0.0,
        "centro": (lons[0], lats[0]) if lats and lons else None,
        "rio": nomes[0] if nomes else None,
        "geom": feats[0].get("geometry"),
        "obs": obs,
    }


def passagem_valida(o: dict) -> list | None:
    """Observação → [t, cota, incerteza | None, qualidade], ou None se fica de fora."""
    if o["t"] is None or o["wse"] is None or o["q"] is None or o["q"] >= 3 or o["parcial"] >= 1:
        return None
    u = o["u"]
    return [int(o["t"]), round(o["wse"], 3), round(u, 3) if u and u > 0 else None, int(o["q"])]


# ---------------------------------------------------------------------------
# Estado de um trecho


def novo_trecho(tr: dict, linhas: list) -> dict:
    return {"rio": tr["rio"], "larg": round(tr["larg"]), "linhas": linhas, "serie": [], "passes": {}, "validos": {}, "revisto": 0.0}


def mesclar(est: dict, obs: list[dict]) -> int:
    """Junta as observações ao trecho; devolve quantas passagens válidas são novas."""
    serie = {s[0]: s for s in est["serie"]}
    novas = 0
    faltas = est.setdefault("faltas", {})
    for o in obs:
        p = o["pass"]
        if p and o["rst"] is not None:
            antes = est["passes"].get(p, 0.0)
            # A órbita voltou a passar (a esperada, mesmo atrasada, ou uma mais nova).
            if o["rst"] >= antes - CICLO_S / 2:
                faltas.pop(p, None)
            est["passes"][p] = max(antes, o["rst"])
        s = passagem_valida(o)
        if s is None:
            continue
        if s[0] not in serie:
            novas += 1
        serie[s[0]] = s  # o Hydrocron manda: passagem reprocessada substitui a antiga
        if p:
            est["validos"][p] = max(est["validos"].get(p, 0.0), float(s[0]))
    est["serie"] = [serie[t] for t in sorted(serie)]
    return novas


def aparar(est: dict, agora: float) -> None:
    """A série é de 12 meses: o que passou disso sai."""
    corte = agora - JANELA_S
    est["serie"] = [s for s in est["serie"] if s[0] >= corte]


def pedido_do_trecho(rid: str, est: dict, agora: float) -> tuple[str, float] | None:
    """('revisao' | 'passagem', início do período) se o trecho deve ser pedido hoje."""
    if agora - est.get("revisto", 0.0) >= REVISAO_S:
        return "revisao", agora - REVISAO_S - MARGEM_S - LATENCIA_S
    faltas = est.get("faltas", {})
    devidos = [
        t + CICLO_S
        for p, t in est["passes"].items()
        if est["validos"].get(p, 0.0) >= agora - JANELA_S
        and faltas.get(p, 0) < FALTAS_MAX
        and agora >= t + CICLO_S + LATENCIA_S
    ]
    if devidos:
        return "passagem", min(devidos) - MARGEM_S
    return None


def desistir(est: dict, agora: float) -> int:
    """
    Passagem que não chegou em DESISTIR_S: a órbita anda para o ciclo seguinte.
    Com FALTAS_MAX seguidas, a órbita para de disparar pedidos (o Hydrocron
    parou de trazê-la para o trecho); a revisão a cada dois ciclos a reativa
    se ela voltar.
    """
    puladas = 0
    faltas = est.setdefault("faltas", {})
    for p, t in list(est["passes"].items()):
        while agora >= t + CICLO_S + DESISTIR_S:
            t += CICLO_S
            puladas += 1
            faltas[p] = faltas.get(p, 0) + 1
        est["passes"][p] = t
    return puladas


def revisao_inicial(rid: str, agora: float) -> float:
    """Escalona as revisões: cada trecho começa num ponto diferente do intervalo."""
    try:
        fase = (int(rid) % 997) / 997
    except ValueError:
        fase = 0.5
    return agora - fase * REVISAO_S


# ---------------------------------------------------------------------------
# Percurso da rede


def poligono_amazonia(folga: float = FOLGA_GRAUS):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    with open(POLIGONO) as f:
        aneis = json.load(f)["aneis"]
    return unary_union([Polygon(a).buffer(0) for a in aneis if len(a) >= 4]).buffer(folga)


def alternativas(reach_id: str, direcao: str) -> list[str]:
    """Os trechos seguintes pela numeração do SWORD (tipo rio), na direção em que se andava."""
    base = int(reach_id) // 10  # CBBBBBRRRR
    passos = (1, 2) if direcao == "up" else (-1, -2)
    out = [str(base * 10 + 1)] if int(reach_id) % 10 != 1 else []
    out += [str((base + d) * 10 + 1) for d in passos]
    return out


def simplificar(geom: dict | None, tol: float = SIMPLIFICAR) -> list:
    from shapely.geometry import LineString

    if not geom or geom.get("type") not in ("LineString", "MultiLineString"):
        return []
    linhas = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
    out = []
    for coords in linhas:
        if len(coords) < 2:
            continue
        s = LineString(coords).simplify(tol, preserve_topology=False)
        out.append([[round(x, 4), round(y, 4)] for x, y in s.coords])
    return out


def percorrer(h: Hydrocron, inicio: float, fim: float, area, limite: int = LIMITE_PERCURSO) -> dict:
    """Busca em largura a partir das sementes; devolve {id: trecho do Hydrocron} dos aceitos."""
    from shapely.geometry import Point

    vistos: dict[str, dict | None] = {}
    aceitos: dict[str, dict] = {}
    # (id, direção, saltos já usados para atravessar trechos que o Hydrocron não conhece)
    fronteira: list[tuple[str, str, int]] = [(s, "ambos", 0) for s in SEMENTES]
    t0 = time.time()
    with cf.ThreadPoolExecutor(PARALELO) as ex:
        while fronteira and h.pedidos < limite:
            lote = []
            for item in fronteira:
                if item[0] not in vistos and all(item[0] != x[0] for x in lote):
                    lote.append(item)
            lote = lote[: max(0, limite - h.pedidos)]
            fronteira = []
            resultados = list(ex.map(lambda it: (it, h.serie(it[0], inicio, fim, CAMPOS_PERCURSO)), lote))
            for (rid, direcao, saltos), tr in resultados:
                vistos[rid] = tr
                if tr is None:
                    if saltos < 2 and direcao in ("up", "dn"):
                        fronteira += [(a, direcao, saltos + 1) for a in alternativas(rid, direcao) if a not in vistos]
                    continue
                dentro = tr["centro"] is not None and area.contains(Point(tr["centro"]))
                if not dentro or tr["larg"] < LARGURA_MIN:
                    continue
                aceitos[rid] = tr
                fronteira += [(v, "up", 0) for v in tr["up"] if v not in vistos]
                fronteira += [(v, "dn", 0) for v in tr["dn"] if v not in vistos]
            print(f"  percurso: {h.pedidos} pedidos, {len(aceitos)} trechos aceitos ({time.time() - t0:.0f} s)", flush=True)
    return aceitos


# ---------------------------------------------------------------------------
# Disco


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


def publicar(trechos: dict, agora: float) -> dict:
    """trechos.json no formato que o app lê (`src/swot/api.ts`), e a cópia gzip ao lado."""
    lista = [
        {"id": rid, "rio": est["rio"], "larg": est["larg"], "linhas": est["linhas"], "serie": est["serie"]}
        for rid, est in sorted(trechos.items())
        if est["linhas"]
    ]
    saida = {
        "versao": VERSAO,
        "gerado": iso(agora),
        "fonte": f"NASA PO.DAAC Hydrocron, {COLECAO} (ids e traçado do SWORD)",
        "inicio": iso(agora - JANELA_S),
        "fim": iso(agora),
        "larguraMin": LARGURA_MIN,
        "referencia": "EGM2008",
        "campos": ["t", "wse", "u", "q"],
        "trechos": lista,
    }
    corpo = json.dumps(saida, ensure_ascii=False, separators=(",", ":")).encode()
    os.makedirs(PASTA, exist_ok=True)
    # O .gz primeiro: o nginx (gzip_static) nunca serve um .gz mais velho que o .json.
    escrever_atomico(os.path.join(PASTA, "trechos.json.gz"), gzip.compress(corpo, 9, mtime=0))
    escrever_atomico(os.path.join(PASTA, "trechos.json"), corpo)
    return {"trechos": len(lista), "com_passagem": sum(1 for t in lista if t["serie"]),
            "passagens": sum(len(t["serie"]) for t in lista), "kb": len(corpo) / 1e3}


def rodada(agora: float | None = None, refazer_percurso: bool = False, h: Hydrocron | None = None) -> dict:
    agora = agora or time.time()
    h = h or Hydrocron(ler_chave())
    estado = ler_json(ESTADO, {})
    trechos: dict[str, dict] = estado.get("trechos", {})
    resumo = {"percurso": False, "pedidos": 0, "novas": 0, "puladas": 0, "pedidos_trecho": 0}

    if refazer_percurso or not trechos:
        resumo["percurso"] = True
        aceitos = percorrer(h, agora - JANELA_S, agora, poligono_amazonia())
        if not aceitos:
            raise RuntimeError("o percurso não aceitou nenhum trecho (Hydrocron fora do ar?)")
        novo = {}
        for rid, tr in aceitos.items():
            linhas = simplificar(tr["geom"])
            if not linhas:
                continue
            est = trechos.get(rid) or novo_trecho(tr, linhas)
            est.update({"rio": tr["rio"], "larg": round(tr["larg"]), "linhas": linhas})
            resumo["novas"] += mesclar(est, tr["obs"])
            est["revisto"] = revisao_inicial(rid, agora)
            novo[rid] = est
        trechos = novo
        estado["percurso"] = agora
    else:
        fila = []
        for rid, est in trechos.items():
            pedido = pedido_do_trecho(rid, est, agora)
            if pedido:
                fila.append((pedido[1], rid, pedido[0]))
        fila.sort()  # o que está esperando há mais tempo primeiro
        fila = fila[:LIMITE_RODADA]

        def um(item):
            inicio, rid, motivo = item
            return item, h.serie(rid, max(inicio, agora - JANELA_S), agora)

        falhas = 0
        with cf.ThreadPoolExecutor(PARALELO) as ex:
            futuros = [ex.submit(um, it) for it in fila]
            for fut in cf.as_completed(futuros):
                try:
                    (inicio, rid, motivo), tr = fut.result()
                except Exception as e:  # noqa: BLE001 — um trecho falhando não para os outros
                    falhas += 1
                    print(f"falha num pedido: {e}", file=sys.stderr)
                    continue
                est = trechos[rid]
                if tr is not None:
                    resumo["novas"] += mesclar(est, tr["obs"])
                if motivo == "revisao":
                    est["revisto"] = agora
                resumo["puladas"] += desistir(est, agora)
        resumo["pedidos_trecho"] = len(fila)
        resumo["falhas"] = falhas

    for est in trechos.values():
        aparar(est, agora)
    estado["trechos"] = trechos
    estado["rodada"] = agora
    resumo["pedidos"] = h.pedidos
    resumo["recuos"] = h.recuos
    resumo.update(publicar(trechos, agora))
    escrever_atomico(ESTADO, json.dumps(estado, separators=(",", ":")).encode())
    return resumo


def main() -> None:
    refazer = len(sys.argv) > 1 and sys.argv[1] == "percurso"
    t0 = time.time()
    r = rodada(refazer_percurso=refazer)
    chave = "com chave" if ler_chave() else "sem chave"
    print(
        f"rodada{' (percurso da rede)' if r['percurso'] else ''}: {r['pedidos']} pedidos ao Hydrocron ({chave}; {r['recuos']} repetidos), "
        f"{r['novas']} passagens novas, {r['puladas']} esperadas que não chegaram; "
        f"{r['trechos']} trechos ({r['com_passagem']} com passagem, {r['passagens']} passagens), "
        f"trechos.json {r['kb']:.0f} KB, {time.time() - t0:.0f} s"
    )
    sys.exit(1 if r.get("falhas") and r["falhas"] >= max(1, r.get("pedidos_trecho", 0)) else 0)


if __name__ == "__main__":
    main()
