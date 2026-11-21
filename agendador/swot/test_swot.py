"""
Testes do SWOT, sem rede: o Hydrocron é trocado por uma rede de trechos
sintética (vizinhos, larguras, passagens).

  python3 -I -m unittest discover -s agendador/swot
"""
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
import swot  # noqa: E402

DIA = swot.DIA
AGORA = 1_795_000_000.0  # ~nov/2026


def obs(t, passe, wse=20.0, q=1, parcial=0, sem=False):
    """Uma passagem como o Hydrocron manda (compacto: listas de texto)."""
    return {
        "time_str": "no_data" if sem else swot.iso(t),
        "range_start_time": swot.iso(t - 600),
        "pass_id": passe,
        "wse": "-999999999999.0" if sem else str(wse),
        "wse_u": "0.2",
        "reach_q": "3" if sem else str(q),
        "partial_f": "1" if sem else str(parcial),
    }


def resposta(rid, passagens, up="", dn="", larg=800, centro=(-63.0, -3.5), rio="Amazon River"):
    props = {k: [o[k] for o in passagens] for k in ("time_str", "range_start_time", "pass_id", "wse", "wse_u", "reach_q", "partial_f")}
    n = len(passagens)
    props.update({"reach_id": [rid] * n, "rch_id_up": [up] * n, "rch_id_dn": [dn] * n, "p_width": [str(larg)] * n,
                  "p_lon": [str(centro[0])] * n, "p_lat": [str(centro[1])] * n, "river_name": [rio] * n})
    geom = {"type": "LineString", "coordinates": [[centro[0], centro[1]], [centro[0] + 0.05, centro[1] + 0.01], [centro[0] + 0.1, centro[1]]]}
    return {"type": "FeatureCollection", "features": [{"id": "0", "type": "Feature", "properties": props, "geometry": geom}]}


class HydrocronFalso(swot.Hydrocron):
    """Responde de uma tabela {id: (lista de passagens, up, dn, larg, centro)} filtrando pelo período."""

    def __init__(self, rede):
        super().__init__(None)
        self.rede = rede
        self.urls = []

    def abrir(self, url):
        self.urls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        rid = q["feature_id"]
        ini, fim = swot.epoch(q["start_time"]), swot.epoch(q["end_time"])
        if rid not in self.rede:
            raise urllib.error.HTTPError(url, 400, "not found", {}, io.BytesIO(b"{}"))
        passagens, up, dn, larg, centro = self.rede[rid]
        dentro = [o for o in passagens if ini <= swot.epoch(o["range_start_time"]) <= fim]
        if not dentro:
            raise urllib.error.HTTPError(url, 400, "not found", {}, io.BytesIO(b"{}"))
        return json.dumps(resposta(rid, dentro, up, dn, larg, centro)).encode()


class Leitura(unittest.TestCase):
    def test_resposta_compacta(self):
        fc = resposta("62293900091", [obs(AGORA - 10 * DIA, "145", 22.94), obs(AGORA - 5 * DIA, "298", sem=True)], up="62293900101", dn="62293900081")
        tr = swot.ler_resposta("62293900091", fc)
        self.assertEqual((tr["up"], tr["dn"], tr["larg"], tr["rio"]), (["62293900101"], ["62293900081"], 800.0, "Amazon River"))
        self.assertEqual(tr["centro"], (-63.0, -3.5))
        self.assertEqual([o["pass"] for o in tr["obs"]], ["145", "298"])
        self.assertIsNone(tr["obs"][1]["t"])  # no_data
        self.assertIsNotNone(tr["obs"][1]["rst"])  # mas a órbita passou

    def test_resposta_embrulhada(self):
        fc = {"status": "200 OK", "results": {"csv": "", "geojson": resposta("1", [obs(AGORA, "145")])}}
        self.assertEqual(len(swot.ler_resposta("1", fc)["obs"]), 1)

    def test_filtro_das_passagens(self):
        def o(**k):
            return swot.ler_resposta("1", resposta("1", [obs(AGORA, "145", **k)]))["obs"][0]

        self.assertEqual(swot.passagem_valida(o(wse=12.3456, q=2)), [int(AGORA), 12.346, 0.2, 2])
        self.assertIsNone(swot.passagem_valida(o(q=3)))
        self.assertIsNone(swot.passagem_valida(o(parcial=1)))
        self.assertIsNone(swot.passagem_valida(o(sem=True)))


