/**
 * Ciclo das embarcações: lê o AIS, funde com o que já sabíamos e escreve o
 * que o GitHub Pages publica.
 *
 * Roda como PASSO do workflow da chuva, e não como workflow próprio. O motivo
 * é concreto: o grupo de concorrência `publicar-pages` guarda no máximo uma
 * execução rodando e uma pendente, e quando uma terceira entra a pendente é
 * **cancelada** — em silêncio, com status `cancelled`. Um terceiro workflow
 * na mesma cadência comeria ciclos da chuva sem ninguém perceber. Um ciclo,
 * uma publicação, zero contenção.
 *
 * Como nas outras camadas, o estado entre ciclos vem da própria publicação
 * anterior, relida pela URL pública. É isso que faz o cadastro (nome, IMO,
 * destino, ETA) encorpar com o tempo sem precisar de banco nem de serviço
 * permanente: cada ciclo acrescenta o que ouviu.
 *
 *   node scripts/navios.mjs
 *
 * Variáveis de ambiente:
 *   BASE_URL           De onde reler a publicação anterior.
 *   SAIDA              Diretório de saída (padrão: `site`).
 *   OPENWATERS_TOKEN   Opcional; eleva o teto de área. Funciona sem.
 *   AIS_JANELA_MS      Janela do WebSocket (padrão 90 s).
 */
