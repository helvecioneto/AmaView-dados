/**
 * Espelho dos radares meteorológicos do SIPAM: as últimas 48 h de PNG de
 * refletividade e taxa de chuva, com um manifesto que diz o que existe.
 *
 * **Por que espelhar, e não ler direto do SIPAM.** O SIPAM não manda
 * cabeçalho CORS. Sem ele o navegador não lê a listagem de arquivos, e um
 * PNG carregado sem CORS "suja" o canvas do mapa — a exportação de vídeo do
 * AmaView quebra com `SecurityError`. O GitHub Pages serve com
 * `Access-Control-Allow-Origin: *`. Os bytes são os mesmos, com o mesmo nome:
 * nenhuma conversão.
 *
 * **O estado entre ciclos NÃO vem do Pages**, ao contrário das outras
 * camadas. São ~4.700 PNG e ~85 MB para 48 h (medido em 21/09/2026); reler
 * isso do Pages a cada 15 min seriam ~8 GB por dia, 240 GB por mês — mais que
 * o dobro do limite brando de banda do Pages (100 GB/mês), gasto só pelo
 * próprio workflow. O estado vai no cache do Actions, num depósito fora de
 * `site/` (`radar-cache/`): o ciclo poda o que saiu da janela, acrescenta o
 * que chegou e copia o resultado para `site/radar/`. O Pages só é lido quando
 * o cache se perdeu, e só o que falta.
 *
 * Roda como PASSO do workflow da chuva, pelo mesmo motivo das embarcações e
 * dos rios: um workflow próprio disputaria o grupo `publicar-pages`, onde a
 * execução PENDENTE é cancelada em silêncio quando uma terceira chega.
 *
 *   node scripts/radar.mjs              # um ciclo: lista, baixa o que falta, publica
 *   node scripts/radar.mjs --preservar  # garante site/radar/ sem falar com o SIPAM
 *
 * Variáveis de ambiente:
 *   BASE_URL           Onde está a publicação (para o cache perdido e o --preservar).
 *   SAIDA              Diretório de saída (padrão: `site`).
 *   RADAR_DEPOSITO     Depósito entre ciclos (padrão: `radar-cache`). NUNCA dentro de SAIDA.
 *   RADAR_JANELA_H     Janela publicada, em horas (padrão 48).
 *   RADAR_MARGEM_MIN   Folga além da janela, em minutos (padrão 30).
 *   RADAR_PRAZO_MIN    Prazo para listar e baixar, em minutos (padrão 4).
 *   SIPAM_URL          Raiz das listagens (padrão https://siger.sipam.gov.br/radar).
 *   SIPAM_WFS          URL da WFS de cobertura (extensão dos radares).
 */
