"""
Testes do nível de 0,5 km (meio_km.py), sem rede.

  python3 -m unittest discover -s agendador/blocos
  (no Mac: BLOCOS_LIBTURBOJPEG=/opt/homebrew/opt/jpeg-turbo/lib/libturbojpeg.dylib)
"""
import os
import sys
import unittest
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import blocos  # noqa: E402
import meio_km as m  # noqa: E402
import tj  # noqa: E402


class Geometria(unittest.TestCase):
    def test_origem_do_setor_na_grade_de_meio_km(self):
        # add_offset do x e do y no CMIPF C02 (centro do 1º pixel).
        self.assertEqual(m.origem_na_grade(-0.151865, 0.151865), (7950, 6528))
        # O dobro da origem na grade de 1 km do georef.json (3975, 3264).
        self.assertEqual(m.origem_na_grade(-0.151858, 0.151858, 28e-6), (3975, 3264))

    def test_grade_fora_do_lugar_recusa(self):
        with self.assertRaises(ValueError):
            m.origem_na_grade(-0.151865 + 7e-6, 0.151865)

    def test_mesma_grade_de_blocos_do_star(self):
        self.assertEqual(m.grade(), (17, 29))
        self.assertEqual((m.ALTURA, m.LARGURA), (8640, 14400))
        for lc in [(0, 0), (3, 7), (16, 28), (16, 0), (0, 28)]:
            self.assertEqual(m.regiao(*lc), blocos.regiao(14400, *lc))

    def test_carimbo_do_arquivo(self):
        nome = "ABI-L2-CMIPF/2026/275/14/OR_ABI-L2-CMIPF-M6C02_G19_s20262751400208_e20262751409516_c20262751409575.nc"
        self.assertEqual(m.carimbo_do_arquivo(nome), "20262751400")
        self.assertIsNone(m.carimbo_do_arquivo(nome.replace("C02", "C13")))


class Sol(unittest.TestCase):
    def test_noite_e_dia_no_setor(self):
        self.assertGreater(m.zenite_minimo(datetime(2026, 10, 2, 3, 5, tzinfo=timezone.utc)), m.ZENITE_NOITE)
        self.assertLess(m.zenite_minimo(datetime(2026, 10, 2, 15, 5, tzinfo=timezone.utc)), 10)


class Tabela(unittest.TestCase):
    def test_isotonica(self):
        y = m._pav(np.array([1.0, 3.0, 2.0, 4.0]), np.ones(4))
        self.assertTrue(np.all(np.diff(y) >= 0))
        np.testing.assert_allclose(y, [1, 2.5, 2.5, 4])

    def test_ajuste_reproduz_a_curva_do_star(self):
        rng = np.random.default_rng(1)
        red = rng.uniform(0, 3800, (400, 600)).astype(np.float32)
        curva = lambda v: np.minimum(246, 255 * np.sqrt(v * 0.00031746))  # noqa: E731
        star = np.round(curva(red) + rng.normal(0, 2, red.shape)).clip(0, 249).astype(np.uint8)
        lut = m.ajustar_lut([(red, star)])
        self.assertTrue(np.all(np.diff(lut.astype(int)) >= 0))
        alvo = curva(np.arange(4096))
        self.assertLess(np.abs(lut[200:3800] - alvo[200:3800]).mean(), 1.0)


