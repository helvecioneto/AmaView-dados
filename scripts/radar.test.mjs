/**
 * Testes das funções puras do espelho dos radares.
 *
 * O manifesto é o contrato com o AmaView: o cliente reconstrói o nome de cada
 * PNG a partir de `t`. Um erro no nome ↔ instante, na janela ou no "só o que
 * existe" vira 404 no meio da animação — por isso esses três têm o teste mais
 * detalhado.
 *
 *   node --test scripts/radar.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import zlib from 'node:zlib';
import {
  FONTE,
  FONTE_URL,
  JANELA_H,
  MARGEM_MIN,
  PRODUTOS,
  RADARES,
  bboxDeGeometria,
  bytesDe,
  chave,
  contarArquivos,
  erroDeTls,
  escolherExtent,
  extentValida,
  extentsDaWfs,
  instantesDoManifesto,
  lerIHDR,
  limiteDaJanela,
  medianaIntervalo,
  montarManifesto,
  naJanela,
  nomeDeT,
  parseListagem,
  planejar,
  pngValido,
  tDeNome,
} from './radares.mjs';

const MIN = 60;
const H = 60 * MIN;

// ---------------------------------------------------------------------------
// Listagem real do SIPAM
// ---------------------------------------------------------------------------

// Trecho de verdade de https://siger.sipam.gov.br/radar/sbbe/dbz/ em
// 21/09/2026, com a linha de diretório pai, e três linhas de subdiretório
// da listagem de /radar/sbbe/ (que não podem virar arquivo). Belém grava
// segundos ≠ 0.
const LISTAGEM_SBBE = `<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">
<html>
 <head>
  <title>Index of /radar/sbbe/dbz</title>
 </head>
 <body>
<h1>Index of /radar/sbbe/dbz</h1>
  <table>
   <tr><th valign="top"><img src="/icons/blank.gif" alt="[ICO]"></th><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=M;O=A">Last modified</a></th><th><a href="?C=S;O=A">Size</a></th><th><a href="?C=D;O=A">Description</a></th></tr>
   <tr><th colspan="5"><hr></th></tr>
<tr><td valign="top"><img src="/icons/back.gif" alt="[PARENTDIR]"></td><td><a href="/radar/sbbe/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/folder.gif" alt="[DIR]"></td><td><a href="dbz/">dbz/</a></td><td align="right">2026-09-21 09:36  </td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/folder.gif" alt="[DIR]"></td><td><a href="rate/">rate/</a></td><td align="right">2026-09-21 09:36  </td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/folder.gif" alt="[DIR]"></td><td><a href="vil/">vil/</a></td><td align="right">2026-09-21 09:36  </td><td align="right">  - </td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/image2.gif" alt="[IMG]"></td><td><a href="2026_09_14_12_20_06.png">2026_09_14_12_20_06.png</a></td><td align="right">2026-09-14 09:35  </td><td align="right">6.6K</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/image2.gif" alt="[IMG]"></td><td><a href="2026_09_21_12_10_06.png">2026_09_21_12_10_06.png</a></td><td align="right">2026-09-21 09:22  </td><td align="right">6.6K</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/image2.gif" alt="[IMG]"></td><td><a href="2026_09_21_12_20_06.png">2026_09_21_12_20_06.png</a></td><td align="right">2026-09-21 09:32  </td><td align="right">6.4K</td><td>&nbsp;</td></tr>
   <tr><th colspan="5"><hr></th></tr>
</table>
<address>Apache/2.4.67 (Debian) Server at 172.21.5.198 Port 80</address>
</body></html>`;

// sbmn/dbz, na mesma data: minutos múltiplos de 12, tamanho com espaço à esquerda.
const LINHAS_SBMN = `<table>
<tr><td valign="top"><img src="/icons/image2.gif" alt="[IMG]"></td><td><a href="2026_09_21_11_48_00.png">2026_09_21_11_48_00.png</a></td><td align="right">2026-09-21 08:57  </td><td align="right"> 25K</td><td>&nbsp;</td></tr>
<tr><td valign="top"><img src="/icons/image2.gif" alt="[IMG]"></td><td><a href="2026_09_21_12_12_00.png">2026_09_21_12_12_00.png</a></td><td align="right">2026-09-21 09:21  </td><td align="right"> 22K</td><td>&nbsp;</td></tr>
</table>`;

// sbmq/dbz em 21/09/2026: o radar existe, o diretório está vazio.
const LISTAGEM_VAZIA = `<h1>Index of /radar/sbmq/dbz</h1>
  <table>
<tr><td valign="top"><img src="/icons/back.gif" alt="[PARENTDIR]"></td><td><a href="/radar/sbmq/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td><td>&nbsp;</td></tr>
</table>`;

test('parseListagem: só nomes de varredura, com os segundos de Belém', () => {
  const itens = parseListagem(LISTAGEM_SBBE);
  assert.deepEqual(
    itens.map((i) => i.nome),
    ['2026_09_14_12_20_06.png', '2026_09_21_12_10_06.png', '2026_09_21_12_20_06.png'],
    'diretório pai, subdiretórios e links de ordenação ficam de fora',
  );
  assert.equal(itens[2].t, Date.UTC(2026, 8, 21, 12, 20, 6) / 1000);
  assert.equal(itens[2].t % 60, 6, 'os segundos entram no instante');
  assert.equal(itens[0].bytes, Math.round(6.6 * 1024));
  assert.equal(itens[2].bytes, Math.round(6.4 * 1024));
});

test('parseListagem: Manaus, tamanho com espaço, em ordem de tempo', () => {
  const itens = parseListagem(LINHAS_SBMN);
  assert.deepEqual(
    itens.map((i) => [i.nome, i.bytes]),
    [
      ['2026_09_21_11_48_00.png', 25 * 1024],
      ['2026_09_21_12_12_00.png', 22 * 1024],
    ],
  );
  assert.equal(itens[1].t, 1789992720);
});

test('parseListagem: diretório vazio (sbmq, sbtf) dá lista vazia, não erro', () => {
  assert.deepEqual(parseListagem(LISTAGEM_VAZIA), []);
  assert.deepEqual(parseListagem(''), []);
  assert.deepEqual(parseListagem(null), []);
});

test('parseListagem: listagem sem tabela ainda dá os nomes, sem tamanho', () => {
  const html = '<ul><li><a href="2026_09_21_12_12_00.png"> x</a></li><li><a href="../">..</a></li></ul>';
  assert.deepEqual(parseListagem(html), [{ nome: '2026_09_21_12_12_00.png', t: 1789992720, bytes: null }]);
});

test('parseListagem: ordem de tempo e sem repetição, mesmo com a listagem fora de ordem', () => {
  const html =
    '<a href="2026_09_21_12_12_00.png">a</a><a href="2026_09_21_11_48_00.png">b</a><a href="2026_09_21_12_12_00.png">c</a>';
  assert.deepEqual(
    parseListagem(html).map((i) => i.t),
    [Date.UTC(2026, 8, 21, 11, 48) / 1000, 1789992720],
  );
});

test('bytesDe lê o tamanho do Apache', () => {
  assert.equal(bytesDe('6.6K'), 6758);
  assert.equal(bytesDe(' 20K'), 20480);
  assert.equal(bytesDe('436'), 436);
  assert.equal(bytesDe('0'), 0);
  assert.equal(bytesDe('1.2M'), Math.round(1.2 * 1024 * 1024));
  assert.equal(bytesDe('  - '), null);
  assert.equal(bytesDe('2026-09-21 09:32'), null);
  assert.equal(bytesDe(undefined), null);
});

// ---------------------------------------------------------------------------
// Nome ↔ instante
// ---------------------------------------------------------------------------

test('nomeDeT e tDeNome: ida e volta, com segundos', () => {
  for (const nome of ['2026_09_21_12_12_00.png', '2026_09_21_12_10_06.png', '2026_12_31_23_59_59.png', '2028_02_29_00_00_00.png']) {
    const t = tDeNome(nome);
    assert.ok(Number.isInteger(t), nome);
    assert.equal(nomeDeT(t), nome);
  }
  assert.equal(tDeNome('2026_09_21_12_12_00.png'), 1789992720);
  assert.equal(nomeDeT(1789992720), '2026_09_21_12_12_00.png');
  // Ida e volta a partir do instante, em toda a janela, de 7 em 7 s.
  for (let t = 1789992720 - 48 * H; t <= 1789992720; t += 7 * 3607) assert.equal(tDeNome(nomeDeT(t)), t);
});

test('tDeNome: o que não é nome de varredura dá null', () => {
  assert.equal(tDeNome('2026_02_31_00_00_00.png'), null, '31/02 não existe — Date.UTC normalizaria em silêncio');
  assert.equal(tDeNome('2026_09_21_24_00_00.png'), null);
  assert.equal(tDeNome('2026_09_21_12_12.png'), null, 'sem segundos');
  assert.equal(tDeNome('2026_09_21_12_12_00.PNG'), null);
  assert.equal(tDeNome('2026_09_21_12_12_00.png.parcial'), null);
  assert.equal(tDeNome('/radar/sbmn/dbz/2026_09_21_12_12_00.png'), null);
  assert.equal(tDeNome('dbz/'), null);
  assert.equal(tDeNome('?C=N;O=D'), null);
  assert.equal(tDeNome(null), null);
  assert.equal(tDeNome(1789992720), null);
});

test('nomeDeT: só instante inteiro', () => {
  assert.equal(nomeDeT(1789992720.5), null);
  assert.equal(nomeDeT(NaN), null);
  assert.equal(nomeDeT('1789992720'), null);
});

// ---------------------------------------------------------------------------
// Janela
// ---------------------------------------------------------------------------

test('janela: 48 h mais 30 min por padrão, borda inclusiva', () => {
  assert.equal(JANELA_H, 48);
  assert.equal(MARGEM_MIN, 30);
  const agoraMs = Date.parse('2026-09-21T12:30:00Z');
  const desde = limiteDaJanela(agoraMs);
  assert.equal(desde, agoraMs / 1000 - 48 * H - 30 * MIN);
  assert.equal(naJanela(desde, desde), true, 'exatamente no limite entra');
  assert.equal(naJanela(desde - 1, desde), false);
  assert.equal(naJanela(desde + 1, desde), true);
  assert.equal(naJanela(desde + 0.5, desde), false, 'instante não inteiro não é nome de arquivo');
  assert.equal(naJanela(NaN, desde), false);
});

test('janela: configurável, e milissegundos do relógio não deslocam a borda', () => {
  const agoraMs = Date.parse('2026-09-21T12:30:00Z') + 999;
  assert.equal(limiteDaJanela(agoraMs, 6, 0), Date.parse('2026-09-21T06:30:00Z') / 1000);
  assert.equal(limiteDaJanela(agoraMs, 1, 15), Date.parse('2026-09-21T11:15:00Z') / 1000);
});

// ---------------------------------------------------------------------------
// PNG
// ---------------------------------------------------------------------------

function bloco(tipo, dados) {
  const tam = Buffer.alloc(4);
  tam.writeUInt32BE(dados.length);
  const corpo = Buffer.concat([Buffer.from(tipo, 'latin1'), dados]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(zlib.crc32(corpo));
  return Buffer.concat([tam, corpo, crc]);
}

/** PNG RGBA mínimo e decodificável, de `w`×`h` pixels transparentes. */
function pngMinimo(w = 1, h = 1) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; // bits por canal
  ihdr[9] = 6; // RGBA
  const linhas = Buffer.alloc(h * (1 + 4 * w)); // filtro 0 + pixels zerados
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    bloco('IHDR', ihdr),
    bloco('IDAT', zlib.deflateSync(linhas)),
    bloco('IEND', Buffer.alloc(0)),
  ]);
}