import { copyFile, mkdir, readdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import { constants, existsSync, realpathSync } from 'node:fs';
import { join, dirname, isAbsolute } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { setTimeout as pausa } from 'node:timers/promises';
import https from 'node:https';
import zlib from 'node:zlib';
import {
  JANELA_H,
  MARGEM_MIN,
  PRODUTOS,
  RADARES,
  TOLERANCIA_EXTENT,
  chave,
  contarArquivos,
  erroDeTls,
  escolherExtent,
  extentsDaWfs,
  instantesDoManifesto,
  lerIHDR,
  limiteDaJanela,
  medianaIntervalo,
  montarManifesto,
  nomeDeT,
  parseListagem,
  planejar,
  pngValido,
  tDeNome,
} from './radares.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const caminho = (v, padrao) => (isAbsolute(v ?? '') ? v : join(RAIZ, v || padrao));
const SAIDA = caminho(process.env.SAIDA, 'site');
const DEPOSITO = caminho(process.env.RADAR_DEPOSITO, 'radar-cache');
const BASE_URL = (process.env.BASE_URL || 'https://helvecioneto.github.io/AmaView-dados').replace(/\/+$/, '');
const SIPAM_URL = (process.env.SIPAM_URL || 'https://siger.sipam.gov.br/radar').replace(/\/+$/, '');
const SIPAM_WFS =
  process.env.SIPAM_WFS ||
  'https://geoserverhidro.sipam.gov.br/geoserver/wfs?service=WFS&version=2.0.0&request=GetFeature' +
    '&typeNames=sipam:sipam_cobertura_radar&outputFormat=application/json&srsName=EPSG:4326';

const JANELA = numeroDoAmbiente('RADAR_JANELA_H', JANELA_H, 0.01);
const MARGEM = numeroDoAmbiente('RADAR_MARGEM_MIN', MARGEM_MIN, 0);

/**
 * Prazo para listar e baixar.
 *
 * Medido de uma máquina no Brasil (21/09/2026): as 22 listagens (~225 KB
 * cada, ~12 KB com gzip) em ~1 s, e ~30 PNG por segundo com 4 conexões. O
 * ciclo normal (~25 arquivos novos) leva uns 5 s; a estreia (~4.700) levaria
 * uns 3 min daqui — do runner do GitHub, nos EUA, a latência é maior e não
 * foi medida. O que não couber fica para o ciclo seguinte, do mais novo para
 * o mais antigo. Sem prazo, um SIPAM lento (20 s × 3 tentativas por arquivo)
 * seguraria o passo por horas, e cada 15 min a mais cancela um ciclo da
 * chuva no grupo de concorrência do Pages.
 */
const PRAZO_MIN = numeroDoAmbiente('RADAR_PRAZO_MIN', 4, 0.05);
const PRAZO_MS = PRAZO_MIN * 60_000;

/**
 * Educação com o servidor: no máximo 4 requisições simultâneas ao SIPAM
 * (listagens, WFS e PNG, todos pelo mesmo limite), e o `User-Agent` diz quem
 * somos para quem olhar o log do Apache de lá.
 *
 * O Pages aguenta 16: é CDN, e só é lido quando o cache se perdeu.
 */
const CONCORRENCIA_SIPAM = 4;
const CONCORRENCIA_PAGES = 16;
const TENTATIVAS = 3;
const TIMEOUT_MS = 20_000;
const RECUO_MS = 1000;
const UA = 'AmaView-dados/1.0 (+https://github.com/helvecioneto/AmaView-dados)';

/**
 * Teto de alerta do que vai ao Pages. O Pages recusa site acima de 1 GB, e o
 * resto do site soma poucos MB; 600 MB avisa com folga para agir (encurtar a
 * janela, tirar um produto) antes que um deploy seja recusado.
 */
const ALERTA_MB = 600;

const limiteSipam = semaforo(CONCORRENCIA_SIPAM);
const limitePages = semaforo(CONCORRENCIA_PAGES);

function numeroDoAmbiente(nome, padrao, minimo) {
  const v = process.env[nome];
  if (v === undefined || v.trim() === '') return padrao;
  const n = Number(v);
  if (!Number.isFinite(n) || n < minimo) {
    console.warn(`[aviso] ${nome}=${v} inválido; usando ${padrao}`);
    return padrao;
  }
  return n;
}

// ---------------------------------------------------------------------------
// Rede
// ---------------------------------------------------------------------------

/** Limita quantas funções rodam ao mesmo tempo. Fila FIFO: a ordem de chegada é a de atendimento. */
function semaforo(n) {
  let livres = n;
  const espera = [];
  return async (fn) => {
    if (livres > 0) livres--;
    else await new Promise((r) => espera.push(r));
    try {
      return await fn();
    } finally {
      const proximo = espera.shift();
      if (proximo) proximo();
      else livres++;
    }
  };
}

/** Hosts do SIPAM que já precisaram do atalho sem validação neste processo. */
const semValidacao = new Set();

const ehSipam = (host) => host === 'sipam.gov.br' || host.endsWith('.sipam.gov.br');

/**
 * GET → `{ status, corpo: Buffer }`.
 *
 * Hoje a cadeia TLS do SIPAM valida, mas ele às vezes serve a cadeia SEM o
 * certificado intermediário, e aí o `fetch` do Node recusa
 * (`UNABLE_TO_VERIFY_LEAF_SIGNATURE`). Nesse caso — e só para hosts do
 * SIPAM — repetimos pelo `node:https` sem validar a cadeia. O risco é
 * limitado: o que vem de lá é PNG conferido byte a byte e uma listagem
 * de nomes, e nada disso é executado. Depois do primeiro tropeço o host vai
 * direto pelo atalho, para não pagar duas requisições por arquivo.
 */
async function obter(url, sinal) {
  const sinais = [AbortSignal.timeout(TIMEOUT_MS)];
  if (sinal) sinais.push(sinal);
  const s = AbortSignal.any(sinais);
  const host = new URL(url).hostname;
  if (!semValidacao.has(host)) {
    try {
      const r = await fetch(url, { signal: s, headers: { 'User-Agent': UA } });
      return { status: r.status, corpo: Buffer.from(await r.arrayBuffer()) };
    } catch (e) {
      if (!ehSipam(host) || !erroDeTls(e)) throw e;
      semValidacao.add(host);
      console.warn(`  [aviso] ${host}: cadeia TLS incompleta (${e.cause?.code ?? e.message}); seguindo sem validar o certificado`);
    }
  }
  return obterSemValidar(url, s);
}

/** O atalho do `obter`: `node:https` com `rejectUnauthorized: false`, descomprimindo à mão. */
export function obterSemValidar(url, sinal) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        rejectUnauthorized: false,
        headers: { 'User-Agent': UA, 'Accept-Encoding': 'gzip, br, deflate' },
        signal: sinal,
      },
      (res) => {
        const partes = [];
        res.on('data', (c) => partes.push(c));
        res.on('error', reject);
        res.on('end', () => {
          try {
            // Conexão cortada no meio também termina em 'end' em algumas
            // versões; `complete` é o que diz se a mensagem veio inteira.
            if (!res.complete) throw new Error('resposta interrompida');
            let corpo = Buffer.concat(partes);
            const esperado = Number(res.headers['content-length']);
            if (Number.isFinite(esperado) && esperado !== corpo.length) {
              throw new Error(`resposta cortada (${corpo.length} de ${esperado} bytes)`);
            }
            const cod = String(res.headers['content-encoding'] ?? '').toLowerCase();
            if (cod === 'gzip' || cod === 'x-gzip') corpo = zlib.gunzipSync(corpo);
            else if (cod === 'br') corpo = zlib.brotliDecompressSync(corpo);
            else if (cod === 'deflate') corpo = zlib.inflateSync(corpo);
            resolve({ status: res.statusCode, corpo });
          } catch (e) {
            reject(e);
          }
        });
      },
    );
    req.on('error', reject);
  });
}

