/**
 * Testes das funções puras dos radares da REDEMET (`redemet.mjs`).
 *
 *   node --test scripts/redemet.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { RADARES } from './radares.mjs';
import {
  MEIA_LARGURA_M,
  RADARES_REDEMET,
  aeqdInversa,
  cobertura,
  escolherCentro,
  extentAeqd,
  horaDaApi,
  horasAConsultar,
  lerRespostaRedemet,
  tDaRedemet,
} from './redemet.mjs';

const H = 3600_000;

// Trecho de verdade da resposta de /produtos/radar/maxcappi?data=2026111820&anima=15
// (18/11/2026), com dois quadros e três radares: um com PNG, um sem (`path`
// null, Macapá) e um com o PNG repetido no quadro seguinte (Porto Velho).
const RESPOSTA = {
  status: true,
  message: 200,
  data: {
    tipo: 'maxcappi',
    anima: ['20:18', '20:28'],
    radar: [
      [
        {
          id: 10, nome: 'Radar - Manaus/AM', localidade: 'mn', raio: 400, lat_center: '-3.149216', lon_center: '-59.991881',
          lon_min: '-62.148', lon_max: '-57.846', lat_min: '-6.718', lat_max: '0.451',
          path: 'https://estatico-redemet.decea.mil.br/radar/2026/11/18/mn/maxcappi/maps/2026-11-18--20:17:49.png',
          tamanho: 5000, data: '2026-11-18 20:17:49',
        },
        { id: 11, nome: 'Radar - Macapá/AP', localidade: 'mq', raio: 400, lat_center: '-0.047222', lon_center: '-51.097778', path: null, tamanho: null, data: null },
        {
          id: 14, nome: 'Radar - Porto Velho/RO', localidade: 'pv', raio: 400, lat_center: '-8.715', lon_center: '-63.893889',
          path: 'https://estatico-redemet.decea.mil.br/radar/2026/11/18/pv/maxcappi/maps/2026-11-18--20:17:43.png',
          tamanho: 762, data: '2026-11-18 20:17:43',
        },
      ],
      [
        {
          id: 10, nome: 'Radar - Manaus/AM', localidade: 'mn', raio: 400, lat_center: '-3.149216', lon_center: '-59.991881',
          path: 'https://estatico-redemet.decea.mil.br/radar/2026/11/18/mn/maxcappi/maps/2026-11-18--20:27:45.png',
          tamanho: 5228, data: '2026-11-18 20:27:45',
        },
        {
          id: 14, nome: 'Radar - Porto Velho/RO', localidade: 'pv', raio: 400, lat_center: '-8.715', lon_center: '-63.893889',
          path: 'https://estatico-redemet.decea.mil.br/radar/2026/11/18/pv/maxcappi/maps/2026-11-18--20:17:43.png',
          tamanho: 762, data: '2026-11-18 20:17:43',
        },
      ],
    ],
  },
};

test('tabela: ids únicos, reserva só de radar do SIPAM que existe, centros na região', () => {
  const ids = RADARES_REDEMET.map((r) => r.id);
  assert.equal(new Set(ids).size, ids.length);
  const sipam = new Set(RADARES.map((r) => r.id));
  for (const r of RADARES_REDEMET) {
    assert.match(r.id, /^rd[a-z]{2}$/);
    assert.equal(r.id.slice(2), r.local);
    if (r.reservaDe !== null) assert.ok(sipam.has(r.reservaDe), `${r.id} reserva de ${r.reservaDe}`);
    assert.ok(r.centro[0] > -75 && r.centro[0] < -40 && r.centro[1] > -17 && r.centro[1] < 4, r.id);
  }
  // Cada radar do SIPAM tem a sua reserva; os que só a REDEMET publica, sem reserva.
  assert.deepEqual(
    RADARES_REDEMET.filter((r) => r.reservaDe).map((r) => r.reservaDe).sort(),
    [...sipam].sort(),
  );
  assert.deepEqual(RADARES_REDEMET.filter((r) => !r.reservaDe).map((r) => r.nome).sort(), ['Chapada dos Guimarães', 'Rio Branco', 'Vilhena']);
});

test('horaDaApi: AAAAMMDDHH em UTC', () => {
  assert.equal(horaDaApi(Date.UTC(2026, 10, 18, 20, 59, 59)), '2026111820');
  assert.equal(horaDaApi(Date.UTC(2026, 0, 2, 3)), '2026010203');
});

test('tDaRedemet: o carimbo da API em segundos UTC; lixo dá null', () => {
  assert.equal(tDaRedemet('2026-11-18 20:28:08'), Date.UTC(2026, 10, 18, 20, 28, 8) / 1000);
  for (const x of [null, '', '2026-11-18T20:28:08Z', '2026-02-31 10:00:00', '2026-11-18 25:00:00']) assert.equal(tDaRedemet(x), null, String(x));
});

test('lerRespostaRedemet: por radar, crescente, sem repetição; sem PNG não entra', () => {
  const { porLocal, centros } = lerRespostaRedemet(RESPOSTA);
  assert.deepEqual([...porLocal.keys()].sort(), ['mn', 'pv']);
  assert.deepEqual(
    porLocal.get('mn').map((x) => x.t),
    [Date.UTC(2026, 10, 18, 20, 17, 49) / 1000, Date.UTC(2026, 10, 18, 20, 27, 45) / 1000],
  );
  assert.equal(porLocal.get('pv').length, 1);
  assert.match(porLocal.get('mn')[1].url, /2026-11-18--20:27:45\.png$/);
  // O centro vem mesmo de quem está sem imagem (Macapá).
  assert.deepEqual(centros.get('mq'), [-51.097778, -0.047222]);
});

test('lerRespostaRedemet: caminho fora do estático da REDEMET não vira download', () => {
  const r = structuredClone(RESPOSTA);
  r.data.radar[0][0].path = 'https://exemplo.com/x.png';
  r.data.radar[1][0].path = 'https://estatico-redemet.decea.mil.br/radar/a b.png';
  assert.equal(lerRespostaRedemet(r).porLocal.has('mn'), false);
});

test('lerRespostaRedemet: resposta de erro lança', () => {
  for (const x of [null, {}, { status: false, message: 'Não foi informado um tipo de radar.' }, { status: true, data: {} }]) {
    assert.throws(() => lerRespostaRedemet(x), /resposta inesperada/);
  }
});

/** Varreduras de 10 em 10 min em [ini, fim) (s), menos as que caem em `buracos` ([a, b) em s). */
function varreduras(ini, fim, buracos = []) {
  const out = [];
  for (let t = ini; t < fim; t += 600) if (!buracos.some(([a, b]) => t >= a && t < b)) out.push(t);
  return out;
}