test('lerIHDR: dimensões de um PNG de verdade', () => {
  assert.deepEqual(lerIHDR(pngMinimo()), { w: 1, h: 1 });
  assert.deepEqual(lerIHDR(pngMinimo(954, 954)), { w: 954, h: 954 });
  assert.deepEqual(lerIHDR(pngMinimo(1000, 1000).subarray(0, 24)), { w: 1000, h: 1000 }, 'basta o começo');
  assert.deepEqual(lerIHDR(new Uint8Array(pngMinimo(3, 2))), { w: 3, h: 2 }, 'aceita Uint8Array');
});

test('lerIHDR: HTML de erro, vazio e cabeçalho torto dão null', () => {
  const html = Buffer.from('<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN"><html><head><title>404 Not Found</title></head></html>');
  assert.equal(lerIHDR(html), null);
  assert.equal(lerIHDR(Buffer.alloc(0)), null);
  assert.equal(lerIHDR(null), null);
  const png = pngMinimo();
  assert.equal(lerIHDR(png.subarray(0, 20)), null, 'curto demais para ter as dimensões');
  const semIhdr = Buffer.from(png);
  semIhdr.write('IDAT', 12, 'latin1');
  assert.equal(lerIHDR(semIhdr), null, 'o primeiro bloco precisa ser o IHDR');
  const zerado = Buffer.from(png);
  zerado.writeUInt32BE(0, 16);
  assert.equal(lerIHDR(zerado), null, 'largura zero');
});

