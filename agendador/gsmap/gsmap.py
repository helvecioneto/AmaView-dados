#!/usr/bin/env python3
"""
Chuva por satélite para o AmaView: o GSMaP_NOW da JAXA (Global Satellite
Mapping of Precipitation, versão de tempo real), recortado no setor NSA do
GOES-19.

- **Por que o NOW, e não o NRT.** O GSMaP_NOW sai a cada 30 min, com atraso
  ~0 h depois da observação; o GSMaP_NRT, de hora em hora, 4 h depois. No loop
  das últimas 48 h do AmaView o NRT deixaria as 4 h mais novas sem chuva — as
  que mais interessam. O NOW usa menos micro-ondas (só o que chega em meia
  hora) e estende a última meia hora pelo vento das nuvens do geoestacionário
  (GOES na América do Sul), então é menos exato: o painel diz isso.
- **Ajustado por pluviômetros.** O `gsmap_gauge_now` corrige o viés do
  satélite com a relação aprendida dos pluviômetros (o CPC da NOAA) no
  GSMaP_Gauge; sobre o mar fica igual ao `gsmap_now`. Sem o arquivo ajustado
  de um horário, entra o do satélite só (`p: "s"` no índice) e é trocado
  quando o ajustado chegar.
- **O valor da JAXA, sem retoque.** Taxa de chuva média da hora (mm/h), a
  grade de 0,1° como ela é (sem interpolação aqui). Só a codificação muda:
  um byte por ponto, raiz quadrada (ver `codificar`).
- **48 h, no máximo.** O diretório `latest/` do servidor da JAXA tem as
  últimas 24 h; o que falta das 48 h vem do arquivo por dia (`half_hour_G/`).
- Roda pelo `amaview-gsmap.timer` (a cada 5 min). A conta (usuário e senha
  do cadastro da JAXA) fica em /etc/amaview/gsmap.netrc, gravada pelo
  `amaview chave gsmap`; sem ela, a rodada só avisa e sai.

Saída (abaixo de GSMAP_RAIZ, servida pelo nginx em /gsmap/vN/):

  gsmap/v1/indice.json                 grade, codificação e quadros (o app lê a cada minuto)
  gsmap/v1/quadros/AAAAMMDDHHNN-g.bin  um por horário (imutável; -g ajustado, -s só satélite)
  gsmap/v1/quadros/….bin.gz            o mesmo comprimido (gzip_static do nginx)

  gsmap.py                 uma rodada (o que o timer chama)
  gsmap.py arquivo ARQ     converte um arquivo local (.dat ou .dat.gz) e imprime o resumo (teste)
  gsmap.py listar          lista o latest/ do servidor (confere a conta)
"""
import gzip
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# Sobe quando o conteúdo dos quadros muda (cache imutável de 1 ano no navegador).
VERSAO = 1
RAIZ = os.environ.get("GSMAP_RAIZ", "/var/cache/amaview-gsmap")
PASTA = os.path.join(RAIZ, "gsmap", f"v{VERSAO}")
QUADROS = os.path.join(PASTA, "quadros")
ESTADO = os.path.join(RAIZ, "estado.json")
NETRC = os.environ.get("GSMAP_NETRC", "/etc/amaview/gsmap.netrc")
SERVIDOR = os.environ.get("GSMAP_SERVIDOR", "hokusai.eorc.jaxa.jp")
# SFTP (porta 22) é o que a JAXA documenta hoje; `ftp` (sempre com TLS
# obrigatório: FTPS) só se pedido no `amaview chave gsmap ftps`.
PROTOCOLO = os.environ.get("GSMAP_PROTOCOLO", "sftp")

JANELA = timedelta(hours=48)
# Arquivos por rodada: o horário novo nunca espera o preenchimento do passado
# (~800 KB cada; o preenchimento das 48 h leva umas 5 rodadas).
MAX_POR_RODADA = 24
# E até 10 min por rodada (o tempo-limite do systemd é 15).
TEMPO_MAX_S = 600
# Dia do arquivo (half_hour_G/AAAA/MM/DD) com horário faltando: listar de
# novo no máximo a cada 30 min. O latest/ é listado em toda rodada.
RELISTAR_S = 1800
# Arquivo que falhou: tentar de novo só depois disto.
ESPERA_FALHA_S = 1800

