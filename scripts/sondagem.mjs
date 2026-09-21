/**
 * Ciclo de ingestão das sondagens.
 *
 * Roda no workflow próprio (`sondagem.yml`), 4×/dia. Busca no Wyoming as
 * sondagens mais recentes de cada estação dentro da janela do loop e escreve
 * `site/sondagem/`, que o ciclo da chuva recebe pelo cache do Actions e publica.
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
import { instantesDeSondagem, sondagem, trajetoria, urlSkewT } from './wyoming.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const SAIDA = isAbsolute(process.env.SAIDA ?? '') ? process.env.SAIDA : join(RAIZ, process.env.SAIDA || 'site');

const ATRIBUICAO =
  'Sondagens: University of Wyoming, Department of Atmospheric Science (https://weather.uwyo.edu/upperair/)';

/** Quantas horas para trás procurar a última sondagem de cada estação. */
export const JANELA_HORAS = 48;

/** Quantas sondagens guardar por estação. */
export const MAX_SONDAGENS = 3;

/**
 * As últimas sondagens de uma estação, da mais recente para a mais antiga.
 *
 * Três, e não uma: o loop do AmaView cobre até 24 h e a rede lança 2× por dia.
 * Com só a mais recente, metade da animação ficava sem nenhuma sondagem no
 * mapa — inclusive lançamentos que estavam DENTRO da janela exibida.
 *
 * Para de procurar ao juntar `MAX_SONDAGENS` ou ao esgotar a janela: varrer
 * tudo seria dezenas de requisições inúteis numa rede que falha com frequência
 * (Manaus passou 55 h muda durante o levantamento).
 */
export async function ultimasSondagens(wmo, agora = Date.now(), janelaHoras = JANELA_HORAS, max = MAX_SONDAGENS) {
  const out = [];
  for (const ms of instantesDeSondagem(agora, janelaHoras)) {
    const s = await sondagem(wmo, ms);
    if (s) out.push(s);
    if (out.length >= max) break;
  }
  return out;
}

async function main() {
  const inicio = Date.now();
  const agora = Date.now();
  const perfis = [];
  let comDado = 0;
  let comErro = 0;

  for (const e of ESTACOES_SONDAGEM) {
    try {
      const lista = await ultimasSondagens(e.wmo, agora);
      if (lista.length === 0) {
        perfis.push({ wmo: e.wmo, ms: null, niveis: [], indices: {}, trilha: null, erro: null });
        console.log(`  ${e.nome.padEnd(26)} sem sondagem nas últimas ${JANELA_HORAS} h`);
        continue;
      }

      const resumo = [];
      for (const s of lista) {
        const niveis = reduzirNiveis(s.niveis);
        // A trilha é um extra: se falhar, o perfil ainda vale.
        let trilha = null;
        try {
          trilha = await trajetoria(e.wmo, s.ms);
        } catch (err) {
          console.warn(`  ${e.nome.padEnd(26)} trilha indisponível: ${err.message}`);
        }
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
          // `t` é o instante medido em cada ponto: é o que permite mostrar
          // ONDE o balão estava no quadro exibido, indo e voltando no tempo.
          // Guardado como segundos relativos a `ms` — negativo antes dele,
          // porque o balão sobe ~45 min antes da hora cheia.
          trilha: trilha
            ? trilha.map((p) => ({
                y: Math.round(p.lat * 1e4) / 1e4,
                x: Math.round(p.lon * 1e4) / 1e4,
                z: Math.round(p.z),
                t: p.ms === null ? null : Math.round((p.ms - s.ms) / 1000),
              }))
            : null,
          erro: null,
        });
        resumo.push(`${new Date(s.ms).toISOString().slice(11, 13)}Z${trilha ? `/${trilha.length}p` : ''}`);
      }
      comDado++;
      const idade = ((agora - lista[0].ms) / 3_600_000).toFixed(0);
      console.log(`  ${e.nome.padEnd(26)} ${lista.length} sondagens: ${resumo.join(' ')} · mais nova ${idade} h`);
    } catch (err) {
      comErro++;
      perfis.push({ wmo: e.wmo, ms: null, niveis: [], indices: {}, trilha: null, erro: String(err.message).slice(0, 120) });
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
    comTrilha: perfis.filter((p) => p.trilha?.length).length,
    perfis: perfis.length,
    maxSondagens: MAX_SONDAGENS,
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
