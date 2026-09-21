/**
 * Testes das funções puras do ciclo das embarcações.
 *
 * O que está aqui decide o que vai ao ar sobre pessoas e embarcações reais.
 * Um erro na classificação publica quem não deveria ser publicado, e um erro
 * na trilha inventa um percurso que não aconteceu — por isso estes dois
 * assuntos têm o teste mais detalhado.
 *
 *   node --test scripts/navios.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CADASTRO_TTL_MS,
  DEDUPE_M,
  ESCUTA_TTL_MS,
  TRILHA_MS,
  arredondar,
  boca,
  chaveCelula,
  classificar,
  ehClasseB,
  celulaParaCaixa,
  comprimentoDe,
  distanciaM,
  juntarAtribuicao,
  mesclarEscuta,
  mesclarEstatico,
  mesclarTrilha,
  podarCadastro,
} from './frota.mjs';

const H = 60 * 60 * 1000;

// ---------------------------------------------------------------------------
// Privacidade
// ---------------------------------------------------------------------------

test('classificar: vela e recreio nunca são publicados', () => {
  assert.equal(classificar(36, 12), 'omitir');
  assert.equal(classificar(37, 12), 'omitir');
  // Nem mesmo um iate grande: o critério é o tipo, não o tamanho.
  assert.equal(classificar(37, 80), 'omitir');
});

test('classificar: pesca pequena é anônima, pesca grande é comercial', () => {
  assert.equal(classificar(30, null), 'anonimo', 'sem comprimento conhecido, trata como pequena');
  assert.equal(classificar(30, 12), 'anonimo');
  assert.equal(classificar(30, 23.9), 'anonimo');
  assert.equal(classificar(30, 24), 'completo');
  assert.equal(classificar(30, 60), 'completo');
});

test('classificar: frota comercial vai completa', () => {
  for (const t of [31, 32, 52, 60, 69, 70, 79, 80, 89, 50, 55]) {
    assert.equal(classificar(t, null), 'completo', `tipo ${t} deveria ser completo`);
  }
});

test('classificar: tipo não declarado decide pela CLASSE do transponder', () => {
  // Classe A é porte obrigatório: é a frota comercial, inclusive os comboios
  // amazônicos que deixam o tipo em 0 (BERTOLINI VI, JOSE GUILHERME…).
  assert.equal(classificar(0, null, 'PositionReport'), 'completo');
  assert.equal(classificar(null, null, 'PositionReport'), 'completo');

  // Classe B é transponder voluntário de barco pequeno: anônimo.
  assert.equal(classificar(0, null, 'StandardClassBPositionReport'), 'anonimo');
  assert.equal(classificar(0, null, 'ExtendedClassBPositionReport'), 'anonimo');
  assert.equal(classificar(0, null, 'StaticDataReport'), 'anonimo');

  // …a menos que o porte diga que é embarcação grande.
  assert.equal(classificar(0, 90, 'StandardClassBPositionReport'), 'completo');

  // Sem saber a classe, o conservador é publicar (o caso medido é 99 % A).
  assert.equal(classificar(0, null, null), 'completo');
});

test('classificar: vela e recreio saem fora mesmo em Classe A', () => {
  assert.equal(classificar(37, 50, 'PositionReport'), 'omitir');
  assert.equal(classificar(36, null, 'PositionReport'), 'omitir');
});

test('ehClasseB reconhece as mensagens da Classe B', () => {
  assert.equal(ehClasseB('StandardClassBPositionReport'), true);
  assert.equal(ehClasseB('ExtendedClassBPositionReport'), true);
  assert.equal(ehClasseB('StaticDataReport'), true);
  assert.equal(ehClasseB('PositionReport'), false);
  assert.equal(ehClasseB('ShipStaticData'), false);
  assert.equal(ehClasseB(null), false);
});

// ---------------------------------------------------------------------------
// Dimensões
// ---------------------------------------------------------------------------

test('comprimento e boca a partir das dimensões do AIS', () => {
  assert.equal(comprimentoDe({ A: 100, B: 36, C: 10, D: 10 }), 136);
  assert.equal(boca({ A: 100, B: 36, C: 10, D: 10 }), 20);
  assert.equal(comprimentoDe({ A: 0, B: 0 }), null, 'zero é "não informado", não um navio de 0 m');
  assert.equal(comprimentoDe(null), null);
  assert.equal(boca({ C: 6, D: 5 }), 11);
});

// ---------------------------------------------------------------------------
// Trilha
// ---------------------------------------------------------------------------

test('mesclarTrilha: acrescenta a amostra nova em ordem', () => {
  const t0 = Date.parse('2026-09-20T12:00:00Z');
  let t = mesclarTrilha(null, { ms: t0, lat: -3.1, lon: -60.0, sog: 8 }, t0);
  assert.equal(t.length, 1);
  t = mesclarTrilha(t, { ms: t0 + H, lat: -3.2, lon: -60.1, sog: 9 }, t0 + H);
  assert.equal(t.length, 2);
  assert.equal(t[1][0], t0 + H);
  assert.equal(t[1][1], -3.2);
});

test('mesclarTrilha: um barco atracado não empilha 96 amostras iguais', () => {
  const t0 = Date.parse('2026-09-20T12:00:00Z');
  let t = mesclarTrilha(null, { ms: t0, lat: -1.45, lon: -48.5, sog: 0 }, t0);
  // O GPS de um navio atracado balança alguns metros em torno do mesmo ponto;
  // ele não deriva sempre para o mesmo lado (isso seria estar navegando).
  for (let i = 1; i <= 20; i++) {
    const ms = t0 + i * 15 * 60_000;
    const jitter = (i % 2 ? 1 : -1) * 0.0002; // ~22 m
    t = mesclarTrilha(t, { ms, lat: -1.45 + jitter, lon: -48.5, sog: 0 }, ms);
  }
  assert.equal(t.length, 1, 'continua sendo uma amostra só');
  assert.equal(t[0][0], t0 + 20 * 15 * 60_000, 'mas o instante avança');
});

test('mesclarTrilha: deriva lenta e contínua acaba virando amostra', () => {
  // O outro lado da moeda: um navio que se arrasta 10 m por ciclo ESTÁ se
  // movendo, e depois de ~1 km isso tem de aparecer na trilha.
  const t0 = Date.parse('2026-09-20T12:00:00Z');
  let t = mesclarTrilha(null, { ms: t0, lat: -1.45, lon: -48.5, sog: 0.2 }, t0);
  for (let i = 1; i <= 20; i++) {
    const ms = t0 + i * 15 * 60_000;
    t = mesclarTrilha(t, { ms, lat: -1.45 + i * 0.00009, lon: -48.5, sog: 0.2 }, ms);
  }
  assert.ok(t.length > 1, 'a deriva acumulada passou do limite e virou percurso');
  assert.ok(t.length < 20, 'mas sem gravar uma amostra por ciclo');
});

test('mesclarTrilha: deslocamento acima do limite vira amostra nova', () => {
  const t0 = Date.parse('2026-09-20T12:00:00Z');
  const t = mesclarTrilha(
    [[t0, -3.0, -60.0, 0]],
    { ms: t0 + H, lat: -3.01, lon: -60.0, sog: 5 }, // ~1,1 km
    t0 + H,
  );
  assert.equal(t.length, 2);
});

test('mesclarTrilha: poda o que passou de 24 h', () => {
  const agora = Date.parse('2026-09-21T12:00:00Z');
  const velha = agora - TRILHA_MS - H;
  const t = mesclarTrilha(
    [
      [velha, -3.0, -60.0, 1],
      [agora - 2 * H, -3.1, -60.1, 2],
    ],
    { ms: agora, lat: -3.5, lon: -60.5, sog: 3 },
    agora,
  );
  assert.equal(t.length, 2, 'a de 25 h saiu');
  assert.ok(t.every((p) => p[0] >= agora - TRILHA_MS));
});

test('mesclarTrilha: sem posição nova só poda (embarcação fora de alcance)', () => {
  const agora = Date.parse('2026-09-21T12:00:00Z');
  const t = mesclarTrilha([[agora - 2 * H, -3.0, -60.0, 1]], null, agora);
  assert.equal(t.length, 1);
});

test('mesclarTrilha: ignora amostra fora de ordem e lixo', () => {
  const t0 = Date.parse('2026-09-20T12:00:00Z');
  const base = [[t0, -3.0, -60.0, 1]];
  assert.equal(mesclarTrilha(base, { ms: t0 - H, lat: -4, lon: -61, sog: 1 }, t0).length, 1);
  assert.equal(mesclarTrilha(base, { ms: t0 + H, lat: NaN, lon: -61, sog: 1 }, t0 + H).length, 1);
  assert.deepEqual(mesclarTrilha([[NaN, 1, 2], 'lixo', [1]], null, t0), []);
});

test('distanciaM confere com uma distância conhecida', () => {
  // 0,01° de latitude ≈ 1,11 km em qualquer longitude.
  const d = distanciaM(-3.0, -60.0, -3.01, -60.0);
  assert.ok(Math.abs(d - 1110) < 20, `esperava ~1110 m, veio ${d.toFixed(0)}`);
  assert.ok(distanciaM(-3.0, -60.0, -3.0, -60.0) < 1);
  assert.ok(DEDUPE_M < 1110, 'o limite de deduplicação tem de ser menor que uma amostra de verdade');
});

test('arredondar guarda ~11 m de precisão', () => {
  assert.equal(arredondar(-3.147815), -3.1478);
  assert.equal(arredondar(-59.928416666), -59.9284);
});

// ---------------------------------------------------------------------------
// Cadastro
// ---------------------------------------------------------------------------

test('mesclarEstatico: campo vazio não apaga o que já sabíamos', () => {
  const t0 = 1_000_000;
  const antes = mesclarEstatico(null, { n: 'EPSILON K', dst: 'BRVDC', i: 9045156 }, t0);
  assert.equal(antes.n, 'EPSILON K');
  assert.equal(antes.d, t0);

  // Ciclo seguinte: o navio transmitiu só o nome.
  const depois = mesclarEstatico(antes, { n: 'EPSILON K', dst: null, i: null }, t0 + H);
  assert.equal(depois.dst, 'BRVDC', 'o destino sobreviveu');
  assert.equal(depois.i, 9045156);
  assert.equal(depois.d, t0, 'nada mudou, então a data da declaração não se move');
});

test('mesclarEstatico: campo que muda atualiza a data da declaração', () => {
  const t0 = 1_000_000;
  const antes = mesclarEstatico(null, { dst: 'BRVDC' }, t0);
  const depois = mesclarEstatico(antes, { dst: 'BRMAO' }, t0 + 5 * H);
  assert.equal(depois.dst, 'BRMAO');
  assert.equal(depois.d, t0 + 5 * H);
});

test('podarCadastro: tira quem não é ouvido há 30 dias', () => {
  const agora = Date.parse('2026-09-21T00:00:00Z');
  const c = podarCadastro(
    {
      111: { n: 'RECENTE', v: agora - H },
      222: { n: 'VELHO', v: agora - CADASTRO_TTL_MS - H },
      333: { n: 'SEM DATA' },
    },
    agora,
  );
  assert.deepEqual(Object.keys(c), ['111']);
});

// ---------------------------------------------------------------------------
// Escuta
// ---------------------------------------------------------------------------

test('chaveCelula agrupa posições vizinhas na mesma célula', () => {
  assert.equal(chaveCelula(-3.10, -60.10), chaveCelula(-3.20, -60.20));
  assert.notEqual(chaveCelula(-3.10, -60.10), chaveCelula(-3.30, -60.10));
});

test('celulaParaCaixa devolve a caixa que contém a posição', () => {
  const [minLat, minLon, maxLat, maxLon] = celulaParaCaixa(chaveCelula(-3.1, -60.1));
  assert.ok(minLat <= -3.1 && -3.1 < maxLat, `${minLat}..${maxLat} deveria conter -3.1`);
  assert.ok(minLon <= -60.1 && -60.1 < maxLon, `${minLon}..${maxLon} deveria conter -60.1`);
});

test('mesclarEscuta acumula e esquece depois de 7 dias', () => {
  const agora = Date.parse('2026-09-21T00:00:00Z');
  const nova = chaveCelula(-3.1, -60.1);
  // Células deliberadamente distintas da que a recepção de agora vai marcar.
  const velha = chaveCelula(-20.0, -40.0);
  const recente = chaveCelula(-10.0, -50.0);
  assert.ok(velha !== nova && recente !== nova, 'o teste precisa de células distintas');

  const c = mesclarEscuta({ [velha]: agora - ESCUTA_TTL_MS - H, [recente]: agora - 2 * H }, [{ lat: -3.1, lon: -60.1 }], agora);
  assert.ok(!(velha in c), 'a célula de 8 dias foi esquecida');
  assert.equal(c[recente], agora - 2 * H, 'a de 2 h continua');
  assert.equal(c[nova], agora, 'a recepção de agora entrou');
});

// ---------------------------------------------------------------------------
// Atribuição
// ---------------------------------------------------------------------------

test('juntarAtribuicao mantém um crédito por fonte, sem concatenar', () => {
  const a = juntarAtribuicao(
    { aishub: 'Open Waters AIS (…). AISHub (…)' },
    { aisstream: 'Open Waters AIS (…). aisstream.io' },
    null,
    { aishub: 'Open Waters AIS (…). AISHub (…)' },
  );
  assert.deepEqual(Object.keys(a).sort(), ['aishub', 'aisstream']);
  // O texto de uma fonte nunca vaza para a outra: licença não se funde.
  assert.ok(!a.aishub.includes('aisstream'));
});
