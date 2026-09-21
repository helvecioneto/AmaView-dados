/**
 * Testes das funções puras do ciclo dos rios.
 *
 *   node --test scripts/rios.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CADENCIA_MS,
  COTA_MAX,
  COTA_MIN,
  JANELA_DOY,
  MIN_ANOS_REFERENCIA,
  PASSO_MS,
  SLOTS,
  anomaliaCm,
  aprovada,
  classePorPercentil,
  cotaPlausivel,
  diaDoAno,
  distanciaDoy,
  extremosDe,
  fimDaGrade,
  gradeHoraria,
  percentil,
  posicaoPercentil,
  referenciaDoDia,
  tendenciaDe,
  ultimoDa,
  variacao,
  venceu,
} from './hidro.mjs';
import { instanteDe } from './ana.mjs';

const H = PASSO_MS;

// ---------------------------------------------------------------------------
// Leitura do XML da ANA
// ---------------------------------------------------------------------------

test('instanteDe: sem fuso, a ANA publica em Brasília (UTC−3)', () => {
  // Medido contra o relógio em 21/09/2026: ler como UTC dá 4 h de atraso e
  // ler como hora local da estação dá dado do futuro nas de UTC−4.
  assert.equal(instanteDe('2026-09-21 01:45:00'), Date.parse('2026-09-21T04:45:00Z'));
  assert.equal(instanteDe('2026-09-21T01:45:00'), Date.parse('2026-09-21T04:45:00Z'));
});

test('instanteDe: quando o serviço manda o deslocamento, ele é respeitado', () => {
  assert.equal(instanteDe('2026-09-20T22:45:00-04:00'), Date.parse('2026-09-21T02:45:00Z'));
  assert.equal(instanteDe('2026-09-20T22:45:00Z'), Date.parse('2026-09-20T22:45:00Z'));
});

test('instanteDe: lixo devolve null', () => {
  assert.equal(instanteDe(''), null);
  assert.equal(instanteDe('nada disso'), null);
  assert.equal(instanteDe(null), null);
  assert.equal(instanteDe(42), null);
});

// ---------------------------------------------------------------------------
// Plausibilidade e qualidade
// ---------------------------------------------------------------------------

test('cotaPlausivel: 0 cm é leitura válida, não ausência', () => {
  assert.equal(cotaPlausivel(0), true);
  assert.equal(cotaPlausivel(-16), true, 'régua com zero acima da mínima dá cota negativa');
  assert.equal(cotaPlausivel(COTA_MIN), true);
  assert.equal(cotaPlausivel(COTA_MAX), true);
  assert.equal(cotaPlausivel(COTA_MIN - 1), false);
  assert.equal(cotaPlausivel(COTA_MAX + 1), false);
  assert.equal(cotaPlausivel(null), false);
  assert.equal(cotaPlausivel(NaN), false);
  assert.equal(cotaPlausivel('1200'), false);
});

test('aprovada reconhece o carimbo de qualidade da ANA', () => {
  assert.equal(aprovada('Dado aprovado'), true);
  assert.equal(aprovada('dado aprovada'), true);
  assert.equal(aprovada(''), false);
  assert.equal(aprovada(null), false);
  assert.equal(aprovada('Dado suspeito'), false);
});

// ---------------------------------------------------------------------------
// Vencimento do ciclo
// ---------------------------------------------------------------------------

test('venceu: falha ABERTO — na dúvida, roda', () => {
  const agora = Date.parse('2026-09-21T12:00:00Z');
  assert.equal(venceu(null, agora), true, 'estreia');
  assert.equal(venceu(undefined, agora), true);
  assert.equal(venceu('não é data', agora), true, 'carimbo ilegível');
  assert.equal(venceu('', agora), true);
  // Carimbo no futuro é relógio errado, não dado fresco.
  assert.equal(venceu(new Date(agora + 60 * 60_000).toISOString(), agora), true);
});

test('venceu: respeita a cadência quando o carimbo é confiável', () => {
  const agora = Date.parse('2026-09-21T12:00:00Z');
  assert.equal(venceu(new Date(agora - 10 * 60_000).toISOString(), agora), false, '10 min: ainda vale');
  assert.equal(venceu(new Date(agora - 54 * 60_000).toISOString(), agora), false);
  assert.equal(venceu(new Date(agora - CADENCIA_MS).toISOString(), agora), true);
  assert.equal(venceu(new Date(agora - 3 * 60 * 60_000).toISOString(), agora), true);
});

// ---------------------------------------------------------------------------
// Grade horária
// ---------------------------------------------------------------------------

/** Leituras de 15 em 15 min terminando em `fim`. */
function serie15min(fim, horas, cm = (i) => 1000 + i) {
  const out = [];
  const n = horas * 4;
  for (let i = 0; i < n; i++) {
    out.push({ ms: fim - (n - 1 - i) * 15 * 60_000, nivel: cm(i), cqNivel: 'Dado aprovado' });
  }
  return out;
}

