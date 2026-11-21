"""
Testes do risco de fogo, sem rede: o NetCDF4 (HDF5) e o NetCDF3 são
sintéticos (mesma grade e atributos que importam) e o INPE é trocado por um
dicionário.

  python3 -I -m unittest discover -s agendador/risco-fogo
"""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zlib
from datetime import datetime, timezone
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import risco_fogo  # noqa: E402

# A grade dos dias sem chuva do INPE (0,1°): lon −114,95…−30,05, lat −60,05…29,95.
LON3 = np.round(-114.95 + 0.1 * np.arange(850), 2)
LAT3 = np.round(-60.05 + 0.1 * np.arange(901), 2)
# A do risco é de 0,01°; aqui uma mais grossa (0,05°), que basta para o recorte.
LON4 = np.round(-90.0 + 0.05 * np.arange(1000), 3)
LAT4 = np.round(-40.0 + 0.05 * np.arange(1000), 3)


def ler_png(dado: bytes) -> np.ndarray:
    """PNG em tons de cinza (8 bits) → array, desfazendo os filtros (sem o Pillow)."""
    pos, idat = 8, b""
    while pos < len(dado):
        n = struct.unpack(">I", dado[pos : pos + 4])[0]
        tipo, corpo = dado[pos + 4 : pos + 8], dado[pos + 8 : pos + 8 + n]
        if tipo == b"IHDR":
            w, h, _, cor = struct.unpack(">IIBB", corpo[:10])
            assert cor == 0
        elif tipo == b"IDAT":
            idat += corpo
        pos += 12 + n
    bruto = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, w + 1)
    out = np.zeros((h, w), np.int32)
    ant = np.zeros(w, np.int32)
    for r in range(h):
        f, x = bruto[r, 0], bruto[r, 1:].astype(np.int32)
        cur = np.zeros(w, np.int32)
        for i in range(w):
            a = cur[i - 1] if i else 0
            b, d = ant[i], (ant[i - 1] if i else 0)
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
    return out.astype(np.uint8)


def netcdf3(valores: np.ndarray, lon=LON3, lat=LAT3, var="ndsc120") -> bytes:
    """NetCDF3 clássico como o do INPE: time ilimitado (1 registro), lat, lon e a variável (time, lat, lon)."""
    def s(texto: str) -> bytes:
        b = texto.encode()
        return struct.pack(">i", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)

    def attr_f(nome, v):
        return s(nome) + struct.pack(">ii", 5, 1) + struct.pack(">f", v)

    def attr_c(nome, v):
        b = v.encode()
        return s(nome) + struct.pack(">ii", 2, len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)

    nlat, nlon = len(lat), len(lon)
    dims = struct.pack(">ii", 10, 3) + s("time") + struct.pack(">i", 0) + s("lat") + struct.pack(">i", nlat) + s("lon") + struct.pack(">i", nlon)
    gatts = struct.pack(">ii", 12, 1) + attr_c("title", "teste")
    # Variáveis: lat, lon (fixas, double), a principal e time (de registro).
    tam_var = nlat * nlon * 4
    specs = [
        ("lat", [1], 6, nlat * 8, []),
        ("lon", [2], 6, nlon * 8, []),
        (var, [0, 1, 2], 5, tam_var, [attr_f("_FillValue", -999.0), attr_c("units", "dias")]),
        ("time", [0], 6, 8, []),
    ]

    def cabecalho(inicios):
        out = b"CDF\x01" + struct.pack(">i", 1) + dims + gatts + struct.pack(">ii", 11, len(specs))
        for (nome, ids, tipo, vsize, atts), ini in zip(specs, inicios):
            out += s(nome) + struct.pack(">i", len(ids)) + b"".join(struct.pack(">i", i) for i in ids)
            out += (struct.pack(">ii", 12, len(atts)) + b"".join(atts)) if atts else struct.pack(">ii", 0, 0)
            out += struct.pack(">iii", tipo, vsize, ini)
        return out

    tam = len(cabecalho([0, 0, 0, 0]))
    ini_lat = tam
    ini_lon = ini_lat + nlat * 8
    ini_var = ini_lon + nlon * 8  # registros: a principal e depois o time
    ini_time = ini_var + tam_var
    corpo = np.asarray(lat, ">f8").tobytes() + np.asarray(lon, ">f8").tobytes()
    corpo += np.asarray(valores, ">f4").tobytes() + np.array([1.0], ">f8").tobytes()
    return cabecalho([ini_lat, ini_lon, ini_var, ini_time]) + corpo


