"""
Testes da área atingida, sem rede: CSV sintético no formato do INPE e focos
montados à mão na projeção.

  python3 -m unittest discover -s agendador/area-queimada
"""
import io
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import area_queimada as aq  # noqa: E402

CAB = "id,lat,lon,data_hora_gmt,satelite,municipio,estado,pais,municipio_id,estado_id,pais_id,numero_dias_sem_chuva,precipitacao,risco_fogo,bioma,frp\n"
URUARA = "1508159"
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def linha(lat, lon, data="2026-11-18 16:30:00", sat="NOAA-20", mun=URUARA, nome="URUARÁ", frp="12.5"):
    return f"x,{lat:11.6f},{lon:11.6f},{data},{sat},{nome},PARÁ,Brasil,{mun},15,33,3,0.0,0.9,Amazônia,{frp}\n"


def focos(pontos, t=None):
    """pontos: [(lon, lat, satélite)] → dicionário de arrays como o arquivo do dia."""
    t = int((t or T0).timestamp())
    return aq.para_numpy({
        "lat": [p[1] for p in pontos],
        "lon": [p[0] for p in pontos],
        "t": [t + i for i in range(len(pontos))],
        "sat": [aq.SATELITES.index(p[2]) for p in pontos],
        "frp": [10.0] * len(pontos),
        "mun": [int(URUARA)] * len(pontos),
    })


def deslocar(lon, lat, dx_m, dy_m):
    """Ponto a dx, dy metros (aprox. local) de (lon, lat)."""
    return lon + dx_m / (111_320 * math.cos(math.radians(lat))), lat + dy_m / 110_574