test('gradeHoraria devolve sempre 48 posições', () => {
  const fim = fimDaGrade(Date.parse('2026-09-21T12:34:00Z'));
  const g = gradeHoraria(serie15min(fim, 48), fim);
  assert.equal(g.v.length, SLOTS);
  assert.equal(g.slots, SLOTS);
  assert.equal(g.passoMin, 60);
  assert.equal(g.t0, fim - (SLOTS - 1) * H);
});

test('gradeHoraria escolhe a leitura mais próxima de cada hora', () => {
  const fim = Date.parse('2026-09-21T12:00:00Z');
  const g = gradeHoraria(
    [
      { ms: fim - 20 * 60_000, nivel: 100, cqNivel: 'Dado aprovado' }, // 20 min antes
      { ms: fim - 5 * 60_000, nivel: 200, cqNivel: 'Dado aprovado' }, // 5 min antes — mais perto
      { ms: fim - 40 * 60_000, nivel: 300, cqNivel: 'Dado aprovado' }, // fora da meia hora
    ],
    fim,
  );
  assert.equal(g.v[SLOTS - 1], 200);
});

test('gradeHoraria ignora leitura implausível e hora sem dado fica null', () => {
  const fim = Date.parse('2026-09-21T12:00:00Z');
  const g = gradeHoraria([{ ms: fim, nivel: 99999, cqNivel: 'Dado aprovado' }], fim);
  assert.equal(g.v[SLOTS - 1], null);
  assert.equal(g.v.filter((x) => x !== null).length, 0);
});

test('gradeHoraria marca a leitura não aprovada sem descartá-la', () => {
  const fim = Date.parse('2026-09-21T12:00:00Z');
  const g = gradeHoraria([{ ms: fim, nivel: 1200, cqNivel: 'Dado suspeito' }], fim);
  assert.equal(g.v[SLOTS - 1], 1200, 'entra na série: buraco é pior para quem olha');
  assert.equal(g.bruta[SLOTS - 1], true, 'mas fica marcada');
});

test('gradeHoraria tolera entrada vazia e lixo', () => {
  const fim = Date.parse('2026-09-21T12:00:00Z');
  for (const entrada of [[], null, undefined, [null, {}, { ms: NaN, nivel: 1 }]]) {
    const g = gradeHoraria(entrada, fim);
    assert.equal(g.v.length, SLOTS);
    assert.equal(g.v.every((x) => x === null), true);
  }
});

// ---------------------------------------------------------------------------
// Tendência
// ---------------------------------------------------------------------------

test('variacao mede a diferença em 24 h', () => {
  const fim = Date.parse('2026-09-21T12:00:00Z');
  // Sobe 1 cm por hora ao longo de 48 h.
  const g = gradeHoraria(serie15min(fim, 48, (i) => 1000 + Math.floor(i / 4)), fim);
  assert.equal(variacao(g.v, 24), 24);
  assert.equal(variacao(g.v, 47), 47);
});

test('variacao devolve null quando falta a ponta antiga', () => {
  const fim = Date.parse('2026-09-21T12:00:00Z');
  const g = gradeHoraria(serie15min(fim, 3), fim);
  assert.equal(variacao(g.v, 24), null, 'inventar estabilidade seria pior que admitir que não sei');
});

test('tendenciaDe: limiar de 5 cm em 24 h', () => {
  assert.equal(tendenciaDe(20), 'subindo');
  assert.equal(tendenciaDe(5), 'subindo');
  assert.equal(tendenciaDe(4), 'estavel');
  assert.equal(tendenciaDe(0), 'estavel');
  assert.equal(tendenciaDe(-4), 'estavel');
  assert.equal(tendenciaDe(-5), 'descendo');
  assert.equal(tendenciaDe(null), 'desconhecida');
  assert.equal(tendenciaDe(NaN), 'desconhecida');
});

test('ultimoDa acha o último valor medido, e 0 conta', () => {
  assert.deepEqual(ultimoDa([1, 2, null, null]), { i: 1, cm: 2 });
  assert.deepEqual(ultimoDa([1, 0]), { i: 1, cm: 0 }, '0 cm é medição');
  assert.equal(ultimoDa([null, null]), null);
});

// ---------------------------------------------------------------------------
// Referência por dia do ano
// ---------------------------------------------------------------------------

test('diaDoAno e distância circular', () => {
  assert.equal(diaDoAno(Date.parse('2026-01-01T00:00:00Z')), 1);
  assert.equal(diaDoAno(Date.parse('2026-12-31T00:00:00Z')), 365);
  // 31 de dezembro e 1º de janeiro são vizinhos, não opostos.
  assert.equal(distanciaDoy(365, 1), 2);
  assert.equal(distanciaDoy(1, 365), 2);
  assert.equal(distanciaDoy(100, 110), 10);
});