def netcdf4(valores: np.ndarray, lon=LON4, lat=LAT4) -> bytes:
    import h5py

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        f.create_dataset("lon", data=lon)
        f.create_dataset("lat", data=lat)
        f.create_dataset("time", data=np.array([0.0]))
        v = f.create_dataset("rf", data=valores[None].astype(np.float32), chunks=(1, 1, len(lon)), compression="gzip")
        v.attrs["_FillValue"] = np.array([-999.0], np.float32)
    return buf.getvalue()


class NetCDF3(unittest.TestCase):
    def test_le_variavel_de_registro_e_coordenadas(self):
        v = np.arange(901 * 850, dtype=np.float32).reshape(1, 901, 850) % 121
        d = risco_fogo.ler_netcdf3(netcdf3(v[0]), ["lon", "lat", "ndsc120"])
        np.testing.assert_allclose(d["lon"][0], LON3)
        np.testing.assert_allclose(d["lat"][0], LAT3)
        arr, attrs = d["ndsc120"]
        self.assertEqual(arr.shape, (1, 901, 850))
        np.testing.assert_array_equal(arr, v)
        self.assertEqual(float(attrs["_FillValue"][0]), -999.0)
        self.assertEqual(attrs["units"], "dias")

    def test_nao_e_netcdf3(self):
        with self.assertRaises(ValueError):
            risco_fogo.ler_netcdf3(b"\x89HDF\r\n\x1a\n" + b"\0" * 64, ["x"])

    def test_variavel_ausente(self):
        with self.assertRaises(ValueError):
            risco_fogo.ler_netcdf3(netcdf3(np.zeros((901, 850))), ["chuva"])


class Recorte(unittest.TestCase):
    def test_dias_sem_chuva(self):
        v = np.full((901, 850), 30.0, np.float32)
        v[LAT3 > 0, :] = -999.0  # norte do equador sem dado
        v[np.argmin(abs(LAT3 + 3.15)), np.argmin(abs(LON3 + 60.05))] = 120.7  # Manaus: satura em 120
        codigo, caixa = risco_fogo.recortar(netcdf3(v), "ndsc120", 1, 120.0)
        # Caixa da Amazônia Legal com 0,5° de folga, nas bordas dos pixels de 0,1°.
        self.assertEqual(caixa, [-74.5, -18.6, -43.4, 5.8])
        self.assertEqual(codigo.shape, (244, 311))
        self.assertEqual(int(codigo[0, 0]), 255)  # norte para cima: a 1ª linha é a do norte, sem dado
        self.assertEqual(int(codigo[-1, 0]), 30)
        self.assertEqual(int(codigo.max()), 255)
        self.assertEqual(int(codigo[codigo != 255].max()), 120)
        np.testing.assert_array_equal(ler_png(risco_fogo.png(codigo)), codigo)

    def test_risco_netcdf4(self):
        v = np.full((1000, 1000), 0.5, np.float32)
        v[:, LON4 < -60] = 0.951
        v[LAT4 < -10, :] = np.nan
        codigo, caixa = risco_fogo.recortar(netcdf4(v), "rf", 200, 1.0)
        self.assertAlmostEqual(caixa[0], -74.525, places=3)
        self.assertEqual(int(codigo[0, 0]), 190)  # 0,951 × 200 = 190,2
        self.assertEqual(int(codigo[0, -1]), 100)
        self.assertEqual(int(codigo[-1, -1]), 255)  # NaN = sem dado, nunca zero