test('pngValido: exige o começo E o fim do PNG', () => {
  const png = pngMinimo(954, 954);
  assert.equal(pngValido(png), true);
  assert.equal(pngValido(png.subarray(0, png.length - 1)), false, 'download cortado no último byte');
  assert.equal(pngValido(png.subarray(0, 40)), false);
  assert.equal(pngValido(Buffer.from('<html>erro</html>')), false);
  assert.equal(pngValido(Buffer.alloc(0)), false);
});

// ---------------------------------------------------------------------------
// Extensão
// ---------------------------------------------------------------------------

const MANAUS = RADARES.find((r) => r.id === 'sbmn');

test('tabela: 11 radares em ordem, extensões válidas, produtos sem vil', () => {
  assert.equal(RADARES.length, 11);
  assert.deepEqual(
    RADARES.map((r) => r.id),
    [...RADARES.map((r) => r.id)].sort(),
  );
  for (const r of RADARES) assert.ok(extentValida(r.extent), `${r.id} com extensão inválida na tabela`);
  assert.deepEqual(PRODUTOS, ['dbz', 'rate']);
});

test('bboxDeGeometria: MultiPolygon da WFS', () => {
  const geom = {
    type: 'MultiPolygon',
    coordinates: [
      [
        [
          [-62.1674, -3.1],
          [-59.99, -0.97289],
          [-57.8154, -3.1],
          [-59.99, -5.32489],
          [-62.1674, -3.1],
        ],
      ],
    ],
  };
  assert.deepEqual(bboxDeGeometria(geom), [-62.1674, -5.32489, -57.8154, -0.97289]);
  assert.deepEqual(bboxDeGeometria({ type: 'Point', coordinates: [1, 2] }), [1, 2, 1, 2]);
  assert.equal(bboxDeGeometria(null), null);
  assert.equal(bboxDeGeometria({ type: 'Polygon', coordinates: [] }), null);
});

