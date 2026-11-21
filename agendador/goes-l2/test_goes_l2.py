"""
Testes das medições do GOES-19, sem rede: os NetCDF são sintéticos (mesmos
nomes e atributos que importam, grade mais grossa) e o S3 é trocado por
dicionários.

  python3 -I -m unittest discover -s agendador/goes-l2
"""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import goes_l2  # noqa: E402

# Grade de 10 km do ABI (1086 × 1086), a do LST, TPW e CAPE.
N10 = 1086
PASSO10 = 0.00028
OFFSET10 = 0.1519


def ler_png(dado: bytes) -> np.ndarray:
    """Decodificador mínimo (8 bits, cinza ou RGB), para conferir o codificador sem o Pillow."""
    assert dado[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(dado):
        n = struct.unpack(">I", dado[pos : pos + 4])[0]
        tipo, corpo = dado[pos + 4 : pos + 8], dado[pos + 8 : pos + 8 + n]
        assert struct.unpack(">I", dado[pos + 8 + n : pos + 12 + n])[0] == zlib.crc32(tipo + corpo) & 0xFFFFFFFF
        if tipo == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", corpo)
        elif tipo == b"IDAT":
            idat += corpo
        pos += 12 + n
    w, h, prof, cor = ihdr[:4]
    assert prof == 8 and cor in (0, 2)
    c = 1 if cor == 0 else 3
    bruto = zlib.decompress(idat)
    lin = w * c
    out = np.zeros((h, lin), np.uint8)
    ant = np.zeros(lin, np.int32)
    for r in range(h):
        f = bruto[r * (lin + 1)]
        x = np.frombuffer(bruto, np.uint8, lin, r * (lin + 1) + 1).astype(np.int32)
        cur = np.zeros(lin, np.int32)
        for i in range(lin):
            a = cur[i - c] if i >= c else 0
            b = ant[i]
            d = ant[i - c] if i >= c else 0
            if f == 0:
                v = x[i]
            elif f == 1:
                v = x[i] + a
            elif f == 2:
                v = x[i] + b
            elif f == 3:
                v = x[i] + (a + b) // 2
            else:
                p = a + b - d
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - d)
                v = x[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else d)
            cur[i] = v & 0xFF
        out[r] = cur
        ant = cur
    return out.reshape(h, w) if c == 1 else out.reshape(h, w, 3)


def netcdf_sintetico(var="TPW", dqf="DQF_Overall", valores=None, dqf_valores=None, escala=0.00152602, desloc=0.0,
                     n=N10, passo=PASSO10, offset=OFFSET10) -> bytes:
    """Produto L2 de disco completo: `valores` (n × n, uint16 crus) e o DQF."""
    import h5py

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        x = f.create_dataset("x", data=np.arange(n, dtype=np.int16))
        x.attrs["scale_factor"] = np.array([passo], np.float32)
        x.attrs["add_offset"] = np.array([-offset], np.float32)
        y = f.create_dataset("y", data=np.arange(n, dtype=np.int16))
        y.attrs["scale_factor"] = np.array([-passo], np.float32)
        y.attrs["add_offset"] = np.array([offset], np.float32)
        v = f.create_dataset(var, data=valores if valores is not None else np.full((n, n), 65535, np.uint16), chunks=(181, 181), compression="gzip")
        v.attrs["_FillValue"] = np.array([65535], np.uint16)
        v.attrs["scale_factor"] = np.array([escala], np.float32)
        v.attrs["add_offset"] = np.array([desloc], np.float32)
        d = f.create_dataset(dqf, data=dqf_valores if dqf_valores is not None else np.zeros((n, n), np.uint8), chunks=(181, 181), compression="gzip")
        d.attrs["_FillValue"] = np.array([255], np.uint8)
        p = f.create_dataset("goes_imager_projection", data=0, dtype=np.int32)
        for k, val in goes_l2.GEOS.items():
            p.attrs[k] = np.array([val])
    return buf.getvalue()


