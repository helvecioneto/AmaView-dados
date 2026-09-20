/**
 * Ciclo de ingestão das sondagens.
 *
 * Roda junto com o da chuva (`build.mjs`), no mesmo workflow. Busca no Wyoming
 * a sondagem mais recente de cada estação dentro da janela do loop e publica
 * um JSON por estação com o perfil reduzido e os índices.
 *
 * Falha de uma estação NÃO derruba o ciclo: o servidor é acadêmico e sem SLA,
 * e já deu timeout durante o levantamento. A estação entra no resultado com
 * `erro`, que o app mostra — silêncio seria pior.
 *
 *   node scripts/sondagem.mjs
 */
import { mkdir, writeFile } from 'node:fs/promises';
import { join, dirname, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ESTACOES_SONDAGEM, reduzirNiveis } from './estacoes-sondagem.mjs';
import { instantesDeSondagem, sondagem, urlSkewT } from './wyoming.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const SAIDA = isAbsolute(process.env.SAIDA ?? '') ? process.env.SAIDA : join(RAIZ, process.env.SAIDA || 'site');

const ATRIBUICAO =
  'Sondagens: University of Wyoming, Department of Atmospheric Science (https://weather.uwyo.edu/upperair/)';

/** Quantas horas para trás procurar a última sondagem de cada estação. */
export const JANELA_HORAS = 48;

/**
 * Procura, do mais recente para o mais antigo, a primeira sondagem que existe.
 *
 * Para na primeira encontrada: a rede lança duas vezes por dia e falha com
 * frequência (Manaus passou 55 h muda durante o levantamento), então varrer
 * tudo seria dezenas de requisições inúteis por estação.
 */
export async function ultimaSondagem(wmo, agora = Date.now(), janelaHoras = JANELA_HORAS) {
  const instantes = instantesDeSondagem(agora, janelaHoras);
  for (const ms of instantes) {
    const s = await sondagem(wmo, ms);
    if (s) return s;
  }
  return null;
}

async function main() {
  const inicio = Date.now();
  const agora = Date.now();
  const perfis = [];
  let comDado = 0;
  let comErro = 0;

  for (const e of ESTACOES_SONDAGEM) {
    try {
      const s = await ultimaSondagem(e.wmo, agora);
      if (!s) {
        perfis.push({ wmo: e.wmo, ms: null, niveis: [], indices: {}, erro: null });
        console.log(`  ${e.nome.padEnd(26)} sem sondagem nas últimas ${JANELA_HORAS} h`);
        continue;
      }
      const niveis = reduzirNiveis(s.niveis);
      perfis.push({
        wmo: e.wmo,
        ms: s.ms,
        niveis: niveis.map((n) => ({
          p: Math.round(n.p * 10) / 10,
          z: Math.round(n.z),
          t: n.t === null ? null : Math.round(n.t * 10) / 10,
          d: n.d === null ? null : Math.round(n.d * 10) / 10,
        })),
        indices: s.indices,
        erro: null,
      });
      comDado++;
      const idade = ((agora - s.ms) / 3_600_000).toFixed(0);
      console.log(`  ${e.nome.padEnd(26)} ${new Date(s.ms).toISOString().slice(0, 13)}Z · ${niveis.length} níveis (${s.niveis.length} brutos) · ${idade} h`);
    } catch (err) {
      comErro++;
      perfis.push({ wmo: e.wmo, ms: null, niveis: [], indices: {}, erro: String(err.message).slice(0, 120) });
      console.warn(`  ${e.nome.padEnd(26)} FALHOU: ${err.message}`);
    }
  }

  const gerado = new Date().toISOString();
  const comum = { fonte: ATRIBUICAO, portal: 'https://weather.uwyo.edu/upperair/', gerado };

  await mkdir(join(SAIDA, 'sondagem'), { recursive: true });

  await escrever('sondagem/estacoes.json', {
    ...comum,
    estacoes: ESTACOES_SONDAGEM.map((e) => ({
      w: e.wmo,
      n: e.nome,
      u: e.uf,
      y: e.lat,
      x: e.lon,
    })),
  });

  await escrever('sondagem/perfis.json', { ...comum, perfis });

  await escrever('sondagem/manifest.json', {
    ...comum,
    estacoes: ESTACOES_SONDAGEM.length,
    comSondagem: comDado,
    comErro,
    janelaHoras: JANELA_HORAS,
    duracaoMs: Date.now() - inicio,
  });

  console.log(
    `\nOK — ${comDado} de ${ESTACOES_SONDAGEM.length} com sondagem` +
      `${comErro ? `, ${comErro} com erro` : ''} · ${((Date.now() - inicio) / 1000).toFixed(0)} s`,
  );

  if (process.env.GITHUB_STEP_SUMMARY) {
    await writeFile(
      process.env.GITHUB_STEP_SUMMARY,
      [
        `### Sondagens — ${gerado}`,
        '',
        `| | |`,
        `|---|---|`,
        `| Com sondagem | ${comDado} de ${ESTACOES_SONDAGEM.length} |`,
        `| Com erro | ${comErro} |`,
        `| Duração | ${((Date.now() - inicio) / 1000).toFixed(0)} s |`,
        '',
      ].join('\n'),
      { flag: 'a' },
    );
  }
}

async function escrever(rel, obj) {
  const texto = JSON.stringify(obj);
  await writeFile(join(SAIDA, rel), texto);
  console.log(`  ${rel}: ${(texto.length / 1024).toFixed(0)} KB`);
}

export { urlSkewT };

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error(`\nFALHOU: ${e.message}`);
    process.exitCode = 1;
  });
}
