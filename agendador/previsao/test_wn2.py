"""Testes do coletor do WeatherNext 2 (sem rede). Rodam com o Python do venv (numcodecs)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import numpy as np
    from numcodecs import Blosc
except ImportError:  # o Python do sistema não tem o numcodecs: os testes ficam para o venv
    Blosc = None

import previsao as P  # noqa: E402

if Blosc is not None:
    import wn2  # noqa: E402


@unittest.skipIf(Blosc is None, "sem numcodecs (rode com /opt/amaview/venv-wn2/bin/python)")
class TestWn2(unittest.TestCase):
    def test_recorte_na_grade_do_ifs(self):
        i0, i1, j0, j1 = wn2.linhas_colunas()
        # y = −90 + 0,25·i: a linha de baixo é −19,75° e a de cima 8°, como a última e a primeira do IFS.
        self.assertEqual(-90 + 0.25 * i0, P.LAT0 - 0.25 * (P.dimensoes("ifs")[0] - 1))
        self.assertEqual(-90 + 0.25 * (i1 - 1), P.LAT0)
        self.assertEqual(0.25 * j0 - 360, P.LON0)
        self.assertEqual((i1 - i0, j1 - j0), P.dimensoes("ifs")[:2])

    def parcial(self, quadro: bytes, escala: float, offset: float):
        """O caminho do coletor: cabeçalho, só os intervalos dos blocos do recorte, quadro remontado."""
        i0, i1, j0, j1 = wn2.linhas_colunas()
        fixo, b0, trechos = wn2.blocos_do_recorte(quadro[:80], i0, i1)
        baixados = {(a, b): quadro[a:b] for a, b in wn2.pedidos(trechos)}
        self.assertLess(sum(b - a for a, b in baixados), 0.6 * len(quadro))  # só uma parte do pedaço desce
        bs = int.from_bytes(fixo[8:12], "little")
        return wn2.recorte_do_quadro(wn2.montar_quadro(fixo, trechos, baixados), b0, bs, i0, i1, j0, j1, escala, offset)

    def test_blosc_parcial_igual_ao_inteiro(self):
        rng = np.random.default_rng(1)
        campo = (rng.normal(size=(wn2.NY, wn2.NX)) * 5 + np.linspace(-30, 30, wn2.NY)[:, None]).astype("<f4")
        i0, i1, j0, j1 = wn2.linhas_colunas()
        # blocksize 0 = 512 KiB, como os pedaços do dynamical.org; o outro dá blocos de 1 MiB.
        for blocksize in (0, 524288):
            quadro = Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE, blocksize=blocksize).encode(campo)
            rec = self.parcial(quadro, 1.0, -273.15)
            np.testing.assert_allclose(rec, campo[i0:i1, j0:j1][::-1] - 273.15, atol=1e-4)
            # Linha 0 = norte.
            np.testing.assert_allclose(rec[0], campo[i1 - 1, j0:j1] - 273.15, atol=1e-4)

    def test_blocos_fora_de_ordem(self):
        # O blosc grava os blocos na ordem em que as threads terminam: o fim de um bloco é o próximo INÍCIO no arquivo.
        cab = bytearray(16 + 4 * 8)
        nbytes, bs = wn2.NY * wn2.NX * 4, 524288
        cab[:16] = bytes([2, 1, 0x21, 4]) + nbytes.to_bytes(4, "little") + bs.to_bytes(4, "little") + (1000).to_bytes(4, "little")
        inicios = [48, 900, 100, 700, 300, 500, 200, 800]
        for k, x in enumerate(inicios):
            cab[16 + 4 * k : 20 + 4 * k] = x.to_bytes(4, "little")
        i0, i1, _, _ = wn2.linhas_colunas()
        _, b0, trechos = wn2.blocos_do_recorte(bytes(cab), i0, i1)
        self.assertEqual(b0, 3)
        self.assertEqual(trechos, [(700, 800), (300, 500)])
        self.assertEqual(wn2.pedidos(trechos), [(300, 500), (700, 800)])
        self.assertEqual(wn2.pedidos([(300, 500), (500, 600)]), [(300, 600)])

    def test_cabecalho_grande_demais_e_ultimo_bloco_recusados(self):
        i0, i1, _, _ = wn2.linhas_colunas()
        nbytes = wn2.NY * wn2.NX * 4
        # Blocos de 32 KiB: 127 inícios não cabem nos 80 bytes lidos → erro claro, não leitura errada.
        cab = bytes([2, 1, 0x21, 4]) + nbytes.to_bytes(4, "little") + (32768).to_bytes(4, "little") + (10**6).to_bytes(4, "little")
        with self.assertRaisesRegex(ValueError, "não coube"):
            wn2.blocos_do_recorte(cab + bytes(64), i0, i1)
        # Um recorte que chegasse ao último bloco (parcial, comprimido diferente): recusado, não lido errado.
        quadro = Blosc(cname="lz4", shuffle=Blosc.SHUFFLE).encode(np.zeros((wn2.NY, wn2.NX), "<f4"))
        with self.assertRaisesRegex(ValueError, "último bloco"):
            wn2.blocos_do_recorte(quadro[:80], wn2.NY - 3, wn2.NY)

    def test_escala_da_chuva(self):
        # scale_offset do Zarr: guardado / escala + offset (a chuva vem em m × 1000 → mm).
        campo = np.full((wn2.NY, wn2.NX), 0.0025, "<f4")
        rec = self.parcial(Blosc(cname="lz4", shuffle=Blosc.SHUFFLE).encode(campo), 0.001, 0.0)
        np.testing.assert_allclose(rec, 2.5, rtol=1e-5)

    def test_produtos(self):
        m = wn2.MEMBROS
        t = np.broadcast_to(np.arange(m, dtype=np.float32)[:, None, None], (m, 2, 3)).copy()
        tp = np.zeros((m, 2, 3), np.float32)
        tp[:16] = 1.0  # 1/4 dos cenários com 1 mm
        tp[:8] = 12.0  # 1/8 com chuva forte
        tp[0, 1, 2] = -0.001  # negativo pequeno do modelo
        tp[5, 0, 0] = np.nan  # um cenário faltando: o ponto fica sem dado
        p = wn2.produtos_do_passo(t, tp, None)
        self.assertAlmostEqual(float(p["chance"][0, 1]), 25.0)
        self.assertAlmostEqual(float(p["forte"][0, 1]), 12.5)
        self.assertTrue(np.isnan(p["chance"][0, 0]))
        self.assertAlmostEqual(float(p["chance"][1, 2]), 15 / 64 * 100, places=4)
        self.assertTrue(np.isnan(p["chance24"]).all())
        self.assertAlmostEqual(float(p["t10"][0, 0]), float(np.percentile(np.arange(m), 10)), places=4)
        self.assertAlmostEqual(float(p["t90"][0, 0]), float(np.percentile(np.arange(m), 90)), places=4)
        p24 = wn2.produtos_do_passo(t, tp, np.where(np.isnan(tp), 0, tp) * 4)
        self.assertAlmostEqual(float(p24["chance24"][0, 1]), 25.0)
        # Quantizado sem perda: k cenários = k na grade de 100/64.
        _, off, esc, _ = P.VARS["wn2"]["chance"]
        self.assertEqual(int(P.quantizar(p["chance"][0, 1], off, esc)), 16)

    def test_rodadas_alvo(self):
        agora = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        base = agora.replace(hour=0) - timedelta(days=1)
        inits = [base + timedelta(hours=6 * k) for k in range(8)]
        alvo = wn2.rodadas_alvo(inits, set(), {}, None)
        self.assertTrue(all(t.hour in (0, 12) for _, t in alvo))
        self.assertEqual([t for _, t in alvo], sorted((t for t in inits if t.hour in (0, 12)), reverse=True))
        feita = P.nome_rodada(alvo[0][1])
        self.assertEqual(wn2.rodadas_alvo(inits, {feita}, {}, None), [])
        # Falhou há pouco: espera antes de tentar de novo, e vai para a anterior.
        depois = wn2.rodadas_alvo(inits, set(), {feita: datetime.now().timestamp()}, None)
        self.assertNotIn(alvo[0][1], [t for _, t in depois])

    def test_indice_e_termos(self):
        with tempfile.TemporaryDirectory() as d:
            antes = wn2.PASTA
            wn2.PASTA = d
            try:
                os.makedirs(os.path.join(d, "2026102112"))
                i = wn2.montar_indice({"faixas": {"2026102112": {"chance": [0, 100]}}}, datetime(2026, 10, 21, 23, tzinfo=timezone.utc))
            finally:
                wn2.PASTA = antes
        m = i["modelos"]["wn2"]
        self.assertEqual(m["rodadas"], ["2026102112"])
        self.assertEqual(m["passos"][:3], [0, 6, 12])
        self.assertEqual(m["grade"]["ni"], 112)
        self.assertIn("terms-of-use.pdf", i["termos"]["url"])
        self.assertTrue(i["termos"]["copyright"].startswith("Copyright"))


if __name__ == "__main__":
    unittest.main()
