"""
Testes dos raios, sem rede: a leitura do GLM usa um netCDF pequeno gerado aqui
(as variáveis e atributos do LCFA medidos em 29/10/2026), e a consulta usa
flashes conhecidos.

  python3 -m unittest discover -s agendador/raios
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import raios  # noqa: E402

T = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def lcfa(caminho: str, lat, lon, seg) -> None:
    """Um LCFA de teste: o offset do primeiro evento em int16 com escala, como o da NOAA."""
    import h5py

    escala, desvio = 0.00038148, -5.0
    with h5py.File(caminho, "w") as f:
        f.attrs["time_coverage_start"] = np.bytes_(b"2026-10-29T12:29:40.0Z")
        f["flash_lat"] = np.asarray(lat, np.float32)
        f["flash_lon"] = np.asarray(lon, np.float32)
        v = f.create_dataset("flash_time_offset_of_first_event", data=np.round((np.asarray(seg) - desvio) / escala).astype(np.int16))
        v.attrs["scale_factor"] = np.array([escala], np.float32)
        v.attrs["add_offset"] = np.array([desvio], np.float32)


class TestLeitura(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.pasta)

    def test_so_o_setor_e_o_instante(self):
        nc = os.path.join(self.pasta, "a.nc")
        lcfa(nc, [-6.6, 30.0, -10.0], [-52.0, -52.0, -100.0], [3.0, 1.0, 2.0])
        inicio, v = raios.ler_flashes(nc)
        self.assertEqual(inicio, datetime(2026, 10, 29, 12, 29, 40, tzinfo=timezone.utc))
        self.assertEqual(len(v), 1)  # só o que cai na caixa
        self.assertAlmostEqual(v[0, 0], -52.0, places=3)
        self.assertAlmostEqual(v[0, 2], 3.0, places=2)

    def test_nomes_e_quadros(self):
        nome = "GLM-L2-LCFA/2026/302/12/OR_GLM-L2-LCFA_G19_s20263021229400_e20263021230000_c20263021230020.nc"
        ini = raios.inicio_do_arquivo(nome)
        self.assertEqual(ini, datetime(2026, 10, 29, 12, 29, 40, tzinfo=timezone.utc))
        self.assertEqual(raios.carimbo_de(raios.quadro_de(ini)), "20263021220")


class TestConsulta(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp()
        self.antes = (raios.QUADROS, raios.PASTA)
        raios.PASTA = os.path.join(self.pasta, "raios", "v1")
        raios.QUADROS = os.path.join(raios.PASTA, "quadros")
        self.r = raios.Raios()
        self.r.carregar()

    def tearDown(self):
        raios.QUADROS, raios.PASTA = self.antes
        shutil.rmtree(self.pasta)

    def test_conta_no_raio_e_na_janela(self):
        self.r.juntar(T - timedelta(hours=5), np.array([[-52.0, -6.6, 3.0], [-60.0, -10.0, 1.0]]))
        self.r.juntar(T - timedelta(hours=80), np.array([[-52.0, -6.6, 3.0]]))
        self.r.juntar(T + timedelta(minutes=20), np.array([[-52.0, -6.6, 3.0]]))  # depois do foco: não conta
        r = self.r.consultar(-6.62, -51.98, T, 72, 25)
        self.assertEqual(r["n"], 1)
        self.assertAlmostEqual(r["mais_perto_km"], 3.1, places=1)
        self.assertEqual(r["ultimo"], "2026-10-29T07:00:03Z")
        self.assertEqual(self.r.consultar(-6.62, -51.98, T, 168, 25)["n"], 2)
        self.assertEqual(self.r.consultar(-6.62, -51.98, T, 72, 2)["n"], 0)

    def test_cobertura_conta_os_arquivos_lidos(self):
        for k in range(raios.ARQUIVOS_POR_QUADRO):
            self.r.juntar(T - timedelta(minutes=10) + timedelta(seconds=20 * k), np.zeros((0, 3)))
        # Janela dentro do quadro das 11:50, todo lido.
        r = self.r.consultar(-6.6, -52.0, T - timedelta(seconds=1), 9 / 60, 25)
        self.assertEqual(r["cobertura"], 1.0)
        # Metade da janela no quadro das 11:40, que não foi lido.
        self.assertAlmostEqual(self.r.consultar(-6.6, -52.0, T - timedelta(minutes=5), 10 / 60, 25)["cobertura"], 0.5, delta=0.05)
        self.assertEqual(r["n"], 0)
        self.assertIsNone(r["mais_perto_km"])

    def test_arquivo_lido_uma_vez_e_disco_de_volta(self):
        ini = T - timedelta(hours=1)
        self.r.juntar(ini, np.array([[-52.0, -6.6, 3.0]]))
        self.r.juntar(ini, np.array([[-52.0, -6.6, 3.0]]))
        self.assertTrue(self.r.ja_lido(ini))
        for c in self.r.alterados:
            self.r.gravar(c)
        self.r.gravar_indice(T)
        with open(os.path.join(raios.PASTA, "indice.json")) as f:
            self.assertEqual(json.load(f)["quadros"], [{"c": "20263021100", "n": 1, "a": 1}])
        outro = raios.Raios()
        outro.carregar()
        self.assertTrue(outro.ja_lido(ini))
        self.assertEqual(outro.consultar(-6.6, -52.0, T, 72, 25)["n"], 1)

    def test_limpa_o_que_saiu_da_janela(self):
        self.r.juntar(T - timedelta(days=8), np.array([[-52.0, -6.6, 3.0]]))
        for c in self.r.alterados:
            self.r.gravar(c)
        self.r.limpar(T)
        self.assertEqual(self.r.quadros, {})
        self.assertEqual(os.listdir(raios.QUADROS), [])


class TestPreenchimento(unittest.TestCase):
    """O passado é lido aos poucos, sem deixar hora para trás (29/10/2026: horas inteiras ficavam vazias por 30 min)."""

    def setUp(self):
        self.pasta = tempfile.mkdtemp()
        self.antes = (raios.QUADROS, raios.PASTA, raios.JANELA, raios.PASSADO_POR_CICLO)
        raios.PASTA = os.path.join(self.pasta, "raios", "v1")
        raios.QUADROS = os.path.join(raios.PASTA, "quadros")
        raios.JANELA = timedelta(hours=8)
        raios.PASSADO_POR_CICLO = 100  # menos que uma hora: o caso que deixava buraco

    def tearDown(self):
        raios.QUADROS, raios.PASTA, raios.JANELA, raios.PASSADO_POR_CICLO = self.antes
        shutil.rmtree(self.pasta)

    @staticmethod
    def chave(t: datetime) -> str:
        e = t + timedelta(seconds=20)
        f = lambda x: f"{x.year}{x.timetuple().tm_yday:03d}{x.hour:02d}{x.minute:02d}{x.second:02d}0"  # noqa: E731
        # Como na NOAA, a pasta da hora é a do FIM do arquivo: o das :59:40 cai na hora seguinte.
        return f"GLM-L2-LCFA/{e.year}/{e.timetuple().tm_yday:03d}/{e.hour:02d}/OR_GLM-L2-LCFA_G19_s{f(t)}_e{f(e)}_c{f(e)}.nc"

    def test_sem_buracos(self):
        agora = T
        todos = [agora - raios.JANELA + timedelta(seconds=20 * k) for k in range(int(raios.JANELA.total_seconds() // 20))]
        por_hora: dict[str, list[str]] = {}
        for t in todos:
            c = self.chave(t)
            por_hora.setdefault(c.rsplit("/", 1)[0] + "/", []).append(c)
        r = raios.Raios()
        r.carregar()
        with mock.patch.object(raios, "listar", lambda p: por_hora.get(p, [])), \
             mock.patch.object(raios, "baixar", lambda url, **k: b""), \
             mock.patch.object(raios, "ler_flashes", lambda nc: (None, np.zeros((0, 3)))), \
             mock.patch.object(raios.time, "time", side_effect=lambda: 1e9):
            for _ in range(int(len(todos) / raios.PASSADO_POR_CICLO) + 3):
                raios.ciclo(r, agora)
        faltando = [t for t in todos if not r.ja_lido(t)]
        self.assertEqual(faltando, [])


class TestPedido(unittest.TestCase):
    def test_parametros(self):
        lat, lon, t, h, r = raios.ler_pedido({"lat": ["-6.62"], "lon": ["-51.98"], "t": ["202610291030"]})
        self.assertEqual((lat, lon, h, r), (-6.62, -51.98, 72, 25.0))
        self.assertEqual(t, datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(raios.ler_pedido({"lat": ["0"], "lon": ["0"], "t": ["202610291030"], "h": ["9999"], "r": ["999"]})[3:], (168, 100.0))
        with self.assertRaises(ValueError):
            raios.ler_pedido({"lat": ["-6.62"], "lon": ["-51.98"]})
        with self.assertRaises(ValueError):
            raios.ler_pedido({"lat": ["nan"], "lon": ["0"], "t": ["202610291030"]})


if __name__ == "__main__":
    unittest.main()