class Incremental(unittest.TestCase):
    def trecho(self, passagens):
        est = {"rio": "x", "larg": 800, "linhas": [[[0, 0], [1, 1]]], "serie": [], "passes": {}, "validos": {}, "revisto": AGORA}
        swot.mesclar(est, swot.ler_resposta("1", resposta("1", passagens))["obs"])
        return est

    def test_so_pede_quando_a_orbita_ja_passou_e_chegou(self):
        ult = AGORA - 10 * DIA
        est = self.trecho([obs(ult - swot.CICLO_S, "145"), obs(ult, "145")])
        rst = ult - 600
        self.assertIsNone(swot.pedido_do_trecho("1", est, AGORA))  # próxima passagem daqui a ~11 dias
        quando = rst + swot.CICLO_S + swot.LATENCIA_S
        self.assertIsNone(swot.pedido_do_trecho("1", est, quando - 3600))
        motivo, ini = swot.pedido_do_trecho("1", est, quando + 1)
        self.assertEqual(motivo, "passagem")
        self.assertAlmostEqual(ini, rst + swot.CICLO_S - swot.MARGEM_S)

    def test_orbita_sem_cota_valida_nao_dispara(self):
        est = self.trecho([obs(AGORA - 40 * DIA, "423", sem=True), obs(AGORA - 19 * DIA, "423", sem=True)])
        self.assertIn("423", est["passes"])
        self.assertIsNone(swot.pedido_do_trecho("1", est, AGORA + 10 * DIA))

    def test_revisao_escalonada(self):
        est = self.trecho([obs(AGORA - 10 * DIA, "145")])
        est["revisto"] = AGORA - swot.REVISAO_S - 1
        self.assertEqual(swot.pedido_do_trecho("1", est, AGORA)[0], "revisao")
        fases = {round(swot.revisao_inicial(str(62293900001 + 10 * k), AGORA)) for k in range(50)}
        self.assertGreater(len(fases), 40)  # cada trecho num dia diferente do intervalo

    def test_desiste_da_passagem_que_nao_chega(self):
        est = self.trecho([obs(AGORA - 60 * DIA, "145")])
        t = est["passes"]["145"]
        self.assertGreater(swot.desistir(est, AGORA), 0)
        self.assertGreater(est["passes"]["145"], t)
        self.assertGreater(est["passes"]["145"] + swot.CICLO_S + swot.DESISTIR_S, AGORA)

    def test_orbita_que_parou_deixa_de_disparar_e_volta(self):
        # O Hydrocron trouxe a órbita 173 até 170 dias atrás e parou.
        est = self.trecho([obs(AGORA - 170 * DIA, "173")])
        self.assertIsNotNone(swot.pedido_do_trecho("1", est, AGORA))
        swot.desistir(est, AGORA)
        self.assertGreaterEqual(est["faltas"]["173"], swot.FALTAS_MAX)
        self.assertIsNone(swot.pedido_do_trecho("1", est, AGORA + DIA))
        # Uma passagem dela chega (pela revisão): volta a disparar no ciclo seguinte.
        t = est["passes"]["173"] + 600
        swot.mesclar(est, swot.ler_resposta("1", resposta("1", [obs(t, "173")]))["obs"])
        self.assertNotIn("173", est["faltas"])
        self.assertEqual(swot.pedido_do_trecho("1", est, t + swot.CICLO_S + swot.LATENCIA_S + DIA)[0], "passagem")

    def test_uma_falta_so_nao_para_a_orbita(self):
        est = self.trecho([obs(AGORA - 30 * DIA, "145")])
        swot.desistir(est, AGORA + 10 * DIA)  # a de ~9 dias atrás não chegou em 16 dias
        self.assertEqual(est["faltas"]["145"], 1)
        self.assertIsNotNone(swot.pedido_do_trecho("1", est, est["passes"]["145"] + swot.CICLO_S + swot.LATENCIA_S))

    def test_mesclar_e_aparar(self):
        est = self.trecho([obs(AGORA - 400 * DIA, "145"), obs(AGORA - 30 * DIA, "145", 20.0)])
        novas = swot.mesclar(est, swot.ler_resposta("1", resposta("1", [obs(AGORA - 30 * DIA, "145", 21.0), obs(AGORA - 9 * DIA, "145")]))["obs"])
        self.assertEqual(novas, 1)
        self.assertEqual([s[1] for s in est["serie"]][-2:], [21.0, 20.0])  # reprocessada substitui
        swot.aparar(est, AGORA)
        self.assertEqual(len(est["serie"]), 2)  # a de 400 dias saiu