class Png(unittest.TestCase):
    def test_cinza_ida_e_volta(self):
        rng = np.random.default_rng(1)
        img = rng.integers(0, 256, (37, 53), dtype=np.uint8)
        img[10:20] = 255  # trechos lisos escolhem outro filtro
        np.testing.assert_array_equal(ler_png(goes_l2.png(img)), img)

    def test_rgb_ida_e_volta(self):
        rng = np.random.default_rng(2)
        img = np.dstack([np.cumsum(rng.integers(0, 3, (21, 30)), axis=1) % 256, rng.integers(0, 4, (21, 30)), np.zeros((21, 30))]).astype(np.uint8)
        np.testing.assert_array_equal(ler_png(goes_l2.png(img)), img)

    def test_forma_invalida(self):
        with self.assertRaises(ValueError):
            goes_l2.png(np.zeros((4, 4, 4), np.uint8))


class Geometria(unittest.TestCase):
    def test_subponto(self):
        x, y = goes_l2.lonlat_para_geos(np.array([-75.0]), np.array([0.0]))
        self.assertAlmostEqual(float(x[0]), 0.0, places=9)
        self.assertAlmostEqual(float(y[0]), 0.0, places=9)

    def test_manaus(self):
        # A mesma referência do teste da fumaça (conferida com o pyproj).
        x, y = goes_l2.lonlat_para_geos(np.array([-60.02]), np.array([-3.10]))
        self.assertAlmostEqual(float(x[0]), 0.045680, delta=0.0001)
        self.assertAlmostEqual(float(y[0]), -0.009514, delta=0.0001)

    def test_recorte_da_amazonia_dentro_do_nsa(self):
        a = goes_l2.angulos_do_recorte("amazonia")
        nsa = goes_l2.NSA
        self.assertGreaterEqual(a["xMin"], nsa["xMin"])
        self.assertLessEqual(a["xMax"], nsa["xMax"])
        self.assertGreaterEqual(a["yMin"], nsa["yMin"])
        self.assertLessEqual(a["yMax"], nsa["yMax"])
        # ~1/3 da área do setor (o motivo do recorte).
        frac = (a["xMax"] - a["xMin"]) * (a["yMax"] - a["yMin"]) / ((nsa["xMax"] - nsa["xMin"]) * (nsa["yMax"] - nsa["yMin"]))
        self.assertTrue(0.2 < frac < 0.45, frac)
        # As quinas da Amazônia Legal (com folga) cabem na caixa.
        o, s, l, n = goes_l2.AMAZONIA_LEGAL
        x, y = goes_l2.lonlat_para_geos(np.array([o, l, o, l]), np.array([s, s, n, n]))
        self.assertTrue(((x >= a["xMin"]) & (x <= a["xMax"]) & (y >= a["yMin"]) & (y <= a["yMax"])).all())

    def test_nsa_sem_recorte(self):
        self.assertEqual(goes_l2.angulos_do_recorte("nsa"), goes_l2.NSA)