import { mkdir, writeFile, readFile, rm, cp } from 'node:fs/promises';
import { join, dirname, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { buscarPosicoes, colherEstaticos, CAIXAS } from './ais.mjs';
import {
  CELULA_GRAU,
  FONTES_ACEITAS,
  arredondar,
  boca,
  classificar,
  comprimentoDe,
  juntarAtribuicao,
  mesclarEscuta,
  mesclarEstatico,
  mesclarTrilha,
  podarCadastro,
} from './frota.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const SAIDA = isAbsolute(process.env.SAIDA ?? '') ? process.env.SAIDA : join(RAIZ, process.env.SAIDA || 'site');
const BASE_URL = process.env.BASE_URL || 'https://helvecioneto.github.io/AmaView-dados';
const JANELA_MS = Number(process.env.AIS_JANELA_MS) || 90_000;

/** Lê um arquivo da publicação anterior. Ausência não é erro: é o 1º ciclo. */
async function anterior(nome) {
  try {
    // `?t=` fura a CDN do Pages (max-age=600): sem isso, um ciclo pode reler
    // a publicação de DOIS ciclos atrás e perder uma amostra da trilha.
    const r = await fetch(`${BASE_URL}/navios/${nome}?t=${Date.now()}`, {
      signal: AbortSignal.timeout(15_000),
      cache: 'no-store',
    });
    if (!r.ok) return null;
    const t = await r.text();
    if (t.length < 2 || t.trimStart().startsWith('<')) return null;
    return JSON.parse(t);
  } catch (e) {
    console.warn(`  [aviso] não li ${nome} da publicação anterior: ${e.message}`);
    return null;
  }
}

/**
 * MMSI que não devem ser publicados, a pedido.
 *
 * É o canal de oposição que a LGPD (art. 18) exige de quem republica dado
 * pessoal. Fica versionado no repositório, e não na publicação, para que a
 * remoção seja auditável e não possa ser desfeita por um ciclo ruim.
 */
async function suprimidos() {
  try {
    const t = await readFile(join(RAIZ, 'dados', 'suprimidos.json'), 'utf8');
    const j = JSON.parse(t);
    return new Set((j.mmsi ?? []).map(Number).filter(Number.isFinite));
  } catch {
    return new Set();
  }
}

async function main() {
  const inicioExec = Date.now();
  const agora = Date.now();

  console.log(`Embarcações — ${CAIXAS.length} caixas, janela de ${JANELA_MS / 1000} s`);

  const [antAtual, antCadastro, antEscuta, omitidos] = await Promise.all([
    anterior('atual.json'),
    anterior('cadastro.json'),
    anterior('escuta.json'),
    suprimidos(),
  ]);
  if (omitidos.size) console.log(`  ${omitidos.size} MMSI na lista de supressão`);

  // 1. Posições — a fonte autoritativa. Se isto falhar, o ciclo falha e a
  //    publicação anterior continua no ar.
  const { navios: posicoes, atribuicao: atrRest, porCaixa } = await buscarPosicoes();
  for (const { nome, n } of porCaixa) console.log(`  ${nome}: ${n}`);
  console.log(`  ${posicoes.length} embarcações distintas`);

  // 2. Estático — acréscimo. Falha aqui não derruba o ciclo.
  const colhido = await colherEstaticos({ msMax: JANELA_MS });
  if (colhido.erro) console.warn(`  [aviso] fluxo estático: ${colhido.erro}`);
  console.log(
    `  fluxo: ${colhido.mensagens} mensagens, ${colhido.estaticos.size} fichas estáticas` +
      (colhido.fontes.length ? ` (${colhido.fontes.map(([f, n]) => `${f}:${n}`).join(', ')})` : ''),
  );

  const atribuicao = juntarAtribuicao(atrRest, colhido.atribuicao);
  console.log(`  fontes creditadas: ${Object.keys(atribuicao).join(', ') || '(nenhuma)'}`);

  // 3. Cadastro: funde o estático novo no acumulado, campo a campo.
  const cadastro = podarCadastro(antCadastro?.navios ?? {}, agora);
  for (const [mmsi, e] of colhido.estaticos) {
    if (omitidos.has(mmsi)) continue;
    if (e.fonte && !FONTES_ACEITAS.includes(e.fonte)) continue;
    const comp = comprimentoDe(e.dim);
    const larg = boca(e.dim);
    // A regra de privacidade vale ANTES de guardar: uma embarcação de recreio
    // ouvida só no fluxo (nunca no snapshot de posições) entraria no cadastro
    // com nome e indicativo, mesmo sendo do tipo que não publicamos.
    if (classificar(e.tipo, comp, 'ShipStaticData') !== 'completo') continue;
    cadastro[mmsi] = mesclarEstatico(
      cadastro[mmsi],
      {
        n: e.nome,
        i: e.imo,
        ind: e.indicativo,
        t: e.tipo,
        comp,
        larg,
        cal: e.calado,
        dst: e.destino,
        eta: etaUtil(e.eta),
        f: e.fonte,
      },
      agora,
    );
  }

  // 4. Trilhas e classificação de privacidade.
  const trilhaAnterior = new Map((antAtual?.navios ?? []).map((n) => [n.m, n.t]));
  const publicados = [];
  const anonimos = [];
  const descartadas = { fonte: 0, suprimida: 0, omitida: 0 };

  for (const p of posicoes) {
    if (omitidos.has(p.mmsi)) {
      descartadas.suprimida++;
      continue;
    }
    if (p.fonte && !FONTES_ACEITAS.includes(p.fonte)) {
      descartadas.fonte++;
      continue;
    }

    const ficha = cadastro[p.mmsi] ?? {};
    // O tipo do cadastro é mais confiável que o do snapshot: veio da mensagem
    // estática, que é onde o campo de fato existe.
    const tipo = ficha.t ?? p.tipo ?? 0;
    const trato = classificar(tipo, ficha.comp ?? null, p.msgType);

    if (trato === 'omitir') {
      descartadas.omitida++;
      // Some também do cadastro: o que não é publicado não é guardado.
      delete cadastro[p.mmsi];
      continue;
    }

    if (trato === 'anonimo') {
      // Sem MMSI, sem nome, sem trilha, sem persistir entre ciclos.
      anonimos.push({
        y: arredondar(p.lat),
        x: arredondar(p.lon),
        c: p.cog === null ? null : Math.round(p.cog),
        s: p.sog === null ? null : Math.round(p.sog * 10) / 10,
        t: tipo || null,
      });
      delete cadastro[p.mmsi];
      continue;
    }

    ficha.v = Math.max(Number(ficha.v) || 0, p.visto || agora);
    cadastro[p.mmsi] = ficha;

    publicados.push({
      m: p.mmsi,
      y: arredondar(p.lat),
      x: arredondar(p.lon),
      s: p.sog === null ? null : Math.round(p.sog * 10) / 10,
      c: p.cog === null ? null : Math.round(p.cog * 10) / 10,
      h: p.heading,
      n: p.navStatus,
      v: p.visto || agora,
      f: p.fonte,
      t: mesclarTrilha(trilhaAnterior.get(p.mmsi), { ms: p.visto || agora, lat: p.lat, lon: p.lon, sog: p.sog }, agora),
    });
  }

  console.log(
    `  publicadas: ${publicados.length} completas, ${anonimos.length} anônimas · ` +
      `descartadas: ${descartadas.omitida} recreio/vela, ${descartadas.fonte} fonte não aceita, ${descartadas.suprimida} suprimidas`,
  );

  // Uma embarcação que saiu do alcance nesta rodada não perde a trilha: ela
  // continua publicada, sem posição nova, até a janela de 24 h esvaziá-la.
  const vistosAgora = new Set(publicados.map((n) => n.m));
  let herdadas = 0;
  for (const antes of antAtual?.navios ?? []) {
    if (vistosAgora.has(antes.m) || omitidos.has(antes.m)) continue;
    const t = mesclarTrilha(antes.t, null, agora);
    if (t.length === 0) continue;
    const ultimo = t[t.length - 1];
    publicados.push({ ...antes, y: ultimo[1], x: ultimo[2], v: ultimo[0], t, sumiu: true });
    herdadas++;
  }
  if (herdadas) console.log(`  ${herdadas} embarcações fora de alcance, com trilha preservada`);

  // 5. Escuta: onde o AIS foi de fato ouvido, para o mapa poder distinguir
  //    "rio vazio" de "sem recepção".
  const celulas = mesclarEscuta(antEscuta?.celulas, [...posicoes, ...anonimos.map((a) => ({ lat: a.y, lon: a.x }))], agora);
  console.log(`  escuta: ${Object.keys(celulas).length} células de ${CELULA_GRAU}° nos últimos 7 dias`);

  // 6. Escrita ATÔMICA: tudo num diretório temporário e só depois para
  //    `site/navios/`. Morrer entre dois arquivos publicaria `atual.json` novo
  //    com `escuta.json` restaurado do ar e carimbo velho no manifesto.
  const gerado = new Date().toISOString();
  const temp = join(SAIDA, '.navios-tmp');
  await rm(temp, { recursive: true, force: true });
  await mkdir(temp, { recursive: true });
  const NOMES = ['atual.json', 'cadastro.json', 'escuta.json', 'manifest.json'];

  try {
  await escrever('navios/atual.json', {
    gerado,
    attribution: atribuicao,
    trilhaHoras: 24,
    navios: publicados,
    anonimos,
  });
  await escrever('navios/cadastro.json', { gerado, attribution: atribuicao, navios: cadastro });
  await escrever('navios/escuta.json', { gerado, celulaGrau: CELULA_GRAU, celulas });
  await escrever('navios/manifest.json', {
    gerado,
    attribution: atribuicao,
    fonte: 'ais.openwaters.io (aiscast)',
    caixas: CAIXAS.map((c) => ({ nome: c.nome, bbox: c.bbox })),
    completas: publicados.length,
    anonimas: anonimos.length,
    comFicha: publicados.filter((n) => cadastro[n.m]?.n).length,
    comDestino: publicados.filter((n) => cadastro[n.m]?.dst).length,
    cadastro: Object.keys(cadastro).length,
    celulasEscuta: Object.keys(celulas).length,
    janelaMs: JANELA_MS,
    mensagensFluxo: colhido.mensagens,
    duracaoMs: Date.now() - inicioExec,
  });

  // Conferência antes de publicar.
  const conf = JSON.parse(await readFile(join(temp, 'atual.json'), 'utf8'));
  if (!Array.isArray(conf.navios)) throw new Error('atual.json sem `navios`');

  await mkdir(join(SAIDA, 'navios'), { recursive: true });
  for (const nome of NOMES) await cp(join(temp, nome), join(SAIDA, 'navios', nome));
  } finally {
    await rm(temp, { recursive: true, force: true });
  }

  console.log(`\nOK — ${publicados.length} embarcações · ${Object.keys(cadastro).length} no cadastro · ${Date.now() - inicioExec} ms`);

  if (process.env.GITHUB_STEP_SUMMARY) {
    const comDestino = publicados.filter((n) => cadastro[n.m]?.dst).length;
    await writeFile(
      process.env.GITHUB_STEP_SUMMARY,
      [
        `### Embarcações — ciclo de ${gerado}`,
        '',
        '| | |',
        '|---|---|',
        `| Publicadas | ${publicados.length} completas + ${anonimos.length} anônimas |`,
        `| Com ficha (nome) | ${publicados.filter((n) => cadastro[n.m]?.n).length} |`,
        `| Com destino declarado | ${comDestino} |`,
        `| Cadastro acumulado | ${Object.keys(cadastro).length} MMSI |`,
        `| Células de escuta | ${Object.keys(celulas).length} |`,
        `| Fontes | ${Object.keys(atribuicao).join(', ') || '—'} |`,
        `| Duração | ${((Date.now() - inicioExec) / 1000).toFixed(1)} s |`,
        '',
      ].join('\n'),
      { flag: 'a' },
    );
  }
}

/** ETA só entra no cadastro se for uma data possível (ver `eta.ts` no app). */
function etaUtil(eta) {
  if (!eta) return null;
  const { Month: mes, Day: dia, Hour: hora, Minute: min } = eta;
  if (!mes || !dia || mes < 1 || mes > 12 || dia < 1 || dia > 31) return null;
  if (!Number.isFinite(hora) || !Number.isFinite(min) || hora > 23 || min > 59) return null;
  return { mes, dia, hora, min };
}

/** Escreve no diretório temporário; o nome vem com o prefixo `navios/` só para o log. */
async function escrever(rel, obj) {
  const texto = JSON.stringify(obj);
  await writeFile(join(SAIDA, '.navios-tmp', rel.replace(/^navios\//, '')), texto);
  console.log(`  ${rel}: ${(texto.length / 1024).toFixed(0)} KB`);
}

main()
  .catch((e) => {
    console.error(`\nFALHOU: ${e.message}`);
    // O passo roda com `continue-on-error`: a chuva publica mesmo assim, e o
    // `preservar.mjs` restaura a pasta `navios/` do ar. Uma falha aqui nunca
    // pode derrubar a camada principal do portal.
    process.exitCode = 1;
  })
  .finally(() => {
    // Um WebSocket que não completou o handshake de fechamento segura o event
    // loop para sempre — e um passo pendurado cancela o ciclo seguinte da chuva.
    process.exit(process.exitCode ?? 0);
  });
