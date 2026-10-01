#!/usr/bin/env python3
"""
Busca em português do AmaView: um pedido em linguagem comum vira ajustes do
mapa ("focos na TI Kayapó nas últimas 24 h", "fumaça sobre Manaus").

Quem entende o pedido é o Jev, da TypeSafe, chamado pela OpenRouter com o SDK
`typesafe_sdk`. O Jev não escreve texto: responde perguntas fechadas, com
probabilidade. Cada pedido é UMA chamada com todas as perguntas de uma vez:

- **Produto** (Choice): qual das 22 imagens do GOES-19, ou "nenhum". Troca a
  imagem só se "nenhum" tem até 30% (bandas vizinhas dividem o resto).
- **Período** (Choice): 1, 3, 6, 12, 24 ou 48 h, ou "nenhum".
- **Área** (Choice): o código pré-seleciona até 24 áreas do `busca.json` do
  AmaView cujo nome aparece no pedido (municípios, TIs, UCs, estados,
  regiões); o Jev escolhe uma, ou "nenhuma". Ele só escolhe — o nome e a chave
  saem do índice, nunca do modelo.
- **Camadas** (uma Noul cada): queimadas, fumaça, radar, estações etc. Só
  liga o que o pedido pede (probabilidade ≥ 0,5); não desliga nada.
- **Variável das estações** (Choice especulativa): só vale se as estações
  forem ligadas.

O app recebe a resposta pronta para aplicar (ids do próprio app) e os números
do modelo, para mostrar com que segurança cada ajuste foi feito. Nada é
gravado; o pedido não sai daqui a não ser para a OpenRouter.

A chave da OpenRouter fica em /etc/amaview/openrouter-key (`amaview chave
openrouter`) e nunca vai para o navegador. Para o gasto não fugir: 12 pedidos
por minuto por IP, um teto diário (BUSCA_TETO_DIA) e memória de 1 h para o
mesmo pedido.

  busca.py                serviço (porta 8091, só local; o nginx fica na frente)
  busca.py "pedido"       interpreta um pedido e sai (teste; chave em OPENROUTER_API_KEY)

URL: GET /busca/v1/?q=pedido — e /busca/saude.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone

VERSAO = 1
PORTA = int(os.environ.get("BUSCA_PORTA", "8091"))
CHAVE = os.environ.get("BUSCA_CHAVE", "/etc/amaview/openrouter-key")
BASE_URL = os.environ.get("BUSCA_BASE_URL", "https://openrouter.ai/api")
MODELO = os.environ.get("BUSCA_MODELO", "jev-latest")
INDICE_URL = os.environ.get("BUSCA_INDICE", "https://helvecioneto.github.io/AmaView/data/busca.json")
TETO_DIA = int(os.environ.get("BUSCA_TETO_DIA", "3000"))
POR_MINUTO = int(os.environ.get("BUSCA_POR_MINUTO", "12"))
MAX_CARACTERES = 200
MAX_CANDIDATOS = 24
LIMIAR = 0.5
LIMIAR_PRODUTO = 0.3  # chance máxima de "nenhum" para trocar a imagem
AGENTE = "AmaView-busca (+https://helvecioneto.github.io/AmaView/)"

# Imagens do GOES-19 (ids de src/config/products.ts do AmaView). A descrição em
# inglês é para o Jev (é a língua em que ele erra menos); o nome em português
# fica junto para casar com o pedido.
PRODUTOS = {
    "GEOCOLOR": "GeoColor natural-color image ('cor natural', 'imagem colorida', like a photo from space)",
    "FireTemperature": "Fire Temperature RGB ('temperatura do fogo'): shows how hot active fires burn, daytime only",
    "Sandwich": "Sandwich RGB for thunderstorms ('tempestades', storm tops, convection), daytime only",
    "AirMass": "Air Mass RGB ('massas de ar'): jet streams, dry and moist air masses",
    "Dust": "Dust RGB ('poeira'): airborne dust",
    "DayNightCloudMicroCombo": "Day/Night Cloud Micro Combo ('nuvens dia e noite'): clouds and fog, day and night",
    "01": "ABI band 1, blue visible ('azul'), daytime only",
    "02": "ABI band 2, red visible high resolution ('vermelho', 'visível'), daytime only",
    "03": "ABI band 3, vegetation near-infrared ('vegetação'), daytime only",
    "04": "ABI band 4, cirrus ('cirros'), daytime only",
    "05": "ABI band 5, snow and ice ('neve e gelo'), daytime only",
    "06": "ABI band 6, cloud particle size ('partículas de nuvem'), daytime only",
    "07": "ABI band 7, shortwave infrared ('ondas curtas', hot spots)",
    "08": "ABI band 8, upper-level water vapor ('vapor d'água alto')",
    "09": "ABI band 9, mid-level water vapor ('vapor d'água médio')",
    "10": "ABI band 10, lower-level water vapor ('vapor d'água baixo')",
    "11": "ABI band 11, cloud-top phase ('fase das nuvens')",
    "12": "ABI band 12, ozone ('ozônio')",
    "13": "ABI band 13, clean infrared ('infravermelho limpo'): cloud-top temperature, day and night",
    "14": "ABI band 14, infrared ('infravermelho')",
    "15": "ABI band 15, dirty infrared ('infravermelho sujo')",
    "16": "ABI band 16, carbon dioxide ('dióxido de carbono')",
}
NENHUM = "nenhum"

PERIODOS = {
    "1h": "the last hour, or right now",
    "3h": "the last 3 hours",
    "6h": "the last 6 hours",
    "12h": "the last 12 hours, or this morning / this afternoon",
    "24h": "the last 24 hours, today, or since yesterday",
    "48h": "the last 2 days, or since the day before yesterday",
}

# Camadas que o pedido pode ligar (ids de painel/camada do AmaView): uma
# pergunta sim/não para cada. A pergunta é sobre o que a pessoa quer saber, não
# sobre o nome da camada ("temperatura em Cuiabá" liga as estações).
CAMADAS = {
    "queimadas": "ask about fire hotspots detected by satellite ('focos de queimada', 'focos de calor', 'queimadas', 'incêndios', 'fogo')",
    "fumaca": "ask about smoke ('fumaça', 'fumaceira')",
    "radar": "ask about rain or showers happening now or recently, as seen by weather radar ('chuva', 'chovendo', 'radar', 'temporal')",
    "ar": "ask about air quality or air pollution ('qualidade do ar', 'poluição', 'PM2.5', 'ar ruim')",
    "meteo": "ask about weather conditions measured on the ground at places, such as air temperature, heat, humidity, wind or air pressure (not rain, and not satellite images such as water vapor or infrared)",
    "estacoes": "ask how much rain fell, as measured by rain gauges ('pluviômetros', 'quanto choveu', 'chuva acumulada')",
    "nivel": "ask about river water levels ('nível do rio', 'cheia', 'seca dos rios', 'vazante', 'régua')",
    "sondagem": "ask about radiosonde soundings or vertical profiles of the atmosphere ('sondagem', 'radiossonda', 'balão meteorológico', 'Skew-T')",
    "barcos": "ask about ships or boats and where they are ('barcos', 'navios', 'embarcações', 'balsas')",
    "rastreio": "ask to track storm or cloud systems and how they move ('rastreio', 'trajetória', 'para onde vão as nuvens')",
    "previsao": "ask where storms or rain will be in the next minutes or hour ('previsão', 'vai chover daqui a pouco', 'para onde a tempestade vai')",
    "terras_indigenas": "ask to draw the outlines of Indigenous lands on the map ('terras indígenas', 'TIs'), or ask about one specific Indigenous land",
    "unidades_conservacao": "ask to draw protected areas or parks on the map ('unidades de conservação', 'UCs', 'parques', 'reservas'), or ask about one specific protected area",
    "rios": "ask to draw rivers on the map ('rios', 'hidrografia')",
    "rodovias": "ask to draw highways or roads on the map ('rodovias', 'estradas', 'BR-163')",
    "municipios": "ask to draw municipality borders on the map ('limites dos municípios', 'divisas')",
}

VARIAVEIS_METEO = {
    "t": "air temperature ('temperatura', 'calor')",
    "u": "relative humidity ('umidade', 'ar seco')",
    "v": "wind ('vento')",
    "p": "air pressure ('pressão')",
    "c": "rainfall measured at the station ('chuva')",
    "d": "dew point ('ponto de orvalho')",
}

# Camadas do busca.json: como cada uma aparece para o Jev.
TIPOS_AREA = {
    "estados": "state",
    "municipios": "municipality (city)",
    "terras_indigenas": "Indigenous land (Terra Indígena)",
    "unidades_conservacao": "protected area (Unidade de Conservação)",
    "amazonia_legal": "the whole Legal Amazon region",
    "regioes_integracao_pa": "Pará integration region",
    "mesorregioes": "IBGE mesoregion",
    "microrregioes": "IBGE microregion",
    "regioes_intermediarias": "IBGE intermediate region",
    "regioes_imediatas": "IBGE immediate region",
}
# Em empate, o que as pessoas mais procuram vem antes.
ORDEM_TIPOS = list(TIPOS_AREA)

# Palavras que não identificam área nenhuma (o resto do pedido).
VAZIAS = set(
    "a ao aos as com da das de do dos e em na nas no nos o os pela pelo por que sobre um uma "
    "agora hoje ontem ultima ultimas ultimo ultimos hora horas dia dias semana mostre mostrar mostra "
    "ver veja quero onde tem ha esta estao perto regiao area mapa imagem satelite "
    "foco focos queimada queimadas fogo fumaca chuva radar vento temperatura ar rio rios "
    "terra terras indigena indigenas ti tis uc ucs unidade unidades conservacao parque reserva "
    "municipio municipios cidade estado animado animacao loop".split()
)


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFD", texto)
    sem_acento = "".join(c for c in sem_acento if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).strip()


def palavras_chave(pedido: str) -> list[str]:
    return [p for p in normalizar(pedido).split() if len(p) >= 3 and p not in VAZIAS]


_pesos: dict = {}


def pesos(indice: dict) -> dict:
    """Peso de cada palavra dos nomes: 1 se aparece em até 3 áreas, menos se é comum."""
    if _pesos.get("de") is not indice:
        conta: dict = defaultdict(int)
        for itens in indice.get("layers", {}).values():
            for _, nome, _ in itens:
                for p in set(normalizar(nome).split()):
                    conta[p] += 1
        _pesos.clear()
        _pesos.update(de=indice, pesos={p: min(1.0, 3 / n) for p, n in conta.items()})
    return _pesos["pesos"]


def candidatos(pedido: str, indice: dict, n: int = MAX_CANDIDATOS) -> list[dict]:
    """
    Áreas cujo nome aparece no pedido, das mais prováveis para as menos.
    Pontos: o nome inteiro no pedido vale mais que cada palavra; palavra
    inteira vale mais que começo de palavra (≥ 4 letras: "kayap" acha
    Kayapó). A sigla do estado (detalhe) desempata Santa Maria do PA e do AM.
    """
    texto = " " + normalizar(pedido) + " "
    chaves = palavras_chave(pedido)
    if not chaves:
        return []
    raridade = pesos(indice)
    achados = []
    for camada, itens in indice.get("layers", {}).items():
        if camada not in TIPOS_AREA:
            continue
        for chave, nome, detalhe in itens:
            nome_n = normalizar(nome)
            palavras = nome_n.split()
            pontos = 0.0
            for c in chaves:
                # Palavra rara pesa mais: "xingu" ganha de "nacional" (dezenas de parques).
                if c in palavras:
                    pontos += 2 * raridade.get(c, 1.0)
                elif len(c) >= 4 and any(p.startswith(c) for p in palavras):
                    pontos += raridade.get(c, 1.0)
            if not pontos:
                continue
            if f" {nome_n} " in texto:
                pontos += 5
            # Fração do nome coberta: "Belém" ganha de "Santa Maria de Belém".
            pontos += sum(1 for p in palavras if p in chaves) / len(palavras)
            if detalhe and f" {normalizar(detalhe)} " in texto:
                pontos += 0.5
            achados.append((-pontos, ORDEM_TIPOS.index(camada), len(nome), camada, chave, nome, detalhe))
    achados.sort()
    return [
        {"layer": camada, "key": chave, "name": nome, "detail": detalhe}
        for _, _, _, camada, chave, nome, detalhe in achados[:n]
    ]


def descrever_area(a: dict) -> str:
    tipo = TIPOS_AREA[a["layer"]]
    extra = ""
    if a["detail"] and a["detail"] != a["name"]:
        rotulo = {"municipios": "state", "terras_indigenas": "people", "unidades_conservacao": "category"}.get(a["layer"], "detail")
        extra = f" ({rotulo}: {a['detail']})"
    return f"{a['name']}{extra}, a {tipo}"


def montar_perguntas(areas: list[dict]) -> dict:
    """As perguntas do pedido; os ids voltam na resposta e só o código os lê."""
    q: dict = {
        "produto": {
            "type": "choice",
            "instructions": (
                "The `pedido` is a request (in Portuguese) to a satellite map of the Brazilian Amazon. "
                "Which satellite image product does it ask to show? Choose 'nenhum' unless the request "
                "names an image type or a phenomenon that is seen mainly in one specific image "
                "(storms, dust, air masses, water vapor, cloud-top temperature). Fire hotspots, smoke, "
                "rain, rivers and places alone do not ask for an image product."
            ),
            "criteria": {**PRODUTOS, NENHUM: "The request does not ask for any specific satellite image product"},
        },
        "periodo": {
            "type": "choice",
            "instructions": (
                "The `pedido` is a request (in Portuguese) to a satellite map that can show up to the last "
                "48 hours. Which time window does it ask to look at? Choose 'nenhum' if it does not mention time."
            ),
            "criteria": {**PERIODOS, NENHUM: "The request does not mention any time window"},
        },
        "animar": {
            "type": "noul",
            "instructions": (
                "Does the `pedido` (a request to a satellite map) ask to play an animation or loop of "
                "the images over time ('animação', 'animar', 'loop', 'evolução', 'como se moveu')?"
            ),
        },
        "variavel_meteo": {
            "type": "choice",
            "instructions": (
                "Suppose the `pedido` (a request to a satellite map) wants weather station readings. "
                "Which measured variable is it most about?"
            ),
            "criteria": VARIAVEIS_METEO,
        },
    }
    for camada, desc in CAMADAS.items():
        q[f"camada.{camada}"] = {
            "type": "noul",
            "instructions": (
                f"Does the `pedido` (a request in Portuguese to a satellite map of the Amazon) {desc}?"
            ),
        }
    if areas:
        opcoes = {f"a{i}": descrever_area(a) for i, a in enumerate(areas)}
        opcoes[NENHUM] = "None of these places: the request names no place, or a place not in this list"
        q["area"] = {
            "type": "choice",
            "instructions": (
                "The `pedido` is a request (in Portuguese) to a satellite map of the Brazilian Amazon. "
                "Which place does it ask to look at? Match the place the person names, including its "
                "kind when they say it (city, state, Indigenous land 'TI', protected area 'UC'). If the "
                "exact name is not listed, choose the listed place that shares its distinctive name."
            ),
            "criteria": opcoes,
        }
    return q


def interpretar(respostas: dict, areas: list[dict]) -> dict:
    """Respostas do Jev → ajustes com os ids do AmaView (só o que foi pedido)."""
    def escolha(qid: str):
        r = respostas.get(qid)
        if not r or r["choice"] == NENHUM:
            return None
        return r["choice"], round(r["confidence"], 2)

    saida: dict = {"produto": None, "periodo": None, "area": None, "camadas": [], "variavelMeteo": None}
    # Produto: vale se "nenhum" é improvável, mesmo com a escolha dividida entre
    # imagens parecidas (bandas 13 e 14, vapor alto e médio); fica a mais provável.
    if (r := respostas.get("produto")) and r["probabilities"].get(NENHUM, 0) <= LIMIAR_PRODUTO:
        melhor = max((k for k in r["probabilities"] if k != NENHUM), key=r["probabilities"].get)
        saida["produto"] = {"id": melhor, "confianca": round(r["probabilities"][melhor], 2)}
    if (p := escolha("periodo")):
        saida["periodo"] = {"id": p[0], "confianca": p[1]}
    if (p := escolha("area")):
        saida["area"] = {**areas[int(p[0][1:])], "confianca": p[1]}
    probabilidades = {c: round(respostas[f"camada.{c}"]["noul"], 2) for c in CAMADAS if f"camada.{c}" in respostas}
    saida["camadas"] = [c for c, pr in probabilidades.items() if pr >= LIMIAR]
    if "meteo" in saida["camadas"] and (p := escolha("variavel_meteo")):
        saida["variavelMeteo"] = p[0]
    saida["animar"] = respostas.get("animar", {}).get("noul", 0) >= LIMIAR
    saida["probabilidades"] = probabilidades
    return saida


def respostas_como_dict(resultado) -> dict:
    """SystemOneResponse do SDK → dicionário simples (o que `interpretar` lê)."""
    saida = {}
    for qid, r in resultado.answers.items():
        if r.type == "noul":
            saida[qid] = {"noul": r.noul}
        else:
            saida[qid] = {"choice": r.choice, "confidence": r.confidence, "probabilities": dict(r.probabilities)}
    return saida


# --- Índice das áreas (o mesmo busca.json que o app usa) ----------------------

_indice: dict = {"dados": None, "baixado": 0.0}
_trava_indice = threading.Lock()


def indice() -> dict:
    with _trava_indice:
        velho = time.time() - _indice["baixado"] > 24 * 3600
        if _indice["dados"] is None or velho:
            try:
                req = urllib.request.Request(INDICE_URL, headers={"User-Agent": AGENTE})
                with urllib.request.urlopen(req, timeout=30) as r:
                    _indice["dados"] = json.load(r)
                _indice["baixado"] = time.time()
            except Exception as e:  # noqa: BLE001
                if _indice["dados"] is None:
                    raise
                print(f"índice: mantendo o anterior ({e})", file=sys.stderr)
                _indice["baixado"] = time.time() - 23 * 3600  # tenta de novo em 1 h
        return _indice["dados"]


# --- Jev ---------------------------------------------------------------------

_cliente = None


def ler_chave() -> str | None:
    try:
        with open(CHAVE) as f:
            return f.read().strip() or None
    except OSError:
        return os.environ.get("OPENROUTER_API_KEY") or None


def cliente():
    global _cliente
    if _cliente is None:
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        chave = ler_chave()
        if not chave:
            raise SemChave()
        _cliente = TypeSafeClient(
            api_key=chave, base_url=BASE_URL, model=MODELO, timeout=20,
            retry=RetryPolicy(max_retries=2), headers={"X-Title": "AmaView", "HTTP-Referer": "https://helvecioneto.github.io/AmaView/"},
        )
    return _cliente


class SemChave(Exception):
    pass


def buscar(pedido: str) -> dict:
    areas = candidatos(pedido, indice())
    t0 = time.time()
    resultado = cliente().system_one(state={"pedido": pedido}, questions=montar_perguntas(areas))
    saida = interpretar(respostas_como_dict(resultado), areas)
    uso = resultado.usage
    saida.update(
        pedido=pedido,
        modelo=resultado.model,
        segundos=round(time.time() - t0, 2),
        tokens=getattr(uso, "input_tokens", None),
    )
    return saida


# --- Serviço -----------------------------------------------------------------

_memoria: OrderedDict = OrderedDict()  # pedido normalizado → (instante, resposta)
_por_ip: dict = defaultdict(deque)
_estado = {"inicio": time.time(), "dia": "", "pedidos_dia": 0, "da_memoria": 0, "falhas": 0, "ultimo_erro": None}
_trava = threading.Lock()


def pode(ip: str) -> str | None:
    """None se o pedido pode seguir; senão, o motivo (429)."""
    agora = time.time()
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _trava:
        if _estado["dia"] != hoje:
            _estado.update(dia=hoje, pedidos_dia=0)
        fila = _por_ip[ip]
        while fila and agora - fila[0] > 60:
            fila.popleft()
        if len(fila) >= POR_MINUTO:
            return "muitos pedidos seguidos; espere um minuto"
        if _estado["pedidos_dia"] >= TETO_DIA:
            return "a busca atingiu o limite de hoje; volta amanhã (UTC)"
        fila.append(agora)
        _estado["pedidos_dia"] += 1
        if len(_por_ip) > 5000:
            _por_ip.clear()
    return None


def lembrar(chave: str) -> dict | None:
    with _trava:
        item = _memoria.get(chave)
        if item and time.time() - item[0] < 3600:
            _memoria.move_to_end(chave)
            _estado["da_memoria"] += 1
            return item[1]
    return None


def guardar(chave: str, resposta: dict) -> None:
    with _trava:
        _memoria[chave] = (time.time(), resposta)
        while len(_memoria) > 1000:
            _memoria.popitem(last=False)


def saude() -> dict:
    return {
        "versao": VERSAO,
        "modelo": MODELO,
        "chave": bool(ler_chave()),
        "indice": bool(_indice["dados"]),
        "no_ar_ha_min": round((time.time() - _estado["inicio"]) / 60),
        "dia": _estado["dia"],
        "pedidos_dia": _estado["pedidos_dia"],
        "teto_dia": TETO_DIA,
        "da_memoria": _estado["da_memoria"],
        "falhas": _estado["falhas"],
        "ultimo_erro": _estado["ultimo_erro"],
    }


class Pedido(http.server.BaseHTTPRequestHandler):
    server_version = "AmaView-busca"

    def _json(self, codigo: int, dados: dict) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/busca/saude":
            return self._json(200, saude())
        if url.path not in ("/busca/v1/", "/busca/v1"):
            return self._json(404, {"erro": "rota desconhecida"})
        pedido = " ".join(urllib.parse.parse_qs(url.query).get("q", [""])[0].split())
        if not pedido:
            return self._json(400, {"erro": "pedido vazio"})
        if len(pedido) > MAX_CARACTERES:
            return self._json(400, {"erro": f"pedido longo demais (até {MAX_CARACTERES} caracteres)"})
        chave = normalizar(pedido)
        if (pronta := lembrar(chave)) is not None:
            return self._json(200, pronta)
        ip = self.headers.get("X-Real-IP") or self.client_address[0]
        if (motivo := pode(ip)):
            return self._json(429, {"erro": motivo})
        try:
            resposta = buscar(pedido)
        except SemChave:
            return self._json(503, {"erro": "a busca ainda não foi configurada no servidor"})
        except Exception as e:  # noqa: BLE001
            with _trava:
                _estado["falhas"] += 1
                _estado["ultimo_erro"] = f"{datetime.now(timezone.utc):%H:%M:%S} {type(e).__name__}: {str(e)[:200]}"
            print(_estado["ultimo_erro"], file=sys.stderr)
            return self._json(502, {"erro": "o modelo não respondeu; tente de novo"})
        guardar(chave, resposta)
        self._json(200, resposta)

    def log_message(self, formato: str, *args) -> None:
        # O pedido em si não vai para o log: só o código de resposta.
        sys.stderr.write(f"{self.command} {args[1] if len(args) > 1 else ''}\n")


def main() -> None:
    if len(sys.argv) == 2:
        print(json.dumps(buscar(sys.argv[1]), ensure_ascii=False, indent=1))
        return
    try:
        indice()
    except Exception as e:  # noqa: BLE001
        print(f"índice indisponível agora ({e}); tento no primeiro pedido", file=sys.stderr)
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    servidor.daemon_threads = True
    print(f"busca v{VERSAO} em 127.0.0.1:{PORTA}, modelo {MODELO}, chave {'presente' if ler_chave() else 'AUSENTE'}")
    servidor.serve_forever()


if __name__ == "__main__":
    main()
