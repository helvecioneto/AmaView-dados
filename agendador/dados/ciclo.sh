#!/usr/bin/env bash
#
# Um ciclo de um grupo de camadas do AmaView-dados, rodado NA ORACLE e
# servido pelo nginx dela em /dados/ — no lugar do GitHub Actions + Pages.
#
#   ciclo.sh radar      # radares do SIPAM (timer a cada 5 min)
#   ciclo.sh chuva      # CEMADEN, embarcações e rios (a cada 15 min; os rios se limitam a 1×/h)
#   ciclo.sh sondagem   # radiossondas do Wyoming (4×/dia)
#
# Por que saiu do GitHub (08/11/2026): duas falhas diferentes do GitHub em três
# dias, cada uma deixando chuva, radar, rios e barcos parados no ar sem erro
# nenhum. Em 05/11, execuções fantasmas presas em "queued" seguraram o grupo
# de concorrência por 26 h. Em 08/11, a partir das 09:52 UTC, o Pages aceitou
# todos os deploys como "success" e continuou servindo o site antigo (os PNG
# novos davam 404). Aqui não há grupo de concorrência nem deploy: cada camada
# é uma pasta no disco.
#
# Os scripts são os mesmos do workflow (scripts/*.mjs). O que muda:
# - SAIDA é uma pasta de trabalho nova a cada ciclo;
# - BASE_URL (de onde cada script relê a publicação anterior) é o próprio
#   nginx desta máquina; na estreia de uma camada, o Pages, uma vez só;
# - a camada só vai ao ar se o script terminou bem e o resultado passou na
#   conferência. Ir ao ar é trocar um link simbólico (rename atômico): quem
#   está baixando nunca vê uma pasta pela metade, e um ciclo que falha deixa
#   no ar o anterior — o papel que os passos "Preservar" tinham no workflow.
set -euo pipefail

GRUPO="${1:?uso: ciclo.sh radar|chuva|sondagem}"
CODIGO="${DADOS_CODIGO:-/opt/amaview}"
RAIZ="${DADOS_RAIZ:-/var/cache/amaview-dados}"
ESTADO="${DADOS_ESTADO:-/var/lib/amaview-dados}"
PUB="$RAIZ/dados"         # o que o nginx serve: /dados/<camada> → versoes/<camada>.<carimbo>
VERSOES="$RAIZ/versoes"
# http e IP: não depende do certificado (de ~6 dias) nem de DNS.
LOCAL="${DADOS_LOCAL:-http://147.15.84.134/dados}"
PAGES="https://helvecioneto.github.io/AmaView-dados"
export PATH="/opt/node/bin:$PATH"

mkdir -p "$PUB" "$VERSOES" "$ESTADO"
TRAB="$(mktemp -d "$RAIZ/trabalho.$GRUPO.XXXXXX")"
trap 'rm -rf "$TRAB"' EXIT

no_ar() { [ -e "$PUB/$1" ]; }

# Valida a pasta $TRAB/<camada>: pelo menos um arquivo, todo JSON legível, e
# a conferência específica da camada (a mesma do workflow).
conferir() {
  local c="$1"
  node - "$TRAB/$c" "$c" <<'JS'
const fs = require('fs');
const path = require('path');
const [dir, camada] = process.argv.slice(2);
const exigidos = {
  cemaden: ['estacoes.json', 'serie.json', 'manifest.json'],
  sondagem: ['estacoes.json', 'perfis.json', 'manifest.json'],
  navios: ['atual.json', 'cadastro.json', 'escuta.json', 'manifest.json'],
  rios: ['estacoes.json', 'atual.json', 'manifest.json'],
  radar: ['manifest.json'],
}[camada] ?? ['manifest.json'];
const ler = (n) => JSON.parse(fs.readFileSync(path.join(dir, n), 'utf8'));
for (const n of exigidos) {
  if (!(fs.statSync(path.join(dir, n), { throwIfNoEntry: false })?.size > 0)) throw new Error(`${camada}/${n} vazio ou ausente`);
}
for (const n of fs.readdirSync(dir)) if (n.endsWith('.json')) ler(n);
if (camada === 'cemaden') {
  const j = ler('serie.json');
  if (!Array.isArray(j.v) || j.v.length === 0) throw new Error('grade vazia');
  if (j.v.some((l) => l.length !== j.slots)) throw new Error('linha com tamanho errado');
}
if (camada === 'sondagem') {
  const j = ler('perfis.json');
  if (!Array.isArray(j.perfis) || !j.perfis.some((p) => p.ms && p.niveis.length)) throw new Error('nenhuma estação com sondagem');
}
if (camada === 'radar') {
  const j = ler('manifest.json');
  if (!Array.isArray(j.radares) || j.radares.length === 0) throw new Error('manifesto sem radares');
}
JS
}

