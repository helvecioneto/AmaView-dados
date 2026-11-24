"""
Testes das aeronaves, sem rede: classificação pelo RAB, identificação do que
a rede devolve, o atraso na publicação e a cobertura da grade de círculos.

  python3 -m unittest discover -s agendador/aeronaves
"""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import aeronaves as av  # noqa: E402

AGORA = 1_795_520_000

RAB = """﻿Atualizado em: 2026-11-24
"MARCAS";"PROPRIETARIOS";"OPERADORES";"CD_TIPO_ICAO";"CD_CLS";"DT_CANC"
"PPNBS";"[]";"[{""NOME"":""CORPO DE BOMBEIROS MIL. DO DIST. FEDERAL""}]";"AT8T";"L1T";""
"PRHFB";"[]";"[{""NOME"":""DEPARTAMENTO DE POLICIA FEDERAL""}]";"AS50";"H1T";""
"PRORL";"[]";"[{""NOME"":""ANTÔNIO RIBAMAR DOS SANTOS""}]";"C210";"L1P";""
"PSIBH";"[]";"[{""NOME"":""INSTITUTO BRASILEIRO DO MEIO AMBIENTE E DOS RECURSOS""}]";"A119";"H1T";""
"PRTLE";"[]";"[{""NOME"":""AEROTERRA - AVIACAO AGRICOLA LTDA""}]";"AT5T";"L1T";""
"PTCGA";"[]";"[{""NOME"":""AEROTERRA - AVIACAO AGRICOLA LTDA""}]";"C182";"L1P";""
"PPGRR";"[]";"[{""NOME"":""AEROCLUBE DO ESTADO DE MINAS GERAIS""}]";"PAUL";"L1P";""
"PRBFL";"[]";"[{""NOME"":""ESTADO DE MATO GROSSO""}]";"AT8T";"L1T";""
"PRVCA";"[]";"[{""NOME"":""ESTADO DO ACRE""}]";"AS50";"H1T";""
"PTXXX";"[]";"[{""NOME"":""CORPO DE BOMBEIROS MILITAR DO PARA""}]";"AS50";"H1T";"2020-01-01"
"""


def frota():
    publicas = av.ler_rab(RAB)
    publicas["PR-HFB"]["h"] = "e4abcd"
    return {"feita": AGORA, "publicas": publicas, "militares": {"e40100": {"r": "FAB2853", "t": "E390", "d": "EMBRAER KC-390"}}}


def visto(h, lat=-3.1, lon=-60.0, **kw):
    a = {"hex": h, "lat": lat, "lon": lon, "seen_pos": 2, "alt_baro": 3000, "gs": 120.0, "track": 90.0}
    a.update(kw)
    return a


class Classificacao(unittest.TestCase):
    def test_rab(self):
        p = av.ler_rab(RAB)
        self.assertEqual(p["PP-NBS"]["c"], "fogo")  # avião-tanque dos bombeiros
        self.assertEqual(p["PR-HFB"]["c"], "policia")
        self.assertEqual(p["PS-IBH"]["c"], "ambiental")
        self.assertEqual(p["PR-TLE"]["c"], "fogo")  # avião-tanque de contratada
        self.assertIn("53/2026", p["PR-TLE"]["k"])
        self.assertEqual(p["PR-BFL"]["c"], "fogo")  # avião-tanque do governo de MT
        self.assertEqual(p["PR-VCA"]["c"], "governo")
        # "RIBAMAR" não é IBAMA; o resto da frota da contratada, aeroclube e cancelada ficam de fora.
        for m in ("PR-ORL", "PT-CGA", "PP-GRR", "PT-XXX"):
            self.assertNotIn(m, p)

    def test_militar(self):
        self.assertEqual(av.classificar_militar("E390", "FAB2853")["c"], "cargueiro")
        self.assertEqual(av.classificar_militar("TUCA", "FAB1446")["c"], "militar")
        self.assertEqual(av.classificar_militar("TUCA", "FAB1446")["o"], "Força Aérea Brasileira")

    def test_tempo_real(self):
        tempo_real = {c for c, (_, tr) in av.CATEGORIAS.items() if tr}
        self.assertEqual(tempo_real, {"fogo", "bombeiros", "defesaCivil", "saude", "cargueiro", "outro"})