test('extentValida: tamanho, números e a troca de eixos', () => {
  assert.equal(extentValida([-62.1674, -5.32489, -57.8154, -0.97289]), true);
  assert.equal(extentValida([-5.32489, -62.1674, -0.97289, -57.8154]), false, 'lat/lon trocados');
  assert.equal(extentValida([-62, -5, -61.5, -0.9]), false, 'estreita demais');
  assert.equal(extentValida([-75, -15, -50, 5]), false, 'larga demais');
  assert.equal(extentValida([-57.8, -0.97, -62.1, -5.32]), false, 'mínimo e máximo invertidos');
  assert.equal(extentValida([-62, -5, -57, NaN]), false);
  assert.equal(extentValida([-62, -5, -57]), false);
  assert.equal(extentValida(['-62', -5, -57, -1]), false);
  assert.equal(extentValida(null), false);
});

test('escolherExtent: WFS válida vence; inválida cai na tabela', () => {
  const daWfs = [-62.1674, -5.32489, -57.8154, -0.97289];
  assert.deepEqual(escolherExtent(MANAUS, daWfs), { extent: daWfs, fonte: 'wfs', divergencia: 0 });

  const macapa = RADARES.find((r) => r.id === 'sbmq');
  const wfsMacapa = [-53.2729, -2.221597, -48.9209, 2.130403]; // o que a WFS devolveu em 21/09/2026
  const e = escolherExtent(macapa, wfsMacapa);
  assert.equal(e.fonte, 'wfs');
  assert.deepEqual(e.extent, wfsMacapa);
  assert.ok(e.divergencia < 0.01, 'arredondamento da tabela não é divergência');

  for (const ruim of [undefined, null, [NaN, 0, 1, 2], [-5.32489, -62.1674, -0.97289, -57.8154], [-62, -5, -61.9, -4.9]]) {
    const r = escolherExtent(MANAUS, ruim);
    assert.equal(r.fonte, 'tabela');
    assert.deepEqual(r.extent, MANAUS.extent);
    assert.notEqual(r.extent, MANAUS.extent, 'cópia, não a própria tabela');
  }

  const mudou = escolherExtent(MANAUS, [-62.2674, -5.32489, -57.8154, -0.97289]);
  assert.equal(mudou.fonte, 'wfs');
  assert.ok(Math.abs(mudou.divergencia - 0.1) < 1e-9);
});

