/**
 * Ciclo de ingestão: busca o CEMADEN, monta a grade e escreve o site que o
 * GitHub Pages publica.
 *
 * Roda a cada 10 minutos pelo workflow `dados.yml`. NÃO faz commit: os
 * arquivos vão direto como artifact do Pages, então o histórico do git não
 * cresce com 144 execuções por dia.
 *
 * O estado entre ciclos (a grade anterior) vem da própria versão publicada,
 * lida de volta pela URL pública. Sem token isso é o que permite a série
 * existir; com token a grade é remontada inteira a cada ciclo e a leitura
 * anterior serve só para preencher buracos.
 *
 *   node scripts/build.mjs
 *
 * Variáveis de ambiente:
 *   CEMADEN_EMAIL  E-mail da conta na PED.
 *   CEMADEN_SENHA  Senha da conta na PED. Com os dois, cada ciclo pede um
 *                  token novo — o JWT da PED vale só 4 h e não pode ser
 *                  guardado como secret.
 *   CEMADEN_TOKEN  JWT pronto (execução manual/depuração). Expira em 4 h.
 *   BASE_URL       De onde reler a publicação anterior.
 *   SAIDA          Diretório de saída (padrão: `site`).
 *
 * Sem credencial nenhuma, cai no instantâneo aberto — que também funciona.
 */
import { mkdir, writeFile, readFile, cp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  dadosRede,
  emLotes,
  horarias48,
  instantaneoAberto,
  minutosAteExpirar,
  renovarToken,
  UFS_AMAZONIA,
} from './cemaden.mjs';
import {
  PASSO_MS,
  SLOTS,
  acum24hPorFatia,
  alinhar,
  arredondar,
  contarMedidas,
  deslocarEGravar,
  fatiasComDado,
  gradeDeLeituras,
} from './serie.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const SAIDA = join(RAIZ, process.env.SAIDA || 'site');
const BASE_URL = process.env.BASE_URL || 'https://helvecioneto.github.io/AmaView-dados';
const TOKEN_FIXO = process.env.CEMADEN_TOKEN?.trim() || null;
const EMAIL = process.env.CEMADEN_EMAIL?.trim() || null;
const SENHA = process.env.CEMADEN_SENHA || null;

/**
 * Token para este ciclo. Credenciais têm prioridade sobre um token fixo,
 * porque só elas sobrevivem ao próximo ciclo.
 *
 * O claim `exp` da PED NÃO é confiável: um token recém-emitido já chegou a
 * anunciar `exp` no passado e mesmo assim foi aceito, devolvendo as ~19 mil
 * leituras do ciclo. Por isso ele nunca decide se vamos usar o token — só
 * entra no relatório, e quem diz a palavra final é a própria API, cuja recusa
 * já cai na fonte aberta em `main()`.
 */
async function obterToken() {
  if (EMAIL && SENHA) {
    const t = await renovarToken(EMAIL, SENHA);
    console.log(`Token renovado${descreverValidade(t)}`);
    return t;
  }
  if (TOKEN_FIXO) {
    console.log(`Usando CEMADEN_TOKEN${descreverValidade(TOKEN_FIXO)}`);
    console.warn('[aviso] token fixo não é renovado; prefira CEMADEN_EMAIL e CEMADEN_SENHA.');
    return TOKEN_FIXO;
  }
  return null;
}

/** Validade anunciada pelo token, sem tratá-la como verdade. */
function descreverValidade(token) {
  const min = minutosAteExpirar(token);
  if (min === null) return '';
  if (min <= 0) return ' (o `exp` já venceu — a PED costuma aceitar mesmo assim)';
  return ` (o \`exp\` anuncia ${min.toFixed(0)} min)`;
}

const ATRIBUICAO =
  'Dados da Rede Observacional do CEMADEN/MCTI — Centro Nacional de Monitoramento e Alertas de Desastres Naturais (https://www.gov.br/cemaden/)';

/** Publicação anterior, para continuar a série. Ausência não é erro. */
async function publicacaoAnterior(nome) {
  try {
    const r = await fetch(`${BASE_URL}/cemaden/${nome}`, { signal: AbortSignal.timeout(30_000) });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    console.warn(`[aviso] não li a publicação anterior (${nome}): ${e.message}`);
    return null;
  }
}

