/**
 * Ciclo dos rios: lê a telemetria da ANA e escreve o que o GitHub Pages
 * publica.
 *
 * Roda como PASSO do workflow da chuva, pelo mesmo motivo das embarcações: um
 * workflow próprio disputaria o grupo `publicar-pages`, onde a execução
 * PENDENTE é cancelada quando uma terceira chega — em silêncio, com status
 * `cancelled`.
 *
 * **Cadência de 1 hora, e não de 15 minutos.** A telemetria publica de 15 em
 * 15 min, mas o nível de um rio amazônico muda de 5 a 30 cm por DIA. Buscar
 * 154 estações a cada ciclo da chuva seriam ~15 mil requisições diárias a um
 * serviço público para um dado que não mudou. O passo confere o carimbo da
 * publicação anterior e só trabalha se venceu; nos outros ciclos sai em
 * silêncio e o `preservar.mjs` mantém a pasta no ar.
 *
 *   node scripts/rios.mjs
 *
 * Variáveis de ambiente:
 *   BASE_URL      De onde reler a publicação anterior.
 *   SAIDA         Diretório de saída (padrão: `site`).
 *   FORCAR_RIOS   `1` ignora o carimbo e roda mesmo assim.
 */
import { mkdir, writeFile, readFile, rm, cp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { emLotes, leituras, situacoesSace } from './ana.mjs';
import {
  PASSO_MS,
  SLOTS,
  anomaliaCm,
  classePorPercentil,
  deslocarSerie,
  diaDoAno,
  extremosDe,
  fimDaGrade,
  gradeHoraria,
  posicaoPercentil,
  referenciaDoDia,
  tendenciaDe,
  ultimoDa,
  variacao,
  venceu,
} from './hidro.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const SAIDA = isAbsolute(process.env.SAIDA ?? '') ? process.env.SAIDA : join(RAIZ, process.env.SAIDA || 'site');
const BASE_URL = process.env.BASE_URL || 'https://helvecioneto.github.io/AmaView-dados';
const FORCAR = process.env.FORCAR_RIOS === '1';

/**
 * Prazo global da coleta.
 *
 * O normal medido é 1–4 min para 154 estações. Sem prazo, uma ANA lenta
 * (timeout em série) levaria o passo a até duas horas — e cada 15 min a mais
 * cancela um ciclo da chuva no grupo de concorrência do Pages. Esgotado o
 * prazo, o que não foi buscado herda a publicação anterior (ver abaixo).
 */
const PRAZO_MS = Number(process.env.RIOS_PRAZO_MS) || 4 * 60 * 1000;

/** Avisa o workflow se o passo trabalhou ou pulou (ver `chuva.yml`). */
async function anunciar(rodou) {
  if (!process.env.GITHUB_OUTPUT) return;
  await writeFile(process.env.GITHUB_OUTPUT, `rodou=${rodou ? 'true' : 'false'}\n`, { flag: 'a' });
}

async function anterior(nome) {
  try {
    // `?t=` fura a CDN do Pages (max-age=600): sem isso um ciclo relê a
    // publicação de dois ciclos atrás e o carimbo velho o faz trabalhar à toa.
    const r = await fetch(`${BASE_URL}/rios/${nome}?t=${Date.now()}`, { signal: AbortSignal.timeout(15_000), cache: 'no-store' });
    if (!r.ok) return null;
    const t = await r.text();
    if (t.length < 2 || t.trimStart().startsWith('<')) return null;
    return JSON.parse(t);
  } catch {
    return null;
  }
}

async function main() {
  const inicioExec = Date.now();

  const [manifestoAnterior, atualAnterior] = await Promise.all([anterior('manifest.json'), anterior('atual.json')]);
  if (!FORCAR && !venceu(manifestoAnterior?.gerado)) {
    const idade = Math.round((Date.now() - Date.parse(manifestoAnterior.gerado)) / 60_000);
    console.log(`Rios: publicação tem ${idade} min, ainda vale. Pulando este ciclo.`);
    await anunciar(false);
    return;
  }

  const cadastro = JSON.parse(await readFile(join(RAIZ, 'dados', 'rios-estacoes.json'), 'utf8'));
  const estacoes = cadastro.estacoes;
  console.log(`Rios — ${estacoes.length} estações da ANA na Amazônia Legal`);

  const fim = fimDaGrade();
  // A grade cobre 48 h; duas horas de folga atrás bastam para a leitura mais
  // próxima do primeiro slot. O `dataFim` do serviço é inclusivo por DIA (em
  // Brasília), então pedir "amanhã" garante que as leituras de hoje venham.
  const inicioBusca = fim - (SLOTS + 1) * PASSO_MS;
  const fimBusca = Date.now() + 24 * 60 * 60 * 1000;

  const prazo = inicioExec + PRAZO_MS;
  const cancelar = new AbortController();
  const relogio = setTimeout(() => cancelar.abort(), Math.max(0, prazo - Date.now()));

  const { ok, erros } = await emLotes(
    estacoes,
    async (e) => {
      const l = await leituras(e.cod, inicioBusca, fimBusca, { sinal: cancelar.signal });
      // Zero linhas numa janela de dois dias não é "rio sem dado": é a ANA
      // respondendo vazio. Tratar como falha faz a estação herdar a anterior.
      if (l.length === 0) throw new Error(`estação ${e.cod}: resposta sem leituras`);
      return { estacao: e, leituras: l };
    },
    { aoAndar: (f, n) => console.log(`  ${f}/${n}…`), prazo },
  );
  clearTimeout(relogio);
  const esgotou = Date.now() > prazo;
  console.log(`  ${ok.length} estações lidas, ${erros.length} falharam${esgotou ? ' · PRAZO ESGOTADO' : ''}`);
  if (erros.length) console.log(`  falhas: ${erros.slice(0, 5).map((x) => x.item.nome).join(', ')}${erros.length > 5 ? '…' : ''}`);

  const historico = await lerHistorico();
  const sace = await situacoesSace();
  if (sace.size) console.log(`  SACE: ${sace.size} estações com situação declarada`);

  const doy = diaDoAno(fim);
  const linhas = {};
  const fichas = [];
  const contagem = { muitoAbaixo: 0, abaixo: 0, normal: 0, acima: 0, muitoAcima: 0, semRef: 0 };
  const lidas = new Set();

  for (const { estacao, leituras: l } of ok) {
    const grade = gradeHoraria(l, fim);
    const ultimo = ultimoDa(grade.v);
    if (!ultimo) continue;
    lidas.add(estacao.cod);

    const d24 = variacao(grade.v, 24);
    const d48 = variacao(grade.v, 47);

    // A referência é do DIA DO ANO, com janela de ±15 dias — não do mês. A
    // mediana mensal equivale ao dia 15 e embutiria meio metro de "anomalia"
    // que é só calendário num rio que sobe 20 cm por dia.
    const dias = historico?.estacoes?.[estacao.cod] ?? null;
    const ref = referenciaDoDia(dias, doy);
    const pct = posicaoPercentil(ultimo.cm, ref);
    const classe = classePorPercentil(pct);
    const ext = extremosDe(dias);

    if (classe) contagem[classe]++;
    else contagem.semRef++;

    linhas[estacao.cod] = grade.v;

    // A vazão é a da última leitura com valor, e não precisa ser a mesma hora
    // do nível: nem toda estação mede as duas coisas no mesmo instante.
    let vazao = null;
    for (let i = l.length - 1; i >= 0; i--) {
      if (Number.isFinite(l[i].vazao)) {
        vazao = Math.round(l[i].vazao * 100) / 100;
        break;
      }
    }

    fichas.push({
      c: estacao.cod,
      cm: ultimo.cm,
      ms: grade.t0 + ultimo.i * PASSO_MS,
      q: vazao,
      d24,
      d48,
      t: tendenciaDe(d24),
      a: classe,
      // Posição percentílica: adimensional, e é o que torna estações de rios
      // diferentes comparáveis entre si.
      p: pct,
      an: anomaliaCm(ultimo.cm, ref),
      ref: ref ? { p50: ref.p50, p25: ref.p25, p75: ref.p75, anos: ref.anos } : undefined,
      // Extremos DESTA estação no período medido — não são o recorde
      // histórico da régua, que em Manaus começa em 1902.
      ext: ext ? { mx: ext.max.cm, mxd: ext.max.data, mn: ext.min.cm, mnd: ext.min.data, desde: ext.desde } : undefined,
      // Leitura mais recente sem o carimbo de aprovação da ANA.
      bruta: grade.bruta[ultimo.i] || undefined,
      sit: sace.get(estacao.cod) ?? undefined,
    });
  }

  // Estação que falhou nesta rodada (ou ficou fora do prazo) HERDA a
  // publicação anterior, com a série deslocada para a grade nova. Sem isso,
  // toda falha transitória apagava a estação do mapa por uma hora — e com o
  // prazo global isso viraria rotina. A ficha herdada carrega `h: true`.
  const herdadas = herdar(atualAnterior, fim, estacoes, lidas, linhas, fichas, contagem);
  if (herdadas) console.log(`  ${herdadas} estações herdaram a publicação anterior`);

  // Uma rodada que só trouxe migalhas, mesmo com herança, não substitui a boa.
  if (fichas.length < estacoes.length * 0.5) {
    throw new Error(`só ${fichas.length} de ${estacoes.length} estações com leitura — não publicando`);
  }

  console.log(
    `  ${fichas.length} com leitura · abaixo do normal ${contagem.muitoAbaixo + contagem.abaixo}, ` +
      `dentro ${contagem.normal}, acima ${contagem.acima + contagem.muitoAcima}, sem referência ${contagem.semRef}`,
  );

  // ------------------------------------------------------------------------
  // Escrita ATÔMICA: monta tudo num diretório temporário e só move para
  // `site/` quando os três arquivos existem e passam na conferência.
  //
  // Sem isso, morrer entre dois `writeFile` publicaria manifesto novo com
  // dado velho — e, pior, o carimbo fresco silenciaria o passo pelas quatro
  // horas seguintes.
  // ------------------------------------------------------------------------
  const temp = join(SAIDA, '.rios-tmp');
  await rm(temp, { recursive: true, force: true });
  await mkdir(temp, { recursive: true });

  try {
  const gerado = new Date().toISOString();
  const comum = { gerado, fonte: ATRIBUICAO, portal: 'https://www.snirh.gov.br/hidroweb/' };

  await escrever(temp, 'estacoes.json', {
    ...comum,
    estacoes: estacoes.map((e) => ({
      c: e.cod,
      n: e.nome,
      y: Math.round(e.lat * 1e5) / 1e5,
      x: Math.round(e.lon * 1e5) / 1e5,
      v: e.vazao || undefined,
    })),
  });

  await escrever(temp, 'atual.json', {
    ...comum,
    t0: fim - (SLOTS - 1) * PASSO_MS,
    passoMin: PASSO_MS / 60_000,
    slots: SLOTS,
    rios: fichas,
    serie: linhas,
  });

  await escrever(temp, 'manifest.json', {
    ...comum,
    estacoes: estacoes.length,
    comLeitura: fichas.length,
    falhas: erros.length,
    herdadas,
    prazoEsgotado: esgotou || undefined,
    contagem,
    historico: historico
      ? { gerado: historico.gerado, periodo: historico.periodo, estacoes: Object.keys(historico.estacoes).length }
      : null,
    duracaoMs: Date.now() - inicioExec,
  });

  // Conferência antes de publicar.
  const atual = JSON.parse(await readFile(join(temp, 'atual.json'), 'utf8'));
  if (!Array.isArray(atual.rios) || atual.rios.length === 0) throw new Error('atual.json sem estações');
  for (const [cod, linha] of Object.entries(atual.serie)) {
    if (!Array.isArray(linha) || linha.length !== SLOTS) {
      throw new Error(`série de ${cod} com ${linha?.length} valores, esperava ${SLOTS}`);
    }
  }

  await mkdir(join(SAIDA, 'rios'), { recursive: true });
  for (const nome of ['estacoes.json', 'atual.json', 'manifest.json']) {
    await cp(join(temp, nome), join(SAIDA, 'rios', nome));
  }
  } finally {
    // Nunca deixar a pasta temporária dentro de `site/`: ela iria para o Pages.
    await rm(temp, { recursive: true, force: true });
  }

  await anunciar(true);
  console.log(`\nOK — ${fichas.length} estações · ${Date.now() - inicioExec} ms`);

  if (process.env.GITHUB_STEP_SUMMARY) {
    await writeFile(
      process.env.GITHUB_STEP_SUMMARY,
      [
        `### Rios — ciclo de ${gerado}`,
        '',
        '| | |',
        '|---|---|',
        `| Estações com leitura | ${fichas.length} de ${estacoes.length} |`,
        `| Abaixo do normal | ${contagem.muitoAbaixo + contagem.abaixo} |`,
        `| Dentro do normal | ${contagem.normal} |`,
        `| Acima do normal | ${contagem.acima + contagem.muitoAcima} |`,
        `| Sem referência histórica | ${contagem.semRef} |`,
        `| Duração | ${((Date.now() - inicioExec) / 1000).toFixed(1)} s |`,
        '',
      ].join('\n'),
      { flag: 'a' },
    );
  }
}

/**
 * Copia para a publicação nova as estações que esta rodada não conseguiu ler,
 * deslocando a série de 48 h para a grade de agora. Devolve quantas herdaram.
 *
 * A ficha herdada mantém cota, tendência e classe da rodada anterior — que é
 * a última verdade conhecida — e ganha `h: true` para a interface poder
 * dizer "leitura anterior". Série que não alcança a grade nova (publicação
 * velha demais) não é herdada: melhor sumir que mostrar dado de dias atrás.
 */
function herdar(anterior, fim, estacoes, lidas, linhas, fichas, contagem) {
  if (!anterior || !anterior.serie) return 0;
  const porCod = new Map((anterior.rios ?? []).map((r) => [r.c, r]));
  let n = 0;
  for (const e of estacoes) {
    if (lidas.has(e.cod)) continue;
    const fichaAnt = porCod.get(e.cod);
    const serie = deslocarSerie(anterior.serie[e.cod], anterior.t0, anterior.passoMin, fim);
    if (!serie || !fichaAnt) continue;
    linhas[e.cod] = serie;
    fichas.push({ ...fichaAnt, h: true });
    if (fichaAnt.a) contagem[fichaAnt.a] = (contagem[fichaAnt.a] ?? 0) + 1;
    else contagem.semRef++;
    n++;
  }
  return n;
}

export const ATRIBUICAO =
  'Nível e vazão: Agência Nacional de Águas e Saneamento Básico (ANA) — rede telemétrica do SNIRH';

/**
 * Médias diárias históricas, commitadas no repositório (`rios-dias.mjs`).
 *
 * A estatística sai daqui a cada ciclo, em vez de vir pronta num arquivo
 * derivado: assim a referência é sempre a do DIA DE HOJE, e mudar o método
 * não exige baixar de novo quase 3 GB de um serviço público.
 *
 * Ausência não derruba a camada: as estações vão sem cor, o que é honesto.
 */
async function lerHistorico() {
  const caminho = join(RAIZ, 'dados', 'rios-dias.json');
  if (!existsSync(caminho)) {
    console.warn('  [aviso] sem histórico: as estações vão sem referência');
    return null;
  }
  try {
    const j = JSON.parse(await readFile(caminho, 'utf8'));
    console.log(`  histórico: ${Object.keys(j.estacoes ?? {}).length} estações, ${j.periodo?.[0]}–${j.periodo?.[1]}`);
    return j;
  } catch (e) {
    console.warn(`  [aviso] histórico ilegível: ${e.message}`);
    return null;
  }
}

async function escrever(dir, nome, obj) {
  const texto = JSON.stringify(obj);
  await writeFile(join(dir, nome), texto);
  console.log(`  rios/${nome}: ${(texto.length / 1024).toFixed(0)} KB`);
}

main()
  .catch(async (e) => {
    console.error(`\nFALHOU: ${e.message}`);
    await anunciar(false);
    // O passo roda com `continue-on-error`: a chuva publica do mesmo jeito e o
    // `preservar.mjs` restaura a pasta `rios/` do ar.
    process.exitCode = 1;
  })
  .finally(() => {
    // Nada pode segurar o event loop depois do ciclo: um passo pendurado
    // cancela o ciclo seguinte da chuva.
    process.exit(process.exitCode ?? 0);
  });
