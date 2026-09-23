"""
Testes das estações meteorológicas, sem rede: o WIS2, o CSV e a API da AWC,
o cadastro do INMET e o OSCAR são trocados por respostas sintéticas.

  python3 -m unittest discover -s agendador/meteo
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import meteo  # noqa: E402

# Quadrado de 10° com um buraco de 2° no meio: basta para o par-ímpar e a faixa.
ANEIS = [
    [(-65.0, -10.0), (-55.0, -10.0), (-55.0, 0.0), (-65.0, 0.0), (-65.0, -10.0)],
    [(-61.0, -6.0), (-59.0, -6.0), (-59.0, -4.0), (-61.0, -4.0), (-61.0, -6.0)],
]
AGORA = datetime(2026, 9, 23, 19, 10, tzinfo=timezone.utc)


def feature(w, nome, valor, lon=-60.0, lat=-3.0, hora="2026-09-23T18:00:00Z", fenomeno=None):
    return {
        "geometry": {"coordinates": [lon, lat, 62.8]},
        "properties": {
            "name": nome,
            "value": valor,
            "reportTime": hora,
            "phenomenonTime": fenomeno or hora,
            "wigos_station_identifier": w,
        },
    }


class Recorte(unittest.TestCase):
    def setUp(self):
        meteo._REGIAO.clear()

    def test_dentro_fora_e_buraco(self):
        self.assertTrue(meteo.dentro(-57.0, -2.0, ANEIS))
        self.assertFalse(meteo.dentro(-60.0, -5.0, ANEIS))  # no buraco
        self.assertFalse(meteo.dentro(-50.0, -5.0, ANEIS))

    def test_faixa_de_100_km(self):
        # 0,5° a leste da borda (~55 km): entra; 2° (~220 km): não.
        self.assertTrue(meteo.na_regiao(-54.5, -5.0, ANEIS))
        self.assertFalse(meteo.na_regiao(-53.0, -5.0, ANEIS))
        # Fora do bbox nem calcula.
        self.assertFalse(meteo.na_regiao(-30.0, -5.0, ANEIS))
        self.assertFalse(meteo.na_regiao(None, -5.0, ANEIS))

    def test_poligono_publicado_cobre_manaus_e_nao_sao_paulo(self):
        aneis = meteo.carregar_aneis()
        self.assertTrue(meteo.na_regiao(-60.02, -3.10, aneis))  # Manaus
        self.assertTrue(meteo.na_regiao(-48.48, -1.45, aneis))  # Belém
        self.assertFalse(meteo.na_regiao(-46.63, -23.55, aneis))  # São Paulo
        self.assertFalse(meteo.na_regiao(-38.5, -3.7, aneis))  # Fortaleza


class Inmet(unittest.TestCase):
    def setUp(self):
        meteo._REGIAO.clear()

    def test_ler_hora(self):
        hora = datetime(2026, 9, 23, 18, tzinfo=timezone.utc)
        cru = {
            "features": [
                feature("A", "air_temperature", 36.9),
                feature("A", "wind_speed", 0.7000000000000001),
                feature("A", "relative_humidity", 51.0),
                # Chuva de 1 h entra; de 12 h, não.
                feature("A", "total_precipitation_or_total_water_equivalent", 2.4, fenomeno="2026-09-23T17:00:00Z/2026-09-23T18:00:00Z"),
                feature("B", "total_precipitation_or_total_water_equivalent", 30.0, lon=-57, lat=-2, fenomeno="2026-09-23T06:00:00Z/2026-09-23T18:00:00Z"),
                feature("B", "air_temperature", 31.0, lon=-57, lat=-2),
                # Sem período (instante): não é chuva de 1 h.
                feature("B", "total_precipitation_or_total_water_equivalent", 1.0, lon=-57, lat=-2),
                # Outra hora, fora da região, valor absurdo, variável que não usamos.
                feature("A", "air_temperature", 20.0, hora="2026-09-23T17:00:00Z"),
                feature("C", "air_temperature", 25.0, lon=-40, lat=-20),
                feature("A", "dewpoint_temperature", 99.0),
                feature("A", "net_radiation_integrated_over_period_specified", 5.0),
                feature("A", "pressure_reduced_to_mean_sea_level", None),
            ]
        }
        obs, pos = meteo.ler_hora(cru, hora, ANEIS)
        self.assertEqual(obs["A"], {"t": 36.9, "vv": 0.7, "ur": 51.0, "chuva": 2.4})
        self.assertEqual(obs["B"], {"t": 31.0})
        self.assertNotIn("C", obs)
        self.assertEqual(pos["A"], [-60.0, -3.0, 62.8])

    def test_periodo(self):
        self.assertEqual(meteo.periodo_h("2026-09-23T11:00:00Z/2026-09-23T12:00:00Z"), 1)
        self.assertEqual(meteo.periodo_h("2026-09-22T12:00:00Z/2026-09-23T12:00:00Z"), 24)
        self.assertIsNone(meteo.periodo_h("2026-09-23T12:00:00Z"))
        self.assertIsNone(meteo.periodo_h("lixo/lixo"))

    def test_cadastro(self):
        automaticas = [{"CD_ESTACAO": "A101", "DC_NOME": "MANAUS", "SG_ESTADO": "AM", "TP_ESTACAO": "Automatica", "CD_WSI": "0-76-0-1302603000000003"}]
        convencionais = [{"CD_ESTACAO": "82353", "DC_NOME": "ALTAMIRA", "SG_ESTADO": "PA", "TP_ESTACAO": "Convencional", "CD_WSI": "0-76-0-1500602000W82353"}]
        c = meteo.ler_cadastro([automaticas, convencionais, None])
        self.assertEqual(c["0-76-0-1302603000000003"], {"c": "A101", "n": "Manaus", "uf": "AM"})
        self.assertEqual(c["0-20000-0-82353"]["c"], "82353")
        self.assertIn("0-76-0-1500602000W82353", c)

    def test_nome_legivel(self):
        self.assertEqual(meteo.nome_legivel("BOA VISTA (AERO)"), "Boa Vista (Aero)")
        self.assertEqual(meteo.nome_legivel("SAO GABRIEL DA CACHOEIRA"), "Sao Gabriel da Cachoeira")
        self.assertEqual(meteo.nome_legivel("Belém"), "Belém")


class Metar(unittest.TestCase):
    def setUp(self):
        meteo._REGIAO.clear()

    CAB = "raw_text,station_id,observation_time,latitude,longitude,temp_c,dewpoint_c,wind_dir_degrees,wind_speed_kt,wind_gust_kt,visibility_statute_mi,altim_in_hg,sea_level_pressure_mb,wx_string,metar_type,elevation_m"

    def csv(self, *linhas) -> bytes:
        return gzip.compress(("\n".join([self.CAB, *linhas]) + "\n").encode())

    def test_csv(self):
        corpo = self.csv(
            '"METAR SBMN 231900Z 15004KT 4000 FU SCT035 35/21 Q1007",SBMN,2026-09-23T19:00:00.000Z,-3.146,-59.986,35,21,150,4,,2.49,29.74,,FU,METAR,71',
            '"SPECI SBEG 231915Z 00000KT 9999 FEW040 36/21 Q1007",SBEG,2026-09-23T19:15:00.000Z,-3.04,-60.05,36,21,0,0,,6+,29.74,,,SPECI,80',
            '"METAR KMIA 231900Z 10010KT 10SM FEW030 31/24 A3001",KMIA,2026-09-23T19:00:00.000Z,25.79,-80.29,31,24,100,10,,10+,30.01,,,METAR,3',
        )
        obs, pos = meteo.ler_csv_metar(corpo, ANEIS)
        self.assertEqual(set(obs), {"SBMN", "SBEG"})
        m = obs["SBMN"]
        self.assertEqual(m["t"], 35.0)
        self.assertEqual(m["vis"], 4000)
        self.assertEqual(m["wx"], "FU")
        self.assertAlmostEqual(m["vv"], 2.1, places=1)  # 4 kt
        self.assertAlmostEqual(m["p"], 1007.1, places=0)
        self.assertEqual(m["ur"], 44)
        self.assertEqual(m["ms"], int(datetime(2026, 9, 23, 19, tzinfo=timezone.utc).timestamp() * 1000))
        self.assertTrue(obs["SBEG"]["speci"])
        self.assertNotIn("dv", obs["SBEG"])  # calmaria: sem direção
        self.assertEqual(obs["SBEG"]["vis"], 10000)
        self.assertEqual(pos["SBMN"], [-59.986, -3.146, 71.0])

    def test_csv_sem_cabecalho(self):
        with self.assertRaises(ValueError):
            meteo.ler_csv_metar(b"lixo\n1,2,3\n", ANEIS)

    def test_visibilidade(self):
        self.assertEqual(meteo.visibilidade_m("METAR SBMN 231900Z 15004KT 4000 FU SCT035 35/21 Q1007", None), 4000)
        self.assertEqual(meteo.visibilidade_m("METAR SBMN 231900Z 15004KT 9999 SCT035 35/21 Q1007", None), 10000)
        self.assertEqual(meteo.visibilidade_m("METAR SBCY 231900Z 08003KT CAVOK 29/22 Q1011", None), 10000)
        # Hora (4 dígitos) de TEMPO/RMK não é visibilidade; sem nada, milhas.
        self.assertEqual(meteo.visibilidade_m("METAR KMIA 231900Z 10010KT 10SM FEW030 31/24 A3001 RMK SLP1234 T0310", "10+"), 10000)
        self.assertEqual(meteo.visibilidade_m("METAR KXXX 231900Z 10010KT 2SM BR", "2"), 3219)
        self.assertIsNone(meteo.visibilidade_m("METAR SLJO 231900Z WO ATTN", None))

    def test_umidade(self):
        self.assertEqual(meteo.ur_de(35, 21), 44)
        self.assertEqual(meteo.ur_de(25, 25), 100)
        self.assertIsNone(meteo.ur_de(None, 20))

    def test_api(self):
        lista = [
            {"icaoId": "SBMN", "obsTime": 1790190000, "temp": 35, "dewp": 21, "wdir": 150, "wspd": 4, "wgst": 15, "visib": 2.49, "altim": 1007, "wxString": "FU", "metarType": "METAR", "rawOb": "METAR SBMN 231900Z 15004G15KT 4000 FU SCT035 35/21 Q1007", "lat": -3.146, "lon": -59.986, "elev": 71, "name": "Manaus/Ponta Peleda, AM, BR"},
            {"icaoId": "SBPV", "obsTime": 1790190000, "temp": 36, "dewp": 23, "wdir": "VRB", "wspd": 3, "rawOb": "METAR SBPV 231900Z VRB03KT 9999 SCT040 36/23 Q1007", "lat": -8.7, "lon": -63.9},
            {"icaoId": "SBXX"},
        ]
        obs, pos, nomes = meteo.ler_api_metar(lista)
        self.assertEqual(set(obs), {"SBMN", "SBPV"})
        self.assertAlmostEqual(obs["SBMN"][0]["raj"], 7.7, places=1)
        self.assertNotIn("dv", obs["SBPV"][0])  # VRB
        self.assertEqual(nomes["SBMN"], "Manaus/Ponta Peleda, AM, BR")
        self.assertEqual(meteo.nome_aerodromo(nomes["SBMN"], "SBMN"), ("Manaus/Ponta Peleda", "BR"))
        self.assertEqual(meteo.nome_aerodromo("Alta Floresta Arpt, SP, BR", "SBAT"), ("Alta Floresta", "BR"))
        self.assertEqual(meteo.nome_aerodromo(None, "SBAT"), ("SBAT", None))

    def test_fundir(self):
        acumulado = {"SBMN": [{"ms": 1000, "raw": "velho", "t": 20.0}, {"ms": 5000, "raw": "a", "t": 30.0}]}
        # Instante repetido (correção) substitui; velho sai; sem medição não entra.
        n = meteo.fundir_metar(acumulado, "SBMN", [{"ms": 5000, "raw": "COR", "t": 31.0}, {"ms": 9000, "raw": "b", "t": 32.0}, {"ms": 9500, "raw": "WO ATTN"}], 2000)
        self.assertEqual(n, 2)
        self.assertEqual([r["raw"] for r in acumulado["SBMN"]], ["COR", "b"])


class Montagem(unittest.TestCase):
    def test_montar(self):
        grade = meteo.horas_da_janela(AGORA)
        self.assertEqual(len(grade), 48)
        self.assertEqual(grade[-1], datetime(2026, 9, 23, 19, tzinfo=timezone.utc))
        h17, h18 = meteo.nome_hora(grade[-3]), meteo.nome_hora(grade[-2])
        horas = {
            h17: {"W1": {"t": 30.0, "chuva": 1.0}},
            h18: {"W1": {"t": 31.0, "chuva": 0.4}, "W2": {"t": 28.0}},
        }
        posicoes = {"W1": [-60.0, -3.0, 60.0], "W2": [-57.0, -2.0, None]}
        cadastro = {"W1": {"c": "A101", "n": "Manaus", "uf": "AM"}, "W2": {"c": "82332", "n": "Manaus (Aero)", "uf": None, "oscar": True}}
        ms18 = meteo.ms(grade[-2])
        metar = {"SBMN": [{"ms": ms18, "raw": "METAR SBMN …", "t": 35.0, "wx": "FU"}]}
        metar_pos = {"SBMN": [-57.01, -2.01, 71.0]}
        est, serie = meteo.montar(AGORA, horas, posicoes, cadastro, metar, metar_pos, {"SBMN": "Manaus/Ponta Peleda, AM, BR"})
        self.assertEqual(serie["t0"], meteo.ms(grade[0]))
        self.assertEqual(serie["slots"], 48)
        self.assertEqual(serie["inmet"]["A101"]["t"][-3:], [30.0, 31.0, None])
        u = est["estacoes"][0]["u"]
        self.assertEqual((est["estacoes"][0]["c"], u["ms"], u["t"], u["chuva24"], u["chuva24n"]), ("A101", ms18, 31.0, 1.4, 2))
        # A sinótica do aeroporto a ~1 km do METAR sai (mesmo lugar, dado repetido).
        self.assertNotIn("82332", serie["inmet"])
        m = [e for e in est["estacoes"] if e["k"] == "m"][0]
        self.assertEqual((m["c"], m["n"], m["pais"], m["u"]["wx"]), ("SBMN", "Manaus/Ponta Peleda", "BR", "FU"))
        self.assertEqual(serie["metar"]["SBMN"]["ms"], [ms18])
        self.assertEqual(serie["metar"]["SBMN"]["raw"], ["METAR SBMN …"])


class Rodada(unittest.TestCase):
    """Uma rodada inteira com a rede trocada por respostas prontas."""

    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        self.antigos = (meteo.RAIZ, meteo.PASTA, meteo.HORAS, meteo.ESTADO, meteo.METAR_ESTADO, meteo.CADASTRO)
        meteo.RAIZ = self.raiz
        meteo.PASTA = os.path.join(self.raiz, "meteo", "v1")
        meteo.HORAS = os.path.join(self.raiz, "horas")
        meteo.ESTADO = os.path.join(self.raiz, "estado.json")
        meteo.METAR_ESTADO = os.path.join(self.raiz, "metar.json")
        meteo.CADASTRO = os.path.join(self.raiz, "cadastro.json")
        meteo._REGIAO.clear()
        self.pedidos: list[str] = []

    def tearDown(self):
        (meteo.RAIZ, meteo.PASTA, meteo.HORAS, meteo.ESTADO, meteo.METAR_ESTADO, meteo.CADASTRO) = self.antigos
        shutil.rmtree(self.raiz, ignore_errors=True)

    def falso(self, url, **_):
        self.pedidos.append(url)
        if "wis2bra" in url:
            hora = url.split("datetime=")[1].split("&")[0]
            if hora.startswith("2026-09-23T19"):  # a hora atual ainda não chegou
                return json.dumps({"numberMatched": 0, "features": []}).encode()
            feats = [feature("0-76-0-1302603000000003", "air_temperature", 30.0, hora=hora)]
            return json.dumps({"numberMatched": 1, "features": feats}).encode()
        if "apitempo" in url:
            outras = [{"CD_ESTACAO": f"A{k}", "DC_NOME": "X", "TP_ESTACAO": "Automatica", "CD_WSI": f"0-76-0-{k}"} for k in range(200, 300)]
            return json.dumps([{"CD_ESTACAO": "A101", "DC_NOME": "MANAUS", "SG_ESTADO": "AM", "TP_ESTACAO": "Automatica", "CD_WSI": "0-76-0-1302603000000003"}, *outras]).encode()
        if "metars.cache" in url:
            return gzip.compress((Metar.CAB + '\n"METAR SBMN 231900Z 15004KT 4000 FU SCT035 35/21 Q1007",SBMN,2026-09-23T19:00:00.000Z,-3.146,-59.986,35,21,150,4,,2.49,29.74,,FU,METAR,71\n').encode())
        if "api/data/metar" in url:
            return b"[]"
        raise AssertionError(url)

    def test_primeira_e_segunda_rodada(self):
        with mock.patch.object(meteo, "baixar", side_effect=self.falso), mock.patch.object(meteo, "carregar_aneis", return_value=ANEIS):
            r = meteo.rodada(AGORA)
            self.assertEqual(r["baixadas"], 47)  # 48 horas menos a atual, vazia
            self.assertEqual((r["inmet"], r["metar"], r["falhas"]), (1, 1, 0))
            est = meteo.ler_json(os.path.join(meteo.PASTA, "estacoes.json"), None)
            self.assertEqual({e["c"] for e in est["estacoes"]}, {"A101", "SBMN"})
            serie = meteo.ler_json(os.path.join(meteo.PASTA, "serie.json"), None)
            self.assertEqual(serie["inmet"]["A101"]["t"][:47], [30.0] * 47)
            self.assertIsNone(serie["inmet"]["A101"]["t"][47])

            # Segunda rodada, 5 min depois: só contagens das horas recentes, nada baixado.
            self.pedidos.clear()
            r = meteo.rodada(AGORA + timedelta(minutes=5))
            self.assertEqual(r["baixadas"], 0)
            wis = [u for u in self.pedidos if "wis2bra" in u]
            self.assertTrue(all("limit=1&" in u for u in wis))
            self.assertLessEqual(len(wis), meteo.RECENTES_H)
            self.assertFalse(any("apitempo" in u for u in self.pedidos))  # cadastro vale um dia
            self.assertFalse(any("api/data/metar" in u for u in self.pedidos))  # API de hora em hora

    def test_fonte_fora_do_ar_nao_derruba_a_rodada(self):
        def falha(url, **_):
            raise meteo.urllib.error.URLError("fora do ar")

        with mock.patch.object(meteo, "baixar", side_effect=falha), mock.patch.object(meteo, "carregar_aneis", return_value=ANEIS):
            r = meteo.rodada(AGORA)
        self.assertEqual((r["inmet"], r["metar"]), (0, 0))
        self.assertGreater(r["falhas"], 0)
        self.assertTrue(os.path.exists(os.path.join(meteo.PASTA, "serie.json")))


if __name__ == "__main__":
    unittest.main()