test('extentsDaWfs: uma extensão por radar, pela propriedade `radar`', () => {
  const quadrado = (x0, y0) => ({
    type: 'MultiPolygon',
    coordinates: [[[[x0, y0], [x0 + 4.352, y0], [x0 + 4.352, y0 + 4.352], [x0, y0 + 4.352], [x0, y0]]]],
  });
  const fc = {
    type: 'FeatureCollection',
    features: [
      { properties: { radar: 'sbmn', cidade: 'Manaus' }, geometry: quadrado(-62.1674, -5.32489) },
      { properties: { radar: 'SBBE' }, geometry: quadrado(-50.6378, -3.5827) },
      { properties: { radar: 'sbmn' }, geometry: quadrado(0, 0) },
      { properties: { cidade: 'sem id' }, geometry: quadrado(0, 0) },
      { properties: { radar: 'sbtf' }, geometry: null },
    ],
  };
  const m = extentsDaWfs(fc);
  assert.deepEqual([...m.keys()].sort(), ['sbbe', 'sbmn']);
  assert.equal(m.get('sbmn')[0], -62.1674, 'repetida vale a primeira');
  assert.equal(extentsDaWfs(null).size, 0);
  assert.equal(extentsDaWfs({ erro: 'x' }).size, 0);
});

// ---------------------------------------------------------------------------
// Planejamento
// ---------------------------------------------------------------------------

const T0 = 1789992720; // 2026-09-21 12:12:00 UTC
const DESDE = T0 - 6 * H;
const dois = [MANAUS, RADARES.find((r) => r.id === 'sbbe')];

function listagem(...ts) {
  return { itens: ts.map((t) => ({ nome: nomeDeT(t), t, bytes: 20_000 })) };
}