# Grade global do GSMaP: 3600 × 1200 pontos de 0,1°, de 60°N a 60°S; o
# primeiro ponto tem centro em (59,95°N; 0,05°E), linha a linha para o sul,
# longitude crescendo para leste de 0° a 360°. float32 little-endian, mm/h;
# negativo = sem dado.
NX, NY, PASSO = 3600, 1200, 0.1

# Recorte (bordas): o setor NSA do GOES-19 vai de 22,06°S a 22,06°N e de
# 89,3°W até a borda do disco sobre a África; a leste de 30°W é só Atlântico
# (como na fumaça). 442 × 594 pontos.
NORTE, SUL, OESTE, LESTE = 22.1, -22.1, -89.4, -30.0
L0 = round((60.0 - NORTE) / PASSO)
L1 = round((60.0 - SUL) / PASSO)
C0 = round((OESTE % 360) / PASSO)
C1 = round((LESTE % 360) / PASSO)
GRADE = {
    "lat0": round(NORTE - PASSO / 2, 2),
    "lon0": round(OESTE + PASSO / 2, 2),
    "passo": PASSO,
    "ni": L1 - L0,
    "nj": C1 - C0,
}

# Um byte por ponto: q = round(20·√v). Abaixo de 0,1 mm/h vira 0 (sem cor no
# mapa, e comprime muito melhor); 254 = 161 mm/h ou mais; 255 = sem dado. O
# passo é fino onde a chuva é fraca (0,03 mm/h perto de 0,5) e largo onde a
# diferença não muda a leitura (1,3 mm/h perto de 40).
FATOR = 20
LIMITE_Q = 254
SEM_DADO = 255
MINIMO_MMH = 0.1
Q_MINIMO = math.ceil(math.sqrt(MINIMO_MMH) * FATOR)

NOME = re.compile(r"^gsmap_(gauge_)?now\.(\d{8})\.(\d{4})(?:_(\d{4}))?\.dat(\.gz)?$")


def instante(carimbo: str) -> datetime:
    """AAAAMMDDHHNN (UTC) → datetime."""
    return datetime.strptime(carimbo, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def carimbo_de(t: datetime) -> str:
    return t.strftime("%Y%m%d%H%M")


def ler_nome(nome: str) -> tuple[str, str] | None:
    """Nome do arquivo → (carimbo do início da hora, 'g' ajustado | 's' só satélite), ou None."""
    m = NOME.match(nome)
    if not m:
        return None
    ajustado, dia, ini, fim, _ = m.groups()
    if fim is not None:
        # A hora vai de HHNN a hhnn (início + 59 min): outra duração não é o produto horário.
        t0 = datetime.strptime(dia + ini, "%Y%m%d%H%M")
        t1 = datetime.strptime(dia + fim, "%Y%m%d%H%M")
        if t1 < t0:
            t1 += timedelta(days=1)
        if t1 - t0 != timedelta(minutes=59):
            return None
    return dia + ini, "g" if ajustado else "s"


# ---------------------------------------------------------------------------
# Conversão


def recortar(bruto: bytes):
    """Arquivo da JAXA (.dat, já descomprimido) → recorte float32 (ni × nj), NaN sem dado."""
    import numpy as np

    if len(bruto) != NX * NY * 4:
        raise ValueError(f"tamanho inesperado: {len(bruto)} bytes (esperado {NX * NY * 4})")
    campo = np.frombuffer(bruto, dtype="<f4").reshape(NY, NX)
    rec = np.array(campo[L0:L1, C0:C1], dtype=np.float32)
    rec[~np.isfinite(rec) | (rec < 0)] = np.nan
    return rec


def codificar(rec):
    """float32 (mm/h, NaN sem dado) → uint8: q = round(20·√v), 0 abaixo de 0,1 mm/h, 255 sem dado."""
    import numpy as np

    sem = np.isnan(rec)
    v = np.where(sem, 0, rec)
    q = np.rint(np.sqrt(np.maximum(v, 0)) * FATOR)
    # 0,100–0,105 mm/h arredondaria para q = 6 (0,09): sem cor no mapa. O
    # menor código com chuva é o primeiro que decodifica ≥ 0,1 (q = 7, 0,12).
    q = np.maximum(q, Q_MINIMO)
    q[v < MINIMO_MMH] = 0
    q = np.minimum(q, LIMITE_Q).astype(np.uint8)
    q[sem] = SEM_DADO
    return q


def decodificar(q):
    """uint8 → float32 mm/h (NaN sem dado) — o mesmo que o navegador faz."""
    import numpy as np

    v = (q.astype(np.float32) / FATOR) ** 2
    v[q == SEM_DADO] = np.nan
    return v


def resumo(rec) -> dict:
    """O que o painel mostra sem baixar o quadro: máximo, fração com chuva, fração sem dado."""
    import numpy as np

    validos = ~np.isnan(rec)
    n = max(1, int(validos.sum()))
    v = rec[validos]
    return {
        "max": round(float(v.max()), 1) if v.size else 0.0,
        # Fração dos pontos com dado que têm chuva de pelo menos 1 mm/h.
        "f1": round(float((v >= 1).sum()) / n, 4),
        "sem": round(1 - validos.sum() / rec.size, 4),
    }


def converter(bruto: bytes) -> tuple[bytes, dict]:
    """Arquivo (gzip ou não) → (quadro uint8, resumo)."""
    if bruto[:2] == b"\x1f\x8b":
        bruto = gzip.decompress(bruto)
    rec = recortar(bruto)
    return codificar(rec).tobytes(), resumo(rec)


# ---------------------------------------------------------------------------
# Servidor da JAXA (curl: SFTP com a chave do servidor em ~/.ssh/known_hosts)


def tem_conta() -> bool:
    try:
        return os.path.getsize(NETRC) > 0
    except OSError:
        return False


def curl(caminho: str, listar: bool = False, tempo: int = 120) -> bytes:
    url = f"{PROTOCOLO}://{SERVIDOR}{caminho}"
    cmd = ["curl", "-sS", "--fail", "--netrc-file", NETRC, "--connect-timeout", "20", "--max-time", str(tempo)]
    if PROTOCOLO == "ftp":
        # Só FTPS: a senha nunca vai em texto claro.
        cmd.append("--ssl-reqd")
    if listar:
        cmd.append("--list-only")
    r = subprocess.run([*cmd, url], capture_output=True, timeout=tempo + 30)
    if r.returncode != 0:
        erro = r.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"curl {r.returncode} em {caminho}: {erro[-1] if erro else ''}")
    return r.stdout