/**
 * Caminho oficial: série real de 10 em 10 min a partir do dado bruto.
 *
 * O CADASTRO vem do instantâneo aberto, não da PED: ele traz as 569 estações
 * da rede independente de terem reportado, enquanto `dados_rede` só devolve
 * quem publicou na janela. Assim o mapa mostra todas as estações nos dois
 * modos, e a lista não muda quando o token entra ou sai — além de economizar
 * 9 das 18 requisições do ciclo.
 */
async function viaToken(TOKEN, fim) {
  console.log('Modo: API oficial PED (com token)');
  const inicio = fim - (SLOTS - 1) * PASSO_MS;

  const { estacoes } = await instantaneoAberto();
  console.log(`  ${estacoes.length} estações no cadastro (fonte aberta)`);

  const leituras = [];
  for (const uf of UFS_AMAZONIA) {
    const dados = await dadosRede(TOKEN, uf, inicio, fim);
    leituras.push(...dados);
    console.log(`  ${uf}: ${dados.length} leituras`);
  }

  const codigos = estacoes.map((e) => e.cod);
  const { t0, v } = gradeDeLeituras(codigos, leituras, fim);
  const cobertos = new Set(leituras.map((l) => l.cod));
  console.log(`  ${cobertos.size} estações com leitura de ${codigos.length}`);
  return { estacoes, codigos, t0, v, grandeza: 'chuva10min', modo: 'ped' };
}

/**
 * Caminho aberto: instantâneo do acumulado de 24 h, empilhado ciclo a ciclo.
 * A grandeza é outra — `acum24h`, não chuva por fatia — e o AmaView trata as
 * duas de forma diferente, por isso ela vai declarada no manifesto.
 */
async function viaAberta(fim, anterior) {
  console.log('Modo: instantâneo aberto (sem token)');
  const { estacoes, instante } = await instantaneoAberto();
  console.log(`  ${estacoes.length} estações na Amazônia Legal (publicado em ${new Date(instante).toISOString()})`);

  const codigos = estacoes.map((e) => e.cod);
  const valores = new Map(estacoes.filter((e) => e.valor !== null).map((e) => [e.cod, e.valor]));

  // Uma grade sem história deixaria a camada vazia para quem abrisse o
  // AmaView antes de 24 h de ciclos. O preenchimento inicial monta as 24 h de
  // uma vez, a partir das séries horárias públicas.
  //
  // O gatilho é a cobertura TEMPORAL da grade anterior, não a quantidade de
  // estações: assim ele dispara quando falta história (grade nova, ou
  // publicação perdida) e não quando a rede do CEMADEN está degradada.
  const fatias = anterior ? fatiasComDado(anterior.v) : 0;
  const precisaPreencher = fatias < SLOTS / 2;
  if (anterior) console.log(`  Grade anterior cobre ${fatias} de ${SLOTS} fatias`);

  const base = precisaPreencher ? ((await preencherInicial(estacoes, fim)) ?? anterior) : anterior;

  const { t0, v } = deslocarEGravar(base, codigos, valores, fim);
  return { estacoes, codigos, t0, v, grandeza: 'acum24h', modo: 'aberto' };
}

/** Grade completa a partir das séries horárias de 48 h (uma vez, no 1º ciclo). */
async function preencherInicial(estacoes, fim) {
  const comId = estacoes.filter((e) => Number.isFinite(e.id));
  if (comId.length === 0) return null;

  console.log(`  Preenchimento inicial: ${comId.length} séries horárias…`);
  const t0 = fim - (SLOTS - 1) * PASSO_MS;
  const inicio = Date.now();

  const linhas = await emLotes(comId, async (e) => {
    const horas = await horarias48(e.id);
    return horas.length ? acum24hPorFatia(horas, t0) : null;
  });

  const codigos = [];
  const v = [];
  let ok = 0;
  comId.forEach((e, i) => {
    if (!linhas[i]) return;
    codigos.push(e.cod);
    v.push(linhas[i]);
    ok++;
  });

  console.log(`  Preenchidas ${ok} de ${comId.length} em ${((Date.now() - inicio) / 1000).toFixed(0)} s`);
  return ok > 0 ? { t0, codigos, v } : null;
}