test('cobertura: fração dos intervalos de 20 min com varredura', () => {
  const ini = Date.UTC(2026, 10, 18, 8) / 1000;
  assert.equal(cobertura(varreduras(ini, ini + 5 * 3600), ini, ini + 5 * 3600), 1);
  assert.equal(cobertura([], ini, ini + 3600), 0);
  assert.equal(cobertura(varreduras(ini, ini + 3600), ini, ini + 2 * 3600), 0.5);
});

test('horasAConsultar: com o depósito em dia, só a hora corrente', () => {
  const agora = Date.UTC(2026, 10, 18, 20, 31);
  const desde = agora / 1000 - 48.5 * 3600;
  const horas = horasAConsultar({ agoraMs: agora, desdeS: desde, instantes: varreduras(desde - 600, agora / 1000) });
  assert.deepEqual(horas, [Date.UTC(2026, 10, 18, 20)]);
});

test('horasAConsultar: estreia — um bloco a cada 4 h até cobrir a janela', () => {
  const agora = Date.UTC(2026, 10, 18, 20, 31);
  const desde = agora / 1000 - 48.5 * 3600;
  const horas = horasAConsultar({ agoraMs: agora, desdeS: desde, instantes: [] });
  assert.equal(horas[0], Date.UTC(2026, 10, 18, 20));
  for (let i = 1; i < horas.length; i++) assert.equal(horas[i - 1] - horas[i], 4 * H);
  // O último bloco (que cobre de H−4 h a H+1 h) alcança o começo da janela.
  const ultimo = horas.at(-1);
  assert.ok(ultimo - 4 * H <= desde * 1000 && ultimo + H > desde * 1000);
  assert.equal(horas.length, 13);
});