test('planejar: reaproveita o depósito e só busca o que falta', () => {
  const deposito = new Map([[chave('sbmn', 'dbz'), new Set([T0 - 24 * MIN, T0 - 12 * MIN])]]);
  const listagens = new Map([[chave('sbmn', 'dbz'), listagem(T0 - 24 * MIN, T0 - 12 * MIN, T0)]]);
  const { porChave, fila } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito, listagens, desde: DESDE });
  const e = porChave.get('sbmn/dbz');
  assert.deepEqual(e.reaproveitar, [T0 - 24 * MIN, T0 - 12 * MIN]);
  assert.deepEqual(fila, [{ id: 'sbmn', produto: 'dbz', t: T0, fonte: 'sipam', naListagem: true }]);
  assert.equal(e.herdado, false);
  assert.equal(e.erro, null);
});

test('planejar: poda o que saiu da janela e não busca o que está fora dela', () => {
  const deposito = new Map([[chave('sbmn', 'dbz'), new Set([DESDE - 1, DESDE, T0])]]);
  const listagens = new Map([[chave('sbmn', 'dbz'), listagem(DESDE - 12 * MIN, DESDE - 1, DESDE, T0)]]);
  const { porChave, fila } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito, listagens, desde: DESDE });
  assert.deepEqual(porChave.get('sbmn/dbz').podar, [DESDE - 1]);
  assert.deepEqual(porChave.get('sbmn/dbz').reaproveitar, [DESDE, T0]);
  assert.equal(fila.length, 0);
});

test('planejar: a fila vai do MAIS NOVO ao mais antigo, misturando radares e produtos', () => {
  const listagens = new Map([
    [chave('sbmn', 'dbz'), listagem(T0 - 24 * MIN, T0)],
    [chave('sbmn', 'rate'), listagem(T0 - 24 * MIN, T0)],
    [chave('sbbe', 'dbz'), listagem(T0 - 10 * MIN + 6, T0 - 50 * MIN + 6)],
    [chave('sbbe', 'rate'), listagem()],
  ]);
  const { fila } = planejar({ radares: dois, deposito: new Map(), listagens, desde: DESDE });
  assert.deepEqual(
    fila.map((x) => `${x.id}/${x.produto}@${(x.t - T0) / MIN}`),
    ['sbmn/dbz@0', 'sbmn/rate@0', 'sbbe/dbz@-9.9', 'sbmn/dbz@-24', 'sbmn/rate@-24', 'sbbe/dbz@-49.9'],
  );
});

test('planejar: listagem que falhou é herdada — sem SIPAM, com o depósito', () => {
  const deposito = new Map([[chave('sbmn', 'dbz'), new Set([DESDE - 60, T0 - 12 * MIN])]]);
  const listagens = new Map([[chave('sbmn', 'dbz'), { erro: 'HTTP 503' }]]);
  const { porChave, fila } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito, listagens, desde: DESDE });
  const e = porChave.get('sbmn/dbz');
  assert.equal(e.herdado, true);
  assert.equal(e.erro, 'HTTP 503');
  assert.deepEqual(e.reaproveitar, [T0 - 12 * MIN], 'o anterior, filtrado pela janela');
  assert.deepEqual(e.podar, [DESDE - 60]);
  assert.equal(fila.length, 0, 'sem listagem não se sabe o que existe no SIPAM');
});

test('planejar: sem listagem nenhuma para a chave também é herdado', () => {
  const { porChave } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito: new Map(), listagens: new Map(), desde: DESDE });
  assert.equal(porChave.get('sbmn/dbz').herdado, true);
  assert.equal(porChave.get('sbmn/dbz').erro, 'sem listagem');
});