function mensagemDe(e) {
  if (e?.name === 'TimeoutError') return `sem resposta em ${TIMEOUT_MS / 1000} s`;
  if (e?.name === 'AbortError') return 'prazo do ciclo esgotado';
  const causa = e?.cause?.code ?? e?.cause?.message;
  return causa ? `${e.message} (${causa})` : String(e?.message ?? e);
}

/**
 * GET com três tentativas e recuo (1 s, 3 s), dentro do `limite` de
 * concorrência. O limite vale por TENTATIVA: quem está esperando o recuo não
 * segura uma das 4 vagas do SIPAM.
 *
 * - 404 volta como `{ status: 404 }`, sem repetir: é "não existe", e não muda
 *   em segundos (arquivo que sumiu entre a listagem e o download; estreia);
 * - outro 4xx também não repete;
 * - 5xx, rede, tempo esgotado e corpo recusado por `aceitar` repetem.
 */
async function requisitar(url, { limite = null, sinal = null, tentativas = TENTATIVAS, aceitar = null } = {}) {
  let ultimo = null;
  for (let i = 0; i < tentativas; i++) {
    if (sinal?.aborted) break;
    try {
      const r = await (limite ? limite(() => (sinal?.aborted ? Promise.reject(new Error('prazo')) : obter(url, sinal))) : obter(url, sinal));
      if (r.status === 404) return { status: 404, corpo: null };
      if (r.status >= 400 && r.status < 500) {
        ultimo = new Error(`HTTP ${r.status}`);
        break;
      }
      if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
      if (aceitar && !aceitar(r.corpo)) throw new Error('resposta não é o esperado');
      return r;
    } catch (e) {
      ultimo = e;
      if (sinal?.aborted) break;
    }
    if (i < tentativas - 1) await pausa(RECUO_MS * 3 ** i, undefined, { signal: sinal ?? undefined }).catch(() => {});
  }
  if (sinal?.aborted) throw new Error('prazo do ciclo esgotado');
  throw new Error(mensagemDe(ultimo));
}

/** Roda `tarefa` sobre `itens`, na ordem, com `concorrencia` trabalhadores; para de pegar item novo quando `sinal` aborta. */
async function emFila(itens, tarefa, { concorrencia, sinal = null }) {
  let i = 0;
  const trabalhador = async () => {
    while (i < itens.length && !sinal?.aborted) await tarefa(itens[i++]);
  };
  await Promise.all(Array.from({ length: Math.min(concorrencia, itens.length) }, trabalhador));
}

// ---------------------------------------------------------------------------
// Fontes
// ---------------------------------------------------------------------------

/** As 22 listagens (11 radares × 2 produtos), cada uma `{ itens }` ou `{ erro }`. */
async function listarTudo(sinal) {
  const listagens = new Map();
  const pares = RADARES.flatMap((r) => PRODUTOS.map((p) => [r.id, p]));
  await Promise.all(
    pares.map(async ([id, p]) => {
      try {
        const r = await requisitar(`${SIPAM_URL}/${id}/${p}/`, {
          limite: limiteSipam,
          sinal,
          // HTML de erro com status 200 (proxy, manutenção) não é listagem.
          // Aceitá-lo daria "zero arquivos" e o radar pararia de andar em silêncio.
          aceitar: (c) => /Index of/i.test(c.toString('latin1')),
        });
        if (r.status === 404) throw new Error('HTTP 404');
        listagens.set(chave(id, p), { itens: parseListagem(r.corpo.toString('latin1')) });
      } catch (e) {
        listagens.set(chave(id, p), { erro: mensagemDe(e) });
      }
    }),
  );
  return listagens;
}

/** Extensões da WFS de cobertura, ou `null` (e aí vale a tabela). */
async function lerWfs(sinal) {
  try {
    const r = await requisitar(SIPAM_WFS, {
      limite: limiteSipam,
      sinal,
      aceitar: (c) => {
        try {
          return Array.isArray(JSON.parse(c.toString('utf8')).features);
        } catch {
          return false;
        }
      },
    });
    if (r.status === 404) throw new Error('HTTP 404');
    return { extents: extentsDaWfs(JSON.parse(r.corpo.toString('utf8'))), erro: null };
  } catch (e) {
    return { extents: null, erro: mensagemDe(e) };
  }
}

/**
 * O manifesto que está no ar: `{ manifesto }`, `{ ausente: true }` (404, a
 * estreia da pasta) ou `{ erro }`.
 *
 * `?t=` fura a CDN do Pages (`max-age=600`). Os PNG vão sem ele de propósito:
 * o mesmo nome é sempre o mesmo arquivo, e a cópia da CDN serve.
 */