def listar(pasta: str) -> dict[tuple[str, str], str]:
    """(carimbo, produto) → caminho no servidor, dos arquivos horários de uma pasta."""
    saida = {}
    for nome in curl(pasta, listar=True, tempo=60).decode(errors="replace").split():
        nome = nome.rsplit("/", 1)[-1]
        chave = ler_nome(nome)
        # No latest/ cada hora tem o arquivo "HHNN" e um link "HHNN_hhnn" para
        # ele: basta um, e o arquivo de verdade ganha do link.
        if chave and (chave not in saida or "_" in saida[chave].rsplit("/", 1)[-1].split(".")[2]):
            saida[chave] = pasta + nome
    return saida


def pasta_do_dia(t: datetime) -> str:
    return f"/now/half_hour_G/{t:%Y/%m/%d}/"


# ---------------------------------------------------------------------------
# Disco: quadros, índice e limpeza


def escrever_atomico(caminho: str, dado: bytes) -> None:
    temp = f"{caminho}.parcial-{os.getpid()}"
    with open(temp, "wb") as f:
        f.write(dado)
    os.replace(temp, caminho)


def ler_json(caminho: str, padrao):
    try:
        with open(caminho) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def no_disco() -> dict[str, str]:
    """carimbo → produto ('g' ganha de 's') dos quadros publicados."""
    out: dict[str, str] = {}
    for nome in os.listdir(QUADROS):
        m = re.match(r"^(\d{12})-([gs])\.bin$", nome)
        if m and out.get(m.group(1)) != "g":
            out[m.group(1)] = m.group(2)
    return out


def gravar(c: str, p: str, quadro: bytes) -> None:
    base = os.path.join(QUADROS, f"{c}-{p}.bin")
    escrever_atomico(base + ".gz", gzip.compress(quadro, compresslevel=9, mtime=0))
    escrever_atomico(base, quadro)


# O de satélite só, já substituído pelo ajustado, fica no disco este tempo: o
# índice que o navegador tem na mão (revalidado a cada minuto) ainda pode
# apontar para ele — apagar junto com a gravação do ajustado dava 404.
MANTER_SUBSTITUIDO_S = 600