# Põe $TRAB/<camada> no ar. Fica a versão anterior (uma), para quem leu o
# manifesto velho um instante antes da troca ainda achar os arquivos.
publicar() {
  local c="$1" v
  v="$VERSOES/$c.$(date -u +%Y%m%dT%H%M%SZ)"
  chmod -R u=rwX,go=rX "$TRAB/$c"
  mv "$TRAB/$c" "$v"
  ln -sfn "$v" "$PUB/.$c.novo"
  mv -T "$PUB/.$c.novo" "$PUB/$c"
  # Da mais nova para a mais velha: mantém as duas primeiras.
  ls -1d "$VERSOES/$c".* 2>/dev/null | sort -r | tail -n +3 | xargs -r rm -rf
  echo "  $c: no ar ($(du -sh "$v" | cut -f1))"
}

# Roda um script de camada. Sucesso + conferência → no ar. Falha → fica o que
# estava, e o grupo segue para a próxima camada (a chuva não pode cair porque
# o AIS caiu, a lição de 09/10).
rodar() {
  local c="$1"; shift
  local base="$LOCAL"
  no_ar "$c" || base="$PAGES"
  rm -rf "${TRAB:?}/$c"
  if (cd "$CODIGO" && SAIDA="$TRAB" BASE_URL="$base" timeout --kill-after=30 "${PRAZO:-8m}" node "$@"); then
    if [ ! -d "$TRAB/$c" ]; then
      echo "  $c: nada novo (o script não precisou rodar)"
      return 0
    fi
    if conferir "$c"; then
      publicar "$c"
      return 0
    fi
    echo "  $c: NÃO publicado — falhou na conferência; fica o que está no ar" >&2
  else
    echo "  $c: NÃO publicado — o script falhou; fica o que está no ar" >&2
  fi
  FALHAS=$((FALHAS + 1))
}

# Estreia de uma camada: traz a publicação do Pages uma vez, para não perder a
# história (a série do CEMADEN, as datas dos rios) nem começar com o mapa vazio.
estrear() {
  local c="$1"
  no_ar "$c" && return 0
  echo "  $c: estreia — trazendo a publicação do Pages"
  if (cd "$CODIGO" && SAIDA="$TRAB" BASE_URL="$PAGES" PRIMEIRA_PUBLICACAO=1 node scripts/preservar.mjs "$c") \
    && [ -d "$TRAB/$c" ] && conferir "$c"; then
    publicar "$c"
  else
    rm -rf "${TRAB:?}/$c"
    echo "  $c: sem publicação no Pages para trazer" >&2
  fi
}

FALHAS=0
case "$GRUPO" in
  radar)
    # O depósito entre ciclos (o papel do cache do Actions) fica em /var/lib.
    # Mesmo sistema de arquivos da saída: RADAR_LIGAR publica por hard link,
    # sem copiar ~60 MB a cada 5 min (os PNG do depósito nunca mudam).
    RADAR_DEPOSITO="$ESTADO/radar-deposito" RADAR_LIGAR=1 PRAZO=4m30s \
      RADAR_PRAZO_MIN="${RADAR_PRAZO_MIN:-3.5}" rodar radar scripts/radar.mjs
    ;;
  chuva)
    for c in cemaden navios rios; do estrear "$c"; done
    # Sem a conta da PED o build.mjs usa o modo público (só a leitura mais
    # nova; a série cresce ciclo a ciclo). Com ela, a grade vem inteira.
    [ -n "${CEMADEN_EMAIL:-}${CEMADEN_TOKEN:-}" ] \
      || echo "  cemaden: sem a conta da PED, modo público — rode: sudo amaview chave cemaden" >&2
    PRAZO=8m rodar cemaden scripts/build.mjs
    PRAZO=4m rodar navios scripts/navios.mjs
    PRAZO=7m rodar rios scripts/rios.mjs
    ;;
  sondagem)
    estrear sondagem
    PRAZO=25m rodar sondagem scripts/sondagem.mjs
    ;;
  *)
    echo "grupo desconhecido: $GRUPO" >&2
    exit 2
    ;;
esac

# Sai com erro quando alguma camada não foi ao ar: o journal e o
# `systemctl --failed` mostram, e o `amaview` aponta.
[ "$FALHAS" -eq 0 ]