async function manifestoNoAr() {
  try {
    const r = await requisitar(`${BASE_URL}/radar/manifest.json?t=${Date.now()}`, {
      aceitar: (c) => instantesDoManifesto(lerJsonDe(c)) !== null,
    });
    if (r.status === 404) return { ausente: true };
    return { manifesto: JSON.parse(r.corpo.toString('utf8')) };
  } catch (e) {
    return { erro: mensagemDe(e) };
  }
}

function lerJsonDe(buf) {
  try {
    return JSON.parse(buf.toString('utf8'));
  } catch {
    return null;
  }
}

/**
 * Baixa um PNG para o depósito. Do Pages quando ele está no ar (cache
 * perdido), senão do SIPAM; se o Pages falhar e a listagem do SIPAM tiver o
 * arquivo, tenta lá. Devolve `{ origem: null }` quando o arquivo sumiu (404):
 * o SIPAM pode apagar entre a listagem e o download, e isso não é falha.
 */
async function baixarPng(item, sinal) {
  const nome = nomeDeT(item.t);
  let r = null;
  let origem = null;
  if (item.fonte === 'pages') {
    try {
      r = await requisitar(`${BASE_URL}/radar/${item.id}/${item.produto}/${nome}`, { limite: limitePages, sinal, aceitar: pngValido });
      origem = 'pages';
    } catch (e) {
      if (!item.naListagem || sinal?.aborted) throw e;
    }
    if (r?.status === 404) r = null;
  }
  if (!r && (item.fonte === 'sipam' || item.naListagem)) {
    r = await requisitar(`${SIPAM_URL}/${item.id}/${item.produto}/${nome}`, { limite: limiteSipam, sinal, aceitar: pngValido });
    origem = 'sipam';
  }
  if (!r || r.status === 404) return { origem: null };
  await gravarAtomico(join(DEPOSITO, item.id, item.produto, nome), r.corpo);
  return { origem, bytes: r.corpo.length, px: lerIHDR(r.corpo) };
}

// ---------------------------------------------------------------------------
// Depósito e publicação
// ---------------------------------------------------------------------------

/**
 * Grava com nome provisório e renomeia. Um passo morto no meio (o
 * `timeout-minutes` do workflow) deixa no máximo um `.parcial`, nunca um PNG
 * pela metade com o nome definitivo — que o ciclo seguinte reaproveitaria.
 */
async function gravarAtomico(destino, conteudo) {
  await mkdir(dirname(destino), { recursive: true });
  const provisorio = `${destino}.parcial`;
  await writeFile(provisorio, conteudo);
  await rename(provisorio, destino);
}

async function lerJson(arquivo) {
  try {
    return JSON.parse(await readFile(arquivo, 'utf8'));
  } catch {
    return null;
  }
}

/**
 * O que há no depósito: por radar/produto, os instantes com PNG válido, e as
 * dimensões e tamanhos de cada um.
 *
 * O depósito é a verdade, e não o manifesto dele: o arquivo no disco é o que
 * será copiado. Cada PNG é conferido de novo (ler ~85 MB leva menos de um
 * segundo), e o que não passa é apagado — assim um arquivo estragado é
 * baixado de novo, em vez de ser publicado para sempre. Só são tocados nomes
 * de varredura dentro de `{radar}/{produto}/`: um RADAR_DEPOSITO apontado
 * para o lugar errado não apaga nada alheio.
 */
async function lerDeposito() {
  const conjuntos = new Map();
  const dims = new Map();
  const tamanhos = new Map();
  const tarefas = [];
  for (const r of RADARES) {
    for (const p of PRODUTOS) {
      const k = chave(r.id, p);
      conjuntos.set(k, new Set());
      const dir = join(DEPOSITO, r.id, p);
      let nomes = [];
      try {
        nomes = await readdir(dir);
      } catch {
        continue;
      }
      for (const nome of nomes) {
        if (nome.endsWith('.png.parcial') && tDeNome(nome.slice(0, -'.parcial'.length)) !== null) {
          tarefas.push({ remover: join(dir, nome) });
          continue;
        }
        const t = tDeNome(nome);
        if (t !== null) tarefas.push({ k, t, arquivo: join(dir, nome) });
      }
    }
  }
  let invalidos = 0;
  await emFila(
    tarefas,
    async (x) => {
      if (x.remover) return rm(x.remover, { force: true });
      const buf = await readFile(x.arquivo);
      if (!pngValido(buf)) {
        invalidos++;
        return rm(x.arquivo, { force: true });
      }
      conjuntos.get(x.k).add(x.t);
      dims.set(`${x.k}/${x.t}`, lerIHDR(buf));
      tamanhos.set(`${x.k}/${x.t}`, buf.length);
    },
    { concorrencia: 32 },
  );
  let total = 0;
  let bytes = 0;
  for (const s of conjuntos.values()) total += s.size;
  for (const b of tamanhos.values()) bytes += b;
  return { conjuntos, dims, tamanhos, invalidos, total, bytes };
}

