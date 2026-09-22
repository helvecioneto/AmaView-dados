"""
Testes do servidor de blocos, sem rede: o quadro do STAR é um JPEG sintético
gerado pela própria TurboJPEG.

  python3 -m unittest discover -s agendador/blocos
  (no Mac: BLOCOS_LIBTURBOJPEG=/opt/homebrew/opt/jpeg-turbo/lib/libturbojpeg.dylib)
"""
import ctypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import blocos  # noqa: E402


def jpeg_sintetico(largura: int, altura: int) -> bytes:
    """JPEG 4:2:0 com um degradê (o mesmo subamostramento do STAR)."""
    lib = blocos._carregar_turbojpeg()
    lib.tjInitCompress.restype = ctypes.c_void_p
    lib.tjCompress2.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    linha = bytes(v for x in range(largura) for v in (x % 256, (x // 16) % 256, 128))
    pixels = b"".join(linha[:] if y % 2 else linha for y in range(altura))
    saida = ctypes.POINTER(ctypes.c_ubyte)()
    tamanho = ctypes.c_ulong()
    h = lib.tjInitCompress()
    try:
        assert lib.tjCompress2(h, pixels, largura, 0, altura, 0, ctypes.byref(saida), ctypes.byref(tamanho), 2, 80, 0) == 0
        return ctypes.string_at(saida, tamanho.value)
    finally:
        lib.tjFree(saida)
        lib.tjDestroy(h)


QUADRO_3600 = jpeg_sintetico(3600, 2160)


def carimbo_recente(minutos: int = 40) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutos)
    return blocos.carimbo_de(t.replace(minute=t.minute - t.minute % 10))


class Grade(unittest.TestCase):
    def test_carimbo_ida_e_volta(self):
        t = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(blocos.carimbo_de(t), "20262642330")
        self.assertEqual(blocos.instante("20262642330"), t)
        with self.assertRaises(ValueError):
            blocos.instante("20264002330")

    def test_grade_dos_blocos(self):
        self.assertEqual(blocos.grade(7200), (9, 15))
        self.assertEqual(blocos.grade(3600), (5, 8))

    def test_regiao_com_sobra_so_onde_ha_vizinho(self):
        self.assertEqual(blocos.regiao(3600, 0, 0), (0, 0, 528, 528))
        self.assertEqual(blocos.regiao(3600, 2, 3), (1520, 1008, 544, 544))
        self.assertEqual(blocos.regiao(3600, 4, 7), (3568, 2032, 32, 128))

    def test_recorte_sem_perda_com_bordas_menores(self):
        regioes = [blocos.regiao(3600, r, c) for r in (0, 2, 4) for c in (0, 3, 7)]
        partes = blocos.recortar(QUADRO_3600, regioes)
        self.assertEqual(
            [blocos.dimensoes(p) for p in partes],
            [(528, 528), (544, 528), (32, 528), (528, 544), (544, 544), (32, 544), (528, 128), (544, 128), (32, 128)],
        )


class Servidor(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        blocos.PASTA = os.path.join(self.raiz, "blocos", "v1", "nsa")
        blocos.RAIZ = self.raiz
        self.baixados = []
        self.ausentes = set()

        def baixar(url):
            self.baixados.append(url)
            if any(a in url for a in self.ausentes):
                raise blocos.Ausente(url)
            return QUADRO_3600

        self._baixar = blocos.baixar
        blocos.baixar = baixar
        self.srv = blocos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), blocos.Pedido)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        blocos.baixar = self._baixar
        shutil.rmtree(self.raiz)

    def pedir(self, caminho):
        try:
            with urllib.request.urlopen(self.base + caminho) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_corta_na_hora_e_depois_serve_do_disco(self):
        c = carimbo_recente()
        codigo, cab, corpo = self.pedir(f"/blocos/v1/nsa/13/{c}/3600/2_3.jpg")
        self.assertEqual(codigo, 200)
        self.assertEqual(cab["Access-Control-Allow-Origin"], "*")
        self.assertIn("immutable", cab["Cache-Control"])
        self.assertEqual(blocos.dimensoes(corpo), (544, 544))
        self.assertEqual(len(os.listdir(blocos.pasta_do_quadro("13", c, 3600))), 40)
        self.assertTrue(self.baixados[0].endswith(f"/13/{c}_GOES19-ABI-nsa-13-3600x2160.jpg"))
        codigo, _, corpo = self.pedir(f"/blocos/v1/nsa/13/{c}/3600/4_7.jpg")
        self.assertEqual((codigo, blocos.dimensoes(corpo)), (200, (32, 128)))
        self.assertEqual(len(self.baixados), 1, "o quadro é baixado e cortado uma vez só")

    def test_pedidos_simultaneos_cortam_uma_vez(self):
        c = carimbo_recente()
        fios = [threading.Thread(target=self.pedir, args=(f"/blocos/v1/nsa/13/{c}/3600/{i % 5}_{i % 8}.jpg",)) for i in range(12)]
        [f.start() for f in fios]
        [f.join() for f in fios]
        self.assertEqual(len(self.baixados), 1)

    def test_recusa_o_que_nao_conhece(self):
        c = carimbo_recente()
        for caminho in (
            f"/blocos/v1/nsa/XX/{c}/3600/0_0.jpg",  # produto
            f"/blocos/v1/nsa/13/{c}/1800/0_0.jpg",  # largura
            f"/blocos/v1/nsa/13/{c}/3600/5_0.jpg",  # fora da grade
            "/blocos/v1/nsa/13/20260010000/3600/0_0.jpg",  # fora das 48 h
            "/blocos/v1/nsa/13/../../etc/passwd",
        ):
            self.assertEqual(self.pedir(caminho)[0], 404, caminho)
        self.assertEqual(self.baixados, [])

    def test_quadro_que_o_star_ainda_nao_publicou(self):
        c = carimbo_recente(5)
        self.ausentes.add(c)
        codigo, cab, _ = self.pedir(f"/blocos/v1/nsa/GEOCOLOR/{c}/3600/0_0.jpg")
        self.assertEqual(codigo, 404)
        self.assertEqual(cab["Cache-Control"], "public, max-age=60")
        self.assertFalse(os.path.exists(blocos.pasta_do_quadro("GEOCOLOR", c, 3600)))

    def test_saude(self):
        codigo, _, corpo = self.pedir("/blocos/saude")
        self.assertEqual(codigo, 200)
        self.assertTrue(json.loads(corpo)["ok"])

    def test_limpeza_tira_o_que_saiu_da_janela(self):
        velho = blocos.carimbo_de(datetime.now(timezone.utc) - timedelta(hours=50))
        novo = carimbo_recente()
        for c in (velho, novo):
            os.makedirs(blocos.pasta_do_quadro("13", c, 3600))
        os.makedirs(blocos.pasta_do_quadro("13", novo, 7200) + ".parcial-1-2")
        antigo = datetime.now().timestamp() - 3600
        os.utime(blocos.pasta_do_quadro("13", novo, 7200) + ".parcial-1-2", (antigo, antigo))
        self.assertEqual(blocos.limpar(datetime.now(timezone.utc)), 1)
        self.assertEqual(os.listdir(os.path.join(blocos.PASTA, "13")), [novo])
        self.assertEqual(os.listdir(os.path.join(blocos.PASTA, "13", novo)), ["3600"])


class Atrasados(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        blocos.PASTA = os.path.join(self.raiz, "blocos", "v1", "nsa")
        for m in (blocos._ausentes, blocos._publicados, blocos._sondados):
            m.clear()
        self._existe = blocos._existe_no_star

    def tearDown(self):
        blocos._existe_no_star = self._existe
        for m in (blocos._ausentes, blocos._publicados, blocos._sondados):
            m.clear()
        shutil.rmtree(self.raiz)

    def test_espera_por_idade_e_publicacao(self):
        self.assertEqual(blocos.espera_ausente(timedelta(minutes=30)), 300)
        self.assertEqual(blocos.espera_ausente(timedelta(hours=3)), 1800)
        self.assertEqual(blocos.espera_ausente(timedelta(hours=10)), 3 * 3600)
        # Horário já publicado: o produto que falta chega em minutos.
        self.assertEqual(blocos.espera_ausente(timedelta(hours=3), publicado=True), 60)

    def test_horario_atrasado_publicado_libera_todos_os_produtos(self):
        agora = datetime.now(timezone.utc)
        atrasado = blocos.carimbo_de((agora - timedelta(hours=2)).replace(minute=0))
        outro = blocos.carimbo_de((agora - timedelta(hours=3)).replace(minute=0))
        for chave in (("13", atrasado, 7200), ("AirMass", atrasado, 3600), ("13", outro, 7200)):
            blocos._ausentes[chave] = time.time() + 3600
        sondados = []
        publicados = {atrasado}
        blocos._existe_no_star = lambda c: sondados.append(c) or c in publicados
        self.assertEqual(blocos.sondar_publicados(agora), [atrasado])
        self.assertNotIn(("13", atrasado, 7200), blocos._ausentes)
        self.assertNotIn(("AirMass", atrasado, 3600), blocos._ausentes)
        self.assertIn(("13", outro, 7200), blocos._ausentes)
        # Cada horário faltante é sondado no máximo uma vez por minuto.
        n = len(sondados)
        blocos.sondar_publicados(agora)
        self.assertEqual(len(sondados), n)
        self.assertLessEqual(n, 37)

    def test_ultimos_por_produto(self):
        for p, c in (("GEOCOLOR", "20262651200"), ("GEOCOLOR", "20262651220"), ("13", "20262650940")):
            os.makedirs(blocos.pasta_do_quadro(p, c, 7200))
        os.makedirs(blocos.pasta_do_quadro("13", "20262651230", 7200) + ".parcial-1-2")
        self.assertEqual(blocos.ultimos(), {"GEOCOLOR": "20262651220", "13": "20262650940"})


if __name__ == "__main__":
    unittest.main()
