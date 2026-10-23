"""
Testes da chuva do GSMaP, sem rede: o arquivo da JAXA é sintético (mesma
grade, mesma ordem de bytes), e o servidor é trocado por dicionários.

  python3 -m unittest discover -s agendador/gsmap
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import gsmap  # noqa: E402


def global_sintetico(valores: dict[tuple[float, float], float] | None = None, fundo: float = 0.0) -> bytes:
    """Grade global 3600 × 1200 float32 LE com `fundo` e os valores pedidos em (lon, lat)."""
    g = np.full((gsmap.NY, gsmap.NX), fundo, dtype="<f4")
    for (lon, lat), v in (valores or {}).items():
        i = int((60 - lat) / 0.1)
        j = int((lon % 360) / 0.1)
        g[i, j] = v
    return g.tobytes()


def ponto(q, lon: float, lat: float):
    """Valor do quadro uint8 no lon/lat (pela grade do índice)."""
    g = gsmap.GRADE
    i = round((g["lat0"] - lat) / g["passo"])
    j = round((lon - g["lon0"]) / g["passo"])
    return q[i, j]


class TesteGrade(unittest.TestCase):
    def test_recorte_cobre_o_setor(self):
        g = gsmap.GRADE
        self.assertEqual((g["ni"], g["nj"]), (442, 594))
        self.assertAlmostEqual(g["lat0"], 22.05)
        self.assertAlmostEqual(g["lon0"], -89.35)
        # Último ponto: 22,05°S e 30,05°W.
        self.assertAlmostEqual(g["lat0"] - (g["ni"] - 1) * g["passo"], -22.05, places=6)
        self.assertAlmostEqual(g["lon0"] + (g["nj"] - 1) * g["passo"], -30.05, places=6)

    def test_ponto_vai_para_o_lugar_certo(self):
        # Manaus (60,02°W; 3,10°S) e um ponto no Caribe.
        bruto = global_sintetico({(-60.02, -3.10): 12.0, (-70.03, 18.04): 3.0})
        q = np.frombuffer(gsmap.converter(bruto)[0], np.uint8).reshape(gsmap.GRADE["ni"], gsmap.GRADE["nj"])
        v = gsmap.decodificar(q)
        self.assertAlmostEqual(float(ponto(v, -60.05, -3.15)), 12.0, delta=0.4)
        self.assertAlmostEqual(float(ponto(v, -70.05, 18.05)), 3.0, delta=0.1)
        self.assertEqual(int((v > 0).sum()), 2)

    def test_tamanho_errado_e_recusado(self):
        with self.assertRaises(ValueError):
            gsmap.converter(b"\0" * 100)


class TesteCodificacao(unittest.TestCase):
    def test_ida_e_volta(self):
        v = np.array([0, 0.05, 0.1, 0.5, 1, 2.5, 10, 40, 160, 500, np.nan, -99], np.float32)
        rec = v.copy()
        rec[rec < 0] = np.nan
        q = gsmap.codificar(rec)
        self.assertEqual(q.dtype, np.uint8)
        d = gsmap.decodificar(q)
        self.assertEqual(d[0], 0)
        self.assertEqual(d[1], 0, "abaixo de 0,1 mm/h é zero")
        self.assertGreaterEqual(gsmap.decodificar(gsmap.codificar(np.array([0.101], np.float32)))[0], 0.1, "0,1 mm/h não vira 0,09")
        for orig, dec in zip(v[2:9], d[2:9]):
            # O passo de √v é 1/20: o erro relativo cai com a chuva (0,1 sobe para 0,12, o menor código com cor).
            self.assertLess(abs(dec - orig), max(0.025, 0.05 * orig), (orig, dec))
        self.assertEqual(q[9], gsmap.LIMITE_Q)
        self.assertTrue(np.isnan(d[10]) and np.isnan(d[11]))

    def test_gzip_e_resumo(self):
        bloco = {(-60.0 + 0.1 * a + 0.03, -3.0 + 0.1 * b + 0.03): 25.0 for a in range(20) for b in range(20)}
        quadro, r = gsmap.converter(gzip.compress(global_sintetico(bloco)))
        self.assertEqual(len(quadro), gsmap.GRADE["ni"] * gsmap.GRADE["nj"])
        self.assertEqual(r["max"], 25.0)
        self.assertAlmostEqual(r["f1"], 400 / len(quadro), places=4)
        self.assertEqual(r["sem"], 0)

    def test_sem_dado_conta_no_resumo(self):
        quadro, r = gsmap.converter(global_sintetico(fundo=-99.0))
        self.assertEqual(r["sem"], 1)
        self.assertEqual(r["max"], 0.0)
        self.assertTrue(all(b == gsmap.SEM_DADO for b in quadro[:100]))


class TesteNomes(unittest.TestCase):
    def test_nomes(self):
        self.assertEqual(gsmap.ler_nome("gsmap_gauge_now.20261023.1630_1729.dat.gz"), ("202610231630", "g"))
        self.assertEqual(gsmap.ler_nome("gsmap_now.20261023.1630_1729.dat.gz"), ("202610231630", "s"))
        self.assertEqual(gsmap.ler_nome("gsmap_gauge_now.20261023.1630.dat.gz"), ("202610231630", "g"))
        self.assertEqual(gsmap.ler_nome("gsmap_now.20261023.2330_0029.dat"), ("202610232330", "s"))
        self.assertIsNone(gsmap.ler_nome("gsmap_now.20261023.1630_1659.dat.gz"), "meia hora não é o produto horário")
        self.assertIsNone(gsmap.ler_nome("gsmap_now.20261023.1630.nc"))
        self.assertIsNone(gsmap.ler_nome("GSMaP_NOW.hourly.rain.ctl"))


class TesteRodada(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(gsmap, "RAIZ", self.dir),
            mock.patch.object(gsmap, "PASTA", os.path.join(self.dir, "gsmap", "v1")),
            mock.patch.object(gsmap, "QUADROS", os.path.join(self.dir, "gsmap", "v1", "quadros")),
            mock.patch.object(gsmap, "ESTADO", os.path.join(self.dir, "estado.json")),
        ]
        for p in self.patches:
            p.start()
        self.agora = datetime(2026, 10, 23, 17, 7, tzinfo=timezone.utc)
        self.servidor: dict[str, dict[str, bytes]] = {"/now/latest/": {}}
        self.baixados: list[str] = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)

    def publicar(self, pasta: str, nome: str, chuva: float = 5.0):
        self.servidor.setdefault(pasta, {})[nome] = gzip.compress(global_sintetico({(-60.0, -3.0): chuva}))

    def curl(self, caminho, listar=False, tempo=120):
        if listar:
            if caminho not in self.servidor:
                raise RuntimeError(f"curl 9 em {caminho}: não existe")
            return "\n".join(self.servidor[caminho]).encode()
        pasta, nome = caminho.rsplit("/", 1)
        self.baixados.append(caminho)
        return self.servidor[pasta + "/"][nome]

    def rodar(self):
        with mock.patch.object(gsmap, "curl", self.curl):
            return gsmap.rodada(self.agora)

    def indice(self):
        with open(os.path.join(gsmap.PASTA, "indice.json")) as f:
            return json.load(f)

    def test_ajustado_e_satelite(self):
        self.publicar("/now/latest/", "gsmap_gauge_now.20261023.1630_1729.dat.gz", 8.0)  # link para o de baixo
        self.publicar("/now/latest/", "gsmap_gauge_now.20261023.1630.dat.gz", 8.0)
        self.publicar("/now/latest/", "gsmap_now.20261023.1630_1729.dat.gz", 9.0)
        self.publicar("/now/latest/", "gsmap_now.20261023.1600_1659.dat.gz", 4.0)
        r = self.rodar()
        self.assertEqual(r["feitos"], 2)
        self.assertEqual(len(self.baixados), 2, "nem o link nem o de satélite quando há o ajustado")
        self.assertIn("/now/latest/gsmap_gauge_now.20261023.1630.dat.gz", self.baixados, "o arquivo de verdade, não o link")
        idx = self.indice()
        self.assertEqual([(q["c"], q["p"]) for q in idx["quadros"]], [("202610231600", "s"), ("202610231630", "g")])
        self.assertEqual(idx["quadros"][1]["max"], 8.0)
        self.assertEqual(idx["grade"]["nj"], 594)
        self.assertTrue(os.path.exists(os.path.join(gsmap.QUADROS, "202610231630-g.bin.gz")))

        # Chega o ajustado das 16:00: substitui o de satélite.
        self.publicar("/now/latest/", "gsmap_gauge_now.20261023.1600_1659.dat.gz", 3.0)
        self.baixados.clear()
        self.rodar()
        self.assertEqual(len(self.baixados), 1)
        idx = self.indice()
        self.assertEqual([(q["c"], q["p"]) for q in idx["quadros"]], [("202610231600", "g"), ("202610231630", "g")])
        # O de satélite fica 10 min no disco: o índice antigo, no navegador, ainda aponta para ele.
        self.assertTrue(os.path.exists(os.path.join(gsmap.QUADROS, "202610231600-s.bin")))
        g = os.path.join(gsmap.QUADROS, "202610231600-g.bin")
        os.utime(g, (os.path.getmtime(g) - 700,) * 2)

        # Nada novo: nada baixado, e o de satélite substituído sai.
        self.baixados.clear()
        self.assertEqual(self.rodar()["feitos"], 0)
        self.assertEqual(self.baixados, [])
        self.assertFalse(os.path.exists(os.path.join(gsmap.QUADROS, "202610231600-s.bin")))
        self.assertFalse(os.path.exists(os.path.join(gsmap.QUADROS, "202610231600-s.bin.gz")))

    def test_arquivo_do_dia_e_limpeza(self):
        # Mais de 24 h: só no arquivo do dia.
        self.publicar("/now/half_hour_G/2026/10/22/", "gsmap_gauge_now.20261022.0100.dat.gz")
        # Fora das 48 h: não entra.
        self.publicar("/now/half_hour_G/2026/10/21/", "gsmap_gauge_now.20261021.1500.dat.gz")
        self.rodar()
        self.assertEqual([q["c"] for q in self.indice()["quadros"]], ["202610220100"])
        # O dia já listado não é listado de novo antes de 30 min.
        with mock.patch.object(gsmap, "listar", wraps=gsmap.listar) as lst, mock.patch.object(gsmap, "curl", self.curl):
            gsmap.rodada(self.agora)
            pastas = [c.args[0] for c in lst.call_args_list]
        self.assertNotIn("/now/half_hour_G/2026/10/22/", pastas)
        # Dois dias depois, sai da janela e do disco.
        self.agora = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
        r = self.rodar()
        self.assertEqual(r["removidos"], 2)
        self.assertEqual(self.indice()["quadros"], [])

    def test_o_que_passou_do_limite_fica_para_a_proxima(self):
        # Um dia inteiro no arquivo, mais que o limite da rodada: o resto vem na
        # rodada seguinte, sem esperar 30 min para listar o dia de novo.
        for h in range(18, 24):
            for m in ("00", "30"):
                self.publicar("/now/half_hour_G/2026/10/22/", f"gsmap_gauge_now.20261022.{h:02d}{m}.dat.gz")
        with mock.patch.object(gsmap, "MAX_POR_RODADA", 5):
            self.assertEqual(self.rodar()["feitos"], 5)
            self.agora = self.agora.replace(minute=12)
            self.assertEqual(self.rodar()["feitos"], 5)
            self.assertEqual(self.rodar()["feitos"], 2)
        self.assertEqual(len(self.indice()["quadros"]), 12)

    def test_pendente_do_latest_que_saiu_dele(self):
        # Uma hora ficou pendente com o caminho do latest/; um dia depois ela só
        # existe no arquivo do dia — o caminho morto não pode tapar o bom.
        self.publicar("/now/latest/", "gsmap_gauge_now.20261023.0100.dat.gz")
        with mock.patch.object(gsmap, "MAX_POR_RODADA", 0):
            self.rodar()
        del self.servidor["/now/latest/"]["gsmap_gauge_now.20261023.0100.dat.gz"]
        self.publicar("/now/half_hour_G/2026/10/23/", "gsmap_gauge_now.20261023.0100.dat.gz")
        self.agora = datetime(2026, 10, 24, 2, 7, tzinfo=timezone.utc)
        self.rodar()
        self.assertIn("/now/half_hour_G/2026/10/23/gsmap_gauge_now.20261023.0100.dat.gz", self.baixados)
        self.assertIn("202610230100", [q["c"] for q in self.indice()["quadros"]])

    def test_arquivo_quebrado_espera(self):
        self.servidor["/now/latest/"]["gsmap_gauge_now.20261023.1630_1729.dat.gz"] = b"quebrado"
        r = self.rodar()
        self.assertEqual(r["falhas"], 1)
        self.baixados.clear()
        self.rodar()
        self.assertEqual(self.baixados, [], "o arquivo que falhou espera 30 min")

    def test_servidor_fora(self):
        del self.servidor["/now/latest/"]
        r = self.rodar()
        self.assertTrue(r["erros"])
        self.assertEqual(r["quadros"], 0)
        self.assertEqual(self.indice()["quadros"], [])


if __name__ == "__main__":
    unittest.main()