def apagar_substituidos() -> int:
    """Apaga os `-s` cujo `-g` já está no índice há mais de MANTER_SUBSTITUIDO_S."""
    removidos = 0
    for nome in os.listdir(QUADROS):
        m = re.match(r"^(\d{12})-s\.bin(\.gz)?$", nome)
        if not m:
            continue
        g = os.path.join(QUADROS, f"{m.group(1)}-g.bin")
        try:
            velho = time.time() - os.path.getmtime(g) > MANTER_SUBSTITUIDO_S
        except FileNotFoundError:
            continue
        if velho:
            os.remove(os.path.join(QUADROS, nome))
            removidos += 1
    return removidos


def limpar(agora: datetime) -> int:
    """Apaga quadros fora das 48 h e restos de escritas interrompidas."""
    removidos = 0
    limite = agora - JANELA
    for nome in os.listdir(QUADROS):
        caminho = os.path.join(QUADROS, nome)
        try:
            velho = instante(nome[:12]) < limite
        except ValueError:
            velho = True
        if velho or (".parcial-" in nome and time.time() - os.path.getmtime(caminho) > 600):
            os.remove(caminho)
            removidos += 1
    return removidos


def montar_indice(resumos: dict[str, dict], agora: datetime) -> dict:
    """Índice só com o que está no disco (a fonte da verdade são os arquivos)."""
    import numpy as np

    quadros = []
    for c, p in sorted(no_disco().items()):
        r = resumos.get(f"{c}-{p}")
        if r is None:  # estado perdido: o resumo sai do próprio quadro
            with open(os.path.join(QUADROS, f"{c}-{p}.bin"), "rb") as f:
                q = np.frombuffer(f.read(), dtype=np.uint8).reshape(GRADE["ni"], GRADE["nj"])
            r = resumo(decodificar(q))
            resumos[f"{c}-{p}"] = r
        quadros.append({"c": c, "p": p, **r})
    return {
        "versao": VERSAO,
        "gerado": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "GSMaP_NOW (JAXA), taxa de chuva horária; p=g ajustado por pluviômetros (GSMaP_Gauge_NOW), p=s só satélite",
        "credito": "GSMaP data by Japan Aerospace Exploration Agency (JAXA)",
        "cadencia_min": 30,
        "duracao_min": 60,
        "grade": GRADE,
        "codificacao": {"tipo": "raiz", "fator": FATOR, "limite": LIMITE_Q, "semDado": SEM_DADO, "minimo": MINIMO_MMH},
        "quadros": quadros,
    }