/** Apaga do depósito todo PNG (e `.parcial`) que o manifesto não publica. */
async function podarDeposito(manifesto) {
  const manter = instantesDoManifesto(manifesto);
  let podados = 0;
  for (const r of RADARES) {
    for (const p of PRODUTOS) {
      const dir = join(DEPOSITO, r.id, p);
      const ficam = manter.get(chave(r.id, p)) ?? new Set();
      let nomes = [];
      try {
        nomes = await readdir(dir);
      } catch {
        continue;
      }
      for (const nome of nomes) {
        const base = nome.endsWith('.parcial') ? nome.slice(0, -'.parcial'.length) : nome;
        const t = tDeNome(base);
        if (t === null) continue;
        if (base !== nome || !ficam.has(t)) {
          await rm(join(dir, nome), { force: true });
          podados++;
        }
      }
    }
  }
  return podados;
}

/**
 * Copia do depósito para `SAIDA/radar/` o que o manifesto lista, e o
 * manifesto por último.
 *
 * ATÔMICO: monta em `SAIDA/.radar-tmp/` e só então troca a pasta. Um passo
 * morto no meio da cópia deixaria em `site/radar/` metade dos PNG — ou um
 * manifesto apontando para arquivos que não foram —, e o `--preservar` acharia
 * que a pasta está pronta.
 *
 * Se algum arquivo listado não estiver no depósito (não deveria acontecer), ele
 * sai do manifesto publicado, com aviso: o contrato é que tudo em `t` existe.
 */
async function publicar(manifesto) {
  const temp = join(SAIDA, '.radar-tmp');
  await rm(temp, { recursive: true, force: true });
  await mkdir(temp, { recursive: true });
  let arquivos = 0;
  let bytes = 0;
  try {
    const tarefas = [];
    for (const r of manifesto.radares) {
      for (const [p, e] of Object.entries(r.produtos)) {
        await mkdir(join(temp, r.id, p), { recursive: true });
        for (const t of e.t) tarefas.push({ id: r.id, p, t });
      }
    }
    const ausentes = new Set();
    await emFila(
      tarefas,
      async ({ id, p, t }) => {
        const nome = nomeDeT(t);
        const destino = join(temp, id, p, nome);
        try {
          // FICLONE: cópia por referência onde o sistema de arquivos deixa; cópia normal no resto.
          await copyFile(join(DEPOSITO, id, p, nome), destino, constants.COPYFILE_FICLONE);
        } catch (e) {
          if (e.code !== 'ENOENT') throw e;
          ausentes.add(`${id}/${p}/${t}`);
          return;
        }
        // Em duas linhas de propósito: `bytes += (await …)` lê `bytes` ANTES
        // do await, e com 32 cópias em paralelo as somas se atropelam.
        const { size } = await stat(destino);
        arquivos++;
        bytes += size;
      },
      { concorrencia: 32 },
    );
    if (ausentes.size) {
      console.warn(`  [aviso] ${ausentes.size} arquivo(s) do manifesto ausentes do depósito; fora da publicação`);
      for (const r of manifesto.radares) {
        for (const [p, e] of Object.entries(r.produtos)) e.t = e.t.filter((t) => !ausentes.has(`${r.id}/${p}/${t}`));
      }
    }
    if (arquivos !== contarArquivos(manifesto)) throw new Error(`copiados ${arquivos}, manifesto lista ${contarArquivos(manifesto)}`);
    await writeFile(join(temp, 'manifest.json'), JSON.stringify(manifesto));

    await rm(join(SAIDA, 'radar'), { recursive: true, force: true });
    await rename(temp, join(SAIDA, 'radar'));
  } finally {
    // Nunca deixar a pasta temporária dentro de `site/`: ela iria para o Pages.
    await rm(temp, { recursive: true, force: true });
  }
  return { manifesto, arquivos, bytes };
}

const mb = (b) => (b / 1024 / 1024).toFixed(1);
const hhmm = (t) => new Date(t * 1000).toISOString().slice(11, 16);

// ---------------------------------------------------------------------------
// Ciclo
// ---------------------------------------------------------------------------

