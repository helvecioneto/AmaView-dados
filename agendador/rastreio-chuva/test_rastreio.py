"""
Testes do rastreio da chuva, sem o pyForTraCC: só as partes que não dependem
dele (horários, janela, movimento, área, publicação e limpeza).

  python3 -m unittest discover -s agendador/rastreio-chuva
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import rastreio  # noqa: E402


class Pastas(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.gsmap = self.tmp / "gsmap"
        (self.gsmap / "quadros").mkdir(parents=True)
        self.publica = self.tmp / "publica"
        self.dados = self.tmp / "dados"
        for nome, valor in (("GSMAP", self.gsmap), ("DADOS", self.dados),
                            ("PUBLICA", self.publica / "rastreio-chuva" / "v1")):
            p = mock.patch.object(rastreio, nome, valor)
            p.start()
            self.addCleanup(p.stop)

    def indice_gsmap(self, quadros, criar=True):
        with open(self.gsmap / "indice.json", "w") as f:
            json.dump({"quadros": [{"c": c, "p": p} for c, p in quadros]}, f)
        if criar:
            for c, p in quadros:
                (self.gsmap / "quadros" / f"{c}-{p}.bin").write_bytes(b"\0")

    def publicar(self, c):
        pasta = rastreio.PUBLICA / "quadros"
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / f"{c}.geojson").write_text('{"type":"FeatureCollection","features":[]}')


class TestHorarios(Pastas):
    def test_le_o_indice_e_ignora_o_que_nao_tem_arquivo(self):
        self.indice_gsmap([("202611021200", "g"), ("202611021230", "s")])
        (self.gsmap / "indice.json").write_text(json.dumps({"quadros": [
            {"c": "202611021200", "p": "g"}, {"c": "202611021230", "p": "s"},
            {"c": "202611021300", "p": "g"},  # sem arquivo
            {"c": "x", "p": "g"}, {"c": "202611021330", "p": "?"}]}))
        h = rastreio.horarios_do_gsmap()
        self.assertEqual(sorted(h), ["202611021200", "202611021230"])
        self.assertEqual(h["202611021230"][1], "s")

    def test_sem_indice_nao_ha_horario(self):
        self.assertEqual(rastreio.horarios_do_gsmap(), {})
        self.assertFalse(rastreio.precisa())

    def test_precisa_so_com_horario_mais_novo_que_o_publicado(self):
        self.indice_gsmap([("202611021200", "g"), ("202611021230", "g")])
        self.assertTrue(rastreio.precisa())
        self.publicar("202611021230")
        self.assertFalse(rastreio.precisa())
        self.indice_gsmap([("202611021230", "g"), ("202611021300", "s")])
        self.assertTrue(rastreio.precisa())

    def test_novos_anda_so_para_a_frente_e_respeita_a_janela(self):
        h = {c: None for c in ("202611010000", "202611021200", "202611021230", "202611021300")}
        self.assertEqual(rastreio.novos(h, "202611021200", "202611011300"), ["202611021230", "202611021300"])
        self.assertEqual(rastreio.novos(h, None, "202611011300"), ["202611021200", "202611021230", "202611021300"])

    def test_ultimo_rastreado_vem_do_estado_do_pyfortracc(self):
        self.assertIsNone(rastreio.ultimo_rastreado())
        estado = self.dados / "saida/track/state"
        estado.mkdir(parents=True)
        (estado / "state.json").write_text(json.dumps({"last_stamp": "2026-11-02 23:30:00"}))
        self.assertEqual(rastreio.ultimo_rastreado(), "202611022330")


class TestContas(unittest.TestCase):
    def test_area_de_um_ponto_no_equador(self):
        self.assertEqual(rastreio.area_km2(1, 0), round(0.1 * 110.574 * 0.1 * 111.320))
        self.assertLess(rastreio.area_km2(100, 20), rastreio.area_km2(100, 0))
        self.assertIsNone(rastreio.area_km2(float("nan"), 0))

    def test_movimento_para_leste_e_para_o_sul(self):
        # 0,1° por meia hora no equador: ~22 km/h.
        m = rastreio.movimento(0.1, 0, 0)
        self.assertEqual(m["rumo"], 90)
        self.assertEqual(m["vel"], round(0.1 * 111.320 * 2))
        self.assertEqual(rastreio.movimento(0, -0.1, 0)["rumo"], 180)
        self.assertEqual(rastreio.movimento(None, 0, 0), {"vel": None, "rumo": None})

    def test_coordenadas_em_duas_casas(self):
        g = rastreio.arredondar({"type": "Polygon", "coordinates": [[[-77.20000000001, -21.9499999], [1, 2]]]})
        self.assertEqual(g["coordinates"], [[[-77.2, -21.95], [1, 2]]])

    def test_grade_nas_bordas(self):
        nl = rastreio.name_list("/e", "/s", "/t")
        self.assertAlmostEqual((nl["lon_max"] - nl["lon_min"]) / nl["x_dim"], 0.1)
        self.assertAlmostEqual((nl["lat_max"] - nl["lat_min"]) / nl["y_dim"], 0.1)
        self.assertAlmostEqual(nl["lat_max"], 22.1)
        self.assertAlmostEqual(nl["lon_min"], -89.4)
        self.assertEqual(nl["thresholds"], [0.1])
        self.assertEqual(nl["operator"], ">=")


class TestPublicacao(Pastas):
    def test_gravar_comprimido_poe_o_gz_ao_lado(self):
        import gzip

        out = rastreio.PUBLICA / "quadros" / "202611021200.geojson"
        rastreio.gravar_json(out, {"a": 1}, comprimido=True)
        self.assertEqual(json.loads(out.read_text()), {"a": 1})
        self.assertEqual(json.loads(gzip.decompress((out.parent / (out.name + ".gz")).read_bytes())), {"a": 1})
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o644")

    def test_indice_resume_os_quadros(self):
        self.indice_gsmap([("202611021200", "s")])
        pasta = rastreio.PUBLICA / "quadros"
        rastreio.gravar_json(pasta / "202611021200.geojson", {"type": "FeatureCollection", "features": [
            {"properties": {"k": "c", "km2": 100, "pmax": 3.5}},
            {"properties": {"k": "c", "km2": 50, "pmax": None}},
            {"properties": {"k": "t"}}]})
        rastreio.indice(rastreio.horarios_do_gsmap())
        i = json.loads((rastreio.PUBLICA / "indice.json").read_text())
        self.assertEqual(i["limiar_mmh"], 0.1)
        self.assertEqual(i["quadros"], [{"c": "202611021200", "p": "s", "n": 2, "km2": 150, "pmax": 3.5}])

    def test_limpar_apaga_o_que_saiu_da_janela(self):
        velho = (datetime.utcnow() - timedelta(hours=49)).strftime("%Y%m%d%H%M")
        novo = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y%m%d%H%M")
        pasta = rastreio.PUBLICA / "quadros"
        for c in (velho, novo):
            rastreio.gravar_json(pasta / f"{c}.geojson", {}, comprimido=True)
        tabelas = self.dados / "saida/track/trackingtable"
        tabelas.mkdir(parents=True)
        t_velha = (datetime.utcnow() - timedelta(hours=14)).strftime("%Y%m%d_%H%M")
        t_nova = (datetime.utcnow() - timedelta(hours=12)).strftime("%Y%m%d_%H%M")
        for t in (t_velha, t_nova):
            (tabelas / f"{t}.parquet").write_bytes(b"")
        rastreio.limpar()
        self.assertEqual(sorted(f.name for f in pasta.iterdir()), [f"{novo}.geojson", f"{novo}.geojson.gz"])
        self.assertEqual([f.stem for f in tabelas.iterdir()], [t_nova])


if __name__ == "__main__":
    unittest.main()
