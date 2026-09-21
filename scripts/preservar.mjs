/**
 * Baixa de volta as partes do site que ESTE ciclo não reconstruiu.
 *
 * O GitHub Pages publica o site inteiro de uma vez: o que não está no artifact
 * some. Com dois workflows independentes — um para a chuva, outro para as
 * sondagens — cada publicação apagaria os dados do outro.
 *
 * Então, antes de publicar, cada workflow restaura as pastas alheias a partir
 * do que já está no ar.
 *
 *   node scripts/preservar.mjs sondagem      # rodado pelo workflow da chuva
 *   node scripts/preservar.mjs cemaden       # rodado pelo da sondagem
 *
 * Se não conseguir restaurar, FALHA. Publicar um site sem metade dos dados é
 * pior que não publicar: o AmaView passaria a mostrar a camada vazia até o
 * outro workflow rodar, o que no caso das sondagens pode levar horas.
 *
 * `PRIMEIRA_PUBLICACAO=1` libera a ausência, para o ciclo que estreia a pasta.
 */
import { mkdir, writeFile, cp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const SAIDA = isAbsolute(process.env.SAIDA ?? '') ? process.env.SAIDA : join(RAIZ, process.env.SAIDA || 'site');
const BASE_URL = process.env.BASE_URL || 'https://helvecioneto.github.io/AmaView-dados';
const PRIMEIRA = process.env.PRIMEIRA_PUBLICACAO === '1';

/** O que cada pasta publica. Manter em sincronia com build.mjs, sondagem.mjs, navios.mjs e rios.mjs. */
export const ARQUIVOS = {
  cemaden: ['estacoes.json', 'serie.json', 'manifest.json'],
  sondagem: ['estacoes.json', 'perfis.json', 'manifest.json'],
  navios: ['atual.json', 'cadastro.json', 'escuta.json', 'manifest.json'],
  rios: ['estacoes.json', 'atual.json', 'manifest.json'],
};

/**
 * Lê um arquivo do que está no ar.
 *
 * O `?t=` na URL fura o cache da CDN do Pages (`max-age=600`): `cache:
 * 'no-store'` só vale para o cliente, e sem isso um workflow que roda um
 * minuto depois de outro restaura a publicação ANTERIOR à dele — regressão
 * de até 10 minutos, com o carimbo velho fazendo o ciclo seguinte trabalhar
 * de novo à toa.
 *
 * Três tentativas: uma falha transitória aqui derruba a publicação inteira
 * (é a regra: publicar sem uma pasta é pior que não publicar), e derrubar a
 * chuva por um soluço de rede de 30 s não vale a pena.
 */
export async function baixar(pasta, nome, tentativas = 3) {
  let ultimo = null;
  for (let i = 0; i < tentativas; i++) {
    try {
      const url = `${BASE_URL}/${pasta}/${nome}?t=${Date.now()}`;
      const r = await fetch(url, { signal: AbortSignal.timeout(30_000), cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const texto = await r.text();
      // Uma resposta vazia ou HTML de erro não é dado: restaurá-la publicaria lixo.
      if (texto.length < 2 || texto.trimStart().startsWith('<')) throw new Error('resposta não é JSON');
      JSON.parse(texto);
      return texto;
    } catch (e) {
      ultimo = e;
      // 404 não muda em segundos: é a estreia da pasta, e repetir só atrasa.
      if (/HTTP 404/.test(e.message)) break;
      if (i < tentativas - 1) await new Promise((r) => setTimeout(r, 3000 * (i + 1)));
    }
  }
  throw ultimo;
}

async function preservar(pasta) {
  const lista = ARQUIVOS[pasta];
  if (!lista) throw new Error(`pasta desconhecida: ${pasta}`);
  await mkdir(join(SAIDA, pasta), { recursive: true });

  let falhas = 0;
  for (const nome of lista) {
    // `site/` nasce vazio a cada execução: um arquivo já presente foi ESTE
    // ciclo que construiu. Restaurar por cima dele publicaria o dado velho no
    // lugar do novo — que é exatamente o contrário do propósito deste script.
    const destino = join(SAIDA, pasta, nome);
    if (existsSync(destino)) {
      console.log(`  ${pasta}/${nome}: construído neste ciclo, mantido`);
      continue;
    }
    try {
      const texto = await baixar(pasta, nome);
      await writeFile(destino, texto);
      console.log(`  ${pasta}/${nome}: ${(texto.length / 1024).toFixed(0)} KB restaurado`);
    } catch (e) {
      falhas++;
      console.warn(`  ${pasta}/${nome}: ${e.message}`);
    }
  }
  return falhas;
}

async function main() {
  const pastas = process.argv.slice(2);
  if (pastas.length === 0) {
    console.error('uso: node scripts/preservar.mjs <pasta> [pasta...]');
    process.exitCode = 1;
    return;
  }

  let falhas = 0;
  for (const p of pastas) falhas += await preservar(p);

  // A página inicial não pertence a nenhum dos dois ciclos.
  const indice = join(RAIZ, 'site-src', 'index.html');
  if (existsSync(indice)) await cp(indice, join(SAIDA, 'index.html'));

  if (falhas > 0) {
    if (PRIMEIRA) {
      console.warn(`\n[aviso] ${falhas} arquivo(s) não restaurado(s), mas PRIMEIRA_PUBLICACAO=1.`);
      return;
    }
    console.error(
      `\nFALHOU: ${falhas} arquivo(s) de ${pastas.join(', ')} não puderam ser restaurados.\n` +
        'Publicar assim apagaria esses dados do ar. Se esta é a estreia da pasta,\n' +
        'rode com PRIMEIRA_PUBLICACAO=1.',
    );
    process.exitCode = 1;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error(`FALHOU: ${e.message}`);
    process.exitCode = 1;
  });
}
