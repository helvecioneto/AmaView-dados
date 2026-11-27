"""
Testes do vento, sem rede: o servidor do ECMWF é um dicionário e a
libeccodes é trocada por um leitor que devolve campos sintéticos.

  python3 -I -m unittest discover -s agendador/vento
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import vento  # noqa: E402

UTC = timezone.utc
AGORA_REAL = time.time
LATS = 90.0 - 0.25 * np.arange(721)
LONS = -180.0 + 0.25 * np.arange(1440)


def t(s: str) -> datetime:
    return vento.instante(s)


class Servidor:
    """data.ecmwf.int de mentira: rodadas publicadas → índice e campos (u = passo + nível, v = −u)."""

    def __init__(self, rodadas: list[str]):
        self.rodadas = set(rodadas)
        self.pedidos: list[str] = []

    def campos(self):
        niveis = [("pl", lv, c) for lv in ("850", "700", "500", "250") for c in ("u", "v")]
        # O 200 hPa e o gh estão no índice de verdade, mas não são pedidos.
        return [("sfc", None, "10u"), ("sfc", None, "10v"), *niveis, ("sfc", None, "sp"), ("pl", "200", "u"), ("pl", "850", "gh")]

    def baixar(self, url, cabecalhos=None, dados=None, tentativas=3, metodo=None):
        self.pedidos.append(url)
        m = url.split("/")[-1]
        rodada, passo = m[:10], int(m.split("-")[1][:-1])
        if rodada not in self.rodadas:
            raise urllib.error.HTTPError(url, 404, "não", {}, None)
        if url.endswith(".index"):
            linhas = []
            for i, (lt, lv, p) in enumerate(self.campos()):
                e = {"levtype": lt, "param": p, "_offset": i * 100, "_length": 100}
                if lv:
                    e["levelist"] = lv
                linhas.append(json.dumps(e))
            return "\n".join(linhas).encode()
        ini = int(cabecalhos["Range"].split("=")[1].split("-")[0]) // 100
        lt, lv, p = self.campos()[ini]
        return json.dumps({"passo": passo, "nivel": lv or "10m", "comp": p[-1]}).encode()


# Uma faixa de "Andes" (70°W–65°W) com o chão a 650 hPa: 850 e 700 hPa ficam abaixo dele.
ANDES = (LONS >= -70) & (LONS <= -65)


class Leitor:
    def ler(self, msg):
        d = json.loads(msg)
        if d["comp"] == "p":
            val = np.full((721, 1440), 100_000.0)
            val[:, ANDES] = 65_000.0
            return {"valores": val, "lats": LATS, "lons": LONS}
        base = d["passo"] + {"10m": 1.0, "850": 5.0, "700": 8.0, "500": 10.0, "250": 40.0}[d["nivel"]]
        val = np.full((721, 1440), base if d["comp"] == "u" else -base, np.float64)
        return {"valores": val, "lats": LATS, "lons": LONS}


class Caso(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for nome, valor in (("RAIZ", self.dir), ("PASTA", os.path.join(self.dir, "vento", "v2")), ("ESTADO", os.path.join(self.dir, "estado-v2.json"))):
            p = mock.patch.object(vento, nome, valor)
            p.start()
            self.addCleanup(p.stop)

    def rodar(self, servidor: Servidor, agora: datetime):
        with mock.patch.object(vento.P, "baixar", servidor.baixar):
            return vento.executar(agora, ec=Leitor())

    def indice(self):
        with open(os.path.join(vento.PASTA, "indice.json")) as f:
            return json.load(f)


class TestTempo(Caso):
    def test_candidato_menor_passo(self):
        ultima = t("2026112700")
        self.assertEqual(vento.candidato(t("2026112603"), ultima), (t("2026112600"), 3))
        self.assertEqual(vento.candidato(t("2026112612"), ultima), (t("2026112612"), 0))
        # Depois da rodada mais nova: a previsão dela, até +18 h.
        self.assertEqual(vento.candidato(t("2026112712"), ultima), (ultima, 12))
        self.assertEqual(vento.candidato(t("2026112718"), ultima), (ultima, 18))
        self.assertIsNone(vento.candidato(t("2026112721"), ultima))

    def test_horarios(self):
        hs = vento.horarios_desejados(datetime(2026, 11, 27, 12, 40, tzinfo=UTC), t("2026112700"))
        self.assertEqual(hs[-1], t("2026112718"))
        self.assertEqual(hs[0], t("2026112418"))
        self.assertTrue(all((b - a) == timedelta(hours=3) for a, b in zip(hs, hs[1:])))

    def test_quantizar(self):
        q = vento.quantizar(np.array([0.0, 1.0, -1.0, 99.0, np.nan]), 0.2)
        self.assertEqual(list(q), [127, 132, 122, 254, 255])


class TestExecucao(Caso):
    def test_publica_e_melhora_com_a_rodada_nova(self):
        rodadas = [vento.nome(t("2026112500") + timedelta(hours=6 * k)) for k in range(9)]  # 25/11 00 … 27/11 00
        srv = Servidor(rodadas)
        agora = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
        self.rodar(srv, agora)
        ind = self.indice()
        self.assertEqual(ind["grade"]["ni"], 161)
        self.assertEqual(ind["grade"]["nj"], 221)
        por_t = {h["t"]: h for h in ind["horarios"]}
        self.assertEqual(por_t["2026112612"], {"t": "2026112612", "r": "2026112612", "p": 0})
        self.assertEqual(por_t["2026112703"]["p"], 3)
        self.assertEqual(por_t["2026112718"], {"t": "2026112718", "r": "2026112700", "p": 18})
        # O arquivo: u e v, valor = (q − 127) × escala.
        with open(vento.arquivo("250", "2026112715", "2026112700"), "rb") as f:
            b = np.frombuffer(f.read(), np.uint8)
        self.assertEqual(b.size, 2 * 161 * 221)
        u, v = (int(b[0]) - 127) * 0.8, (int(b[-1]) - 127) * 0.8
        self.assertAlmostEqual(u, 15 + 40, delta=0.4)
        self.assertAlmostEqual(v, -(15 + 40), delta=0.4)
        self.assertEqual(sorted(ind["niveis"]), ["10m", "250", "500", "700", "850"])

        # Abaixo do chão (a faixa de 650 hPa, coluna de 67°W): 850 e 700 sem dado; 500 e a superfície, com.
        j = round((-67 - vento.LON0) / vento.PASSO)

        def no_andes(nv):
            with open(vento.arquivo(nv, "2026112715", "2026112700"), "rb") as f:
                return int(np.frombuffer(f.read(), np.uint8)[j])

        self.assertEqual(no_andes("850"), 255)
        self.assertEqual(no_andes("700"), 255)
        self.assertNotEqual(no_andes("500"), 255)
        self.assertNotEqual(no_andes("10m"), 255)
        with open(vento.arquivo("850", "2026112715", "2026112700"), "rb") as f:
            self.assertAlmostEqual((int(np.frombuffer(f.read(), np.uint8)[0]) - 127) * 0.3, 15 + 5, delta=0.2)

        # Nada novo: nenhum passo baixado de novo.
        srv.pedidos.clear()
        self.rodar(srv, agora + timedelta(minutes=20))
        self.assertFalse([p for p in srv.pedidos if p.endswith(".grib2")])

        # Sai a de 06 UTC: os horários dela passam a vir dela, e o arquivo velho fica uma hora.
        srv.rodadas.add("2026112706")
        agora2 = datetime(2026, 11, 27, 14, 0, tzinfo=UTC)
        # (A de 06 UTC deu 404 às 9 h: só se pergunta de novo depois da espera.)
        with mock.patch.object(vento.time, "time", lambda: AGORA_REAL() + vento.ESPERA_404_S + 1):
            self.rodar(srv, agora2)
        por_t = {h["t"]: h for h in self.indice()["horarios"]}
        self.assertEqual(por_t["2026112706"], {"t": "2026112706", "r": "2026112706", "p": 0})
        self.assertEqual(por_t["2026112800"], {"t": "2026112800", "r": "2026112706", "p": 18})
        velho = vento.arquivo("10m", "2026112706", "2026112700")
        self.assertTrue(os.path.exists(velho))
        with mock.patch.object(vento.time, "time", lambda: AGORA_REAL() + 2 * 3600):
            self.rodar(srv, agora2 + timedelta(minutes=20))
        self.assertFalse(os.path.exists(velho))
        self.assertTrue(os.path.exists(vento.arquivo("10m", "2026112706", "2026112706")))

    def test_rodada_que_saiu_do_servidor_nao_trava(self):
        # Só as duas mais novas no servidor: as mais velhas dão 404 e ficam de fora, sem erro.
        srv = Servidor(["2026112618", "2026112700"])
        self.rodar(srv, datetime(2026, 11, 27, 9, 0, tzinfo=UTC))
        ts = [h["t"] for h in self.indice()["horarios"]]
        self.assertEqual(ts[0], "2026112618")
        self.assertIsNone(self.indice()["erro"])

    def test_campo_faltando_publica_o_erro(self):
        srv = Servidor(["2026112700"])
        srv.campos = lambda: [("sfc", None, "10u"), ("sfc", None, "10v")]
        with self.assertRaises(RuntimeError):
            self.rodar(srv, datetime(2026, 11, 27, 9, 0, tzinfo=UTC))
        self.assertIn("sem", self.indice()["erro"])
        self.assertEqual(self.indice()["horarios"], [])


if __name__ == "__main__":
    unittest.main()