test('horasAConsultar: bloco incompleto pedido há pouco espera a reconsulta', () => {
  const agora = Date.UTC(2026, 10, 18, 20, 31);
  const desde = agora / 1000 - 10 * 3600;
  const h1 = Date.UTC(2026, 10, 18, 16);
  const h2 = Date.UTC(2026, 10, 18, 12);
  const consultas = new Map([
    [h1, agora - 10 * 60_000],
    [h2, agora - 2 * H],
  ]);
  const horas = horasAConsultar({ agoraMs: agora, desdeS: desde, instantes: [], consultas });
  assert.deepEqual(horas, [Date.UTC(2026, 10, 18, 20), h2]);
});

test('horasAConsultar: refaz o bloco com buraco, mesmo com varredura nas pontas dele', () => {
  const agora = Date.UTC(2026, 10, 18, 20, 31);
  const desde = agora / 1000 - 12 * 3600;
  // Sem nada das 09:00 às 13:00: o bloco da hora 12 (08:00–13:00) fica com 1 h de 5.
  const buraco = [Date.UTC(2026, 10, 18, 9) / 1000, Date.UTC(2026, 10, 18, 13) / 1000];
  const horas = horasAConsultar({ agoraMs: agora, desdeS: desde, instantes: varreduras(desde - 600, agora / 1000, [buraco]) });
  assert.deepEqual(horas, [Date.UTC(2026, 10, 18, 20), Date.UTC(2026, 10, 18, 12)]);
});

test('aeqdInversa: confere com o pyproj (esfera de 6371 km, centro de Manaus)', () => {
  const c = [-59.991881, -3.149216];
  const casos = [
    [[400000, 0], [-56.38916823408102, -3.1430048657362195]],
    [[0, 400000], [-59.99188100000001, 0.4480704236749217]],
    [[-400000, -400000], [-63.61183578268132, -6.7378867541547125]],
    [[123456, -234567], [-58.877174257923635, -5.25800204245898]],
  ];
  for (const [[x, y], [lon, lat]] of casos) {
    const [a, b] = aeqdInversa(c, x, y);
    assert.ok(Math.abs(a - lon) < 1e-9 && Math.abs(b - lat) < 1e-9, `${x},${y}: ${a},${b}`);
  }
  assert.deepEqual(aeqdInversa(c, 0, 0), c);
});

test('extentAeqd: o quadrado de 800 km em volta do radar, mais largo em longitude longe do equador', () => {
  const [x0, y0, x1, y1] = extentAeqd([-59.991881, -3.149216], MEIA_LARGURA_M);
  assert.ok(Math.abs(y1 - y0 - 7.19) < 0.02, `altura ${y1 - y0}`);
  assert.ok(x1 - x0 > 7.2 && x1 - x0 < 7.3, `largura ${x1 - x0}`);
  const [a0, , a1] = extentAeqd([-67.8961, -9.86163]);
  assert.ok(a1 - a0 > x1 - x0, 'Rio Branco (10° S) é mais largo em longitude que Manaus (3° S)');
});

test('escolherCentro: o da API quando válido; senão o da tabela', () => {
  const r = RADARES_REDEMET.find((x) => x.id === 'rdmn');
  assert.deepEqual(escolherCentro(r, [-59.99, -3.15]), { centro: [-59.99, -3.15], fonte: 'api', divergencia: escolherCentro(r, [-59.99, -3.15]).divergencia });
  assert.ok(escolherCentro(r, [-59.99, -3.15]).divergencia < 0.01);
  for (const x of [undefined, [NaN, 1], [-3.15, -59.99], [1, 2, 3]]) {
    assert.deepEqual(escolherCentro(r, x), { centro: r.centro, fonte: 'tabela', divergencia: null });
  }
});