class Mascara(unittest.TestCase):
    def test_linhas_de_noite(self):
        noite = np.zeros((4320, 7200), np.uint8)
        noite[2000, 3000:3100] = 230  # fronteira
        noite[2100, 3000] = 30  # ruído do JPEG em volta: não é linha
        noite[-10, 50] = 255  # rodapé: tratado à parte
        mk, cinza = m.linhas_de_noite(noite)
        self.assertTrue(mk[2000, 3050])
        self.assertFalse(mk[2100, 3000] or mk[-10, 50])
        self.assertEqual(cinza[2000, 3050], 230)

    def test_linhas_rodape_logo_e_fora_do_disco(self):
        prev = np.full((4320, 7200), 100, np.float32)
        linhas = np.zeros((4320, 7200), bool)
        linhas[2000, 3000:3100] = True
        prev[:, 7000:] = np.nan  # fora do disco
        mk = m.mascara_do_star(prev, linhas)
        self.assertTrue(mk[2000, 3050])
        self.assertFalse(mk[1000, 1000] or mk[2001, 3050])
        self.assertTrue(mk[-1, 4000] and mk[4320 - 44, 4000] and not mk[4320 - 45, 4000])
        self.assertTrue(mk[4000, 100])  # logotipo
        self.assertTrue(mk[100, 7100])

    def test_compor_dado_a_meio_km_e_desenho_do_star(self):
        lut = np.clip(np.arange(4096) // 16, 0, 255).astype(np.uint8)
        cmi = np.full((m.ALTURA, 13746), 1600, np.int16)  # cinza 100
        cmi[1000, 1001] = 3200  # um pixel de 0,5 km mais claro: tem de sobreviver
        star = np.full((4320, 7200), 99, np.uint8)
        star[-44:] = 255
        linhas = np.zeros((4320, 7200), bool)
        linhas[300, 400] = True
        cinza = np.zeros((4320, 7200), np.uint8)
        cinza[300, 400] = 230
        out = m.compor(cmi, star, lut, (linhas, cinza))
        self.assertEqual(out.shape, (8640, 14400))
        self.assertEqual(out[1000, 1001], 200)
        self.assertEqual(out[1000, 1000], 100)
        self.assertTrue(np.all(out[600:602, 800:802] == 230))  # linha 2×2
        self.assertTrue(np.all(out[-88:, 5000] == 255))  # rodapé do STAR
        self.assertEqual(out[100, 14000], 99)  # fora do disco: o STAR


class Remoto(unittest.TestCase):
    def test_download_em_partes(self):
        dado = bytes(range(256)) * 20001
        pedidos = []

        def falso(url, inicio=None, fim=None, tentativas=3):
            pedidos.append((inicio, fim))
            return dado[inicio:fim]

        orig = m._pedir
        m._pedir = falso
        try:
            self.assertEqual(bytes(m.baixar_paralelo("x", len(dado), 7)), dado)
            self.assertEqual(len(pedidos), 7)
        finally:
            m._pedir = orig


class Reducao(unittest.TestCase):
    def test_media_2x2_e_fora_do_disco(self):
        cmi = np.full((m.ALTURA, 13746), 100, np.int16)
        cmi[0, 0] = 104
        cmi[2, 2] = -1  # sem dado: o pixel de 7200 inteiro fica NaN
        r = m.reduzir(cmi)
        self.assertEqual(r.shape, (4320, 7200))
        self.assertEqual(r[0, 0], 101)
        self.assertTrue(np.isnan(r[1, 1]))
        self.assertTrue(np.isnan(r[0, 6873]))  # além da borda do disco
        self.assertEqual(r[5, 6872], 100)


class Saude(unittest.TestCase):
    def test_anuncia_o_nivel_so_com_a_rodada_em_dia(self):
        import json
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as d:
            caminho = os.path.join(d, "meio-km.json")
            orig = blocos.ESTADO_MEIO_KM
            blocos.ESTADO_MEIO_KM = caminho
            try:
                self.assertEqual(blocos.nivel_meio_km(time.time()), {"niveis": {}})
                with open(caminho, "w") as f:
                    json.dump({"quadros": 3, "ultimo": "20262751400"}, f)
                s = blocos.nivel_meio_km(time.time())
                self.assertEqual(s["niveis"], {"02": [3600, 7200, 14400]})
                self.assertEqual(s["meio_km"]["ultimo"], "20262751400")
                self.assertEqual(blocos.nivel_meio_km(time.time() + 3600)["niveis"], {})
            finally:
                blocos.ESTADO_MEIO_KM = orig


class Jpeg(unittest.TestCase):
    def test_bloco_cinza_progressivo(self):
        img = (np.arange(544 * 544).reshape(544, 544) % 251).astype(np.uint8)
        jpeg = tj.codificar_cinza(img, 92, True)
        self.assertIn(b"\xff\xc2", jpeg)  # SOF2: progressivo
        volta = tj.decodificar_cinza(jpeg)
        self.assertEqual(volta.shape, (544, 544))
        self.assertLess(np.abs(volta.astype(int) - img).mean(), 3)


if __name__ == "__main__":
    unittest.main()