class LeituraCsv(unittest.TestCase):
    def test_filtra_amazonia_separa_dias_e_le_frp(self):
        texto = CAB + linha(-3.5, -53.4) + linha(-3.6, -53.5, data="2026-11-19 00:10:00", frp="-999") \
            + linha(-23.5, -46.6, mun="3550308", nome="SÃO PAULO") + linha(-3.7, -53.6, data="2026-11-18")
        nomes = {}
        dias = aq.ler_focos(io.StringIO(texto), {URUARA}, nomes)
        self.assertEqual(sorted(dias), ["20261118", "20261119"])
        self.assertEqual(len(dias["20261118"]["lat"]), 2)
        self.assertEqual(dias["20261118"]["t"][1], int(datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(math.isnan(dias["20261119"]["frp"][0]))
        self.assertEqual(nomes, {URUARA: ["URUARÁ", "PARÁ"]})

    def test_satelite_desconhecido_fica_como_outro(self):
        dias = aq.ler_focos(io.StringIO(CAB + linha(-3.5, -53.4, sat="NOVO-1")), {URUARA}, {})
        self.assertEqual(dias["20261118"]["sat"], [aq.OUTRO])


class Areas(unittest.TestCase):
    def rodar(self, pontos, **kw):
        f = focos(pontos)
        t = int(T0.timestamp())
        return aq.areas(f, t - 86400, t + 86400, nomes={URUARA: ["URUARÁ", "PARÁ"]}, **kw)

    def test_pixel_isolado_tem_a_area_do_pixel(self):
        r = self.rodar([(-53.4, -3.5, "NOAA-20")])
        self.assertEqual(len(r["areas"]), 1)
        # 375 m × 375 m = 0,14 km² (disco de mesma área, polígono de 16 lados).
        self.assertAlmostEqual(r["areas"][0][0], 0.14, delta=0.01)
        self.assertEqual(r["municipios"], [["URUARÁ", int(URUARA)]])

    def test_vizinhos_viram_uma_area_e_distantes_duas(self):
        lon, lat = -53.4, -3.5
        perto = deslocar(lon, lat, 700, 0)    # bordas a ~280 m: mesma área
        longe = deslocar(lon, lat, 3000, 0)   # bordas a ~2,6 km: outra área
        r = self.rodar([(lon, lat, "NOAA-20"), (*perto, "NOAA-21"), (*longe, "NPP-375")])
        self.assertEqual(len(r["areas"]), 2)
        maior, menor = r["areas"]
        self.assertEqual(maior[1], 2)          # focos
        self.assertEqual(maior[2], [2, 0])     # VIIRS, MODIS
        # O fechamento preenche o vão: mais que dois pixels soltos.
        self.assertGreater(maior[0], 2 * 0.14)
        self.assertEqual(menor[1], 1)

    def test_mesmo_pixel_em_varias_passagens_conta_os_focos_mas_nao_a_area(self):
        r = self.rodar([(-53.4, -3.5, "NOAA-20")] * 5)
        self.assertEqual(len(r["areas"]), 1)
        self.assertEqual(r["areas"][0][1], 5)
        self.assertAlmostEqual(r["areas"][0][0], 0.14, delta=0.01)

    def test_geoestacionario_fica_de_fora_por_padrao(self):
        r = self.rodar([(-53.4, -3.5, "GOES-19"), (-53.4, -3.5, "MSG-03")])
        self.assertEqual(r["areas"], [])
        r = self.rodar([(-53.4, -3.5, "GOES-19")], classes=(1, 2, 3))
        self.assertEqual(len(r["areas"]), 1)

    def test_min_focos(self):
        r = self.rodar([(-53.4, -3.5, "NOAA-20"), (-52.0, -3.5, "NOAA-20"), (-52.0, -3.5, "AQUA_M-T")], min_focos=2)
        self.assertEqual(len(r["areas"]), 1)
        self.assertEqual(r["areas"][0][2], [1, 1])

    def test_geometria_codificada_volta_ao_lugar(self):
        r = self.rodar([(-53.4, -3.5, "AQUA_M-T")])
        anel = r["areas"][0][8][0][0]
        x = np.cumsum(anel[0::2]) / 1e4
        y = np.cumsum(anel[1::2]) / 1e4
        self.assertAlmostEqual(float(x.mean()), -53.4, delta=0.002)
        self.assertAlmostEqual(float(y.mean()), -3.5, delta=0.002)
        # Raio de 564 m (1 km²): ~0,005° para cada lado.
        self.assertAlmostEqual(float(x.max() - x.min()), 0.0101, delta=0.001)

    def test_horarios_em_minutos_desde_o_inicio(self):
        f = focos([(-53.4, -3.5, "NOAA-20")])
        t = int(T0.timestamp())
        r = aq.areas(f, t - 3600, t + 60)
        self.assertEqual(r["areas"][0][3], 60)
        self.assertEqual(r["areas"][0][4], 60)


class Rodada(unittest.TestCase):
    def test_arquivo_diario_mensal_e_saida(self):
        agora = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
        diarios = {"20261119", "20261120"}

        def cab(url):
            # O de ontem foi fechado às 12h05 de hoje? Não: ainda aberto (06h).
            return ("Thu, 20 Nov 2026 05:05:00 GMT", "100")

        def pedir(url, metodo="GET", tentativas=3):
            if url.endswith("/diario/Brasil/"):
                corpo = "".join(f'<a href="focos_diario_br_{d}.csv">' for d in diarios).encode()
            elif "focos_diario_br_20261119" in url:
                corpo = (CAB + linha(-3.5, -53.4, data="2026-11-19 16:30:00")).encode()
            elif "focos_diario_br_20261120" in url:
                corpo = (CAB + linha(-3.5, -53.4, data="2026-11-20 04:30:00")).encode()
            elif "mensal" in url:
                corpo = (CAB + linha(-5.0, -52.0, data="2026-10-01 16:30:00", sat="AQUA_M-T")).encode()
            else:
                raise AssertionError(url)
            return io.BytesIO(corpo)

        with tempfile.TemporaryDirectory() as raiz, \
                mock.patch.object(aq, "pedir", side_effect=pedir), \
                mock.patch.object(aq, "cabecalhos", side_effect=cab):
            indice = aq.rodada(raiz, agora, log=lambda *_: None)
            self.assertEqual([p["dias"] for p in indice["periodos"]], list(aq.PERIODOS))
            por = {p["dias"]: p for p in indice["periodos"]}
            self.assertEqual(por[3]["focos"], 2)       # os dois dias do diário (mesmo pixel)
            self.assertEqual(por[3]["areas"], 1)
            self.assertEqual(por[60]["focos"], 3)      # + o de outubro, do mensal
            self.assertEqual(por[60]["areas"], 2)
            saida = os.path.join(raiz, "area-queimada", "v1")
            with open(os.path.join(saida, por[60]["arquivo"])) as f:
                d60 = json.load(f)
            self.assertEqual(d60["campos"], aq.CAMPOS)
            self.assertEqual(d60["satelites"], ["viirs", "modis"])
            estado = json.load(open(os.path.join(raiz, "estado.json")))
            self.assertFalse(estado["20261119"]["fechado"])
            self.assertEqual(estado["20261001"]["fonte"], "mensal")
            self.assertEqual(len(os.listdir(os.path.join(raiz, "arquivo"))), aq.GUARDA_DIAS + 1)  # + municipios.json
            # Rodada seguinte, nada mudou no INPE: nada é baixado de novo.
            chamadas = []
            with mock.patch.object(aq, "pedir", side_effect=lambda u, *a, **k: chamadas.append(u) or pedir(u)):
                aq.rodada(raiz, agora + timedelta(minutes=1), log=lambda *_: None)
            self.assertEqual([u for u in chamadas if "focos_" in u], [])
            # Gerações antigas saem.
            aq.rodada(raiz, agora + timedelta(days=1), log=lambda *_: None)
            geracoes = [d for d in os.listdir(saida) if d[0].isdigit()]
            self.assertEqual(len(geracoes), aq.GERACOES_GUARDADAS)


if __name__ == "__main__":
    unittest.main()