async function main() {
  const inicioExec = Date.now();
  const fim = alinhar(Date.now());

  const serieAnterior = await publicacaoAnterior('serie.json');
  const anterior =
    serieAnterior?.v && Array.isArray(serieAnterior.estacoes)
      ? { t0: serieAnterior.t0, codigos: serieAnterior.estacoes, v: serieAnterior.v }
      : null;
  if (anterior) {
    const idade = Math.round((fim - anterior.t0 - (SLOTS - 1) * PASSO_MS) / 60_000);
    console.log(`Grade anterior: ${anterior.codigos.length} estações, ${idade} min atrás`);
  } else {
    console.log('Sem grade anterior — primeira execução ou publicação indisponível.');
  }

  let token = null;
  try {
    token = await obterToken();
  } catch (e) {
    // Credencial errada não pode derrubar a camada: a fonte aberta atende.
    console.warn(`[aviso] não consegui um token (${e.message}) — usando a fonte aberta.`);
  }

  const r = token ? await viaToken(token, fim) : await viaAberta(fim, anterior);
  const v = arredondar(r.v);
  const stats = contarMedidas(v);

  const gerado = new Date().toISOString();
  const comum = { fonte: ATRIBUICAO, portal: 'https://www.gov.br/cemaden/', gerado, modo: r.modo };

  await mkdir(join(SAIDA, 'cemaden'), { recursive: true });

  await escrever('cemaden/estacoes.json', {
    ...comum,
    estacoes: r.estacoes.map((e) => ({
      c: e.cod,
      i: e.id,
      n: e.nome,
      u: e.uf,
      m: e.municipio,
      b: e.codibge,
      y: Math.round(e.lat * 1e5) / 1e5,
      x: Math.round(e.lon * 1e5) / 1e5,
      t: e.tipo,
    })),
  });

  await escrever('cemaden/serie.json', {
    ...comum,
    grandeza: r.grandeza,
    t0: r.t0,
    passoMin: PASSO_MS / 60_000,
    slots: SLOTS,
    semDado: -1,
    estacoes: r.codigos,
    v,
  });

  await escrever('cemaden/manifest.json', {
    ...comum,
    grandeza: r.grandeza,
    t0: r.t0,
    fim: r.t0 + (SLOTS - 1) * PASSO_MS,
    passoMin: PASSO_MS / 60_000,
    slots: SLOTS,
    estacoes: r.codigos.length,
    medidas: stats.com,
    comChuva: stats.chuva,
    ufs: UFS_AMAZONIA,
    duracaoMs: Date.now() - inicioExec,
  });

  // Página inicial do repositório de dados (documenta os endpoints).
  const indice = join(RAIZ, 'site-src', 'index.html');
  if (existsSync(indice)) await cp(indice, join(SAIDA, 'index.html'));

  const cobertura = ((100 * stats.com) / Math.max(1, stats.total)).toFixed(1);
  console.log(
    `\nOK — ${r.codigos.length} estações · ${stats.com} medidas (${cobertura}% da grade) · ` +
      `${stats.chuva} com chuva · ${Date.now() - inicioExec} ms`,
  );

  // Resumo na aba Summary do Actions.
  if (process.env.GITHUB_STEP_SUMMARY) {
    await writeFile(
      process.env.GITHUB_STEP_SUMMARY,
      [
        `### CEMADEN — ciclo de ${gerado}`,
        '',
        `| | |`,
        `|---|---|`,
        `| Modo | \`${r.modo}\` (${r.grandeza}) |`,
        `| Estações | ${r.codigos.length} |`,
        `| Células com medição | ${stats.com} de ${stats.total} (${cobertura}%) |`,
        `| Células com chuva | ${stats.chuva} |`,
        `| Duração | ${((Date.now() - inicioExec) / 1000).toFixed(1)} s |`,
        '',
      ].join('\n'),
      { flag: 'a' },
    );
  }
}

async function escrever(rel, obj) {
  const caminho = join(SAIDA, rel);
  const texto = JSON.stringify(obj);
  await writeFile(caminho, texto);
  console.log(`  ${rel}: ${(texto.length / 1024).toFixed(0)} KB`);
}

main().catch(async (e) => {
  console.error(`\nFALHOU: ${e.message}`);
  // Uma falha não pode apagar o que já estava publicado: se a saída ficou
  // incompleta, o workflow não publica (ver `dados.yml`).
  if (!existsSync(join(SAIDA, 'cemaden', 'serie.json'))) {
    try {
      const antes = await readFile(join(SAIDA, 'cemaden', 'serie.json'), 'utf8');
      console.error(`(mantendo publicação anterior, ${antes.length} bytes)`);
    } catch {
      /* nada a manter */
    }
  }
  process.exitCode = 1;
});