def rodada(agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    os.makedirs(QUADROS, exist_ok=True)
    estado = ler_json(ESTADO, {})
    listadas: dict[str, float] = estado.get("listadas", {})
    resumos: dict[str, dict] = estado.get("resumos", {})
    removidos = limpar(agora) + apagar_substituidos()
    # Versões antigas do formato: só depois de 2 dias sem rodar — o app já
    # publicado ainda pede o endereço antigo até a versão nova dele entrar.
    for nome in os.listdir(os.path.dirname(PASTA)):
        antiga = os.path.join(os.path.dirname(PASTA), nome)
        if nome == os.path.basename(PASTA):
            continue
        try:
            parada = time.time() - os.path.getmtime(os.path.join(antiga, "indice.json")) > 2 * 86400
        except FileNotFoundError:
            parada = True
        if parada:
            shutil.rmtree(antiga, ignore_errors=True)

    limite = agora - JANELA
    publicados = no_disco()
    # Horários esperados da janela (de 30 em 30 min, já terminada a meia hora do início).
    fim = agora.replace(minute=agora.minute - agora.minute % 30, second=0, microsecond=0)
    esperados = []
    t = fim
    while t >= limite:
        esperados.append(carimbo_de(t))
        t -= timedelta(minutes=30)

    # O que já foi listado e ainda não baixado (passou do limite da rodada, ou
    # falhou) continua na fila: sem isto, o dia listado esperaria 30 min para
    # ser visto de novo e o preenchimento das 48 h empacava.
    remotos: dict[tuple[str, str], str] = {}
    for k, caminho in estado.get("pendentes", {}).items():
        c, p = k.split("-", 1)
        if c in esperados and publicados.get(c) != "g":
            remotos[(c, p)] = caminho
    erros = []
    try:
        remotos.update(listar("/now/latest/"))
    except Exception as e:  # noqa: BLE001
        erros.append(str(e))
    # O que o latest/ não cobre (mais de ~24 h), pelo arquivo do dia: só os
    # dias com horário faltando, no máximo a cada 30 min.
    dias = {}
    for c in esperados:
        if instante(c) < agora - timedelta(hours=23) and publicados.get(c) != "g" and (c, "g") not in remotos:
            dias.setdefault(pasta_do_dia(instante(c)), True)
    for pasta in dias:
        if time.time() - listadas.get(pasta, 0) < RELISTAR_S:
            continue
        try:
            for chave, caminho in listar(pasta).items():
                remotos.setdefault(chave, caminho)
            listadas[pasta] = time.time()
        except Exception as e:  # noqa: BLE001 — um dia falhando não para os outros
            erros.append(str(e))
    listadas = {k: v for k, v in listadas.items() if time.time() - v < 3 * 86400}

    falhados: dict[str, float] = {k: v for k, v in estado.get("falhados", {}).items() if time.time() - v < ESPERA_FALHA_S}
    # Fila, do mais novo para o mais velho: o ajustado onde não há nada ou só o
    # de satélite; o de satélite só onde não há nada e o ajustado não veio.
    fila = []
    for c in esperados:
        tem = publicados.get(c)
        if tem == "g":
            continue
        if (c, "g") in remotos:
            fila.append((c, "g"))
        elif tem is None and (c, "s") in remotos:
            fila.append((c, "s"))
    feitos, falhas = 0, 0
    inicio = time.time()

    def publicar_indice() -> dict:
        indice = montar_indice(resumos, agora)
        escrever_atomico(os.path.join(PASTA, "indice.json"), json.dumps(indice, separators=(",", ":")).encode())
        return indice

    for c, p in [x for x in fila if f"{x[0]}-{x[1]}" not in falhados][:MAX_POR_RODADA]:
        # Rodada longa (a rede da JAXA lenta): o resto fica para a próxima, bem
        # antes do tempo-limite do systemd (15 min).
        if time.time() - inicio > TEMPO_MAX_S:
            break
        t0 = time.time()
        try:
            quadro, r = converter(curl(remotos[(c, p)]))
            gravar(c, p, quadro)
            resumos[f"{c}-{p}"] = r
            feitos += 1
            # A hora nova no ar já: uma rodada interrompida não perde o que baixou.
            publicar_indice()
            print(f"{c}-{p}: máx {r['max']} mm/h, {r['f1'] * 100:.1f}% ≥ 1 mm/h ({time.time() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            falhas += 1
            falhados[f"{c}-{p}"] = time.time()
            print(f"falha em {c}-{p}: {e}", file=sys.stderr)

    publicados = no_disco()
    indice = publicar_indice()
    presentes = {f"{q['c']}-{q['p']}" for q in indice["quadros"]}
    pendentes = {
        f"{c}-{p}": caminho
        for (c, p), caminho in remotos.items()
        if c in esperados and publicados.get(c) != "g" and not (p == "s" and c in publicados)
    }
    estado = {
        "listadas": listadas,
        "pendentes": pendentes,
        "resumos": {k: v for k, v in resumos.items() if k in presentes},
        "falhados": falhados,
    }
    escrever_atomico(ESTADO, json.dumps(estado).encode())
    # Faltando de verdade: a JAXA já devia ter publicado (mais de 1 h desde o início da hora).
    faltam = sum(1 for c in esperados if c not in publicados and instante(c) <= agora - timedelta(hours=1))
    return {"feitos": feitos, "falhas": falhas, "removidos": removidos, "quadros": len(indice["quadros"]), "faltam": faltam, "erros": erros}


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "arquivo":
        with open(argv[2], "rb") as f:
            quadro, r = converter(f.read())
        print(json.dumps({"bytes": len(quadro), "grade": GRADE, **r}))
        return 0
    if not tem_conta():
        print(f"sem conta da JAXA em {NETRC} — rode: sudo amaview chave gsmap", file=sys.stderr)
        return 0
    if len(argv) >= 2 and argv[1] == "listar":
        for (c, p), caminho in sorted(listar("/now/latest/").items()):
            print(c, p, caminho)
        return 0
    t0 = time.time()
    r = rodada()
    print(
        f"rodada: {r['feitos']} novos, {r['falhas']} falhas, {r['removidos']} removidos; "
        f"{r['quadros']} no índice, {r['faltam']} horários faltando ({time.time() - t0:.0f} s)"
    )
    for e in r["erros"]:
        print(f"aviso: {e}", file=sys.stderr)
    # Sem nada listado (conta recusada, servidor fora): falha visível no systemd.
    return 1 if r["erros"] and r["quadros"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
