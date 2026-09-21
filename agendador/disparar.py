#!/usr/bin/env python3
"""
Dispara um workflow do AmaView-dados pela API do GitHub.

Existe porque o agendamento do GitHub Actions não é confiável: medido no
repositório, pedindo um ciclo a cada 15 minutos, o intervalo real entre
disparos foi de 135 minutos em média. Uma máquina com relógio de verdade
resolve o que o `cron` do GitHub não entrega.

O `schedule` do workflow continua ativo como rede de segurança: se esta
máquina cair, os disparos irregulares do GitHub ainda mantêm o dado vivo.

  disparar.py chuva.yml

O token é lido de /etc/amaview/token (uma linha, modo 600). Ele nunca aparece
em argumento nem em log — argumentos de processo são visíveis para qualquer
usuário da máquina.
"""
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("AMAVIEW_REPO", "helvecioneto/AmaView-dados")
ARQUIVO_TOKEN = os.environ.get("AMAVIEW_TOKEN_FILE", "/etc/amaview/token")
REF = os.environ.get("AMAVIEW_REF", "main")


def ler_token() -> str:
    try:
        with open(ARQUIVO_TOKEN, encoding="utf-8") as f:
            token = f.read().strip()
    except FileNotFoundError:
        sys.exit(
            f"token ausente em {ARQUIVO_TOKEN}.\n"
            "Crie um fine-grained PAT com acesso só a este repositório e\n"
            "permissão 'Actions: Read and write', e grave-o ali com modo 600."
        )
    except PermissionError:
        sys.exit(f"sem permissão para ler {ARQUIVO_TOKEN} (rode como root)")
    if not token:
        sys.exit(f"{ARQUIVO_TOKEN} está vazio")
    return token


def disparar(workflow: str) -> None:
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/dispatches"
    req = urllib.request.Request(
        url,
        data=json.dumps({"ref": REF}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {ler_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "amaview-agendador",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            # A API responde 204 sem corpo quando aceita.
            print(f"{workflow}: disparado (HTTP {r.status})")
    except urllib.error.HTTPError as e:
        corpo = e.read().decode(errors="replace")[:300]
        # 403/404 aqui quase sempre é permissão do token, e não repositório
        # inexistente: a API esconde o que o token não pode ver.
        sys.exit(f"{workflow}: HTTP {e.code} — {corpo}")
    except urllib.error.URLError as e:
        sys.exit(f"{workflow}: rede — {e.reason}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("uso: disparar.py <workflow.yml>")
    disparar(sys.argv[1])