async function ciclo() {
  const inicio = Date.now();
  const desde = limiteDaJanela(inicio, JANELA, MARGEM);
  const prazo = new AbortController();
  const relogio = setTimeout(() => prazo.abort(), PRAZO_MS);
  const sinal = prazo.signal;

  console.log(
    `Radares — ${RADARES.length} radares × ${PRODUTOS.length} produtos (${PRODUTOS.join(', ')}), ` +
      `janela de ${JANELA} h + ${MARGEM} min, prazo de ${PRAZO_MIN} min`,
  );
  // Resto de um passo morto no meio da publicação: não pode ir para o Pages.
  await rm(join(SAIDA, '.radar-tmp'), { recursive: true, force: true });

  // 1. O depósito restaurado do cache do Actions.
  const dep = await lerDeposito();
  const antigo = await lerJson(join(DEPOSITO, 'manifest.json'));
  console.log(
    `  depósito: ${dep.total} PNG, ${mb(dep.bytes)} MB` +
      (antigo?.gerado ? ` (manifesto de ${antigo.gerado})` : ' — sem manifesto: cache perdido ou estreia') +
      (dep.invalidos ? ` · ${dep.invalidos} inválido(s) apagado(s)` : ''),
  );

  // 2. Listagens, extensão e o manifesto no ar, em paralelo.
  const [listagens, wfs, ar] = await Promise.all([listarTudo(sinal), lerWfs(sinal), manifestoNoAr()]);
  const falhas = [...listagens.entries()].filter(([, l]) => l.erro).sort(([a], [b]) => (a < b ? -1 : 1));
  console.log(`  listagens: ${listagens.size - falhas.length} de ${listagens.size}`);
  for (const [k, l] of falhas) console.warn(`  [aviso] listagem ${k}: ${l.erro}`);

  // Com TODAS as listagens fora, o problema é o SIPAM (ou a rede até ele), e
  // publicar só repetiria o ciclo anterior com um carimbo novo, mentindo sobre
  // o frescor. Falhar deixa o depósito intacto; o `--preservar` publica.
  if (falhas.length === listagens.size) {
    clearTimeout(relogio);
    throw new Error(`nenhuma das ${listagens.size} listagens do SIPAM respondeu (${falhas[0][1].erro}) — depósito intacto`);
  }

  if (wfs.erro) console.warn(`  [aviso] WFS de cobertura: ${wfs.erro} — extensões da tabela`);
  const noAr = ar.manifesto ? instantesDoManifesto(ar.manifesto) : null;
  if (ar.erro) console.warn(`  [aviso] manifesto no ar: ${ar.erro}`);
  else if (ar.ausente) console.log('  manifesto no ar: nenhum (estreia da pasta)');

  // 3. O plano: o que reaproveitar, o que buscar e em que ordem.
  const plano = planejar({ deposito: dep.conjuntos, listagens, noAr, desde });
  const doPages = plano.fila.filter((x) => x.fonte === 'pages').length;
  console.log(`  a buscar: ${plano.fila.length} (${plano.fila.length - doPages} do SIPAM, ${doPages} do Pages)`);

  // 4. Downloads, do mais novo para o mais antigo, até o prazo.
  const presentes = new Map([...plano.porChave].map(([k, e]) => [k, new Set(e.reaproveitar)]));
  const contagem = new Map([...plano.porChave.keys()].map((k) => [k, { sipam: 0, pages: 0, sumiram: 0, falhas: 0, erro: null }]));
  let feitos = 0;
  await emFila(
    plano.fila,
    async (item) => {
      const k = chave(item.id, item.produto);
      const c = contagem.get(k);
      try {
        const r = await baixarPng(item, sinal);
        if (r.origem === null) c.sumiram++;
        else {
          c[r.origem]++;
          presentes.get(k).add(item.t);
          dep.dims.set(`${k}/${item.t}`, r.px);
          dep.tamanhos.set(`${k}/${item.t}`, r.bytes);
        }
      } catch (e) {
        if (!sinal.aborted) {
          c.falhas++;
          c.erro ??= e.message;
        }
      }
      if (++feitos % 500 === 0) console.log(`  ${feitos}/${plano.fila.length}…`);
    },
    { concorrencia: CONCORRENCIA_SIPAM + CONCORRENCIA_PAGES, sinal },
  );
  const esgotou = sinal.aborted;
  clearTimeout(relogio);

  // 5. O manifesto: só o que está no depósito, dentro da janela.
  const naWfs = wfs.extents ?? new Map();
  for (const id of naWfs.keys()) {
    if (!RADARES.some((r) => r.id === id)) console.warn(`  [aviso] a WFS tem o radar "${id}", que não está na tabela — acrescentar?`);
  }
  const radares = RADARES.map((r) => {
    const ext = escolherExtent(r, naWfs.get(r.id));
    if (wfs.extents && ext.fonte === 'tabela') console.warn(`  [aviso] ${r.id}: extensão da WFS ausente ou inválida — tabela`);
    if (ext.divergencia !== null && ext.divergencia > TOLERANCIA_EXTENT) {
      console.warn(`  [aviso] ${r.id}: WFS diverge da tabela em ${ext.divergencia.toFixed(4)}° — o SIPAM mudou o recorte? Atualizar a tabela.`);
    }
    // `px` do PNG mais recente do radar, entre os produtos (dbz no empate).
    let maisNovo = null;
    for (const p of PRODUTOS) {
      for (const t of presentes.get(chave(r.id, p))) if (!maisNovo || t > maisNovo.t) maisNovo = { p, t };
    }
    const d = maisNovo ? dep.dims.get(`${chave(r.id, maisNovo.p)}/${maisNovo.t}`) : null;
    return {
      id: r.id,
      nome: r.nome,
      extent: ext.extent,
      extentFonte: ext.fonte,
      px: d ? [d.w, d.h] : null,
      produtos: Object.fromEntries(
        PRODUTOS.map((p) => {
          const e = plano.porChave.get(chave(r.id, p));
          return [p, { t: presentes.get(chave(r.id, p)), herdado: e.herdado, erro: e.erro }];
        }),
      ),
    };
  });
  const gerado = new Date().toISOString();
  const manifesto = montarManifesto({
    gerado,
    janelaHoras: JANELA,
    radares,
    desde,
    existe: (id, p, t) => presentes.get(chave(id, p))?.has(t) ?? false,
  });
  const total = contarArquivos(manifesto);

  // Uma rodada que só trouxe migalhas não substitui a boa. Acontece se o
  // cache se perdeu E o Pages falhou nos downloads: publicar isso apagaria do
  // ar horas de radar que continuam lá.
  // Só conta o que ainda espelhamos: tirar um produto ou um radar da tabela
  // não pode travar a publicação para sempre.
  if (noAr) {
    let noArNaJanela = 0;
    for (const k of plano.porChave.keys()) for (const t of noAr.get(k) ?? []) if (t >= desde) noArNaJanela++;
    if (total < noArNaJanela * 0.5) {
      throw new Error(`só ${total} arquivos contra ${noArNaJanela} no ar, na mesma janela — não publicando`);
    }
  }

  // 6. Depósito: manifesto novo, depois a poda. Nessa ordem, um passo morto
  //    entre as duas deixa arquivos a mais, nunca um manifesto apontando para
  //    arquivo podado.
  await gravarAtomico(join(DEPOSITO, 'manifest.json'), JSON.stringify(manifesto));
  const podados = await podarDeposito(manifesto);

  // 7. Publicação.
  const pub = await publicar(manifesto);
  if (contarArquivos(pub.manifesto) !== total) await gravarAtomico(join(DEPOSITO, 'manifest.json'), JSON.stringify(pub.manifesto));

  // 8. Relatório.
  const agoraS = Date.now() / 1000;
  const linhas = [];
  let novosSipam = 0;
  let novosPages = 0;
  let pendentes = 0;
  for (const r of pub.manifesto.radares) {
    for (const p of PRODUTOS) {
      const k = chave(r.id, p);
      const e = r.produtos[p];
      const pl = plano.porChave.get(k);
      const c = contagem.get(k);
      const faltam = pl.buscar.length - c.sipam - c.pages - c.sumiram - c.falhas;
      novosSipam += c.sipam;
      novosPages += c.pages;
      pendentes += faltam;
      const ult = e.t.at(-1);
      const idade = ult ? Math.round((agoraS - ult) / 60) : null;
      const passo = medianaIntervalo(e.t);
      const situacao = [
        e.herdado ? `herdado: ${e.erro}` : null,
        c.falhas ? `${c.falhas} falha(s): ${c.erro}` : null,
        c.sumiram ? `${c.sumiram} sumiu(ram) do SIPAM` : null,
        faltam ? `${faltam} para o próximo ciclo` : null,
      ].filter(Boolean);
      linhas.push({ r, p, n: e.t.length, c, reap: pl.reaproveitar.length, ult, idade, situacao });
      console.log(
        `  ${r.id} ${p.padEnd(4)} ${String(e.t.length).padStart(4)} na janela · ${c.sipam} novos` +
          (c.pages ? ` · ${c.pages} do Pages` : '') +
          ` · ${pl.reaproveitar.length} reaproveitados` +
          (ult ? ` · última ${hhmm(ult)} UTC (${idade} min)` : ' · sem varredura') +
          (passo ? ` · passo mediano ${Math.round(passo / 60)} min` : '') +
          (situacao.length ? ` · ${situacao.join(' · ')}` : ''),
      );
    }
  }

  const duracao = ((Date.now() - inicio) / 1000).toFixed(1);
  console.log(
    `\nOK — ${pub.arquivos} PNG, ${mb(pub.bytes)} MB publicados · ${novosSipam} do SIPAM, ${novosPages} do Pages · ` +
      `${podados} podados · ${duracao} s${esgotou ? ` · PRAZO ESGOTADO, ${pendentes} para o próximo ciclo` : ''}`,
  );
  const alerta = pub.bytes / 1024 / 1024 > ALERTA_MB;
  if (alerta) {
    const msg = `radar/ com ${mb(pub.bytes)} MB, acima de ${ALERTA_MB} MB — o Pages recusa site acima de 1 GB`;
    console.warn(process.env.GITHUB_ACTIONS ? `::warning::${msg}` : `[alerta] ${msg}`);
  }

  if (process.env.GITHUB_STEP_SUMMARY) {
    await writeFile(
      process.env.GITHUB_STEP_SUMMARY,
      [
        `### Radares (SIPAM) — ciclo de ${gerado}`,
        '',
        '| Radar | Produto | Na janela | Novos (SIPAM) | Do Pages | Reaproveitados | Última varredura | Idade | Situação |',
        '|---|---|--:|--:|--:|--:|---|--:|---|',
        ...linhas.map(
          (l) =>
            `| ${l.r.id} ${l.r.nome} | ${l.p} | ${l.n} | ${l.c.sipam} | ${l.c.pages} | ${l.reap} | ` +
            `${l.ult ? `${hhmm(l.ult)} UTC` : '—'} | ${l.idade === null ? '—' : `${l.idade} min`} | ${l.situacao.join('; ') || 'ok'} |`,
        ),
        '',
        `**Total:** ${pub.arquivos} PNG, ${mb(pub.bytes)} MB publicados · ${novosSipam} do SIPAM, ${novosPages} do Pages · ` +
          `${podados} podados · ${duracao} s` +
          (esgotou ? ` · **prazo esgotado**, ${pendentes} para o próximo ciclo` : ''),
        alerta ? `\n**Alerta:** acima de ${ALERTA_MB} MB — o Pages recusa site acima de 1 GB.` : '',
        '',
      ].join('\n'),
      { flag: 'a' },
    );
  }
}

