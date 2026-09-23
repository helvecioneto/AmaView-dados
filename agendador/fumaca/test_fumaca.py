"""
Testes da fumaça, sem rede: o netCDF do ADPF é sintético (mesma grade, mesmos
atributos que importam), e o S3 é trocado por dicionários.

  python3 -m unittest discover -s agendador/fumaca
"""
import io
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
import fumaca  # noqa: E402

PROJ = {
    "semi_major_axis": 6378137.0,
    "semi_minor_axis": 6356752.31414,
    "perspective_point_height": 35786023.0,
    "longitude_of_projection_origin": -75.0,
}
N = 5424
PASSO = 5.6e-05
OFFSET = 0.151844


def netcdf_sintetico(fumaca_em=None, noite=False) -> bytes:
    """ADPF de disco completo com `Smoke` 0 no disco, 1 em `fumaca_em` (linhas, colunas) e vazio fora."""
    import h5py

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        x = f.create_dataset("x", data=np.arange(N, dtype=np.int16))
        x.attrs["scale_factor"] = np.array([PASSO], np.float32)
        x.attrs["add_offset"] = np.array([-OFFSET], np.float32)
        y = f.create_dataset("y", data=np.arange(N, dtype=np.int16))
        y.attrs["scale_factor"] = np.array([-PASSO], np.float32)
        y.attrs["add_offset"] = np.array([OFFSET], np.float32)
        # De noite a NOAA grava 0 ("sem fumaça"), não vazio: medido em 23/09/2026.
        s = np.zeros((N, N), np.int8)
        if fumaca_em is not None:
            s[fumaca_em] = 1
        d = f.create_dataset("Smoke", data=s, chunks=(48, N), compression="gzip")
        d.attrs["_FillValue"] = np.array([-128], np.int8)
        # PQI1: bits 2–3 = ângulo solar (0 válido, 2 fora da faixa: noite).
        f.create_dataset("PQI1", data=np.full((N, N), 8 if noite else 0, np.uint16), chunks=(24, N), compression="gzip")
        p = f.create_dataset("goes_imager_projection", data=0, dtype=np.int32)
        for k, v in PROJ.items():
            p.attrs[k] = np.array([v])
    return buf.getvalue()


def celula(lon: float, lat: float) -> tuple[int, int]:
    """(linha, coluna) do pixel do ADPF mais perto de lon/lat (busca na grade, só para montar o teste)."""
    cols = -OFFSET + np.arange(N) * PASSO
    lins = OFFSET - np.arange(N) * PASSO
    # Grade de 16 em 16 pixels: basta para cair dentro do lugar certo.
    gx, gy = np.meshgrid(cols[::16], lins[::16])
    lo, la = fumaca.geos_para_lonlat(gx, gy, PROJ)
    d = np.nan_to_num((lo - lon) ** 2 + (la - lat) ** 2, nan=1e9)
    r, c = np.unravel_index(np.argmin(d), d.shape)
    return int(r * 16), int(c * 16)


