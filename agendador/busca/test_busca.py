"""
Testes da busca, sem rede: o índice é um recorte do busca.json do AmaView e
as respostas do Jev são dicionários no formato que o SDK devolve (medidos em
01/10/2026).

  python3 -m unittest discover -s agendador/busca
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import busca  # noqa: E402

INDICE = {
    "layers": {
        "estados": [["PA", "Pará", "PA"], ["AM", "Amazonas", "AM"]],
        "municipios": [
            ["1302603", "Manaus", "AM"],
            ["1501402", "Belém", "PA"],
            ["1507300", "São Félix do Xingu", "PA"],
            ["1200401", "Rio Branco", "AC"],
        ],
        "terras_indigenas": [["Kayapó", "Kayapó", "Kayapó"], ["Parque do Xingu", "Parque do Xingu", "Kaiabi"]],
        "unidades_conservacao": [
            ["Parque Nacional do Jaú", "Parque Nacional do Jaú", "Parque"],
            ["Parque Nacional Mapinguari", "Parque Nacional Mapinguari", "Parque"],
            ["Parque Nacional do Acari", "Parque Nacional do Acari", "Parque"],
            ["Parque Nacional do Viruá", "Parque Nacional do Viruá", "Parque"],
            ["Parque Estadual do Xingu", "Parque Estadual do Xingu", "Parque"],
        ],
        "amazonia_legal": [["Amazônia Legal", "Amazônia Legal", ""]],
        "camada_nova": [["x", "Manaus", ""]],
    }
}


def nomes(pedido: str) -> list[str]:
    return [a["name"] for a in busca.candidatos(pedido, INDICE)]


class Candidatos(unittest.TestCase):
    def test_nome_com_acento_e_caixa(self):
        self.assertEqual(nomes("focos na TI kayapo")[0], "Kayapó")
        self.assertEqual(nomes("fumaça sobre MANAUS")[0], "Manaus")

    def test_para_e_o_estado_e_nao_so_preposicao(self):
        self.assertIn("Pará", nomes("tempestades no Pará hoje"))

    def test_nome_inteiro_ganha_de_palavra_solta(self):
        self.assertEqual(nomes("focos em São Félix do Xingu")[0], "São Félix do Xingu")

    def test_palavra_rara_ganha_de_palavra_comum(self):
        # "nacional" está em quatro parques; "xingu" é o que identifica o lugar.
        primeiros = nomes("queimadas no parque nacional do Xingu")[:3]
        self.assertIn("Parque Estadual do Xingu", primeiros)
        self.assertIn("Parque do Xingu", primeiros)

    def test_sem_lugar_nao_ha_candidato(self):
        self.assertEqual(nomes("mostre a fumaça agora"), [])
        self.assertEqual(nomes(""), [])

    def test_ignora_camada_desconhecida_e_respeita_o_maximo(self):
        self.assertNotIn("camada_nova", {a["layer"] for a in busca.candidatos("Manaus", INDICE)})
        self.assertLessEqual(len(busca.candidatos("parque nacional", INDICE, n=2)), 2)


class Perguntas(unittest.TestCase):
    def test_sem_candidato_nao_ha_pergunta_de_area(self):
        q = busca.montar_perguntas([])
        self.assertNotIn("area", q)
        self.assertEqual({k for k in q if k.startswith("camada.")}, {f"camada.{c}" for c in busca.CAMADAS})

    def test_opcoes_de_area_e_nenhuma(self):
        areas = busca.candidatos("focos na TI Kayapó", INDICE)
        criterios = busca.montar_perguntas(areas)["area"]["criteria"]
        self.assertEqual(list(criterios)[-1], busca.NENHUM)
        self.assertIn("Indigenous land", criterios["a0"])

    def test_produtos_sao_os_do_app(self):
        criterios = busca.montar_perguntas([])["produto"]["criteria"]
        self.assertEqual(len(criterios), 23)  # 22 produtos + nenhum


def resposta(produto=None, periodo=None, area=None, camadas=(), meteo="t", animar=0.0):
    """Respostas no formato de `respostas_como_dict`."""
    def choice(prob):
        melhor = max(prob, key=prob.get)
        return {"choice": melhor, "confidence": prob[melhor], "probabilities": prob}

    r = {
        "produto": choice(produto or {busca.NENHUM: 0.96, "13": 0.04}),
        "periodo": choice(periodo or {busca.NENHUM: 1.0}),
        "variavel_meteo": choice({meteo: 0.9, "u": 0.1}),
        "animar": {"noul": animar},
    }
    for c in busca.CAMADAS:
        r[f"camada.{c}"] = {"noul": 0.97 if c in camadas else 0.02}
    if area is not None:
        r["area"] = choice(area)
    return r


class Interpretar(unittest.TestCase):
    def test_focos_na_ti_nas_ultimas_24h(self):
        areas = busca.candidatos("focos na TI Kayapó nas últimas 24h", INDICE)
        r = resposta(periodo={"24h": 1.0, busca.NENHUM: 0.0}, area={"a0": 0.99, busca.NENHUM: 0.01},
                     camadas=("queimadas", "terras_indigenas"))
        s = busca.interpretar(r, areas)
        self.assertIsNone(s["produto"])
        self.assertEqual(s["periodo"], {"id": "24h", "confianca": 1.0})
        self.assertEqual((s["area"]["layer"], s["area"]["key"]), ("terras_indigenas", "Kayapó"))
        self.assertEqual(s["camadas"], ["queimadas", "terras_indigenas"])
        self.assertFalse(s["animar"])
        self.assertIsNone(s["variavelMeteo"])  # estações não foram pedidas

    def test_produto_dividido_entre_bandas_vale(self):
        s = busca.interpretar(resposta(produto={"13": 0.46, "14": 0.41, "15": 0.08, busca.NENHUM: 0.05}), [])
        self.assertEqual(s["produto"], {"id": "13", "confianca": 0.46})

    def test_produto_duvidoso_nao_troca_a_imagem(self):
        # "focos de calor": banda 07 com 50%, mas "nenhum" com 45%.
        s = busca.interpretar(resposta(produto={"07": 0.5, busca.NENHUM: 0.45, "FireTemperature": 0.05}), [])
        self.assertIsNone(s["produto"])

    def test_area_nenhuma_e_variavel_das_estacoes(self):
        areas = busca.candidatos("vento em Manaus", INDICE)
        s = busca.interpretar(resposta(area={"a0": 0.2, busca.NENHUM: 0.8}, camadas=("meteo",), meteo="v", animar=0.9), areas)
        self.assertIsNone(s["area"])
        self.assertEqual(s["variavelMeteo"], "v")
        self.assertTrue(s["animar"])


class Servico(unittest.TestCase):
    """O servidor de verdade numa porta livre; o Jev é trocado por um falso."""

    def setUp(self):
        busca._memoria.clear()
        busca._por_ip.clear()
        busca._estado.update(dia="", pedidos_dia=0, falhas=0)
        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), busca.Pedido)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"
        self.chamadas = 0

        def falso(pedido):
            self.chamadas += 1
            return {"pedido": pedido, "camadas": ["fumaca"]}

        p = mock.patch.object(busca, "buscar", side_effect=falso)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def get(self, caminho):
        try:
            with urllib.request.urlopen(self.base + caminho) as r:
                return r.status, json.load(r), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.load(e), e.headers

    def test_resposta_tem_cors_e_nao_vai_para_cache(self):
        codigo, dados, cab = self.get("/busca/v1/?q=fuma%C3%A7a%20em%20Manaus")
        self.assertEqual(codigo, 200)
        self.assertEqual(dados["camadas"], ["fumaca"])
        self.assertEqual(cab["Access-Control-Allow-Origin"], "*")
        self.assertEqual(cab["Cache-Control"], "no-store")

    def test_mesmo_pedido_vem_da_memoria(self):
        self.get("/busca/v1/?q=fuma%C3%A7a%20em%20Manaus")
        self.get("/busca/v1/?q=Fumaca%20em%20%20manaus")
        self.assertEqual(self.chamadas, 1)

    def test_pedido_vazio_ou_longo(self):
        self.assertEqual(self.get("/busca/v1/?q=%20")[0], 400)
        self.assertEqual(self.get("/busca/v1/?q=" + "a" * 201)[0], 400)
        self.assertEqual(self.get("/outra")[0], 404)

    def test_limite_por_minuto_e_por_dia(self):
        for i in range(busca.POR_MINUTO):
            self.assertEqual(self.get(f"/busca/v1/?q=pedido{i}")[0], 200)
        self.assertEqual(self.get("/busca/v1/?q=mais%20um")[0], 429)
        busca._por_ip.clear()
        busca._estado["pedidos_dia"] = busca.TETO_DIA
        self.assertEqual(self.get("/busca/v1/?q=amanha")[0], 429)

    def test_sem_chave_e_falha_do_modelo(self):
        with mock.patch.object(busca, "buscar", side_effect=busca.SemChave()):
            self.assertEqual(self.get("/busca/v1/?q=a")[0], 503)
        with mock.patch.object(busca, "buscar", side_effect=TimeoutError("lento")):
            codigo, dados, _ = self.get("/busca/v1/?q=b")
        self.assertEqual(codigo, 502)
        self.assertEqual(busca.saude()["falhas"], 1)

    def test_saude(self):
        codigo, dados, _ = self.get("/busca/saude")
        self.assertEqual(codigo, 200)
        self.assertEqual(dados["teto_dia"], busca.TETO_DIA)


if __name__ == "__main__":
    unittest.main()