// ---------------------------------------------------------------------------
// --preservar
// ---------------------------------------------------------------------------

/**
 * Garante `SAIDA/radar/` quando o ciclo não a construiu: o passo falhou, ou
 * quem publica é o workflow das sondagens (que também sobe o site inteiro, e
 * apagaria o radar do ar).
 *
 * 1. `radar/manifest.json` já existe → foi este ciclo que construiu; nada a fazer.
 * 2. Há depósito restaurado do cache → copia dele. Nenhum byte do Pages.
 * 3. Senão, restaura do Pages: o manifesto e todos os PNG que ele lista.
 * 4. Sem manifesto no ar (404, estreia da pasta) não falha: não há o que perder.
 *
 * Com manifesto no ar e restauração incompleta, FALHA — a regra do
 * repositório: publicar sem a pasta apagaria a camada do ar, e falhar mantém
 * a publicação anterior.
 */
async function preservar() {
  await rm(join(SAIDA, '.radar-tmp'), { recursive: true, force: true });
  if (existsSync(join(SAIDA, 'radar', 'manifest.json'))) {
    console.log('  radar/: construído neste ciclo, mantido');
    return;
  }

  const doDeposito = await lerJson(join(DEPOSITO, 'manifest.json'));
  if (instantesDoManifesto(doDeposito)) {
    const pub = await publicar(doDeposito);
    console.log(`  radar/: ${pub.arquivos} PNG, ${mb(pub.bytes)} MB copiados do depósito (manifesto de ${doDeposito.gerado})`);
    return;
  }

  const ar = await manifestoNoAr();
  if (ar.ausente) {
    console.log('  radar/: nada no ar (estreia da pasta) e sem depósito — nada a preservar');
    return;
  }
  if (ar.erro) throw new Error(`manifesto no ar ilegível: ${ar.erro}`);

  // O que o passo principal já tiver baixado antes de morrer não vai de novo.
  const dep = await lerDeposito();
  const faltam = [];
  for (const [k, ts] of instantesDoManifesto(ar.manifesto)) {
    const [id, produto] = k.split('/');
    if (!RADARES.some((r) => r.id === id) || !PRODUTOS.includes(produto)) continue;
    for (const t of ts) if (!dep.conjuntos.get(k)?.has(t)) faltam.push({ id, produto, t, fonte: 'pages', naListagem: false });
  }
  console.log(`  radar/: restaurando do Pages — ${faltam.length} PNG a baixar, ${dep.total} já no depósito`);

  let erros = 0;
  let primeiro = null;
  await emFila(
    faltam,
    async (item) => {
      try {
        const r = await baixarPng(item, null);
        if (r.origem === null) throw new Error('HTTP 404');
      } catch (e) {
        erros++;
        primeiro ??= `${item.id}/${item.produto}/${nomeDeT(item.t)}: ${e.message}`;
      }
    },
    { concorrencia: CONCORRENCIA_PAGES },
  );
  if (erros) throw new Error(`${erros} de ${faltam.length} PNG não restaurados (${primeiro})`);

  await gravarAtomico(join(DEPOSITO, 'manifest.json'), JSON.stringify(ar.manifesto));
  const pub = await publicar(ar.manifesto);
  if (pub.arquivos !== contarArquivos(ar.manifesto)) throw new Error('restauração incompleta');
  console.log(`  radar/: ${pub.arquivos} PNG, ${mb(pub.bytes)} MB restaurados do Pages (manifesto de ${ar.manifesto.gerado})`);
}

// `realpath` porque no macOS `/tmp` é link para `/private/tmp`, e o
// `import.meta.url` vem sempre resolvido.
if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const modo = process.argv.includes('--preservar') ? preservar : ciclo;
  modo()
    .catch((e) => {
      console.error(`\nFALHOU: ${e.message}`);
      // O passo roda com `continue-on-error`: a chuva publica do mesmo jeito,
      // e o `--preservar` põe no ar o depósito (ou o que já estava no ar).
      process.exitCode = 1;
    })
    .finally(() => {
      // Conexões keep-alive seguram o event loop por alguns segundos; um
      // passo pendurado atrasa o ciclo inteiro da chuva.
      process.exit(process.exitCode ?? 0);
    });
}