class Rodada(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.antes = (swot.RAIZ, swot.PASTA, swot.ESTADO, swot.SEMENTES, swot.PARALELO)
        swot.RAIZ = self.dir
        swot.PASTA = os.path.join(self.dir, "swot", "v1")
        swot.ESTADO = os.path.join(self.dir, "estado.json")
        swot.PARALELO = 1
        # Rede: 100 → 101 → (102 que o Hydrocron não conhece) → 111 (pela numeração) e um
        # afluente estreito (200) e um trecho fora da Amazônia Legal (103).
        p = lambda *ts: [obs(t, "145") for t in ts]  # noqa: E731
        self.ult = AGORA - 10 * DIA
        self.rede = {
            "62293901001": (p(self.ult - swot.CICLO_S, self.ult), "62293901011", "", 900, (-63.0, -3.5)),
            "62293901011": (p(self.ult), "62293901021", "62293901001", 900, (-62.9, -3.5)),
            "62293901031": (p(self.ult), "62293901041", "", 900, (-62.7, -3.5)),
            "62293901041": (p(self.ult), "", "62293901031", 900, (-20.0, -3.5)),  # no Atlântico
            "62293902001": (p(self.ult), "", "62293901001", 150, (-63.1, -3.4)),  # estreito
        }
        self.rede["62293901001"] = (self.rede["62293901001"][0], "62293901011", "62293902001", 900, (-63.0, -3.5))
        swot.SEMENTES = ["62293901001"]

    def tearDown(self):
        swot.RAIZ, swot.PASTA, swot.ESTADO, swot.SEMENTES, swot.PARALELO = self.antes
        shutil.rmtree(self.dir, ignore_errors=True)

    def saida(self):
        with open(os.path.join(swot.PASTA, "trechos.json")) as f:
            d = json.load(f)
        with gzip.open(os.path.join(swot.PASTA, "trechos.json.gz")) as f:
            self.assertEqual(json.load(f), d)
        return d

    def test_percurso_e_formato(self):
        h = HydrocronFalso(self.rede)
        r = swot.rodada(AGORA, h=h)
        self.assertTrue(r["percurso"])
        d = self.saida()
        for campo in ("gerado", "inicio", "fim", "referencia", "campos", "trechos"):
            self.assertIn(campo, d)
        ids = [t["id"] for t in d["trechos"]]
        # O 1021 não existe: a busca pula para o 1031 pela numeração do SWORD.
        self.assertEqual(ids, ["62293901001", "62293901011", "62293901031"])
        t = d["trechos"][0]
        self.assertEqual(set(t), {"id", "rio", "larg", "linhas", "serie"})
        self.assertEqual(len(t["serie"]), 2)
        self.assertEqual(len(t["serie"][0]), 4)

    def test_dia_seguinte_so_pede_o_que_passou(self):
        swot.rodada(AGORA, h=HydrocronFalso(self.rede))
        # Nada passou ainda: nenhum pedido (fora a revisão escalonada, que não cai hoje).
        with open(swot.ESTADO) as f:
            est = json.load(f)
        for e in est["trechos"].values():
            e["revisto"] = AGORA
        with open(swot.ESTADO, "w") as f:
            json.dump(est, f)
        h = HydrocronFalso(self.rede)
        r = swot.rodada(AGORA + DIA, h=h)
        self.assertFalse(r["percurso"])
        self.assertEqual(h.pedidos, 0)
        # A órbita passa de novo e chega ao Hydrocron: só aí os trechos são pedidos, desde a passagem.
        novo = self.ult + swot.CICLO_S
        for rid in ("62293901001", "62293901011"):
            self.rede[rid] = (self.rede[rid][0] + [obs(novo, "145", 18.5)],) + self.rede[rid][1:]
        quando = novo + swot.LATENCIA_S + DIA
        h = HydrocronFalso(self.rede)
        r = swot.rodada(quando, h=h)
        self.assertEqual(h.pedidos, 3)  # os três trechos aceitos têm a órbita 145
        self.assertEqual(r["novas"], 2)
        for url in h.urls:
            ini = swot.epoch(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["start_time"])
            self.assertGreater(ini, quando - swot.CICLO_S)  # período curto, não os 12 meses
        d = {t["id"]: t for t in self.saida()["trechos"]}
        self.assertEqual(d["62293901001"]["serie"][-1][1], 18.5)
        # No dia seguinte, quem já recebeu não é pedido de novo; o 1031 (sem passagem nova) ainda é.
        h = HydrocronFalso(self.rede)
        swot.rodada(quando + DIA, h=h)
        self.assertEqual(h.pedidos, 1)

    def test_sem_hydrocron_nao_publica_nada(self):
        with self.assertRaises(RuntimeError):
            swot.rodada(AGORA, h=HydrocronFalso({}))
        self.assertFalse(os.path.exists(swot.ESTADO))


if __name__ == "__main__":
    unittest.main()
