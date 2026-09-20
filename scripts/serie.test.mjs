import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PASSO_MS,
  SEM_DADO,
  alinhar,
  arredondar,
  contarMedidas,
  deslocarEGravar,
  gradeDeLeituras,
  indiceDaFatia,
  inicioDaGrade,
  linhaVazia,
} from './serie.mjs';

const T = Date.UTC(2026, 8, 20, 16, 0, 0);

test('alinhar desce para a fatia de 10 min', () => {
  assert.equal(alinhar(T), T);
  assert.equal(alinhar(T + 9 * 60_000), T);
  assert.equal(alinhar(T + 10 * 60_000), T + PASSO_MS);
  assert.equal(alinhar(T - 1), T - PASSO_MS);
});

test('a grade termina na fatia de `fim`', () => {
  const t0 = inicioDaGrade(T, 6);
  assert.equal(t0, T - 5 * PASSO_MS);
  assert.equal(indiceDaFatia(t0, T, 6), 5);
  assert.equal(indiceDaFatia(t0, t0, 6), 0);
});

test('fatias fora da janela são descartadas', () => {
  const t0 = inicioDaGrade(T, 6);
  assert.equal(indiceDaFatia(t0, t0 - PASSO_MS, 6), -1);
  assert.equal(indiceDaFatia(t0, T + PASSO_MS, 6), -1);
});

test('gradeDeLeituras põe cada leitura na fatia do próprio carimbo', () => {
  const { t0, v } = gradeDeLeituras(
    ['A', 'B'],
    [
      { cod: 'A', ms: T, valor: 2.5 },
      { cod: 'B', ms: T - 2 * PASSO_MS, valor: 1 },
    ],
    T,
    4,
  );
  assert.equal(t0, T - 3 * PASSO_MS);
  assert.deepEqual(v[0], [SEM_DADO, SEM_DADO, SEM_DADO, 2.5]);
  assert.deepEqual(v[1], [SEM_DADO, 1, SEM_DADO, SEM_DADO]);
});

test('leituras na mesma fatia somam — é chuva acumulada, não instantânea', () => {
  const { v } = gradeDeLeituras(
    ['A'],
    [
      { cod: 'A', ms: T, valor: 1.2 },
      { cod: 'A', ms: T + 60_000, valor: 0.8 },
    ],
    T,
    2,
  );
  assert.equal(v[0][1], 2);
});

test('0 mm medido não vira "sem dado"', () => {
  const { v } = gradeDeLeituras(['A'], [{ cod: 'A', ms: T, valor: 0 }], T, 2);
  assert.equal(v[0][1], 0);
  assert.equal(v[0][0], SEM_DADO);
});

test('estação desconhecida e fatia fora da janela são ignoradas', () => {
  const { v } = gradeDeLeituras(
    ['A'],
    [
      { cod: 'Z', ms: T, valor: 9 },
      { cod: 'A', ms: T - 99 * PASSO_MS, valor: 9 },
    ],
    T,
    3,
  );
  assert.deepEqual(v[0], linhaVazia(3));
});

// ---------------------------------------------------------------------------
// Deslocamento da janela (caminho sem token)
// ---------------------------------------------------------------------------

test('a primeira execução grava só a última fatia', () => {
  const { t0, v } = deslocarEGravar(null, ['A'], new Map([['A', 5]]), T, 4);
  assert.equal(t0, T - 3 * PASSO_MS);
  assert.deepEqual(v[0], [SEM_DADO, SEM_DADO, SEM_DADO, 5]);
});

test('o ciclo seguinte desloca o histórico e acrescenta uma coluna', () => {
  const a = deslocarEGravar(null, ['A'], new Map([['A', 5]]), T, 4);
  const b = deslocarEGravar(
    { t0: a.t0, codigos: ['A'], v: a.v },
    ['A'],
    new Map([['A', 7]]),
    T + PASSO_MS,
    4,
  );
  // O 5 andou uma casa para a esquerda; o 7 entrou no fim.
  assert.deepEqual(b.v[0], [SEM_DADO, SEM_DADO, 5, 7]);
});

test('o valor mais antigo cai fora quando a janela anda', () => {
  let g = { t0: inicioDaGrade(T, 3), codigos: ['A'], v: [[1, 2, 3]] };
  g = deslocarEGravar(g, ['A'], new Map([['A', 4]]), T + PASSO_MS, 3);
  assert.deepEqual(g.v[0], [2, 3, 4]);
});

test('um salto maior que a grade limpa tudo menos a fatia nova', () => {
  const g = { t0: inicioDaGrade(T, 3), codigos: ['A'], v: [[1, 2, 3]] };
  const d = deslocarEGravar(g, ['A'], new Map([['A', 9]]), T + 50 * PASSO_MS, 3);
  assert.deepEqual(d.v[0], [SEM_DADO, SEM_DADO, 9]);
});

test('estação nova entra sem histórico; a que sumiu não desloca as outras', () => {
  const g = { t0: inicioDaGrade(T, 3), codigos: ['A', 'B'], v: [[1, 2, 3], [4, 5, 6]] };
  const d = deslocarEGravar(g, ['B', 'C'], new Map([['B', 7], ['C', 8]]), T + PASSO_MS, 3);
  // B mantém o próprio histórico mesmo tendo mudado de linha.
  assert.deepEqual(d.v[0], [5, 6, 7]);
  assert.deepEqual(d.v[1], [SEM_DADO, SEM_DADO, 8]);
});

test('estação sem valor no ciclo atual conserva o histórico e fica sem dado no fim', () => {
  const g = { t0: inicioDaGrade(T, 3), codigos: ['A'], v: [[1, 2, 3]] };
  const d = deslocarEGravar(g, ['A'], new Map(), T + PASSO_MS, 3);
  assert.deepEqual(d.v[0], [2, 3, SEM_DADO]);
});

test('dois ciclos na mesma fatia sobrescrevem, sem deslocar', () => {
  const a = deslocarEGravar(null, ['A'], new Map([['A', 5]]), T, 4);
  const b = deslocarEGravar({ t0: a.t0, codigos: ['A'], v: a.v }, ['A'], new Map([['A', 6]]), T + 60_000, 4);
  assert.equal(b.t0, a.t0);
  assert.deepEqual(b.v[0], [SEM_DADO, SEM_DADO, SEM_DADO, 6]);
});

// ---------------------------------------------------------------------------

test('arredondar preserva o marcador de ausência', () => {
  assert.deepEqual(arredondar([[SEM_DADO, 1.2345, 0]]), [[SEM_DADO, 1.2, 0]]);
});

test('contarMedidas separa medição de chuva', () => {
  assert.deepEqual(contarMedidas([[SEM_DADO, 0, 2.5], [1, SEM_DADO, 0]]), {
    com: 4,
    chuva: 2,
    total: 6,
  });
});
