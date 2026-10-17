"""
Testes da previsão, sem rede e sem libeccodes: os campos são sintéticos, e o
ADS e o ECMWF são trocados por funções.

  python3 -m unittest discover -s agendador/previsao
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import previsao  # noqa: E402

UTC = timezone.utc


def campo_global(valor_de, passo=0.25):
    """Campo global como a libeccodes devolve: norte em cima, lon de −180 a 180."""
    lats = 90 - np.arange(round(180 / passo) + 1) * passo
    lons = -180 + np.arange(round(360 / passo)) * passo
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    return {"valores": valor_de(la, lo), "lats": lats, "lons": lons}


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = mock.patch.multiple(
            previsao,
            RAIZ=self.dir,
            PASTA=os.path.join(self.dir, "previsao", "v1"),
            ESTADO=os.path.join(self.dir, "estado.json"),
        )
        self.p.start()
        os.makedirs(previsao.PASTA)

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)


class Grade(unittest.TestCase):
    def test_dimensoes_fecham_em_blocos(self):
        for modelo in ("ifs", "cams"):
            ni, nj, b = previsao.dimensoes(modelo)
            self.assertEqual((ni % b, nj % b), (0, 0), modelo)
            # Os dois modelos têm os mesmos blocos.
            self.assertEqual((ni // b, nj // b), (14, 17))

    def test_recorte_pega_o_ponto_certo(self):
        c = campo_global(lambda la, lo: la * 1000 + lo)
        r = previsao.recortar(c, "ifs")
        self.assertEqual(r.shape, (112, 136))
        self.assertAlmostEqual(float(r[0, 0]), 8 * 1000 - 76)
        # Linha 44 = 3°S; coluna 64 = 60°W.
        self.assertAlmostEqual(float(r[44, 64]), -3 * 1000 - 60)

    def test_recorte_com_longitude_0_360(self):
        lats = 90 - np.arange(721) * 0.25
        lons = np.arange(1440) * 0.25  # 0..359,75, como em algumas grades do ECMWF
        lons_norm = (lons + 180) % 360 - 180
        la, lo = np.meshgrid(lats, lons_norm, indexing="ij")
        r = previsao.recortar({"valores": la * 1000 + lo, "lats": lats, "lons": lons_norm}, "ifs")
        self.assertAlmostEqual(float(r[0, 0]), 8 * 1000 - 76)

    def test_recorte_recusa_grade_que_nao_cobre(self):
        c = campo_global(lambda la, lo: la, passo=1.0)
        c["lats"] = c["lats"][:50]  # só o hemisfério norte
        c["valores"] = c["valores"][:50]
        with self.assertRaises(ValueError):
            previsao.recortar(c, "cams")


class Conversoes(unittest.TestCase):
    def test_quantizar_e_sem_dado(self):
        q = previsao.quantizar([25.0, np.nan, -100.0, 1e9], -60.0, 0.01)
        self.assertEqual(q.dtype.str, "<u2")
        self.assertEqual(q[0], 8500)
        self.assertEqual(q[1], previsao.SEM_DADO)
        self.assertEqual(q[2], 0)  # abaixo da escala: o mínimo, nunca "sem dado"
        self.assertEqual(q[3], previsao.SEM_DADO - 1)

    def test_umidade_saturada_e_seca(self):
        self.assertAlmostEqual(float(previsao.umidade_relativa(np.array(30.0), np.array(30.0))), 100.0, places=3)
        ur = float(previsao.umidade_relativa(np.array(35.0), np.array(15.0)))
        self.assertTrue(28 < ur < 32, ur)  # ~30% (tabela psicrométrica)

    def test_chuva_e_a_diferenca_dos_acumulados(self):
        ni, nj, _ = previsao.dimensoes("ifs")
        um = np.ones((ni, nj), np.float32)
        brutos = {
            0: {"2t": um * 300, "2d": um * 290, "tp": um * 0.0, "10u": um, "10v": um, "tcc": um * 0.5},
            3: {"2t": um * 301, "2d": um * 290, "tp": um * 0.004, "10u": um, "10v": um, "10fg": um * 7, "tcc": um},
            6: {"2t": um * 302, "2d": um * 290, "tp": um * 0.0039, "10u": um, "10v": um, "10fg": um * 8, "tcc": um},
        }
        d = previsao.converter_ifs(brutos)
        self.assertTrue(np.isnan(d["chuva"][0]).all())  # passo 0: sem intervalo
        self.assertAlmostEqual(float(d["chuva"][1, 0, 0]), 4.0, places=3)
        self.assertEqual(float(d["chuva"][2, 0, 0]), 0.0)  # nunca negativa
        self.assertAlmostEqual(float(d["t2m"][2, 5, 5]), 28.85, places=3)
        self.assertTrue(np.isnan(d["rajada"][0]).all())  # 10fg não existe no passo 0
        self.assertAlmostEqual(float(d["nuvens"][0, 0, 0]), 50.0)

    def test_queimadas_e_a_soma_om_bc(self):
        ni, nj, _ = previsao.dimensoes("cams")
        um = np.ones((ni, nj), np.float32)
        d = previsao.converter_cams({0: {"omaod550": um * 0.4, "bcaod550": um * 0.05, "aod550": um * 0.6, "pm2p5": um * 35e-9}})
        self.assertAlmostEqual(float(d["bbaod"][0, 1, 1]), 0.45, places=5)
        self.assertAlmostEqual(float(d["pm25"][0, 1, 1]), 35.0, places=3)


def grib2_falso(n_corpo: int) -> bytes:
    total = 16 + n_corpo + 4
    return b"GRIB" + b"\0\0\0" + bytes([2]) + total.to_bytes(8, "big") + b"x" * n_corpo + b"7777"


class Mensagens(unittest.TestCase):
    def test_fatia_grib1_e_grib2(self):
        g1 = b"GRIB" + (12).to_bytes(3, "big") + bytes([1]) + b"7777"
        dado = b"lixo" + grib2_falso(10) + g1 + grib2_falso(3)
        msgs = list(previsao.mensagens(dado))
        self.assertEqual([len(m) for m in msgs], [30, 12, 23])

    def test_para_em_mensagem_truncada(self):
        self.assertEqual(len(list(previsao.mensagens(grib2_falso(10)[:-5]))), 0)


class Publicacao(Temp):
    def dados_cams(self):
        ni, nj, _ = previsao.dimensoes("cams")
        n = len(previsao.CAMS_PASSOS)
        base = np.arange(ni * nj, dtype=np.float32).reshape(ni, nj) / 1e4
        # Dentro da escala de todas as variáveis (o AOD vai até 6,55).
        return {v: np.stack([base + k * 0.01 for k in range(n)]) for v in previsao.VARS["cams"]}

    def test_mapa_e_blocos(self):
        dados = self.dados_cams()
        previsao.publicar("cams", "2026101700", previsao.CAMS_PASSOS, dados)
        raiz = os.path.join(previsao.PASTA, "cams", "2026101700")
        ni, nj, b = previsao.dimensoes("cams")
        mapa = np.fromfile(os.path.join(raiz, "mapa", "bbaod", "024.bin"), "<u2").reshape(ni, nj)
        k24 = previsao.CAMS_PASSOS.index(24)
        self.assertAlmostEqual(mapa[3, 7] * 0.0001, float(dados["bbaod"][k24, 3, 7]), places=4)
        self.assertEqual(len(os.listdir(os.path.join(raiz, "serie"))), 14 * 17)
        # Bloco (2, 5): linhas 10–14, colunas 25–29; ordem [var][passo][linha][coluna].
        bloco = np.fromfile(os.path.join(raiz, "serie", "02_05.bin"), "<u2")
        nv, ns = len(previsao.VARS["cams"]), len(previsao.CAMS_PASSOS)
        self.assertEqual(bloco.size, nv * ns * b * b)
        bloco = bloco.reshape(nv, ns, b, b)
        iv = list(previsao.VARS["cams"]).index("pm25")
        self.assertAlmostEqual(bloco[iv, k24, 1, 2] * 0.1, float(dados["pm25"][k24, 11, 27]), delta=0.05)
        self.assertFalse(os.path.exists(raiz + ".parcial"))

    def test_limpar_guarda_as_ultimas(self):
        for r in ("2026101500", "2026101512", "2026101600", "2026101612"):
            os.makedirs(os.path.join(previsao.PASTA, "ifs", r))
        previsao.limpar("ifs")
        self.assertEqual(sorted(os.listdir(os.path.join(previsao.PASTA, "ifs"))), ["2026101512", "2026101600", "2026101612"])

    def test_indice_so_com_o_que_esta_no_disco(self):
        os.makedirs(os.path.join(previsao.PASTA, "cams", "2026101700"))
        estado = {"cams": {"faixas": {"2026101700": {"bbaod": [0, 3]}}, "pedido": {"rodada": "2026101712"}}}
        i = previsao.montar_indice(estado, datetime(2026, 10, 17, 18, tzinfo=UTC))
        self.assertEqual(i["modelos"]["cams"]["rodadas"], ["2026101700"])
        self.assertEqual(i["modelos"]["cams"]["fila"], "2026101712")
        self.assertEqual(i["modelos"]["ifs"]["rodadas"], [])
        self.assertEqual(i["modelos"]["ifs"]["grade"]["bloco"], 8)
        self.assertEqual(i["modelos"]["cams"]["vars"]["pm25"]["esc"], 0.1)


class Rodadas(unittest.TestCase):
    def test_candidatas(self):
        r = previsao.rodadas_candidatas(datetime(2026, 10, 17, 9, 30, tzinfo=UTC), 3)
        self.assertEqual([previsao.nome_rodada(t) for t in r], ["2026101700", "2026101612", "2026101600"])
        r = previsao.rodadas_candidatas(datetime(2026, 10, 17, 12, 0, tzinfo=UTC), 1)
        self.assertEqual(previsao.nome_rodada(r[0]), "2026101712")


class Cams(Temp):
    """A máquina de estados do pedido ao ADS: pedir, esperar a fila, baixar."""

    def setUp(self):
        super().setUp()
        chave = os.path.join(self.dir, "ads-key")
        with open(chave, "w") as f:
            f.write("url: https://ads.atmosphere.copernicus.eu/api\nkey: abc\n")
        self.pc = mock.patch.object(previsao, "CHAVE_ADS", chave)
        self.pc.start()
        self.agora = datetime(2026, 10, 17, 14, tzinfo=UTC)

    def tearDown(self):
        self.pc.stop()
        super().tearDown()

    def test_sem_chave(self):
        with mock.patch.object(previsao, "CHAVE_ADS", "/nao/existe"):
            st = {}
            self.assertEqual(previsao.cams_andar(st, self.agora, None), "sem chave")
            self.assertIn("sem chave", st["erro"])

    def test_pede_espera_e_publica(self):
        st = {}
        chamadas = []

        def ads(caminho, chave, corpo=None):
            chamadas.append(caminho)
            if caminho.endswith("/execution"):
                self.assertEqual(corpo["inputs"]["date"], ["2026-10-17/2026-10-17"])
                self.assertEqual(corpo["inputs"]["time"], ["12:00"])
                return {"jobID": "j1", "status": "accepted"}
            if caminho == "/jobs/j1":
                return {"status": ads.status}
            if caminho == "/jobs/j1/results":
                return {"asset": {"value": {"href": "https://x/arquivo.grib"}}}
            raise AssertionError(caminho)

        ads.status = "running"
        with mock.patch.object(previsao, "ads", ads):
            r = previsao.cams_andar(st, self.agora, None)
            self.assertIn("2026101712 na fila", r)
            self.assertEqual(st["pedido"]["job"], "j1")
            # Na próxima olhada, pronto: baixa, lê e publica.
            ads.status = "successful"
            ni, nj, _ = previsao.dimensoes("cams")
            dados = {v: np.zeros((len(previsao.CAMS_PASSOS), ni, nj), np.float32) for v in previsao.VARS["cams"]}
            with mock.patch.object(previsao, "baixar", return_value=b"grib"), mock.patch.object(
                previsao, "cams_ler", return_value=(previsao.CAMS_PASSOS, dados)
            ):
                r = previsao.cams_andar(st, self.agora, None)
        self.assertEqual(r, "2026101712 publicada")
        self.assertNotIn("pedido", st)
        self.assertEqual(st["feitas"], ["2026101712"])
        self.assertTrue(os.path.isdir(os.path.join(previsao.PASTA, "cams", "2026101712", "serie")))
        self.assertEqual(chamadas.count(f"/processes/{previsao.ADS_CONJUNTO}/execution"), 1)
        # Em dia: não pede de novo a mesma rodada nem uma mais velha.
        with mock.patch.object(previsao, "ads", side_effect=AssertionError("não devia chamar")):
            self.assertEqual(previsao.cams_andar(st, self.agora, None), "em dia")

    def test_rodada_ainda_fora_do_ads_pede_a_anterior(self):
        st = {}
        tentadas = []

        def ads(caminho, chave, corpo=None):
            if caminho.endswith("/execution"):
                tentadas.append(corpo["inputs"]["time"][0])
                if corpo["inputs"]["time"] == ["12:00"]:
                    raise previsao.urllib.error.HTTPError(caminho, 400, "x", {}, io.BytesIO(b"date not available"))
                return {"jobID": "j00", "status": "accepted"}
            return {"status": "accepted"}

        with mock.patch.object(previsao, "ads", ads):
            r = previsao.cams_andar(st, self.agora, None)
        self.assertEqual(tentadas, ["12:00", "00:00"])
        self.assertIn("2026101700 na fila", r)
        self.assertIn("2026101712", st["falhas"])
        self.assertIn("date not available", st["erro"])

    def test_falha_espera_antes_de_tentar_de_novo(self):
        st = {"pedido": {"rodada": "2026101712", "job": "j9", "desde": time.time()}}

        def ads(caminho, chave, corpo=None):
            if caminho == "/jobs/j9":
                return {"status": "failed"}
            raise previsao.urllib.error.HTTPError(caminho, 400, "x", {}, None)

        with mock.patch.object(previsao, "ads", ads):
            r = previsao.cams_andar(st, self.agora, None)
        self.assertIn("falhou", r)
        self.assertIn("2026101712", st["falhas"])
        # A rodada que falhou fica de fora por meia hora; a anterior pode ser pedida.
        pedidos = []

        def ads2(caminho, chave, corpo=None):
            if caminho.endswith("/execution"):
                pedidos.append(corpo["inputs"]["time"][0])
                return {"jobID": "j10", "status": "accepted"}
            return {"status": "accepted"}

        with mock.patch.object(previsao, "ads", ads2):
            previsao.cams_andar(st, self.agora, None)
        self.assertEqual(pedidos, ["00:00"])

    def test_pedido_esquecido_na_fila_e_refeito(self):
        st = {"pedido": {"rodada": "2026101712", "job": "velho", "desde": time.time() - 7 * 3600}}

        def ads(caminho, chave, corpo=None):
            if caminho.endswith("/execution"):
                return {"jobID": "novo", "status": "accepted"}
            self.assertEqual(caminho, "/jobs/novo")
            return {"status": "accepted"}

        with mock.patch.object(previsao, "ads", ads):
            previsao.cams_andar(st, self.agora, None)
        self.assertEqual(st["pedido"]["job"], "novo")


class Rede(unittest.TestCase):
    def test_429_espera_e_tenta_de_novo(self):
        respostas = [
            previsao.urllib.error.HTTPError("u", 429, "x", {"Retry-After": "3"}, None),
            previsao.urllib.error.HTTPError("u", 429, "x", {}, None),
        ]

        class Ok:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"dado"

        def urlopen(pedido, timeout):
            if respostas:
                raise respostas.pop(0)
            return Ok()

        esperas = []
        with mock.patch.object(previsao.urllib.request, "urlopen", urlopen), mock.patch.object(previsao.time, "sleep", esperas.append):
            self.assertEqual(previsao.baixar("https://x"), b"dado")
        self.assertEqual(esperas, [3.0, 30.0])

    def test_400_nao_insiste(self):
        def urlopen(pedido, timeout):
            raise previsao.urllib.error.HTTPError("u", 400, "x", {}, None)

        with mock.patch.object(previsao.urllib.request, "urlopen", urlopen), mock.patch.object(previsao.time, "sleep", lambda s: None):
            with self.assertRaises(previsao.urllib.error.HTTPError):
                previsao.baixar("https://x")


class Ifs(Temp):
    def test_escolhe_a_rodada_mais_nova_pronta(self):
        st = {"feitas": ["2026101612"]}
        prontas = {"2026101700"}
        ni, nj, _ = previsao.dimensoes("ifs")
        dados = {v: np.zeros((2, ni, nj), np.float32) for v in previsao.VARS["ifs"]}
        with mock.patch.object(previsao, "ifs_pronta", lambda t: previsao.nome_rodada(t) in prontas), mock.patch.object(
            previsao, "ifs_rodada", return_value=([0, 3], dados)
        ) as rod:
            r = previsao.ifs_andar(st, datetime(2026, 10, 17, 15, tzinfo=UTC), None)
        self.assertEqual(r, "2026101700 publicada")
        self.assertEqual(previsao.nome_rodada(rod.call_args[0][0]), "2026101700")
        self.assertEqual(st["feitas"], ["2026101612", "2026101700"])
        # Feita a 00, nada mais novo pronto: não baixa de novo.
        with mock.patch.object(previsao, "ifs_pronta", lambda t: previsao.nome_rodada(t) in prontas), mock.patch.object(
            previsao, "ifs_rodada", side_effect=AssertionError("não devia baixar")
        ):
            self.assertEqual(previsao.ifs_andar(st, datetime(2026, 10, 17, 15, tzinfo=UTC), None), "em dia")

    def test_falha_marca_a_rodada(self):
        st = {}
        with mock.patch.object(previsao, "ifs_pronta", lambda t: True), mock.patch.object(
            previsao, "ifs_rodada", side_effect=OSError("rede caiu")
        ):
            with self.assertRaises(OSError):
                previsao.ifs_andar(st, datetime(2026, 10, 17, 15, tzinfo=UTC), None)
        self.assertIn("2026101712", st["falhas"])
        self.assertIn("rede caiu", st["erro"])


if __name__ == "__main__":
    unittest.main()
