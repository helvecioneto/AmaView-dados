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

// ---------------------------------------------------------------------------
// Preenchimento inicial a partir das séries horárias
// ---------------------------------------------------------------------------

import { acum24hPorFatia, fatiasComDado } from './serie.mjs';

const H = 3_600_000;

test('acum24hPorFatia soma as horas da janela de 24 h', () => {
  const t0 = T;
  // Uma hora de 5 mm começando 2 h antes de t0 conta em todas as fatias.
  const linha = acum24hPorFatia([{ ms: t0 - 2 * H, mm: 5 }], t0, 3);
  assert.deepEqual(linha, [5, 5, 5]);
});

test('horas fora da janela de 24 h não contam', () => {
  const t0 = T;
  // 25 h antes do fim da 1ª fatia: já saiu da janela.
  assert.deepEqual(acum24hPorFatia([{ ms: t0 - 25 * H, mm: 9 }], t0, 1), [SEM_DADO]);
});

test('a hora mais antiga da janela conta — ela cabe inteira nas 24 h', () => {
  // Hora que COMEÇA 24 h antes de t0: a chuva dela cai em [t0-24h, t0-23h),
  // inteiramente dentro da janela que termina em t0. Comparar pelo início da
  // hora a excluía; a comparação é pelo FIM.
  const linha = acum24hPorFatia([{ ms: T - 24 * H, mm: 4 }], T, 2);
  assert.equal(linha[0], 4);
});

test('a hora que só COMEÇA no fim da fatia não conta — é chuva do futuro', () => {
  // Hora [t0, t0+1h): nada dela caiu até t0. Comparar pelo início a incluía,
  // e chuva de agora aparecia no acumulado de fatias de horas atrás.
  const linha = acum24hPorFatia([{ ms: T, mm: 9 }], T, 1);
  assert.equal(linha[0], SEM_DADO);
});

test('a hora sai da janela quando a fatia avança 24 h além dela', () => {
  // Janela DESLIZANTE. A hora termina em T−23h, e a janela da fatia s começa
  // em T + s·10min − 24h; ela só deixa de alcançá-la quando esse início passa
  // de T−23h, ou seja, a partir da 7ª fatia (60 min).
  const linha = acum24hPorFatia([{ ms: T - 24 * H, mm: 4 }], T, 7);
  assert.equal(linha[0], 4);
  assert.equal(linha[5], 4);
  assert.equal(linha[6], SEM_DADO);
});

test('várias horas somam', () => {
  const t0 = T;
  const linha = acum24hPorFatia(
    [
      { ms: t0 - 3 * H, mm: 1 },
      { ms: t0 - 2 * H, mm: 2 },
      { ms: t0 - H, mm: 3 },
    ],
    t0,
    1,
  );
  assert.deepEqual(linha, [6]);
});

test('série vazia devolve a linha toda sem dado', () => {
  assert.deepEqual(acum24hPorFatia([], T, 3), [SEM_DADO, SEM_DADO, SEM_DADO]);
  assert.deepEqual(acum24hPorFatia(null, T, 2), [SEM_DADO, SEM_DADO]);
});

test('horas medidas com 0 mm dão acumulado 0, não ausência', () => {
  assert.deepEqual(acum24hPorFatia([{ ms: T - H, mm: 0 }], T, 1), [0]);
});

test('a ordem de entrada não importa', () => {
  const a = acum24hPorFatia([{ ms: T - H, mm: 3 }, { ms: T - 5 * H, mm: 2 }], T, 1);
  const b = acum24hPorFatia([{ ms: T - 5 * H, mm: 2 }, { ms: T - H, mm: 3 }], T, 1);
  assert.deepEqual(a, b);
});

test('fatiasComDado mede história, não quantidade de estações', () => {
  // Grade recém-criada: muitas estações, mas só a última fatia ocupada.
  const nova = Array.from({ length: 50 }, () => [SEM_DADO, SEM_DADO, 1]);
  assert.equal(fatiasComDado(nova, 3), 1);
  // Rede degradada: uma estação só, mas com história completa.
  const degradada = [[1, 2, 3], ...Array.from({ length: 49 }, () => [SEM_DADO, SEM_DADO, SEM_DADO])];
  assert.equal(fatiasComDado(degradada, 3), 3);
});

// ---------------------------------------------------------------------------
// Ausência nunca vira zero (achados da auditoria)
// ---------------------------------------------------------------------------

import { herdarVazias } from './serie.mjs';

test('leitura sem valor deixa a fatia SEM_DADO, não 0', () => {
  // Era o pior bug possível: silêncio do sensor publicado como "não choveu".
  const { v } = gradeDeLeituras(['A'], [{ cod: 'A', ms: T, valor: null }], T, 2);
  assert.deepEqual(v[0], [SEM_DADO, SEM_DADO]);
});

test('leitura sem valor não apaga uma medição válida da mesma fatia', () => {
  const { v } = gradeDeLeituras(
    ['A'],
    [
      { cod: 'A', ms: T, valor: 3 },
      { cod: 'A', ms: T, valor: null },
    ],
    T,
    2,
  );
  assert.equal(v[0][1], 3);
});

test('arredondar nunca CRIA o marcador de ausência', () => {
  // -0.96 arredondaria para -1, que é SEM_DADO: medição viraria "sem dado".
  assert.deepEqual(arredondar([[-0.96, -0.04, 2.55]]), [[0, 0, 2.6]]);
});

test('arredondar preserva SEM_DADO como está', () => {
  assert.deepEqual(arredondar([[SEM_DADO, 1.24]]), [[SEM_DADO, 1.2]]);
});

test('herdarVazias reaproveita a linha antiga quando o ciclo não trouxe nada', () => {
  const anterior = { t0: inicioDaGrade(T, 3), codigos: ['A'], v: [[1, 2, 3]] };
  const atual = { t0: inicioDaGrade(T + PASSO_MS, 3), v: [linhaVazia(3)] };
  const { herdadas, v } = herdarVazias(atual, ['A'], anterior, 3);
  assert.equal(herdadas, 1);
  assert.deepEqual(v[0], [2, 3, SEM_DADO]);
});

test('herdarVazias NÃO sobrescreve linha com dado novo', () => {
  const anterior = { t0: inicioDaGrade(T, 3), codigos: ['A'], v: [[1, 2, 3]] };
  const atual = { t0: inicioDaGrade(T, 3), v: [[SEM_DADO, SEM_DADO, 9]] };
  const { herdadas, v } = herdarVazias(atual, ['A'], anterior, 3);
  assert.equal(herdadas, 0);
  assert.deepEqual(v[0], [SEM_DADO, SEM_DADO, 9]);
});

test('herdarVazias ignora estação sem histórico e grade anterior inválida', () => {
  const atual = { t0: T, v: [linhaVazia(3)] };
  assert.equal(herdarVazias(atual, ['X'], { t0: T, codigos: ['A'], v: [[1, 2, 3]] }, 3).herdadas, 0);
  assert.equal(herdarVazias(atual, ['A'], { t0: NaN, codigos: ['A'], v: [[1, 2, 3]] }, 3).herdadas, 0);
  assert.equal(herdarVazias(atual, ['A'], null, 3).herdadas, 0);
});
