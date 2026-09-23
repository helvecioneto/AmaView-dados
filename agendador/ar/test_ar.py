"""
Testes da qualidade do ar, sem rede: as APIs são trocadas por respostas
gravadas (no formato medido em 23/09/2026). As da RedeAr, AirGradient e UFAC
estão em `amostras/` (recortes de respostas reais); as da PurpleAir e da
OpenAQ, que pedem chave, seguem o formato da documentação de cada uma.

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
    if "redear.org.br" in url and url.endswith("/sensors"):
        return amostra("redear-sensors.json")
    if "redear.org.br" in url and "/readings?" in url:
        return amostra("redear-leituras.json")
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
    if url.endswith("/world/locations/measures/current"):
        return amostra("airgradient-mundo.json")
    if "/world/locations/" in url and url.endswith("/measures/current"):
        lid = int(url.split("/world/locations/")[1].split("/")[0])
        return next(x for x in amostra("airgradient-mundo.json") if x["locationId"] == lid)
    raise AssertionError(url)


def amostra(nome):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "amostras", nome)) as f:
        return json.load(f)


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
        ids = [s["id"] for s in p["sensores"]["lista"] if s["fonte"] == "ufac"]
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


# ---------------------------------------------------------------------------
# Sensores de baixo custo: RedeAr, AirGradient, PurpleAir e OpenAQ

# Depois das amostras gravadas (RedeAr até 20:20, AirGradient 20:22 UTC).
AGORA2 = datetime(2026, 9, 23, 20, 25, tzinfo=timezone.utc).timestamp()

# A UFAC às 19:53 (recorte real) e o cadastro com os quatro sensores.
CADASTRO2 = [
    {"code": "MNL1", "sensor_index": 25531, "name": "MPAC_MNL_01_promotoria", "municipio": "Mâncio Lima"},
    {"code": "RBR3", "sensor_index": 31109, "name": "MPAC_RBR", "municipio": "Rio Branco"},
    {"code": "FIJ1", "sensor_index": 25551, "name": "MPAC_FIJ_01_promotoria", "municipio": "Feijó"},
    {"code": "SNM2", "sensor_index": 31095, "name": "MPAC_SNM_02_promotoria", "municipio": "Sena Madureira"},
]

# PurpleAir v1 (formato da documentação: `fields` + `data`).
PA_DESCOBERTA = {
    "api_version": "V1.0.14-0.0.58",
    "time_stamp": 1790195100,
    "data_time_stamp": 1790195080,
    "location_type": 0,
    "max_age": 3600,
    "fields": ["sensor_index", "last_seen", "name", "latitude", "longitude"],
    "data": [
        [25531, 1790195000, "MPAC_MNL_01_promotoria", -7.613, -72.903],  # já vem pela UFAC e pela RedeAr
        [999001, 1790195000, "SEMA_DCAM_07", -3.1, -60.02],  # Manaus: só pela PurpleAir
        [999002, 1790195000, "Sao Paulo", -23.5, -46.6],  # fora da Amazônia Legal
    ],
}
PA_LEITURAS = {
    "api_version": "V1.0.14-0.0.58",
    "time_stamp": 1790195100,
    "fields": ["sensor_index", "last_seen", "pm2.5_atm_a", "pm2.5_atm_b"],
    "data": [[999001, 1790195040, 60.2, 58.4]],
}


class ApiFalsa:
    """As APIs de verdade, com saldo da PurpleAir que desce e registro dos pedidos."""

    def __init__(self, saldo=999_000, gasto=500, redear_fora=False, openaq=None):
        self.saldo = saldo
        self.gasto = gasto
        self.redear_fora = redear_fora
        self.openaq = openaq or {}
        self.pedidos = []

    def __call__(self, url, **kw):
        self.pedidos.append((url, kw.get("cabecalhos")))
        if "hmg.api.redear" in url and self.redear_fora:
            raise ar.urllib.error.URLError("hmg fora")
        if "acrequalidadedoar" in url and url.endswith("/sensors"):
            return CADASTRO2
        if url.endswith("/readings/latest-by-sensor"):
            return amostra("ufac-latest.json")
        if "api.purpleair.com/v1/organization" in url:
            return {"api_version": "V1.0.14", "organization_id": "x", "remaining_points": self.saldo}
        if "api.purpleair.com/v1/sensors" in url:
            self.saldo -= self.gasto
            return PA_LEITURAS if "show_only=" in url else PA_DESCOBERTA
        if "api.openaq.org/v3/locations?" in url:
            return {"meta": {"found": len(self.openaq.get("locais", []))}, "results": self.openaq.get("locais", [])}
        if "api.openaq.org/v3/locations/" in url and url.endswith("/latest"):
            lid = int(url.split("/locations/")[1].split("/")[0])
            return {"results": self.openaq.get("ultimas", {}).get(lid, [])}
        return falso_baixar_json(url, **kw)


def local_openaq(lid, nome, lat, lon, sensor, monitor=False):
    """Um local de /v3/locations (formato da documentação)."""
    return {
        "id": lid,
        "name": nome,
        "locality": None,
        "isMobile": False,
        "isMonitor": monitor,
        "owner": {"id": 4, "name": "Dono do sensor"},
        "provider": {"id": 66, "name": "AirGradient"},
        "coordinates": {"latitude": lat, "longitude": lon},
        "sensors": [
            {"id": sensor, "name": "pm25 µg/m³", "parameter": {"id": 2, "name": "pm25", "units": "µg/m³"}},
            {"id": sensor + 1, "name": "relativehumidity %", "parameter": {"id": 98, "name": "relativehumidity", "units": "%"}},
        ],
    }


class TestCorrecao(unittest.TestCase):
    def test_lrapa_bate_com_o_corrigido_da_ufac(self):
        # Pares medidos em 23/09/2026 (bruto da RedeAr × `pm2_5_corrected` da UFAC, mesmo sensor e instante).
        for a, b, ufac in ((10, 11.3, 4.665), (7.1, 7.4, 2.965), (4.4, 5.2, 1.74), (11.7, 13.8, 5.715), (13.5, 11.2, 5.515)):
            pm, fl = ar.corrigir_ab(a, b)
            self.assertAlmostEqual(pm, ufac, delta=0.006)
            self.assertEqual(fl, 0)

    def test_sem_negativo(self):
        self.assertEqual(ar.corrigir_ab(0.5, 0.5), (0.0, 0))

    def test_checagem_ab_da_epa(self):
        # Canal A morto (0) e B em 35: descartada.
        self.assertEqual(ar.corrigir_ab(0, 35.1), (None, ar.FL_AB))
        self.assertEqual(ar.corrigir_ab(20, 8.4), (None, ar.FL_AB))
        # Diferença relativa grande mas de poucos µg/m³: vale (as duas condições da EPA).
        self.assertEqual(ar.corrigir_ab(0, 4)[1], 0)
        # Diferença grande em µg/m³ mas pequena em proporção: vale.
        self.assertEqual(ar.corrigir_ab(115.4, 125.7)[1], 0)

    def test_um_canal_so(self):
        self.assertEqual(ar.corrigir_ab(None, 6.1), (2.39, 1))
        self.assertEqual(ar.corrigir_ab(5.7, None), (2.19, 2))
        self.assertEqual(ar.corrigir_ab(None, None), (None, None))
        # Lixo não é leitura.
        self.assertEqual(ar.corrigir_ab(-1, 99999), (None, None))
        self.assertEqual(ar.corrigir_ab(True, "3"), (None, None))


class TestLugarEDono(unittest.TestCase):
    def test_municipio_pela_coordenada(self):
        self.assertEqual(ar.municipio_em(-60.02, -3.1), ("Manaus", "AM"))
        # O cadastro da RedeAr diz Belém/PA; a coordenada é do Amapá.
        self.assertEqual(ar.municipio_em(-51.028, 0.284), ("Macapá", "AP"))
        self.assertEqual(ar.municipio_em(-46.6, -23.5), (None, None))

    def test_dono_pelo_nome(self):
        casos = {
            "SEMA_DCAM_12": "SEMA-AM",
            "SEMAS_034_CAM5": "SEMAS-PA",
            "SEMA-IFMT-GUARANTÃ DO NORTE": "SEMA-MT / IFMT",
            "SEMA-MT SEDE": "SEMA-MT",
            "UEA_EducAIR_31": "UEA (EducAIR)",
            "(IPAM) Medicilândia": "IPAM",
            "Tefé IDSM-IPAM": "IPAM",
            "MPAC_RBR_01": "MPAC",
            "PROMOTORIA_CPX_01_prefeitura": "MPAC",
            "INPA Roraima": "INPA",
            "Guarajá-Mirim (ICMBio)": "ICMBio",
            "1_RedeAr_UFAC_Floresta": "UFAC",
            "Vale do Javari": None,
        }
        for nome, dono in casos.items():
            self.assertEqual(ar.dono_por_nome(nome), dono, nome)

    def test_redear_so_amazonia_legal_e_temperatura_em_celsius(self):
        lidos = ar.ler_redear(amostra("redear-sensors.json"), ar.carregar_anel())
        # Brasília (próprio da RedeAr) e a escola do DF ficam de fora.
        self.assertEqual(sorted(lidos, key=int), ["4", "25531", "25551", "31095", "129339", "161259", "242087"])
        meta, leituras = lidos["25531"]
        self.assertTrue(meta["pa"])
        self.assertFalse(lidos["4"][0]["pa"])
        # PurpleAir manda °F: 105 °F = 40,6 °C.
        cru = next(x for x in amostra("redear-sensors.json") if x["sensor_id"] == 25531)["readings"][0]
        self.assertEqual(leituras[0][3]["temp"], round((cru["bme_temperature"] - 32) * 5 / 9, 1))
        # O próprio da RedeAr (BME280) já vem em °C.
        cru4 = next(x for x in amostra("redear-sensors.json") if x["sensor_id"] == 4)["readings"][0]
        self.assertEqual(lidos["4"][1][0][3]["temp"], round(cru4["bme_temperature"], 1))


class TestFontesNovas(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = ApiFalsa()
        self.patches = [
            mock.patch.object(ar, "RAIZ", self.dir),
            mock.patch.object(ar, "PASTA", os.path.join(self.dir, "ar", "v1")),
            mock.patch.object(ar, "ESTADO", os.path.join(self.dir, "estado.json")),
            mock.patch.object(ar, "CAMS_BRUTO", os.path.join(self.dir, "cams-bruto.json")),
            mock.patch.object(ar, "CHAVE_PURPLEAIR", os.path.join(self.dir, "purpleair-key")),
            mock.patch.object(ar, "CHAVE_OPENAQ", os.path.join(self.dir, "openaq-key")),
            mock.patch.object(ar, "CONF", os.path.join(self.dir, "ar.conf")),
            mock.patch.object(ar, "baixar_json", side_effect=lambda url, **kw: self.api(url, **kw)),
            mock.patch.object(ar.time, "sleep"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)

    def ler(self):
        with open(os.path.join(self.dir, "ar", "v1", "pontos.json")) as f:
            return json.load(f)

    def chave(self, nome, texto="CHAVE-DE-TESTE"):
        with open(os.path.join(self.dir, f"{nome}-key"), "w") as f:
            f.write(texto + "\n")

    def sensores(self):
        return {s["id"]: s for s in self.ler()["sensores"]["lista"]}

    def test_rodada_sem_chaves_junta_ufac_redear_e_airgradient(self):
        r = ar.rodada(AGORA2)
        self.assertEqual(r["falhas"], 0)
        p = self.ler()
        ids = [s["id"] for s in p["sensores"]["lista"]]
        self.assertEqual(len(ids), len(set(ids)), "dois símbolos no mesmo sensor")
        s = self.sensores()
        # Na UFAC e na RedeAr: um ponto só, da UFAC, com a série completada pela RedeAr.
        mnl = s[25531]
        self.assertEqual(mnl["fonte"], "ufac")
        self.assertEqual(mnl["tambem"], ["redear"])
        self.assertEqual(mnl["dono"], "MPAC")
        self.assertGreaterEqual(sum(v is not None for v in mnl["serie"]), 3)
        self.assertIn("a", mnl["ab"])
        # A RedeAr descarta (A/B discordam), mas o valor publicado pela UFAC continua no ponto.
        fij = s[25551]
        self.assertIsNotNone(fij["ult"]["pm"])
        self.assertEqual(fij["ab"]["fl"], ar.FL_AB)
        # Próprio da RedeAr: id na faixa dela, município pela coordenada, série da API.
        q = s[900000004]
        self.assertEqual((q["fonte"], q["mun"], q["uf"], q["pa"], q["ref"]), ("redear", "Macapá", "AP", False, 4))
        self.assertGreaterEqual(sum(v is not None for v in q["serie"]), 3)
        self.assertTrue(any("/sensors/4/readings?" in u for u, _ in self.api.pedidos))
        # Os espelhados da PurpleAir não têm série na API da RedeAr: não se pede.
        self.assertFalse(any("/sensors/161259/readings" in u for u, _ in self.api.pedidos))
        self.assertNotIn(900000001, s)  # Brasília
        self.assertNotIn(137722, s)  # DF
        # Só na RedeAr (PurpleAir espelhado): dono pelo nome, município pela coordenada.
        uea = s[161259]
        self.assertEqual((uea["fonte"], uea["dono"], uea["mun"], uea["uf"], uea["pa"]), ("redear", "UEA (EducAIR)", "Manaus", "AM", True))
        # Um canal só (Poconé: o A não veio).
        self.assertEqual(s[242087]["ult"]["fl"], 1)
        # AirGradient: só o de Imperatriz é da Amazônia Legal; a mesma correção sobre o pm02.
        ag = [x for x in s.values() if x["fonte"] == "airgradient"]
        self.assertEqual([(x["id"], x["mun"], x["uf"]) for x in ag], [(910174298, "Imperatriz", "MA")])
        self.assertEqual(ag[0]["ab"]["a"], 4.7)
        self.assertEqual(ag[0]["ult"]["pm"], 1.69)
        # Sem chave, as fontes pagas nem são chamadas, e o painel sabe por quê.
        self.assertEqual(p["fontes"]["purpleair"]["nota"], "sem chave")
        self.assertEqual(p["fontes"]["openaq"]["nota"], "sem chave")
        self.assertFalse(any("purpleair" in u or "openaq" in u for u, _ in self.api.pedidos))
        self.assertIsNone(p["fontes"]["redear"]["erro"])

    def test_sensor_parado_no_cadastro_da_redear_sem_leitura_nao_entra(self):
        ar.rodada(AGORA2)
        # Amanã (129339) está no cadastro, sem nenhuma leitura: não há o que desenhar.
        self.assertNotIn(129339, self.sensores())

    def test_serie_acumula_entre_rodadas_sem_repetir_horario(self):
        ar.rodada(AGORA2)
        antes = sum(v is not None for v in self.sensores()[161259]["serie"])
        ar.rodada(AGORA2 + 300)
        self.assertEqual(sum(v is not None for v in self.sensores()[161259]["serie"]), antes)

    def test_redear_cai_para_a_producao(self):
        self.api.redear_fora = True
        r = ar.rodada(AGORA2)
        self.assertEqual(r["falhas"], 0)
        self.assertTrue(any(u.startswith(ar.REDEAR_PROD) for u, _ in self.api.pedidos))
        self.assertIn(161259, self.sensores())

    def test_purpleair_com_chave_so_consulta_o_que_falta(self):
        self.chave("purpleair")
        ar.rodada(AGORA2)
        pa = [(u, c) for u, c in self.api.pedidos if "api.purpleair.com" in u]
        self.assertTrue(all(c == {"X-API-Key": "CHAVE-DE-TESTE"} for _, c in pa))
        consulta = next(u for u, _ in pa if "show_only=" in u)
        # 25531 já vem pela UFAC e pela RedeAr; São Paulo está fora.
        self.assertIn("show_only=999001", consulta)
        self.assertNotIn("25531", consulta)
        self.assertIn("pm2.5_atm_a%2Cpm2.5_atm_b%2Clast_seen", consulta)
        s = self.sensores()
        self.assertNotIn(999002, s)
        m = s[999001]
        self.assertEqual((m["fonte"], m["dono"], m["mun"], m["tol"]), ("purpleair", "SEMA-AM", "Manaus", 75))
        self.assertEqual(m["ult"]["pm"], 28.99)  # 0,5 × 59,3 − 0,66
        f = self.ler()["fontes"]["purpleair"]
        # Duas consultas de 500 pontos: o gasto medido pelo saldo.
        self.assertEqual((f["saldo"], f["gastoRodada"], f["gastoDia"]), (998_000, 1000, 24_000))
        self.assertIsNone(f["erro"])
        # Na rodada seguinte (5 min depois), nada de PurpleAir: o intervalo é de 60 min.
        n = len(self.api.pedidos)
        ar.rodada(AGORA2 + 300)
        self.assertFalse(any("purpleair" in u for u, _ in self.api.pedidos[n:]))
        self.assertIn(999001, self.sensores())

    def test_purpleair_para_com_saldo_baixo(self):
        self.chave("purpleair")
        self.api.saldo = 40_000
        ar.rodada(AGORA2)
        self.assertFalse(any("purpleair.com/v1/sensors" in u for u, _ in self.api.pedidos))
        f = self.ler()["fontes"]["purpleair"]
        self.assertIn("suspensas", f["nota"])
        self.assertEqual(f["saldo"], 40_000)

    def test_intervalo_configuravel(self):
        with open(ar.CONF, "w") as f:
            f.write("# comentário\nPURPLEAIR_INTERVALO_MIN = 30\nPURPLEAIR_SALDO_MIN=1000\nOUTRA=5\n")
        conf = ar.ler_conf()
        self.assertEqual((conf["PURPLEAIR_INTERVALO_MIN"], conf["PURPLEAIR_SALDO_MIN"], conf["OPENAQ_INTERVALO_MIN"]), (30, 1000, 60))

    def test_chave_ilegivel_vira_aviso(self):
        if os.geteuid() == 0:
            self.skipTest("root lê tudo")
        self.chave("purpleair")
        os.chmod(os.path.join(self.dir, "purpleair-key"), 0)
        ar.rodada(AGORA2)
        self.assertIn("sem permissão", self.ler()["fontes"]["purpleair"]["nota"])

    def test_chave_recusada_fica_como_erro(self):
        self.chave("purpleair")

        def recusa(url, **kw):
            if "purpleair" in url:
                raise RuntimeError('HTTP 403: {"error": "ApiKeyInvalidError"}')
            return self.api(url, **kw)

        with mock.patch.object(ar, "baixar_json", side_effect=recusa):
            r = ar.rodada(AGORA2)
        self.assertEqual(r["falhas"], 1)
        self.assertIn("403", self.ler()["fontes"]["purpleair"]["erro"])

    def test_openaq_com_chave_e_sem_repetir_o_airgradient(self):
        self.chave("openaq")
        self.api.openaq = {
            "locais": [
                # O mesmo aparelho de Imperatriz, repassado pela OpenAQ (a ~50 m).
                local_openaq(3000001, "Imperatriz", -5.5240, -47.4780, 11),
                local_openaq(5009964, "Manaus-5009964", -3.08, -60.0, 21),
                local_openaq(3000002, "Lima", -12.0, -77.0, 31),
            ],
            "ultimas": {
                3000001: [{"datetime": {"utc": "2026-09-23T20:00:00Z"}, "value": 4.0, "sensorsId": 11}],
                5009964: [
                    {"datetime": {"utc": "2026-09-23T20:00:00Z"}, "value": 30.0, "sensorsId": 21},
                    {"datetime": {"utc": "2026-09-23T20:00:00Z"}, "value": 80.0, "sensorsId": 22},
                ],
            },
        }
        ar.rodada(AGORA2)
        s = self.sensores()
        oaq = [x for x in s.values() if x["fonte"] == "openaq"]
        self.assertEqual([x["id"] for x in oaq], [920000000 + 5009964])
        self.assertEqual(oaq[0]["ult"]["pm"], 14.34)  # 0,5 × 30 − 0,66
        self.assertEqual(oaq[0]["ab"]["a"], 30.0)
        self.assertEqual(oaq[0]["tol"], 90)
        self.assertIn("AirGradient", oaq[0]["dono"])
        self.assertIn("openaq", s[910174298]["tambem"])
        self.assertTrue(all(c == {"X-API-Key": "CHAVE-DE-TESTE"} for u, c in self.api.pedidos if "openaq" in u))

    def test_juntar_prefere_a_ordem_e_nunca_duplica(self):
        base = {"lat": -3.1, "lon": -60.0, "fl": [], "nome": None, "mun": None, "uf": None, "dono": None}
        ufac = dict(base, id=1, fonte="ufac", serie=[1.0, None, None], ult={"t": 10, "pm": 1.0, "fl": 0})
        red = dict(base, id=1, fonte="redear", serie=[9.0, 2.0, None], ult={"t": 20, "pm": 2.0, "fl": 0}, nome="x")
        pa = dict(base, id=1, fonte="purpleair", serie=[None, None, 3.0], ult={"t": 30, "pm": None, "fl": 4})
        out = ar.juntar_sensores({"purpleair": [pa], "redear": [red], "ufac": [ufac]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["fonte"], "ufac")
        self.assertEqual(out[0]["serie"], [1.0, 2.0, 3.0])
        # A mais recente COM valor: a descartada da PurpleAir não apaga a da RedeAr.
        self.assertEqual(out[0]["ult"]["pm"], 2.0)
        self.assertEqual(out[0]["tambem"], ["redear", "purpleair"])
        self.assertEqual(out[0]["nome"], "x")


if __name__ == "__main__":
    unittest.main()