class Rodada(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.antes = (risco_fogo.RAIZ, risco_fogo.PASTA)
        risco_fogo.RAIZ = self.dir
        risco_fogo.PASTA = os.path.join(self.dir, "risco-fogo", "v1")
        self.risco = netcdf4(np.full((1000, 1000), 0.3, np.float32))
        self.dias = netcdf3(np.full((901, 850), 12.0, np.float32))
        self.pedidos = []

    def tearDown(self):
        risco_fogo.RAIZ, risco_fogo.PASTA = self.antes
        shutil.rmtree(self.dir, ignore_errors=True)

    def rodar(self, agora, publicados):
        """`publicados` = dias (AAAAMMDD) que o INPE já tem."""
        def baixar(url):
            self.pedidos.append(url)
            dia = url[-11:-3]
            if dia not in publicados:
                return None
            return self.dias if "_NDWR_" in url else self.risco

        with mock.patch.object(risco_fogo, "baixar", side_effect=baixar):
            return risco_fogo.rodada(agora)

    def indice(self):
        with open(os.path.join(risco_fogo.PASTA, "indice.json")) as f:
            return json.load(f)

    def arquivos(self, produto):
        return sorted(os.listdir(os.path.join(risco_fogo.PASTA, produto)))

    def coerente(self):
        """Tudo o que o índice no ar cita existe no disco."""
        for p in self.indice()["produtos"].values():
            for d in p["dias"]:
                self.assertTrue(os.path.exists(os.path.join(risco_fogo.PASTA, d["arquivo"])), d["arquivo"])

    def test_formato_do_indice_e_tres_dias(self):
        # 21/11 às 4h30 de Brasília: o mais novo esperado é o dia 20.
        agora = datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc)
        self.rodar(agora, {"20261120", "20261119", "20261118", "20261117"})
        ind = self.indice()
        self.assertEqual(set(ind), {"versao", "gerado", "fonte", "produtos"})
        for produto, escala, maximo in (("risco", 200, 1.0), ("dias", 1, 120.0)):
            p = ind["produtos"][produto]
            self.assertEqual(set(p), {"grade", "escala", "semDado", "maximo", "dias"})
            self.assertEqual((p["escala"], p["semDado"], p["maximo"]), (escala, 255, maximo))
            self.assertEqual(len(p["grade"]["caixa"]), 4)
            self.assertEqual([d["data"] for d in p["dias"]], ["2026-11-18", "2026-11-19", "2026-11-20"])
            # O app usa o `arquivo` do índice; o nome leva a grade (cache imutável).
            g = risco_fogo.sufixo_da_grade(p["grade"])
            self.assertEqual(p["dias"][0]["arquivo"], f"{produto}/2026-11-18_{g}.png")
            self.assertEqual(self.arquivos(produto), [f"2026-11-{d}_{g}.png" for d in (18, 19, 20)])
        self.coerente()

    def test_dia_novo_entra_e_o_velho_sai_depois_sem_baixar_de_novo(self):
        self.rodar(datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc), {"20261120", "20261119", "20261118"})
        self.pedidos.clear()
        self.rodar(datetime(2026, 11, 22, 7, 30, tzinfo=timezone.utc), {"20261121", "20261120", "20261119", "20261118"})
        self.assertEqual(len(self.pedidos), 2)  # só o dia 21, dos dois produtos
        p = self.indice()["produtos"]["risco"]
        self.assertEqual([d["data"] for d in p["dias"]], ["2026-11-19", "2026-11-20", "2026-11-21"])
        # O dia 18 saiu do índice, mas o arquivo fica até a rodada seguinte (índice anterior).
        self.assertEqual(len(self.arquivos("risco")), 4)
        self.rodar(datetime(2026, 11, 23, 7, 30, tzinfo=timezone.utc), {"20261122", "20261121", "20261120", "20261119"})
        nomes = self.arquivos("risco")
        self.assertFalse(any(n.startswith("2026-11-18") for n in nomes))  # fora dos dois índices: apagado
        self.assertTrue(any(n.startswith("2026-11-19") for n in nomes))  # o anterior ainda o cita
        self.coerente()

    def test_inpe_atrasado_e_nova_tentativa(self):
        # 4h30: o dia 21 ainda não saiu; ficam os três anteriores.
        r = self.rodar(datetime(2026, 11, 22, 7, 30, tzinfo=timezone.utc), {"20261120", "20261119", "20261118"})
        self.assertFalse(r["em_dia"])
        self.assertEqual(self.indice()["produtos"]["dias"]["dias"][-1]["data"], "2026-11-20")
        # 7h30: saiu; entra.
        self.rodar(datetime(2026, 11, 22, 10, 30, tzinfo=timezone.utc), {"20261121", "20261120", "20261119", "20261118"})
        self.assertEqual(self.indice()["produtos"]["dias"]["dias"][-1]["data"], "2026-11-21")
        # E uma terceira rodada no mesmo dia não pede nada ao INPE.
        self.pedidos.clear()
        r = self.rodar(datetime(2026, 11, 22, 11, 0, tzinfo=timezone.utc), set())
        self.assertTrue(r["em_dia"])
        self.assertEqual(self.pedidos, [])

    def test_inpe_parado_mais_de_dez_dias_mantem_os_ultimos(self):
        self.rodar(datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc), {"20261120", "20261119", "20261118"})
        # 12 dias depois, nada novo: os três de antes continuam no índice e no disco.
        r = self.rodar(datetime(2026, 12, 3, 7, 30, tzinfo=timezone.utc), set())
        self.assertEqual(r["produtos"]["risco"]["ausentes"], risco_fogo.PROCURAR)
        for p in self.indice()["produtos"].values():
            self.assertEqual([d["data"] for d in p["dias"]], ["2026-11-18", "2026-11-19", "2026-11-20"])
        self.coerente()

    def test_dia_quebrado_nao_derruba_o_produto(self):
        risco_bom = self.risco

        def baixar(url):
            self.pedidos.append(url)
            if url.endswith("20261120.nc"):
                return b"lixo"  # o dia 20 do INPE veio quebrado
            return self.dias if "_NDWR_" in url else risco_bom

        with mock.patch.object(risco_fogo, "baixar", side_effect=baixar):
            r = risco_fogo.rodada(datetime(2026, 11, 22, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(r["produtos"]["risco"]["erros"], 1)
        # O dia novo (21) entra; o 20 fica de fora; completa com os anteriores.
        self.assertEqual([d["data"] for d in self.indice()["produtos"]["risco"]["dias"]], ["2026-11-18", "2026-11-19", "2026-11-21"])
        self.coerente()

    def test_um_produto_falhando_mantem_o_outro_e_o_anterior(self):
        agora = datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc)
        self.rodar(agora, {"20261120", "20261119", "20261118"})
        with mock.patch.object(risco_fogo, "rodada_produto", side_effect=[RuntimeError("disco"), (None, {"baixados": 0, "ausentes": 0, "erros": 0, "dias": []})]):
            r = risco_fogo.rodada(datetime(2026, 11, 22, 7, 30, tzinfo=timezone.utc))
        self.assertIn("erro", r["produtos"]["risco"])
        ind = self.indice()
        self.assertEqual(ind["produtos"]["risco"]["dias"][-1]["data"], "2026-11-20")  # o que estava no ar
        self.assertNotIn("dias", ind["produtos"])  # sem nenhum dia: sai do índice
        self.coerente()

    def test_produto_cujos_arquivos_sumiram_sai_do_indice(self):
        self.rodar(datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc), {"20261120", "20261119", "20261118"})
        shutil.rmtree(os.path.join(risco_fogo.PASTA, "dias"))
        with mock.patch.object(risco_fogo, "rodada_produto", side_effect=[(self.indice()["produtos"]["risco"], {"dias": []}), RuntimeError("x")]):
            risco_fogo.rodada(datetime(2026, 11, 22, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(set(self.indice()["produtos"]), {"risco"})
        self.coerente()

    def test_grade_nova_muda_o_nome(self):
        self.rodar(datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc), {"20261120", "20261119", "20261118"})
        antes = self.indice()["produtos"]["risco"]["dias"][-1]["arquivo"]
        self.risco = netcdf4(np.full((1000, 1000), 0.3, np.float32), lon=LON4 + 0.01)
        self.rodar(datetime(2026, 11, 22, 7, 30, tzinfo=timezone.utc), {"20261121", "20261120", "20261119", "20261118"})
        p = self.indice()["produtos"]["risco"]
        self.assertTrue(all(risco_fogo.sufixo_da_grade(p["grade"]) in d["arquivo"] for d in p["dias"]))
        self.assertNotIn(antes, [d["arquivo"] for d in p["dias"]])
        self.coerente()

    def test_parciais_velhos_sao_apagados(self):
        os.makedirs(risco_fogo.PASTA)
        velho = os.path.join(risco_fogo.PASTA, "indice.json.parcial-123")
        recente = os.path.join(risco_fogo.PASTA, "indice.json.parcial-456")
        for c in (velho, recente):
            with open(c, "w") as f:
                f.write("x")
        os.utime(velho, (time.time() - 7200, time.time() - 7200))
        self.rodar(datetime(2026, 11, 21, 7, 30, tzinfo=timezone.utc), {"20261120"})
        self.assertFalse(os.path.exists(velho))
        self.assertTrue(os.path.exists(recente))


if __name__ == "__main__":
    unittest.main()
