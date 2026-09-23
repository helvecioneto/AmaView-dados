"""
Testes da qualidade do ar, sem rede: as três APIs são trocadas por respostas
gravadas (no formato medido em 23/09/2026).

  python3 -m unittest discover -s agendador/ar
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import ar  # noqa: E402

AGORA = datetime(2026, 9, 23, 19, 12, tzinfo=timezone.utc).timestamp()

SENSORES = [
    {"id": 1, "code": "RBR3", "sensor_index": 31109, "name": "MPAC_RBR", "municipio": "Rio Branco", "latitude": None, "longitude": None},
    {"id": 2, "code": "CPX1", "sensor_index": 56663, "name": "MPAC_CPX_01_qpm", "municipio": "Capixaba", "latitude": None, "longitude": None},
]
ULTIMAS = [
    {"sensor_index": 31109, "time_stamp": "2026-09-23T18:53:46Z", "pm2_5_corrected": 14.790000000000001, "latitude": -9.966662, "longitude": -67.84944, "channel_flags": 0},
    # Parado desde março: fica no mapa como "sem dado", sem série.
    {"sensor_index": 56663, "time_stamp": "2026-03-25T17:27:36Z", "pm2_5_corrected": 0.44, "latitude": -10.153018, "longitude": -67.73821, "channel_flags": 0},
    # Fora do cadastro e parado desde 2022: saiu da rede.
    {"sensor_index": 3968, "time_stamp": "2022-09-12T10:58:21Z", "pm2_5_corrected": 40.91, "latitude": -9.95768, "longitude": -67.86895, "channel_flags": None},
    # Sem coordenada: não há onde desenhar.
    {"sensor_index": 25525, "time_stamp": "2026-09-23T18:50:00Z", "pm2_5_corrected": 26.9, "latitude": None, "longitude": None, "channel_flags": None},
]
HISTORICO = [
    {"municipio": "Rio Branco", "bucket": "2026-09-23T16:00:00Z", "pm2_5_avg": 8.27666666666667},
    {"municipio": "Rio Branco", "bucket": "2026-09-23T17:00:00Z", "pm2_5_avg": 8.721111111111115},
    {"municipio": "Rio Branco", "bucket": "2026-09-20T17:00:00Z", "pm2_5_avg": 30.0},
]


def estacao_monitorar(ide, nome, uf, lon, dt, medicoes=None):
    return {
        "idEstacao": ide,
        "noEstacao": nome,
        "nuLatitude": "-2.589",
        "nuLongitude": str(lon),
        "noFonteDados": "Secretaria Estadual de Meio Ambiente - SEMA",
        "municipio": {"noMunicipio": "São Luís", "sgUf": uf},
        "poluentes": [{"noPoluente": "MP₂,₅", "dsPoluente": "Material Particulado", "medicoes": medicoes or []}],
        "indiceQualidadeArAtual": {
            "poluente": {"noPoluente": "MP₂,₅"},
            "classificacaoIqAr": {"id": 2, "noClassificacao": "Moderada"},
            "medicao": {"indiceQualidadeAr": 43.0, "dtMedicao": dt},
        },
        "dtUltimaAtualizacao": dt,
    }


MEDICOES = [
    {"indiceQualidadeAr": 44.0, "dtMedicao": "2026-09-23 13:00", "stDadoValidado": True, "classificacaoIqAr": {"id": 2}},
    {"indiceQualidadeAr": 43.0, "dtMedicao": "2026-09-23 14:00", "stDadoValidado": True, "classificacaoIqAr": {"id": 2}},
]
TODAS = [
    estacao_monitorar(136573, "Gapara", "MA", -44.3033, "2026-09-23 14:00"),
    # Leste de 44°W: fora da Amazônia Legal.
    estacao_monitorar(999, "Teresina", "MA", -42.8, "2026-09-23 14:00"),
    estacao_monitorar(1000, "Rio", "RJ", -43.4, "2026-09-23 14:00"),
    # Data no futuro (existe uma, com 2066): não conta como atualizada.
    estacao_monitorar(136554, "SEST SENAT", "MA", -44.2664, "2066-03-23 16:00"),
]


def resposta_cams(url):
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    n = len(q["latitude"][0].split(","))
    base = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())
    tempos = [base + 3600 * k for k in range(72)]
    um = {"hourly": {"time": tempos, **{v: [float(k) for k in range(72)] for v in ar.VARS_CAMS}}, "hourly_units": {"pm2_5": "μg/m³"}}
    return [um] * n if n > 1 else um


def falso_baixar_json(url, **_):
    if url.endswith("/sensors"):
        return SENSORES
    if url.endswith("/readings/latest-by-sensor"):
        return ULTIMAS
    if "/readings/history" in url:
        return HISTORICO
    if url.endswith("/estacao/todas"):
        return TODAS
    if "/estacao/por-ids" in url:
        return [estacao_monitorar(136573, "Gapara", "MA", -44.3033, "2026-09-23 14:00", MEDICOES), TODAS[3]]
    if url.endswith("meta.json"):
        return {"last_run_initialisation_time": 1790121600, "last_run_availability_time": 1790158882}
    if "/v1/air-quality" in url:
        return resposta_cams(url)
    raise AssertionError(url)


class TestTempo(unittest.TestCase):
    def test_monitorar_e_horario_de_brasilia(self):
        # 14:00 em Brasília = 17:00 UTC.
        self.assertEqual(ar.monitorar_para_s("2026-09-23 14:00"), datetime(2026, 9, 23, 17, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(ar.monitorar_para_s("lixo"))
        self.assertIsNone(ar.monitorar_para_s(None))

    def test_grade_dos_sensores_cobre_48_h_ate_o_proximo_multiplo(self):
        t0, n = ar.grade_sensores(AGORA)
        self.assertEqual(n, 577)
        self.assertEqual(t0 + (n - 1) * 300, datetime(2026, 9, 23, 19, 15, tzinfo=timezone.utc).timestamp())

    def test_serie_na_grade_arredonda_ou_contem(self):
        self.assertEqual(ar.serie_na_grade([(290, 1.0)], 0, 300, 3, arredondar=True), [None, 1.0, None])
        self.assertEqual(ar.serie_na_grade([(290, 1.0)], 0, 300, 3, arredondar=False), [1.0, None, None])
        # Fora da janela some; dois no mesmo horário: o mais novo.
        self.assertEqual(ar.serie_na_grade([(-900, 5.0), (0, 1.0), (10, 2.0)], 0, 300, 2, arredondar=True), [2.0, None])

    def test_num_nunca_inventa_zero(self):
        self.assertIsNone(ar.num(None))
        self.assertIsNone(ar.num(float("nan")))
        self.assertIsNone(ar.num(True))
        self.assertEqual(ar.num(14.790000000000001), 14.79)


class TestGrade(unittest.TestCase):
    def test_grade_cobre_a_amazonia_legal_sobre_a_grade_do_cams(self):
        g = ar.grade_cams(ar.carregar_anel())
        self.assertGreater(len(g), 600)
        self.assertLess(len(g), 800)
        # Centros múltiplos de 0,4° (a grade do CAMS global).
        for lon, lat in g:
            self.assertAlmostEqual(lon / 0.4, round(lon / 0.4), places=6)
            self.assertAlmostEqual(lat / 0.4, round(lat / 0.4), places=6)
        # Manaus dentro, São Paulo fora.
        self.assertTrue(any(abs(lon + 60.0) < 0.5 and abs(lat + 3.2) < 0.5 for lon, lat in g))
        self.assertFalse(any(abs(lon + 46.6) < 0.5 and abs(lat + 23.5) < 0.5 for lon, lat in g))


class TestRodada(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(ar, "RAIZ", self.dir),
            mock.patch.object(ar, "PASTA", os.path.join(self.dir, "ar", "v1")),
            mock.patch.object(ar, "ESTADO", os.path.join(self.dir, "estado.json")),
            mock.patch.object(ar, "CAMS_BRUTO", os.path.join(self.dir, "cams-bruto.json")),
            mock.patch.object(ar, "baixar_json", side_effect=falso_baixar_json),
            mock.patch.object(ar.time, "sleep"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)

    def ler(self, nome):
        with open(os.path.join(self.dir, "ar", "v1", nome)) as f:
            return json.load(f)

    def test_rodada_publica_os_tres_arquivos(self):
        r = ar.rodada(AGORA)
        self.assertEqual(r["falhas"], 0)
        p = self.ler("pontos.json")
        ids = [s["id"] for s in p["sensores"]["lista"]]
        # Cadastrado e vivo, cadastrado e parado; fora do cadastro e parado some; sem coordenada some.
        self.assertEqual(sorted(ids), [31109, 56663])
        rb = next(s for s in p["sensores"]["lista"] if s["id"] == 31109)
        self.assertEqual(rb["cod"], "RBR3")
        self.assertEqual(rb["ult"]["pm"], 14.79)
        self.assertEqual(sum(v is not None for v in rb["serie"]), 1)
        parado = next(s for s in p["sensores"]["lista"] if s["id"] == 56663)
        self.assertTrue(all(v is None for v in parado["serie"]))
        # Média horária do município: só o que cai nas 48 h.
        mun = p["municipios"]["serie"]["Rio Branco"]
        self.assertEqual(len(mun), 48)
        self.assertEqual(mun[-4:], [8.28, 8.72, None, None])
        # Só as estações da Amazônia Legal.
        est = {e["nome"]: e for e in p["estacoes"]["lista"]}
        self.assertEqual(sorted(est), ["Gapara", "SEST SENAT"])
        self.assertIsNone(est["SEST SENAT"]["ult"])
        pm = est["Gapara"]["polu"][0]
        # 13:00 e 14:00 de Brasília = 16:00 e 17:00 UTC: as horas 44 e 45 (a última, 47, é 19:00 UTC).
        self.assertEqual(pm["iqar"][-4:], [44.0, 43.0, None, None])
        self.assertEqual(pm["cl"][-4:-2], [2, 2])
        self.assertEqual(p["fontes"]["cams"]["erro"], None)
        # O modelo: 48 horas até a hora corrente, sem previsão.
        c = self.ler("cams.json")
        self.assertEqual(c["horas"], 48)
        self.assertEqual(len(c["pm2_5"]), len(c["celulas"]))
        # Hora corrente 19:00 UTC do dia 23 = índice 67 a partir de 21/09 00:00.
        self.assertEqual(c["pm2_5"][0][-1], 67.0)
        self.assertEqual(c["pm2_5"][0][0], 20.0)
        s = self.ler("cams-series.json")
        self.assertEqual(s["vars"], ar.VARS_CAMS)
        self.assertEqual(len(s["series"]["aerosol_optical_depth"]), len(c["celulas"]))
        self.assertIn("Copernicus", c["atribuicao"])

    def test_acumula_a_serie_entre_rodadas_sem_duplicar(self):
        ar.rodada(AGORA)
        ULTIMAS[0] = dict(ULTIMAS[0], time_stamp="2026-09-23T18:58:46Z", pm2_5_corrected=20.0)
        try:
            ar.rodada(AGORA + 300)
            ar.rodada(AGORA + 600)
        finally:
            ULTIMAS[0] = dict(ULTIMAS[0], time_stamp="2026-09-23T18:53:46Z", pm2_5_corrected=14.790000000000001)
        rb = next(s for s in self.ler("pontos.json")["sensores"]["lista"] if s["id"] == 31109)
        self.assertEqual([v for v in rb["serie"] if v is not None], [14.79, 20.0])

    def test_cams_so_baixa_de_novo_com_rodada_nova(self):
        ar.rodada(AGORA)
        chamadas = [c for c in ar.baixar_json.call_args_list if "/v1/air-quality" in c.args[0]]
        ar.rodada(AGORA + 300)
        depois = [c for c in ar.baixar_json.call_args_list if "/v1/air-quality" in c.args[0]]
        self.assertEqual(len(chamadas), len(depois))
        self.assertGreater(len(chamadas), 1)  # em lotes

    def test_fonte_fora_nao_derruba_as_outras(self):
        def quebrado(url, **kw):
            if "ufac" in url:
                raise ar.urllib.error.URLError("fora do ar")
            return falso_baixar_json(url, **kw)

        with mock.patch.object(ar, "baixar_json", side_effect=quebrado):
            r = ar.rodada(AGORA)
        self.assertEqual(r["falhas"], 1)
        p = self.ler("pontos.json")
        self.assertIn("fora do ar", p["fontes"]["ufac"]["erro"])
        self.assertEqual(len(p["estacoes"]["lista"]), 2)


if __name__ == "__main__":
    unittest.main()