test('planejar: cache perdido — o que está no ar vem do Pages, o resto do SIPAM', () => {
  const listagens = new Map([[chave('sbmn', 'dbz'), listagem(T0 - 36 * MIN, T0 - 24 * MIN, T0 - 12 * MIN, T0)]]);
  const noAr = new Map([[chave('sbmn', 'dbz'), new Set([DESDE - 60, T0 - 36 * MIN, T0 - 24 * MIN, T0 - 12 * MIN])]]);
  const { fila } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito: new Map(), listagens, noAr, desde: DESDE });
  assert.deepEqual(
    fila.map((x) => [(x.t - T0) / MIN, x.fonte]),
    [
      [0, 'sipam'],
      [-12, 'pages'],
      [-24, 'pages'],
      [-36, 'pages'],
    ],
    'o que saiu da janela não é restaurado',
  );
  assert.ok(fila.every((x) => x.naListagem), 'se o Pages falhar, o SIPAM tem o arquivo');
});

test('planejar: cache perdido E listagem falhou — o ar sustenta o radar, pelo Pages', () => {
  const noAr = new Map([[chave('sbmn', 'dbz'), new Set([T0 - 12 * MIN])]]);
  const listagens = new Map([[chave('sbmn', 'dbz'), { erro: 'sem resposta em 20 s' }]]);
  const { porChave, fila } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito: new Map(), listagens, noAr, desde: DESDE });
  assert.equal(porChave.get('sbmn/dbz').herdado, true);
  assert.deepEqual(fila, [{ id: 'sbmn', produto: 'dbz', t: T0 - 12 * MIN, fonte: 'pages', naListagem: false }]);
});

test('planejar: arquivo de 0 byte na listagem não é buscado', () => {
  const listagens = new Map([
    [chave('sbmn', 'dbz'), { itens: [{ nome: nomeDeT(T0), t: T0, bytes: 0 }, { nome: nomeDeT(T0 - 720), t: T0 - 720, bytes: null }] }],
  ]);
  const { fila } = planejar({ radares: [MANAUS], produtos: ['dbz'], deposito: new Map(), listagens, desde: DESDE });
  assert.deepEqual(
    fila.map((x) => x.t),
    [T0 - 720],
    'tamanho desconhecido (null) é buscado',
  );
});

// ---------------------------------------------------------------------------
// Manifesto
// ---------------------------------------------------------------------------

function entrada(id, produtos, extra = {}) {
  const r = RADARES.find((x) => x.id === id);
  return { id, nome: r.nome, extent: r.extent, extentFonte: 'wfs', px: [954, 954], produtos, ...extra };
}

test('montarManifesto: o formato do contrato', () => {
  const m = montarManifesto({
    gerado: '2026-09-21T12:30:00.000Z',
    radares: [entrada('sbmn', { dbz: { t: [T0], herdado: false, erro: null } })],
  });
  assert.deepEqual(m, {
    gerado: '2026-09-21T12:30:00.000Z',
    fonte: FONTE,
    fonteUrl: FONTE_URL,
    janelaHoras: 48,
    produtos: ['dbz', 'rate'],
    radares: [
      {
        id: 'sbmn',
        nome: 'Manaus',
        extent: MANAUS.extent,
        extentFonte: 'wfs',
        px: [954, 954],
        produtos: {
          dbz: { t: [T0], herdado: false, erro: null },
          rate: { t: [], herdado: false, erro: null },
        },
      },
    ],
  });
  assert.equal(FONTE, 'SIPAM — Sistema de Proteção da Amazônia (Censipam)');
  assert.equal(FONTE_URL, 'https://siger.sipam.gov.br/radar/');
});

test('montarManifesto: radares por id, t crescente e sem repetição', () => {
  const m = montarManifesto({
    gerado: 'x',
    radares: [
      entrada('sbua', { dbz: { t: [T0] } }),
      entrada('sbbe', { dbz: { t: new Set([T0, T0 - 600, T0 - 1200]) } }),
      entrada('sbmn', { rate: { t: [T0, T0 - 720, T0, T0 - 1440, T0 - 720] } }),
    ],
  });
  assert.deepEqual(
    m.radares.map((r) => r.id),
    ['sbbe', 'sbmn', 'sbua'],
  );
  assert.deepEqual(m.radares[0].produtos.dbz.t, [T0 - 1200, T0 - 600, T0]);
  assert.deepEqual(m.radares[1].produtos.rate.t, [T0 - 1440, T0 - 720, T0]);
});

