"""
Testes do terreno, sem rede: os recortes do MapBiomas são GeoTIFFs pequenos
gerados aqui (grade de 30 m como a do MapBiomas, valores conhecidos) e o SICAR
é trocado por uma resposta gravada (formato medido em 28/10/2026).

  python3 -m unittest discover -s agendador/terreno
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from osgeo import gdal

sys.path.insert(0, os.path.dirname(__file__))
import terreno  # noqa: E402

RES = 0.00026949458523585647  # a do MapBiomas (1/3600 × 0,97…)
X0, Y0 = -64.0, -8.6  # canto noroeste da grade de teste
LADO = 200  # px

# Resposta real do WFS do SICAR (28/10/2026, ponto -6.5, -52.5 no PA), só os campos pedidos.
CAR_PA = {
    "type": "FeatureCollection",
    "features": [
        {"properties": {"cod_imovel": "PA-1507300-3FA9D62A97314E429106B71165BEA291", "status_imovel": "CA",
                        "condicao": "Cancelado por decisão administrativa", "area": 42831.4737, "m_fiscal": 571.0863,
                        "tipo_imovel": "IRU", "municipio": "São Félix do Xingu", "uf": "PA"}},
        {"properties": {"cod_imovel": "PA-1507300-AAAA", "status_imovel": "AT", "condicao": "Aguardando análise",
                        "area": 1200.5, "m_fiscal": 16.0, "tipo_imovel": "IRU", "municipio": "São Félix do Xingu", "uf": "PA"}},
        {"properties": {"cod_imovel": "PA-1507300-BBBB", "status_imovel": "AT", "condicao": "Aguardando análise",
                        "area": 80.0, "m_fiscal": 1.07, "tipo_imovel": "AST", "municipio": "São Félix do Xingu", "uf": "PA"}},
    ],
}


def gravar(pasta, ano, valores, x0=X0):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(os.path.join(pasta, f"{ano}.tif"), LADO, LADO, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((x0, RES, 0, Y0, 0, -RES))
    ds.GetRasterBand(1).WriteArray(valores)
    ds = None


class Resposta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestJanela(unittest.TestCase):
    def test_circulo_de_375_m_tem_a_area_certa(self):
        gt = (X0, RES, 0, Y0, 0, -RES)
        lat, lon = Y0 - 100 * RES, X0 + 100 * RES
        janela, mascara = terreno.janela_circulo(gt, (LADO, LADO), lat, lon, 375)
        px_m2 = (RES * terreno.M_POR_GRAU) * (RES * terreno.M_POR_GRAU * np.cos(np.radians(lat)))
        area = mascara.sum() * px_m2
        self.assertAlmostEqual(area / (np.pi * 375**2), 1, delta=0.08)
        self.assertEqual(mascara.shape, (janela[3], janela[2]))

    def test_fora_da_grade(self):
        gt = (X0, RES, 0, Y0, 0, -RES)
        self.assertEqual(terreno.janela_circulo(gt, (LADO, LADO), Y0 - 2 * RES, X0 + 2 * RES, 375), (None, None))

    def test_contar_ignora_o_zero_e_ordena(self):
        self.assertEqual(terreno.contar(np.array([0, 3, 15, 15, 3, 15, 0], dtype=np.uint8)), [[15, 3], [3, 2]])


class TestRecortes(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.pasta)

    def test_serie_mostra_a_floresta_virando_pasto(self):
        floresta = np.full((LADO, LADO), 3, dtype=np.uint8)
        metade = floresta.copy()
        metade[:, 100:] = 15  # leste do ponto vira pastagem
        gravar(self.pasta, 1985, floresta)
        gravar(self.pasta, 2025, metade)
        r = terreno.Recortes(self.pasta)
        self.assertEqual(r.anos, [1985, 2025])
        comp = r.composicao(Y0 - 100 * RES, X0 + 100 * RES)
        self.assertEqual(len(comp), 2)
        self.assertEqual([c for c, _ in comp[0]], [3])
        classes = dict(comp[1])
        self.assertEqual(set(classes), {3, 15})
        self.assertAlmostEqual(classes[3] / sum(classes.values()), 0.5, delta=0.06)

    def test_ano_com_grade_diferente_ou_ilegivel_fica_de_fora(self):
        gravar(self.pasta, 1985, np.zeros((LADO, LADO), dtype=np.uint8))
        gravar(self.pasta, 1986, np.zeros((LADO, LADO), dtype=np.uint8), x0=X0 + RES)
        with open(os.path.join(self.pasta, "1987.tif"), "wb") as f:
            f.write(b"isto nao e um tiff")
        self.assertEqual(terreno.Recortes(self.pasta).anos, [1985])

    def test_pasta_vazia_nao_quebra(self):
        r = terreno.Recortes(self.pasta)
        self.assertEqual(r.anos, [])
        self.assertIsNone(r.composicao(-8.7, -63.9))

    def test_completa_os_anos_que_chegam_depois(self):
        gravar(self.pasta, 1985, np.full((LADO, LADO), 3, dtype=np.uint8))
        r = terreno.Recortes(self.pasta)
        gravar(self.pasta, 1986, np.full((LADO, LADO), 3, dtype=np.uint8))
        r.completar()
        self.assertEqual(r.anos, [1985])  # olhou há menos de 1 min
        r.visto -= 61
        r.completar()
        self.assertEqual(r.anos, [1985, 1986])


class TestCar(unittest.TestCase):
    def setUp(self):
        terreno.CACHE_CAR.itens.clear()
        terreno._falha_car[0] = 0.0

    def test_url_usa_lat_lon_e_so_campos_publicos(self):
        u = terreno.url_car("PA", -6.5, -52.5)
        self.assertIn("sicar%3Asicar_imoveis_pa", u)
        self.assertIn("POINT%28-6.5+-52.5%29", u)
        self.assertIn("propertyName=cod_imovel", u)

    def test_imoveis_ativos_primeiro_e_menores_antes(self):
        cods = [i["cod"] for i in terreno.imoveis_car(CAR_PA)]
        self.assertEqual(cods, ["PA-1507300-BBBB", "PA-1507300-AAAA", "PA-1507300-3FA9D62A97314E429106B71165BEA291"])
        primeiro = terreno.imoveis_car(CAR_PA)[0]
        self.assertEqual(primeiro["areaHa"], 80.0)
        self.assertEqual(primeiro["tipo"], "AST")

    def test_consulta_guarda_em_cache(self):
        chamadas = []

        def abrir(pedido, timeout):
            chamadas.append(pedido.full_url)
            return Resposta(json.dumps(CAR_PA).encode())

        a = terreno.consultar_car("PA", -6.5, -52.5, abrir)
        b = terreno.consultar_car("PA", -6.5, -52.5, abrir)
        self.assertEqual(a["situacao"], "ok")
        self.assertEqual(len(a["imoveis"]), 3)
        self.assertEqual(a, b)
        self.assertEqual(len(chamadas), 1)

    def test_sicar_fora_do_ar_pausa_um_minuto(self):
        chamadas = []

        def abrir(pedido, timeout):
            chamadas.append(1)
            raise TimeoutError("timed out")

        r = terreno.consultar_car("PA", -6.5, -52.5, abrir)
        self.assertEqual(r, {"situacao": "indisponivel", "imoveis": []})
        self.assertIsNone(terreno.CACHE_CAR.pegar(("PA", -6.5, -52.5)))
        terreno.consultar_car("PA", -6.6, -52.6, abrir)
        self.assertEqual(len(chamadas), 1)  # o segundo nem tentou
        terreno._falha_car[0] -= terreno.PAUSA_CAR
        terreno.consultar_car("PA", -6.6, -52.6, abrir)
        self.assertEqual(len(chamadas), 2)

    def test_sem_uf_nao_consulta(self):
        self.assertEqual(terreno.consultar_car(None, -6.5, -52.5, None)["situacao"], "sem_uf")


class TestPedido(unittest.TestCase):
    def test_ponto_valido(self):
        self.assertEqual(terreno.ler_ponto({"lat": ["-8.761234"], "lon": ["-63.90129"], "uf": ["ro"]}), (-8.7612, -63.9013, "RO"))

    def test_ponto_fora_ou_uf_errada(self):
        with self.assertRaises(ValueError):
            terreno.ler_ponto({"lat": ["-23.5"], "lon": ["-46.6"]})
        self.assertEqual(terreno.ler_ponto({"lat": ["-8.7"], "lon": ["-63.9"], "uf": ["GO"]}), (-8.7, -63.9, None))
        with self.assertRaises(ValueError):
            terreno.ler_ponto({"lat": ["nan"], "lon": ["-63.9"]})
        with self.assertRaises(KeyError):
            terreno.ler_ponto({"lat": ["-8.7"]})

    def test_bloco_ilegivel_nao_leva_o_car_junto(self):
        r = terreno.Recortes(tempfile.mkdtemp())
        r.composicao = mock.Mock(side_effect=RuntimeError("TIFFReadEncodedTile() failed"))
        corpo, validade = terreno.responder_ponto(r, -8.7, -63.9, "RO", lambda *a: {"situacao": "ok", "imoveis": [{"cod": "X"}]})
        self.assertIsNone(corpo["mapbiomas"])
        self.assertEqual(corpo["car"]["imoveis"], [{"cod": "X"}])
        self.assertEqual(validade, terreno.CACHE_SEM_CAR)

    def test_resposta_guarda_pouco_sem_car(self):
        pasta = tempfile.mkdtemp()
        try:
            gravar(pasta, 2025, np.full((LADO, LADO), 15, dtype=np.uint8))
            r = terreno.Recortes(pasta)
            lat, lon = Y0 - 100 * RES, X0 + 100 * RES
            ok, validade_ok = terreno.responder_ponto(r, lat, lon, "RO", lambda *a: {"situacao": "ok", "imoveis": []})
            _, validade_fora = terreno.responder_ponto(r, lat, lon, "RO", lambda *a: {"situacao": "indisponivel", "imoveis": []})
            self.assertEqual(ok["mapbiomas"]["anos"], [2025])
            self.assertEqual(ok["mapbiomas"]["comp"][0][0][0], 15)
            self.assertEqual(ok["raioM"], 375)
            self.assertEqual(validade_ok, terreno.CACHE_OK)
            self.assertEqual(validade_fora, terreno.CACHE_SEM_CAR)
        finally:
            shutil.rmtree(pasta)


if __name__ == "__main__":
    unittest.main()