class Processar(unittest.TestCase):
    def test_tpw_codifica_valor_e_qualidade(self):
        v = np.full((N10, N10), 65535, np.uint16)
        q = np.full((N10, N10), 255, np.uint8)
        # Um pixel no meio do NSA: x = 0.06 rad, y = 0 → coluna/linha da grade de 10 km.
        col = int(round((0.06 + OFFSET10) / PASSO10))
        lin = int(round(OFFSET10 / PASSO10))
        v[lin, col] = round(40.0 / 0.00152602)  # 40 mm
        q[lin, col] = 3  # degradado (DQF_Overall)
        v[lin, col + 1] = round(10.0 / 0.00152602)
        q[lin, col + 1] = 5  # inválido
        p = goes_l2.PRODUTOS["tpw"]
        imagem, grade, cob = goes_l2.processar(p, netcdf_sintetico(valores=v, dqf_valores=q))
        img = ler_png(imagem)
        self.assertEqual(img.shape, (grade["altura"], grade["largura"], 3))
        # O setor NSA na grade de 10 km: 688 × 432, como o protótipo (a leste, o NSA passa da borda do disco).
        self.assertEqual((grade["largura"], grade["altura"]), (688, 432))
        validos = np.argwhere(img[:, :, 0] != 255)
        self.assertEqual(len(validos), 2)
        r, c = validos[0]
        self.assertEqual(int(img[r, c, 0]), 100)  # 40 mm / 0,4
        self.assertEqual(int(img[r, c, 1]), 2)
        self.assertEqual(int(img[r, c + 1, 0]), 25)
        self.assertEqual(int(img[r, c + 1, 1]), 3)
        self.assertTrue((img[:, :, 2] == 0).all())
        self.assertEqual(cob, round(2 / (688 * 432), 4))
        # Caixa em metros na projeção (ângulo × altura do satélite), nas bordas dos pixels.
        h = goes_l2.GEOS["perspective_point_height"]
        x = np.arange(N10) * float(np.float32(PASSO10)) + float(np.float32(-OFFSET10))
        dentro = x[(x >= goes_l2.NSA["xMin"]) & (x <= goes_l2.NSA["xMax"])]
        self.assertAlmostEqual(grade["caixa"][0], (dentro[0] - PASSO10 / 2) * h, delta=1)
        self.assertAlmostEqual(grade["caixa"][2], (dentro[-1] + PASSO10 / 2) * h, delta=1)

    def test_lst_kelvin_e_aod_recortado(self):
        n, passo, off = 2712, 0.000112, 0.151816  # grade de 4 km (só para o teste ficar leve)
        v = np.full((n, n), 65535, np.uint16)
        # Manaus a 30 °C.
        col = int(round((0.045680 + off) / passo))
        lin = int(round((off + 0.009514) / passo))
        v[lin, col] = round((303.15 - 190.0) / 0.0025)
        lst = goes_l2.PRODUTOS["lst"]
        img = ler_png(goes_l2.processar(lst, netcdf_sintetico("LST", "DQF", v, np.zeros((n, n), np.uint8), 0.0025, 190.0, n, passo, off))[0])
        r, c = np.argwhere(img[:, :, 0] != 255)[0]
        self.assertEqual(int(img[r, c, 0]), 160)  # (30 − (−10)) / 0,25
        # O AOD sai só na caixa da Amazônia Legal: menor que o NSA, com Manaus dentro.
        aod = goes_l2.PRODUTOS["aod"]
        v2 = np.full((n, n), 65535, np.uint16)
        v2[lin, col] = round((0.5 + 0.05) / 7.706e-05)
        imagem, grade, cob = goes_l2.processar(aod, netcdf_sintetico("AOD", "DQF", v2, np.ones((n, n), np.uint8), 7.706e-05, -0.05, n, passo, off))
        _, gnsa, _ = goes_l2.processar(lst, netcdf_sintetico("LST", "DQF", v, None, 0.0025, 190.0, n, passo, off))
        self.assertLess(grade["largura"] * grade["altura"], 0.45 * gnsa["largura"] * gnsa["altura"])
        img = ler_png(imagem)
        r, c = np.argwhere(img[:, :, 0] != 255)[0]
        self.assertEqual(int(img[r, c, 0]), 25)  # 0,5 / 0,02
        self.assertEqual(int(img[r, c, 1]), 1)