/** Histórico sintético: `anos` anos, cota subindo 1 cm por dia do ano. */
function historico(anos, offsetPorAno = 0) {
  const dias = {};
  for (let a = 0; a < anos; a++) {
    const ano = 2014 + a;
    for (let d = 1; d <= 365; d++) {
      const ms = Date.UTC(ano, 0, d);
      const iso = new Date(ms).toISOString().slice(0, 10);
      dias[iso] = 1000 + d + a * offsetPorAno;
    }
  }
  return dias;
}

test('referenciaDoDia exige anos suficientes — ausência não é normalidade', () => {
  const doy = 200;
  assert.equal(referenciaDoDia(historico(MIN_ANOS_REFERENCIA - 1), doy), null);
  assert.notEqual(referenciaDoDia(historico(MIN_ANOS_REFERENCIA), doy), null);
  assert.equal(referenciaDoDia(null, doy), null);
  assert.equal(referenciaDoDia({}, doy), null);
});

test('referenciaDoDia usa a janela de ±15 dias em torno do dia', () => {
  const ref = referenciaDoDia(historico(10), 200);
  assert.equal(ref.anos, 10);
  // 10 anos × 31 dias na janela.
  assert.equal(ref.n, 10 * (2 * JANELA_DOY + 1));
  // Cota = 1000 + dia, então a mediana da janela é o próprio dia.
  assert.equal(ref.p50, 1200);
  assert.equal(ref.min, 1000 + 200 - JANELA_DOY);
  assert.equal(ref.max, 1000 + 200 + JANELA_DOY);
});

test('a referência do dia NÃO é a do mês — é esse o ponto da camada', () => {
  // Rio que sobe 1 cm por dia: no dia 20 de um mês de 30, a mediana MENSAL
  // fica ~10 cm abaixo da mediana do DIA. Num rio que sobe 20 cm/dia isso
  // viraria dois metros de "anomalia" que é só calendário.
  const dias = historico(10);
  const doy20 = 200;
  const ref = referenciaDoDia(dias, doy20);
  assert.equal(ref.p50, 1200, 'a mediana do dia acompanha a sazonalidade');

  const refOutroDia = referenciaDoDia(dias, doy20 + 10);
  assert.equal(refOutroDia.p50, 1210, 'dez dias depois, a referência andou dez centímetros');
});

test('posicaoPercentil é adimensional e ordena corretamente', () => {
  const ref = referenciaDoDia(historico(10), 200);
  assert.equal(posicaoPercentil(ref.min - 100, ref), 0, 'abaixo de tudo');
  assert.equal(posicaoPercentil(ref.max + 100, ref), 100, 'acima de tudo');
  const meio = posicaoPercentil(ref.p50, ref);
  assert.ok(meio > 35 && meio < 65, `mediana deveria cair no meio, veio ${meio}`);
  assert.equal(posicaoPercentil(1200, null), null);
  assert.equal(posicaoPercentil(null, ref), null);
});

test('classePorPercentil corta nos quartis e nas pontas', () => {
  assert.equal(classePorPercentil(0), 'muitoAbaixo');
  assert.equal(classePorPercentil(10), 'muitoAbaixo');
  assert.equal(classePorPercentil(11), 'abaixo');
  assert.equal(classePorPercentil(24), 'abaixo');
  assert.equal(classePorPercentil(25), 'normal');
  assert.equal(classePorPercentil(50), 'normal');
  assert.equal(classePorPercentil(75), 'normal');
  assert.equal(classePorPercentil(76), 'acima');
  assert.equal(classePorPercentil(89), 'acima');
  assert.equal(classePorPercentil(90), 'muitoAcima');
  assert.equal(classePorPercentil(100), 'muitoAcima');
  assert.equal(classePorPercentil(null), null, 'sem referência não é "normal"');
});

test('anomaliaCm é a diferença para a mediana do dia', () => {
  const ref = referenciaDoDia(historico(10), 200);
  assert.equal(anomaliaCm(1200, ref), 0);
  assert.equal(anomaliaCm(1050, ref), -150);
  assert.equal(anomaliaCm(1200, null), null);
});

test('extremosDe reporta o período medido, e não "recorde histórico"', () => {
  const dias = { '2014-06-01': 3000, '2024-10-04': 1211, '2020-01-01': 2000 };
  const e = extremosDe(dias);
  assert.equal(e.max.cm, 3000);
  assert.equal(e.max.data, '2014-06-01');
  assert.equal(e.min.cm, 1211);
  assert.equal(e.min.data, '2024-10-04');
  assert.equal(e.desde, 2014, 'a interface precisa poder dizer "desde 2014, nesta estação"');
  assert.equal(e.anos, 3);
  assert.equal(extremosDe(null), null);
  assert.equal(extremosDe({}), null);
});

test('percentil interpola e aguenta lista de um elemento', () => {
  assert.equal(percentil([10], 0.5), 10);
  assert.equal(percentil([0, 10], 0.5), 5);
  assert.equal(percentil([0, 10, 20], 0.5), 10);
  assert.equal(percentil([], 0.5), null);
});