class Nomes(unittest.TestCase):
    def test_carimbo_do_arquivo(self):
        nome = "ABI-L2-ADPF/2026/266/17/OR_ABI-L2-ADPF-M6_G19_s20262661720197_e20262661729505_c20262661733589.nc"
        self.assertEqual(fumaca.carimbo_do_arquivo(nome), "20262661720")
        self.assertIsNone(fumaca.carimbo_do_arquivo("OR_ABI-L2-ADPC-M6_G19_s20262661720197_e2026266172950_c1.nc"))

    def test_carimbo_ida_e_volta(self):
        t = datetime(2026, 9, 23, 17, 20, tzinfo=timezone.utc)
        self.assertEqual(fumaca.carimbo_de(t), "20262661720")
        self.assertEqual(fumaca.instante("20262661720"), t)

    def test_prefixo(self):
        t = datetime(2026, 1, 5, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(fumaca.prefixo_da_hora(t), "ABI-L2-ADPF/2026/005/03/")


class Geometria(unittest.TestCase):
    def test_subponto(self):
        lon, lat = fumaca.geos_para_lonlat(np.array([0.0]), np.array([0.0]), PROJ)
        self.assertAlmostEqual(lon[0], -75.0, places=6)
        self.assertAlmostEqual(lat[0], 0.0, places=6)

    def test_manaus(self):
        # Manaus (−60,02; −3,10) na grade do GOES-East, conferida com o pyproj (+proj=geos … +sweep=x +ellps=GRS80).
        lon, lat = fumaca.geos_para_lonlat(np.array([0.045680]), np.array([-0.009514]), PROJ)
        self.assertAlmostEqual(lon[0], -60.02, delta=0.005)
        self.assertAlmostEqual(lat[0], -3.10, delta=0.005)

    def test_fora_do_disco(self):
        lon, _ = fumaca.geos_para_lonlat(np.array([0.2]), np.array([0.0]), PROJ)
        self.assertTrue(np.isnan(lon[0]))

    def test_enxugar_tira_colineares(self):
        anel = np.array([[0, 0], [1, 0], [2, 0], [2, 1], [2, 2], [1, 2], [0, 2], [0, 1], [0, 0]], float)
        out = fumaca.enxugar(anel)
        self.assertEqual(len(out), 5)  # 4 cantos + fechamento
        np.testing.assert_array_equal(out[0], out[-1])

    def test_mancha_com_buraco(self):
        m = np.zeros((20, 20), bool)
        m[5:15, 5:15] = True
        m[9:11, 9:11] = False
        eixo_x = lambda c: -OFFSET + (c + 3000) * PASSO  # noqa: E731
        eixo_y = lambda r: OFFSET - (r + 2700) * PASSO  # noqa: E731
        polis = fumaca.poligonos(m, 0, 0, eixo_x, eixo_y, PROJ)
        self.assertEqual(len(polis), 1)
        aneis, km2 = polis[0]
        self.assertEqual(len(aneis), 2)
        self.assertGreater(fumaca.area_assinada(aneis[0]), 0)  # externo anti-horário
        self.assertLess(fumaca.area_assinada(aneis[1]), 0)  # buraco horário
        # 100 − 4 pixels de ~2 km, contorno a meio pixel: da ordem de 350 km².
        self.assertTrue(250 < km2 < 450, km2)


class Quadro(unittest.TestCase):
    def test_fumaca_sobre_rondonia(self):
        r, c = celula(-63.0, -10.0)
        nc = netcdf_sintetico((slice(r, r + 6), slice(c, c + 6)))
        gj, resumo = fumaca.processar(nc)
        self.assertEqual(resumo["n"], 1)
        self.assertEqual(resumo["cob"], 1.0)
        anel = np.array(gj["features"][0]["geometry"]["coordinates"][0])
        self.assertTrue(abs(anel[:, 0].mean() + 63.0) < 0.2 and abs(anel[:, 1].mean() + 10.0) < 0.2, anel.mean(axis=0))
        self.assertGreater(gj["features"][0]["properties"]["km2"], 50)

    def test_fora_do_setor_nao_entra(self):
        r, c = celula(-100.0, 20.0)  # México: fora do NSA
        gj, resumo = fumaca.processar(netcdf_sintetico((slice(r, r + 6), slice(c, c + 6))))
        self.assertEqual(resumo["n"], 0)
        self.assertEqual(gj["features"], [])

    def test_africa_fica_de_fora(self):
        # Borda do disco sobre o Saara: onde o ADP confunde poeira com fumaça.
        r, c = celula(-5.0, 21.0)
        gj, resumo = fumaca.processar(netcdf_sintetico((slice(r, r + 6), slice(c, c + 6))))
        self.assertEqual(resumo["n"], 0)

    def test_noite_sem_cobertura(self):
        _, resumo = fumaca.processar(netcdf_sintetico(noite=True))
        self.assertEqual(resumo, {"n": 0, "km2": 0, "cob": 0.0})


class Rodada(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(fumaca, "RAIZ", self.raiz),
            mock.patch.object(fumaca, "PASTA", os.path.join(self.raiz, "fumaca", "v1")),
            mock.patch.object(fumaca, "QUADROS", os.path.join(self.raiz, "fumaca", "v1", "quadros")),
            mock.patch.object(fumaca, "ESTADO", os.path.join(self.raiz, "estado.json")),
        ]
        for p in self.patches:
            p.start()
        self.agora = datetime(2026, 9, 23, 17, 40, tzinfo=timezone.utc)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.raiz)

    def nome(self, t: datetime) -> str:
        c = fumaca.carimbo_de(t)
        return f"{fumaca.prefixo_da_hora(t)}OR_ABI-L2-ADPF-M6_G19_s{c}197_e{c}9505_c{c}9589.nc"

    def test_baixa_o_novo_apaga_o_velho_e_indexa(self):
        os.makedirs(fumaca.QUADROS)
        velho = fumaca.carimbo_de(self.agora - timedelta(hours=49))
        open(os.path.join(fumaca.QUADROS, f"{velho}.geojson"), "w").write('{"features":[]}')
        publicados = {fumaca.carimbo_de(self.agora - timedelta(minutes=m)): None for m in (20, 30)}
        listadas = []

        def listar(prefixo):
            listadas.append(prefixo)
            return {c: self.nome(fumaca.instante(c)) for c in publicados if self.nome(fumaca.instante(c)).startswith(prefixo)}

        nc = netcdf_sintetico(noite=True)
        with mock.patch.object(fumaca, "listar", side_effect=listar), mock.patch.object(fumaca, "baixar", return_value=nc) as baixar:
            r = fumaca.rodada(self.agora)
            self.assertEqual(r["novos"], 2)
            self.assertEqual(r["removidos"], 1)
            self.assertEqual(baixar.call_count, 2)
            # 49 horas com quadros faltando, listadas uma vez cada.
            self.assertEqual(len(listadas), 49)

            # Segunda rodada: nada novo baixado; só as duas horas mais novas relistadas.
            listadas.clear()
            r = fumaca.rodada(self.agora + timedelta(minutes=2))
            self.assertEqual(r["novos"], 0)
            self.assertEqual(baixar.call_count, 2)
            self.assertEqual(len(listadas), 2)

        indice = json.load(open(os.path.join(fumaca.PASTA, "indice.json")))
        self.assertEqual([q["c"] for q in indice["quadros"]], sorted(publicados))
        self.assertEqual(indice["quadros"][0]["cob"], 0.0)
        self.assertFalse(os.path.exists(os.path.join(fumaca.QUADROS, f"{velho}.geojson")))
        # Nenhum netCDF no disco, nunca.
        self.assertFalse([n for _, _, fs in os.walk(self.raiz) for n in fs if n.endswith(".nc")])

    def test_fila_que_sobra_continua_na_proxima_rodada(self):
        publicados = {fumaca.carimbo_de(self.agora - timedelta(hours=h, minutes=20)): None for h in (5, 6, 7)}
        listar = lambda prefixo: {c: self.nome(fumaca.instante(c)) for c in publicados if self.nome(fumaca.instante(c)).startswith(prefixo)}  # noqa: E731
        nc = netcdf_sintetico(noite=True)
        with mock.patch.object(fumaca, "MAX_POR_RODADA", 2), mock.patch.object(fumaca, "listar", side_effect=listar), mock.patch.object(fumaca, "baixar", return_value=nc):
            r = fumaca.rodada(self.agora)
            self.assertEqual((r["novos"], r["fila"]), (2, 1))
            # Dois minutos depois as horas antigas não são relistadas, mas o que sobrou é feito.
            r = fumaca.rodada(self.agora + timedelta(minutes=2))
            self.assertEqual((r["novos"], r["fila"], r["quadros"]), (1, 0, 3))

    def test_arquivo_quebrado_espera_antes_de_tentar_de_novo(self):
        c = fumaca.carimbo_de(self.agora - timedelta(minutes=20))
        listar = lambda prefixo: {c: self.nome(fumaca.instante(c))} if self.nome(fumaca.instante(c)).startswith(prefixo) else {}  # noqa: E731
        with mock.patch.object(fumaca, "listar", side_effect=listar), mock.patch.object(fumaca, "baixar", return_value=b"lixo") as baixar:
            self.assertEqual(fumaca.rodada(self.agora)["falhas"], 1)
            self.assertEqual(fumaca.rodada(self.agora + timedelta(minutes=2))["falhas"], 0)
            self.assertEqual(baixar.call_count, 1)

    def test_indice_se_refaz_sem_estado(self):
        os.makedirs(fumaca.QUADROS)
        c = fumaca.carimbo_de(self.agora - timedelta(minutes=30))
        gj = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"km2": 12}, "geometry": None}]}
        open(os.path.join(fumaca.QUADROS, f"{c}.geojson"), "w").write(json.dumps(gj))
        with mock.patch.object(fumaca, "listar", return_value={}):
            fumaca.rodada(self.agora)
        indice = json.load(open(os.path.join(fumaca.PASTA, "indice.json")))
        self.assertEqual(indice["quadros"], [{"c": c, "n": 1, "km2": 12, "cob": None}])


if __name__ == "__main__":
    unittest.main()
