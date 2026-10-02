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


def decodificar(jpeg: bytes) -> bytes:
    """Pixels RGB do JPEG (pela TurboJPEG): para comparar v1 e v2 pixel a pixel."""
    lib = blocos._carregar_turbojpeg()
    lib.tjInitDecompress.restype = ctypes.c_void_p
    lib.tjDecompress2.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    w, h = blocos.dimensoes(jpeg)
    saida = ctypes.create_string_buffer(w * h * 3)
    d = lib.tjInitDecompress()
    try:
        assert lib.tjDecompress2(d, jpeg, len(jpeg), saida, w, 0, h, 0, 0) == 0
        return saida.raw
    finally:
        lib.tjDestroy(d)


def ler(caminho: str) -> bytes:
    with open(caminho, "rb") as f:
        return f.read()


def progressivo(jpeg: bytes) -> bool:
    """SOF2 (progressivo) em vez de SOF0 (baseline)."""
    return b"\xff\xc2" in jpeg[: jpeg.find(b"\xff\xda")] and b"\xff\xc0" not in jpeg[: jpeg.find(b"\xff\xda")]


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
        blocos.PASTAS = {v: os.path.join(self.raiz, "blocos", f"v{v}", "nsa") for v in blocos.VERSOES}
        blocos.RAIZ = self.raiz
        self.baixados = []
        self.ausentes = set()

        def baixar(url):
            self.baixados.append(url)
            if any(a in url for a in self.ausentes):
                raise blocos.Ausente(url)
            return QUADRO_3600

        self._baixar = blocos.baixar
        self._espera_truncado = blocos.ESPERA_TRUNCADO
        blocos.baixar = baixar
        self.srv = blocos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), blocos.Pedido)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        blocos.baixar = self._baixar
        blocos.ESPERA_TRUNCADO = self._espera_truncado
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
        self.assertEqual(len(os.listdir(blocos.pasta_do_quadro("13", c, 3600, 1))), 40)
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
        self.assertFalse(os.path.exists(blocos.pasta_do_quadro("GEOCOLOR", c, 3600, 1)))

    def test_base_ainda_nao_espelhada(self):
        c = carimbo_recente()
        status, cab, _ = self.pedir(f"/blocos/v2/nsa/13/{c}/900.jpg")
        self.assertEqual((status, cab["Cache-Control"]), (404, "public, max-age=60"))
        status, cab, _ = self.pedir(f"/blocos/v2/nsa/13/{c}/3600.jpg")
        self.assertEqual((status, cab["Cache-Control"]), (404, "no-store"))
        self.assertEqual(self.baixados, [])  # nunca vai ao STAR na hora do pedido

    def test_saude(self):
        codigo, _, corpo = self.pedir("/blocos/saude")
        self.assertEqual(codigo, 200)
        self.assertTrue(json.loads(corpo)["ok"])

    def test_v2_progressivo_com_os_mesmos_pixels_do_v1(self):
        c = carimbo_recente()
        for bloco in ("0_0", "2_3", "4_7"):
            _, _, v1 = self.pedir(f"/blocos/v1/nsa/13/{c}/3600/{bloco}.jpg")
            codigo, cab, v2 = self.pedir(f"/blocos/v2/nsa/13/{c}/3600/{bloco}.jpg")
            self.assertEqual(codigo, 200)
            self.assertIn("immutable", cab["Cache-Control"])
            self.assertFalse(progressivo(v1))
            self.assertTrue(progressivo(v2))
            self.assertEqual(decodificar(v1), decodificar(v2), bloco)
        # O v2 sai do v1 já cortado: o quadro do STAR foi baixado uma vez só.
        self.assertEqual(len(self.baixados), 1)

    def test_transicao_do_v1_sem_baixar_e_v1_velho_sai(self):
        velho = blocos.carimbo_de((datetime.now(timezone.utc) - timedelta(hours=20)).replace(minute=0))
        blocos.cortar("13", velho, 3600, versao=1)
        self.assertEqual(len(self.baixados), 1)
        v1 = {n: ler(os.path.join(blocos.pasta_do_quadro("13", velho, 3600, 1), n)) for n in ("0_0.jpg", "4_7.jpg")}
        pasta = blocos.cortar("13", velho, 3600, origem="pre")
        self.assertEqual(len(self.baixados), 1, "a transição não baixa de novo")
        for n, corpo in v1.items():
            self.assertEqual(decodificar(ler(os.path.join(pasta, n))), decodificar(corpo))
        self.assertFalse(os.path.exists(blocos.pasta_do_quadro("13", velho, 3600, 1)), "v1 com mais de 12 h sai")
        self.assertEqual(blocos._travas, {}, "nenhuma trava fica para trás")

    def test_pre_corte_recente_faz_v1_e_v2_com_um_download(self):
        recente, velho = carimbo_recente(), blocos.carimbo_de((datetime.now(timezone.utc) - timedelta(hours=8)).replace(minute=0))
        blocos._aquecer_um(("13", recente, 3600))
        blocos._aquecer_um(("13", velho, 3600))
        self.assertEqual(len(self.baixados), 2)
        self.assertTrue(os.path.isdir(blocos.pasta_do_quadro("13", recente, 3600, 1)))
        self.assertTrue(os.path.isdir(blocos.pasta_do_quadro("13", recente, 3600, 2)))
        self.assertFalse(os.path.isdir(blocos.pasta_do_quadro("13", velho, 3600, 1)), "fora das 3 h, só o v2")
        self.assertTrue(os.path.isdir(blocos.pasta_do_quadro("13", velho, 3600, 2)))

    def test_arquivo_truncado_tenta_de_novo_e_responde_503(self):
        c = carimbo_recente()
        blocos.ESPERA_TRUNCADO = 0
        tentativas = []

        def truncado(url):
            tentativas.append(url)
            return QUADRO_3600[: len(QUADRO_3600) // 2]

        blocos.baixar = truncado
        codigo, cab, _ = self.pedir(f"/blocos/v2/nsa/13/{c}/3600/0_0.jpg")
        self.assertEqual(codigo, 503)
        self.assertEqual(cab["Retry-After"], "30")
        self.assertEqual(len(tentativas), 2)
        self.assertFalse(os.path.exists(blocos.pasta_do_quadro("13", c, 3600)))

    def test_truncado_na_primeira_inteiro_na_segunda(self):
        c = carimbo_recente()
        blocos.ESPERA_TRUNCADO = 0
        respostas = [QUADRO_3600[:-5000], QUADRO_3600]
        blocos.baixar = lambda url: respostas.pop(0)
        self.assertEqual(self.pedir(f"/blocos/v2/nsa/13/{c}/3600/0_0.jpg")[0], 200)

    def test_ultimos_nas_duas_versoes(self):
        c = carimbo_recente()
        os.makedirs(blocos.pasta_do_quadro("13", c, 7200))
        for v in (1, 2):
            codigo, _, corpo = self.pedir(f"/blocos/v{v}/ultimos")
            self.assertEqual((codigo, json.loads(corpo)), (200, {"13": c}))

    def test_cliente_que_desiste_nao_vira_traceback(self):
        class Quebrado:
            def write(self, _):
                raise BrokenPipeError

        p = blocos.Pedido.__new__(blocos.Pedido)
        p.wfile = Quebrado()
        p.request_version = "HTTP/1.1"
        p.requestline = "GET /x HTTP/1.1"
        p.command = "GET"
        p.client_address = ("127.0.0.1", 1)
        p._headers_buffer = []
        p._responder(200, b"x", "image/jpeg", "no-store")  # não levanta
        self.assertTrue(p.close_connection)

    def test_limpeza_tira_o_que_saiu_da_janela(self):
        velho = blocos.carimbo_de(datetime.now(timezone.utc) - timedelta(hours=50))
        novo = carimbo_recente()
        for c in (velho, novo):
            os.makedirs(blocos.pasta_do_quadro("13", c, 3600))
        os.makedirs(blocos.pasta_do_quadro("13", novo, 7200) + ".parcial-1-2")
        antigo = datetime.now().timestamp() - 3600
        os.utime(blocos.pasta_do_quadro("13", novo, 7200) + ".parcial-1-2", (antigo, antigo))
        self.assertEqual(blocos.limpar(datetime.now(timezone.utc)), 1)
        self.assertEqual(os.listdir(os.path.join(blocos.PASTAS[2], "13")), [novo])
        self.assertEqual(os.listdir(os.path.join(blocos.PASTAS[2], "13", novo)), ["3600"])


class Atrasados(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        blocos.PASTAS = {v: os.path.join(self.raiz, "blocos", f"v{v}", "nsa") for v in blocos.VERSOES}
        for m in (blocos._ausentes, blocos._publicados, blocos._publicados_produto, blocos._sondados):
            m.clear()
        self._existe = blocos._existe_no_star

    def tearDown(self):
        blocos._existe_no_star = self._existe
        for m in (blocos._ausentes, blocos._publicados, blocos._publicados_produto, blocos._sondados):
            m.clear()
        shutil.rmtree(self.raiz)

    def test_espera_por_idade_e_publicacao(self):
        self.assertEqual(blocos.espera_ausente(timedelta(minutes=30)), 300)
        self.assertEqual(blocos.espera_ausente(timedelta(hours=3)), 1800)
        self.assertEqual(blocos.espera_ausente(timedelta(hours=10)), 3 * 3600)
        # Horário já publicado: o produto que falta chega em minutos.
        self.assertEqual(blocos.espera_ausente(timedelta(hours=3), publicado=True), 60)
        # Na última hora, o 7200 sai segundos depois do 450 da sonda.
        self.assertEqual(blocos.espera_ausente(timedelta(minutes=12), publicado=True), 15)

    def test_horario_atrasado_publicado_libera_todos_os_produtos(self):
        agora = datetime.now(timezone.utc)
        atrasado = blocos.carimbo_de((agora - timedelta(hours=2)).replace(minute=0))
        outro = blocos.carimbo_de((agora - timedelta(hours=3)).replace(minute=0))
        for chave in (("13", atrasado, 7200), ("AirMass", atrasado, 3600), ("13", outro, 7200)):
            blocos._ausentes[chave] = time.time() + 3600
        sondados = []
        publicados = {atrasado}
        blocos._existe_no_star = lambda c, p=blocos.PRODUTO_SONDA: sondados.append((c, p)) or c in publicados
        self.assertEqual(blocos.sondar_publicados(agora), [atrasado])
        self.assertNotIn(("13", atrasado, 7200), blocos._ausentes)
        self.assertNotIn(("AirMass", atrasado, 3600), blocos._ausentes)
        self.assertIn(("13", outro, 7200), blocos._ausentes)
        # Cada horário faltante é sondado no máximo uma vez por minuto.
        n = len(sondados)
        blocos.sondar_publicados(agora)
        self.assertEqual(len(sondados), n)
        # 31 horários de 1–6 h (um HEAD cada) + 6 da última hora (um por produto).
        self.assertLessEqual(n, 31 + 7 * len(blocos.PRODUTOS))

    def test_na_ultima_hora_a_sonda_e_por_produto(self):
        agora = datetime.now(timezone.utc)
        recente = blocos.carimbo_de((agora - timedelta(minutes=20)).replace(second=0, microsecond=0, minute=(agora - timedelta(minutes=20)).minute // 10 * 10))
        for chave in (("13", recente, 7200), ("13", recente, 3600), ("GEOCOLOR", recente, 7200)):
            blocos._ausentes[chave] = time.time() + 300
        # A banda 13 já saiu; o GEOCOLOR (o último) ainda não.
        blocos._existe_no_star = lambda c, p=blocos.PRODUTO_SONDA: c == recente and p == "13"
        self.assertIn(f"13/{recente}", blocos.sondar_publicados(agora))
        self.assertNotIn(("13", recente, 7200), blocos._ausentes)
        self.assertNotIn(("13", recente, 3600), blocos._ausentes)
        self.assertIn(("GEOCOLOR", recente, 7200), blocos._ausentes)
        self.assertNotIn(recente, blocos._publicados)

    def test_v1_velho_sai_so_depois_de_existir_o_v2(self):
        agora = datetime.now(timezone.utc)
        velho = blocos.carimbo_de((agora - timedelta(hours=20)).replace(minute=0))
        sem_v2 = blocos.carimbo_de((agora - timedelta(hours=21)).replace(minute=0))
        novo = blocos.carimbo_de((agora - timedelta(hours=2)).replace(minute=0))
        for c in (velho, sem_v2, novo):
            os.makedirs(blocos.pasta_do_quadro("13", c, 7200, 1))
        for c in (velho, novo):
            os.makedirs(blocos.pasta_do_quadro("13", c, 7200, 2))
        blocos.limpar(agora)
        self.assertEqual(sorted(os.listdir(os.path.join(blocos.PASTAS[1], "13"))), sorted([sem_v2, novo]))
        self.assertEqual(sorted(os.listdir(os.path.join(blocos.PASTAS[2], "13"))), sorted([velho, novo]))

    def test_ultimos_por_produto(self):
        for p, c in (("GEOCOLOR", "20262651200"), ("GEOCOLOR", "20262651220"), ("13", "20262650940")):
            os.makedirs(blocos.pasta_do_quadro(p, c, 7200))
        os.makedirs(blocos.pasta_do_quadro("13", "20262651230", 7200) + ".parcial-1-2")
        self.assertEqual(blocos.ultimos(), {"GEOCOLOR": "20262651220", "13": "20262650940"})


class Frente(unittest.TestCase):
    """O que a sonda acha é cortado na hora, sem esperar o lote do preenchimento."""

    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        blocos.PASTAS = {v: os.path.join(self.raiz, "blocos", f"v{v}", "nsa") for v in blocos.VERSOES}
        self._cortar = blocos.cortar
        blocos._ausentes.clear()
        blocos._em_curso.clear()

    def tearDown(self):
        blocos.cortar = self._cortar
        blocos._ausentes.clear()
        blocos._em_curso.clear()
        shutil.rmtree(self.raiz)

    def test_chaves_do_horario_e_do_produto(self):
        c = "20262751600"
        os.makedirs(blocos.pasta_do_quadro("13", c, 7200))
        todas = blocos.chaves_do_novo(c)
        self.assertNotIn(("13", c, 7200), todas)
        self.assertIn(("13", c, 3600), todas)
        self.assertEqual(len(todas), len(blocos.PRODUTOS) * len(blocos.LARGURAS) - 1)
        # Maior largura primeiro.
        self.assertEqual(blocos.chaves_do_novo(f"GEOCOLOR/{c}"), [("GEOCOLOR", c, 7200), ("GEOCOLOR", c, 3600)])

    def test_insiste_enquanto_o_star_termina_de_publicar(self):
        tentativas = []

        def cortar(p, c, w, origem="pedido", versao=blocos.VERSAO):
            tentativas.append(origem)
            if len(tentativas) < 3:
                raise blocos.Ausente("ainda não")
            return "ok"

        blocos.cortar = cortar
        chave = ("GEOCOLOR", "20262751600", 7200)
        blocos._ausentes[chave] = time.time() + 300
        blocos._em_curso.add(chave)
        esperas = []
        self.assertTrue(blocos.cortar_novo(chave, espera=esperas.append))
        self.assertEqual(tentativas, ["novo"] * 3)
        self.assertEqual(esperas, [blocos.NOVO_A_CADA] * 2)
        self.assertNotIn(chave, blocos._ausentes)
        self.assertNotIn(chave, blocos._em_curso)

    def test_desiste_depois_do_prazo(self):
        def cortar(*a, **k):
            raise blocos.Truncado("curto")

        blocos.cortar = cortar
        relogio = [time.time()]
        esperas = []

        def espera(s):
            esperas.append(s)
            relogio[0] += s

        t = blocos.time.time
        blocos.time.time = lambda: relogio[0]
        try:
            self.assertFalse(blocos.cortar_novo(("13", "20262751600", 7200), espera=espera))
        finally:
            blocos.time.time = t
        self.assertLessEqual(sum(esperas), blocos.NOVO_POR_ATE)
        self.assertGreater(len(esperas), 10)

    def test_frente_corta_sem_esperar_o_laco(self):
        feitos = []
        pronto = threading.Event()

        def cortar(p, c, w, origem="pedido", versao=blocos.VERSAO):
            feitos.append((p, c, w, origem))
            if len(feitos) == 2:
                pronto.set()
            return "ok"

        blocos.cortar = cortar
        threading.Thread(target=blocos.frente_para_sempre, daemon=True).start()
        blocos.enfileirar_novos(["13/20262751600"])
        self.assertTrue(pronto.wait(5))
        self.assertEqual(sorted(feitos), sorted([("13", "20262751600", 7200, "novo"), ("13", "20262751600", 3600, "novo")]))


class Contadores(unittest.TestCase):
    def test_janela_por_tempo_nao_por_quantidade(self):
        from collections import deque

        fila = deque()
        agora = 1_000_000.0
        for k in range(8000):  # mais que o antigo maxlen de 5000, tudo na última hora
            blocos._registrar(fila, (agora - 3000 + k * 0.1, 1), agora=agora)
        self.assertEqual(len(fila), 8000)
        blocos._registrar(fila, (agora + 3700, 1), agora=agora + 3700)
        self.assertTrue(all(agora + 3700 - t <= blocos.JANELA_CONTADORES for t, _ in fila))


QUADRO_450 = jpeg_sintetico(450, 270)


class Espelho(unittest.TestCase):
    """A base inteira do STAR, byte a byte, só se veio completa."""

    def setUp(self):
        self.raiz = tempfile.mkdtemp()
        blocos.PASTAS = {v: os.path.join(self.raiz, "blocos", f"v{v}", "nsa") for v in blocos.VERSOES}
        blocos.RAIZ = self.raiz
        blocos._ausentes.clear()
        blocos._espelho["arquivos"].clear()
        self._get = blocos._get_star
        self.respostas = {}
        self.pedidos = []

        def get(url):
            self.pedidos.append(url)
            return self.respostas.get(url, (404, {}, b"nada"))

        blocos._get_star = get

    def tearDown(self):
        blocos._get_star = self._get
        blocos._ausentes.clear()
        blocos._espelho["arquivos"].clear()
        shutil.rmtree(self.raiz)

    def responder(self, produto, carimbo, largura, corpo, **cab):
        cab.setdefault("content-length", str(len(corpo)))
        cab.setdefault("etag", f'"6abfd659-{len(corpo):x}"')
        self.respostas[blocos.url_star(produto, carimbo, largura)] = (200, cab, corpo)

    def test_grava_igual_ao_star_sem_temporario(self):
        c = carimbo_recente()
        self.responder("13", c, 450, QUADRO_450)
        destino = blocos.espelhar("13", c, 450)
        self.assertEqual(destino, os.path.join(blocos.PASTAS[2], "13", c, "450.jpg"))
        self.assertEqual(ler(destino), QUADRO_450)
        self.assertEqual(os.listdir(os.path.dirname(destino)), ["450.jpg"])
        # Já no disco: não baixa de novo.
        blocos.espelhar("13", c, 450)
        self.assertEqual(len(self.pedidos), 1)

    def test_recusa_o_que_veio_incompleto(self):
        c = carimbo_recente()
        casos = {
            "sem EOI": dict(corpo=QUADRO_450[:-2]),
            "Content-Length maior": dict(corpo=QUADRO_450, cab={"content-length": str(len(QUADRO_450) + 10)}),
            "ETag de outro tamanho": dict(corpo=QUADRO_450, cab={"etag": f'"6abfd659-{len(QUADRO_450) + 1:x}"'}),
        }
        for nome, caso in casos.items():
            with self.subTest(nome):
                self.responder("13", c, 450, caso["corpo"], **caso.get("cab", {}))
                with self.assertRaises(blocos.Truncado):
                    blocos.espelhar("13", c, 450)
                self.assertFalse(os.path.exists(blocos.arquivo_inteiro("13", c, 450)))

    def test_largura_errada_nao_grava(self):
        c = carimbo_recente()
        self.responder("13", c, 900, QUADRO_450)
        with self.assertRaises(ValueError):
            blocos.espelhar("13", c, 900)

    def test_pendentes_so_de_horario_cortado_do_mais_novo_ao_mais_velho(self):
        agora = datetime.now(timezone.utc)
        novo, velho, sem_corte = carimbo_recente(20), carimbo_recente(120), carimbo_recente(60)
        for c in (velho, novo):
            os.makedirs(blocos.pasta_do_quadro("13", c, 7200))
        os.makedirs(blocos.pasta_do_quadro("GEOCOLOR", sem_corte, 3600))
        fila = blocos._pendentes_inteiros(agora)
        self.assertEqual(fila, [("13", novo, w) for w in blocos.INTEIROS] + [("13", velho, w) for w in blocos.INTEIROS])
        # 404 (largura que o STAR ainda não soltou): espera antes de tentar de novo.
        blocos._espelhar_um(("13", novo, 450))
        self.assertNotIn(("13", novo, 450), blocos._pendentes_inteiros(agora))
        # Espelhado: sai da fila.
        self.responder("13", novo, 900, jpeg_sintetico(900, 540))
        blocos._espelhar_um(("13", novo, 900))
        self.assertNotIn(("13", novo, 900), blocos._pendentes_inteiros(agora))
        self.assertEqual(blocos.inteiros_anunciados(time.time()), list(blocos.INTEIROS) if blocos.ESPELHAR else [])

    def test_limpeza_com_bases_e_temporarios(self):
        velho = blocos.carimbo_de(datetime.now(timezone.utc) - timedelta(hours=50))
        novo = carimbo_recente()
        for c in (velho, novo):
            os.makedirs(blocos.pasta_do_quadro("13", c, 7200))
            with open(blocos.arquivo_inteiro("13", c, 900), "wb") as f:
                f.write(b"x")
        temp = blocos.arquivo_inteiro("13", novo, 450) + ".parcial-1-2"
        with open(temp, "wb") as f:
            f.write(b"x")
        antigo = datetime.now().timestamp() - 3600
        os.utime(temp, (antigo, antigo))
        blocos._ausentes[("inteiro", "13", velho, 900)] = time.time() + 60
        blocos.limpar(datetime.now(timezone.utc))
        self.assertEqual(os.listdir(os.path.join(blocos.PASTAS[2], "13")), [novo])
        self.assertEqual(sorted(os.listdir(os.path.join(blocos.PASTAS[2], "13", novo))), ["7200", "900.jpg"])
        self.assertEqual(blocos._ausentes, {})

    def test_etag_do_nginx(self):
        self.assertEqual(blocos._tamanho_do_etag('"6abfd65a-1b05c9"'), 0x1B05C9)
        self.assertEqual(blocos._tamanho_do_etag('W/"6abfd65a-10"'), 16)
        self.assertIsNone(blocos._tamanho_do_etag(None))
        self.assertIsNone(blocos._tamanho_do_etag('"abc"'))


if __name__ == "__main__":
    unittest.main()