class Identificacao(unittest.TestCase):
    def setUp(self):
        self.f = frota()
        self.por_hex, self.publicas = av.indices(self.f)

    def ident(self, a, estado=None):
        return av.identificar(a, self.f, self.por_hex, self.publicas, estado or {})

    def test_publica_por_endereco_marca_e_indicativo(self):
        self.assertEqual(self.ident(visto("e4abcd"))["c"], "policia")
        self.assertEqual(self.ident(visto("e49999", r="PP-NBS"))["c"], "fogo")
        self.assertEqual(self.ident(visto("e49998", flight="PSIBH   "))["c"], "ambiental")

    def test_militar(self):
        self.assertEqual(self.ident(visto("e40100"))["c"], "cargueiro")
        m = self.ident(visto("e40200", dbFlags=1, r="FAB1446", t="TUCA"))
        self.assertEqual((m["c"], m["r"]), ("militar", "FAB1446"))

    def test_outras_sem_dono(self):
        m = self.ident(visto("e48011", r="PR-MBH", t="A320", flight="TAM4684"))
        self.assertEqual(m["c"], "outro")
        self.assertNotIn("o", m)
        # Militar estrangeiro é "outra aeronave".
        self.assertEqual(self.ident(visto("0aca79", dbFlags=1, r="FAC1220"))["c"], "outro")

    def test_escondidas(self):
        self.assertIsNone(self.ident(visto("a12345", dbFlags=8)))  # LADD
        self.assertIsNone(self.ident(visto("a12345", dbFlags=4)))  # PIA
        self.assertIsNone(self.ident(visto("~2a0001")))  # endereço inventado (TIS-B)


class Publicacao(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = mock.patch.multiple(
            av,
            PASTA=os.path.join(self.dir, "pub"),
            ARQ_ATUAL=os.path.join(self.dir, "pub", "atual.json"),
            ARQ_ESTADO=os.path.join(self.dir, "estado.json"),
            ARQ_FROTA=os.path.join(self.dir, "frota.json"),
        )
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def test_atraso_e_janela(self):
        pts = [[AGORA - m * 60, -3.0, -60.0, 1000, 100.0, 90, 0, 0] for m in (400, 120, 61, 59, 1)]
        estado_av = {
            "e4abcd": {"m": {"r": "PR-HFB", "t": "AS50", "c": "policia", "o": "DEPARTAMENTO DE POLICIA FEDERAL"}, "p": pts},
            "e49999": {"m": {"r": "PP-NBS", "t": "AT8T", "c": "fogo", "o": "CORPO DE BOMBEIROS"}, "p": pts},
            "e48011": {"m": {"r": "PR-MBH", "t": "A320", "c": "outro", "o": "QUALQUER"}, "p": pts},
        }
        saida = av.publicar(estado_av, {"fonte": 0, "consultado": AGORA}, AGORA)
        por = {a["h"]: a for a in saida["aeronaves"]}
        # A polícia: só o que tem mais de 1 h, nas 6 h antes disso (até 7 h atrás).
        self.assertEqual([round((AGORA - p[0]) / 60) for p in por["e4abcd"]["p"]], [400, 120, 61])
        self.assertTrue(por["e4abcd"]["a"])
        # Os bombeiros: em tempo real, sem o ponto de fora das 6 h.
        self.assertEqual([round((AGORA - p[0]) / 60) for p in por["e49999"]["p"]], [120, 61, 59, 1])
        self.assertFalse(por["e49999"]["a"])
        # Particular: sem o nome do dono.
        self.assertNotIn("o", por["e48011"])
        self.assertEqual(saida["attribution"], {"adsb.fi": av.CREDITOS["adsb.fi"]})
        with open(av.ARQ_ATUAL) as f:
            self.assertEqual(json.load(f)["atrasoMin"], 60)

    def test_rodada_sem_rede(self):
        with open(av.ARQ_FROTA, "w") as f:
            json.dump(frota(), f)
        resposta = {"ac": [visto("e4abcd"), visto("e49999", r="PP-NBS"), visto("a00001", dbFlags=8), visto("e40001", lat=-30)]}
        with mock.patch.object(av, "baixar_json_cab", return_value=(resposta, {})), mock.patch.object(av.time, "sleep"):
            saida = av.rodada(AGORA)
        hexes = {a["h"] for a in saida["aeronaves"]}
        # Fora da caixa e LADD ficam de fora; a polícia ainda não tem ponto com 1 h.
        self.assertEqual(hexes, {"e49999"})
        with open(av.ARQ_ESTADO) as f:
            self.assertIn("e4abcd", json.load(f)["av"])

    def test_cota_da_adsb_lol(self):
        estado = {"cota": {"adsb.lol": {"restante": 20, "zera": AGORA + 3600}}}
        with self.assertRaises(RuntimeError):
            av.consultar_rede(av.FONTES[1], [0, 1], estado, AGORA)


class Grade(unittest.TestCase):
    def test_cobre_a_caixa(self):
        o, s, l, n = av.CAIXA
        raio = av.RAIO_NM * 1.852
        for i in range(int((n - s) / 0.25) + 1):
            for j in range(int((l - o) / 0.25) + 1):
                la, lo = s + 0.25 * i, o + 0.25 * j
                d = min(
                    math.hypot((lo - b) * 111.32 * math.cos(math.radians((la + a) / 2)), (la - a) * 110.57)
                    for a, b in av.GRADE
                )
                self.assertLessEqual(d, raio, f"({la}, {lo}) fora de todos os círculos")

    def test_giro(self):
        estado = {}
        vistos = set()
        for _ in range(av.GIRO):
            circ, _ = av.circulos_da_rodada(estado, AGORA)
            vistos.update(circ)
        self.assertEqual(vistos, set(range(len(av.GRADE))))


if __name__ == "__main__":
    unittest.main()