test('montarManifesto: só o que existe, só na janela, só inteiros', () => {
  const existe = (id, p, t) => !(id === 'sbmn' && p === 'dbz' && t === T0 - 720);
  const m = montarManifesto({
    gerado: 'x',
    desde: DESDE,
    existe,
    radares: [entrada('sbmn', { dbz: { t: [T0, T0 - 720, DESDE - 1, DESDE, T0 + 0.5, NaN] } })],
  });
  assert.deepEqual(m.radares[0].produtos.dbz.t, [DESDE, T0]);
});

test('montarManifesto: herdado, erro, px nulo e radar vazio (sbmq)', () => {
  const m = montarManifesto({
    gerado: 'x',
    janelaHoras: 6,
    radares: [
      entrada('sbmq', {}, { px: null, extentFonte: 'tabela' }),
      entrada('sbmn', { dbz: { t: [T0], herdado: true, erro: 'HTTP 503' } }),
    ],
  });
  assert.equal(m.janelaHoras, 6);
  const [manaus, macapa] = m.radares;
  assert.deepEqual(manaus.produtos.dbz, { t: [T0], herdado: true, erro: 'HTTP 503' });
  assert.equal(macapa.px, null);
  assert.equal(macapa.extentFonte, 'tabela');
  assert.deepEqual(macapa.produtos, {
    dbz: { t: [], herdado: false, erro: null },
    rate: { t: [], herdado: false, erro: null },
  });
  assert.equal(contarArquivos(m), 1);
});

test('instantesDoManifesto: volta ao formato do planejamento', () => {
  const m = montarManifesto({
    gerado: 'x',
    radares: [entrada('sbmn', { dbz: { t: [T0, T0 - 720] } })],
  });
  const s = instantesDoManifesto(m);
  assert.deepEqual([...s.get('sbmn/dbz')], [T0 - 720, T0]);
  assert.deepEqual([...s.get('sbmn/rate')], []);
  assert.equal(instantesDoManifesto(null), null);
  assert.equal(instantesDoManifesto({ erro: 1 }), null);
  assert.equal(instantesDoManifesto(JSON.parse('"<html>"')), null);
});

// ---------------------------------------------------------------------------
// Log e rede
// ---------------------------------------------------------------------------

test('medianaIntervalo: o passo típico, imune a um buraco', () => {
  assert.equal(medianaIntervalo([0, 600, 1200, 1800, 9000]), 600);
  assert.equal(medianaIntervalo([0, 720, 1440, 2880]), 720, 'Manaus pulando o :00');
  assert.equal(medianaIntervalo([0, 600, 1320, 1920]), 600);
  assert.equal(medianaIntervalo([0, 600, 1300, 2000]), 700, 'número par de intervalos: média dos dois do meio');
  assert.equal(medianaIntervalo([5]), null);
  assert.equal(medianaIntervalo([]), null);
  assert.equal(medianaIntervalo(undefined), null);
});

test('erroDeTls: reconhece a cadeia incompleta pelo `cause` do fetch', () => {
  // A forma medida contra uma cadeia sem o intermediário.
  const cadeia = Object.assign(new TypeError('fetch failed'), {
    cause: Object.assign(new Error('unable to verify the first certificate'), { code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' }),
  });
  assert.equal(erroDeTls(cadeia), true);
  const expirado = Object.assign(new TypeError('fetch failed'), { cause: { code: 'CERT_HAS_EXPIRED', message: 'certificate has expired' } });
  assert.equal(erroDeTls(expirado), true);
  const recusada = Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED', message: 'connect ECONNREFUSED' } });
  assert.equal(erroDeTls(recusada), false, 'rede fora não é motivo para desligar a validação');
  assert.equal(erroDeTls(new DOMException('The operation was aborted due to timeout', 'TimeoutError')), false);
  assert.equal(erroDeTls(null), false);
});