class Rodada(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.antes = (goes_l2.RAIZ, goes_l2.PASTA, goes_l2.ESTADO)
        goes_l2.RAIZ = self.dir
        goes_l2.PASTA = os.path.join(self.dir, "goes-l2", "v1")
        goes_l2.ESTADO = os.path.join(self.dir, "estado.json")
        v = np.full((N10, N10), 65535, np.uint16)
        v[400:420, 600:620] = 20000
        self.nc = netcdf_sintetico(valores=v)
        self.agora = datetime(2026, 11, 21, 15, 4, tzinfo=timezone.utc)

    def tearDown(self):
        goes_l2.RAIZ, goes_l2.PASTA, goes_l2.ESTADO = self.antes
        shutil.rmtree(self.dir, ignore_errors=True)

    def chaves(self, horas=3):
        """carimbo → chave do TPW de 10 em 10 min nas últimas `horas`."""
        out = {}
        t = self.agora.replace(minute=0) - timedelta(hours=horas)
        while t <= self.agora - timedelta(minutes=14):
            c = goes_l2.carimbo_de(t)
            out[c] = f"ABI-L2-TPWF/{t:%Y}/{c[4:7]}/{t:%H}/OR_ABI-L2-TPWF-M6_G19_s{c}217_e{c}526_c{c}540.nc"
            t += timedelta(minutes=10)
        return out

    def rodar(self, chaves, falhar=()):
        def listar(p, prefixo):
            return {c: k for c, k in chaves.items() if k.startswith(prefixo)}

        def baixar(url):
            if any(c in url for c in falhar):
                raise OSError("quebrado")
            return self.nc

        with mock.patch.object(goes_l2, "listar", side_effect=listar), mock.patch.object(goes_l2, "baixar", side_effect=baixar):
            return goes_l2.rodada(self.agora, [goes_l2.PRODUTOS["tpw"]])["tpw"]

    def indice(self):
        with open(os.path.join(goes_l2.PASTA, "tpw", "indice.json")) as f:
            return json.load(f)

    def test_indice_no_formato_do_app(self):
        chaves = self.chaves(1)
        r = self.rodar(chaves)
        self.assertEqual(r["novos"], len(chaves))
        ind = self.indice()
        for campo in ("gerado", "produto", "unidade", "escala", "deslocamento", "semValor", "grade", "quadros"):
            self.assertIn(campo, ind)
        self.assertEqual(ind["produto"], "tpw")
        self.assertEqual(ind["escala"], 0.4)
        self.assertEqual(ind["semValor"], 255)
        self.assertEqual(len(ind["grade"]["caixa"]), 4)
        self.assertEqual([q["c"] for q in ind["quadros"]], sorted(chaves))
        for q in ind["quadros"]:
            self.assertEqual(q["arquivo"], f"{q['c']}.png")
            self.assertTrue(os.path.exists(os.path.join(goes_l2.PASTA, "tpw", q["arquivo"])))
            self.assertGreater(q["cob"], 0)

    def test_teto_por_rodada_do_mais_novo_para_o_mais_velho(self):
        chaves = self.chaves(6)
        r = self.rodar(chaves)
        self.assertEqual(r["novos"], goes_l2.PRODUTOS["tpw"].max_por_rodada)
        self.assertEqual(r["fila"], len(chaves) - r["novos"])
        feitos = [q["c"] for q in self.indice()["quadros"]]
        self.assertEqual(feitos, sorted(chaves)[-len(feitos):])  # os mais novos primeiro
        r = self.rodar(chaves)  # a fila continua na próxima rodada
        self.assertEqual(r["quadros"], len(chaves))

    def test_apaga_o_que_sai_das_48_h(self):
        self.rodar(self.chaves(1))
        velho = goes_l2.carimbo_de(self.agora - timedelta(hours=49))
        pasta = os.path.join(goes_l2.PASTA, "tpw")
        with open(os.path.join(pasta, f"{velho}.png"), "wb") as f:
            f.write(b"x")
        ind = self.indice()
        ind["quadros"].insert(0, {"c": velho, "arquivo": f"{velho}.png", "cob": 0.5})
        with open(os.path.join(pasta, "indice.json"), "w") as f:
            json.dump(ind, f)
        r = self.rodar(self.chaves(1))
        self.assertEqual(r["removidos"], 1)
        self.assertNotIn(velho, [q["c"] for q in self.indice()["quadros"]])
        self.assertFalse(os.path.exists(os.path.join(pasta, f"{velho}.png")))

    def test_falha_espera_e_nao_derruba(self):
        chaves = self.chaves(1)
        ruim = sorted(chaves)[-1]
        r = self.rodar(chaves, falhar=(ruim,))
        self.assertEqual(r["falhas"], 1)
        self.assertEqual(r["novos"], len(chaves) - 1)
        r = self.rodar(chaves)  # dentro dos 30 min de espera: nem tenta
        self.assertEqual((r["novos"], r["falhas"]), (0, 0))

    def test_grade_nova_apaga_os_quadros_antigos(self):
        chaves = self.chaves(1)
        self.rodar(chaves)
        pasta = os.path.join(goes_l2.PASTA, "tpw")
        ind = self.indice()
        ind["grade"]["largura"] += 1  # como se o recorte tivesse mudado
        with open(os.path.join(pasta, "indice.json"), "w") as f:
            json.dump(ind, f)
        novo = goes_l2.carimbo_de(self.agora - timedelta(minutes=4))
        chaves[novo] = chaves[sorted(chaves)[-1]].replace(sorted(chaves)[-1], novo)
        r = self.rodar(chaves)
        self.assertEqual(r["quadros"], 1)
        self.assertEqual([q["c"] for q in self.indice()["quadros"]], [novo])
        self.assertEqual(sorted(os.listdir(pasta)), sorted([f"{novo}.png", "indice.json"]))

    def test_png_sem_indice_volta_para_a_fila(self):
        chaves = self.chaves(1)
        self.rodar(chaves)
        os.remove(os.path.join(goes_l2.PASTA, "tpw", "indice.json"))
        r = self.rodar(chaves)
        self.assertEqual(r["novos"], len(chaves))
        self.assertEqual(r["quadros"], len(chaves))


if __name__ == "__main__":
    unittest.main()
