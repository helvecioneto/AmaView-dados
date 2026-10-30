"""
Testes do retrato da denúncia, sem rede: o código do link curto (o mesmo da
página), os recortes de cada fonte e a regra de que o primeiro retrato fica.

  python3 -m unittest discover -s agendador/retrato
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import retrato  # noqa: E402


class Codigo(unittest.TestCase):
    def test_o_mesmo_da_pagina(self):
        # src/denuncia/denuncia.test.ts: codigoDoFoco(FOCO) === '4yywg.7l9yo.8eed6.6'
        f = retrato.ler_codigo("4yywg.7l9yo.8eed6.6")
        self.assertEqual(f, {"lat": -6.5, "lon": -52.5, "t": datetime(2026, 10, 28, 17, 30, tzinfo=timezone.utc), "satelite": "NPP-375", "fonte": "diario"})
        self.assertEqual(retrato.ler_codigo("568la.7gobc.8efpi.1")["fonte"], "goes10min")

    def test_invalido(self):
        for c in ["", "a.b.c", "a.b.c.d.e", "A.b.c.0", "a.b.c.~x", "../x.y.z.0", "a.b.c.zz"]:
            self.assertIsNone(retrato.ler_codigo(c), c)


class Recortes(unittest.TestCase):
    def test_meteo_so_inmet_perto_com_a_serie(self):
        estacoes = {"versao": 1, "estacoes": [
            {"k": "i", "c": "A1", "x": -52.5, "y": -6.6},
            {"k": "i", "c": "A2", "x": -40.0, "y": -6.5},
            {"k": "m", "c": "SBXX", "x": -52.5, "y": -6.5},
        ]}
        serie = {"t0": 1, "passoMin": 60, "slots": 48, "inmet": {"A1": {"t": [1]}, "A2": {"t": [2]}}, "metar": {"SBXX": {}}}
        r = retrato.recorte_meteo(estacoes, serie, -6.5, -52.5)
        self.assertEqual([e["c"] for e in r["estacoes"]["estacoes"]], ["A1"])
        self.assertEqual(r["serie"], {"t0": 1, "passoMin": 60, "slots": 48, "inmet": {"A1": {"t": [1]}}, "metar": {}})

    def test_cemaden_linhas_da_grade(self):
        estacoes = {"gerado": "x", "estacoes": [{"c": "P1", "x": -52.5, "y": -6.7}, {"c": "P2", "x": -50, "y": -6.5}]}
        serie = {"t0": 5, "slots": 2, "passoMin": 10, "estacoes": ["P2", "P1"], "v": [[9, 9], [1, 2]]}
        r = retrato.recorte_cemaden(estacoes, serie, -6.5, -52.5)
        self.assertEqual(r["estacoes"]["estacoes"], [{"c": "P1", "x": -52.5, "y": -6.7}])
        self.assertEqual(r["serie"], {"t0": 5, "slots": 2, "passoMin": 10, "estacoes": ["P1"], "v": [[1, 2]]})

    def test_fumaca_so_as_areas_perto(self):
        quadrado = lambda x, y: {"type": "Feature", "properties": {"km2": 3}, "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + 0.1, y], [x + 0.1, y + 0.1], [x, y]]]}}  # noqa: E731
        geo = {"type": "FeatureCollection", "features": [quadrado(-52.6, -6.6), quadrado(-45, -6.6)]}
        r = retrato.recorte_quadro(geo, -6.5, -52.5)
        self.assertEqual(len(r["features"]), 1)


class Guardar(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp()
        self.antes = retrato.PASTA
        retrato.PASTA = self.pasta
        retrato.GUARDA = retrato.Guarda()

    def tearDown(self):
        retrato.PASTA = self.antes
        shutil.rmtree(self.pasta)

    def partes(self, falha=()):
        def f(nome):
            def g(*_):
                if nome in falha:
                    raise OSError("fora do ar")
                return {"de": nome}
            return g
        return mock.patch.multiple(retrato, parte_terreno=f("terreno"), parte_raios=f("raios"), parte_meteo=f("meteo"), parte_fumaca=f("fumaca"), parte_cemaden=f("cemaden"))

    def test_o_primeiro_fica_e_o_que_faltou_e_completado(self):
        with self.partes(falha=("cemaden",)):
            codigo, r = retrato.pedir_retrato("4yywg.7l9yo.8eed6.6", "PA")
        self.assertEqual((codigo, r["novo"], r["faltam"]), (200, True, ["cemaden"]))
        guardado = retrato.ler("4yywg.7l9yo.8eed6.6")
        self.assertEqual(guardado["partes"]["terreno"], {"de": "terreno"})
        self.assertEqual(guardado["foco"]["satelite"], "NPP-375")
        criado = guardado["criado"]
        # O segundo pedido só completa a parte que faltou.
        with self.partes():
            codigo, r = retrato.pedir_retrato("4yywg.7l9yo.8eed6.6", None)
        guardado = retrato.ler("4yywg.7l9yo.8eed6.6")
        self.assertEqual((r["novo"], r["faltam"], guardado["criado"], guardado["uf"]), (False, [], criado, "PA"))
        self.assertEqual(guardado["partes"]["cemaden"], {"de": "cemaden"})
        # Completo: o terceiro não busca nada.
        with mock.patch.object(retrato, "montar", side_effect=AssertionError("não devia montar")):
            self.assertEqual(retrato.pedir_retrato("4yywg.7l9yo.8eed6.6", "PA")[1]["novo"], False)

    def test_nada_de_quem_pede(self):
        with self.partes():
            retrato.pedir_retrato("4yywg.7l9yo.8eed6.6", "PA")
        self.assertEqual(set(retrato.ler("4yywg.7l9yo.8eed6.6")), {"versao", "criado", "foco", "uf", "partes", "quando"})

    def test_teto_do_dia(self):
        with mock.patch.object(retrato, "MAX_POR_DIA", 1), self.partes():
            self.assertEqual(retrato.pedir_retrato("4yywg.7l9yo.8eed6.6", "PA")[0], 200)
            self.assertEqual(retrato.pedir_retrato("4yywg.7l9yo.8eed6.8", "PA")[0], 429)

    def test_codigo_invalido(self):
        self.assertEqual(retrato.pedir_retrato("../../etc", None)[0], 400)


if __name__ == "__main__":
    unittest.main()
