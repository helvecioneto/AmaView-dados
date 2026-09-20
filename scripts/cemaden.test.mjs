import test from 'node:test';
import assert from 'node:assert/strict';
import { lerDadosRede, minutosAteExpirar } from './cemaden.mjs';

// Resposta real de /pcds/dados_rede, copiada dos logs do workflow.
const CSV = `OBS.: Rede com horario UTC!
cod.estacao;nome;municipio;uf;latitude;longitude;datahora;sensor;valor;qualificacao;offset
120005401A;G2-120005401A;ASSIS BRASIL;AC;-10.9401;-69.567;2026-09-19 17:10:00;chuva;0.2;1;0
120005401A;G2-120005401A;ASSIS BRASIL;AC;-10.9401;-69.567;2026-09-19 17:20:00;chuva;0;1;0
130190203A;Colônia;ITACOATIARA;AM;-3.14061;-58.45201;2026-09-19 18:00:00;chuva;1.4;1;0`;

test('lê o CSV do dados_rede, pulando o preâmbulo', () => {
  const r = lerDadosRede(CSV, 'AC');
  assert.equal(r.length, 3);
  assert.equal(r[0].cod, '120005401A');
  assert.equal(r[0].valor, 0.2);
  assert.equal(r[0].ms, Date.UTC(2026, 8, 19, 17, 10, 0));
});

test('datahora sem fuso é tratada como UTC', () => {
  // O próprio preâmbulo do CEMADEN declara "Rede com horario UTC".
  const r = lerDadosRede(CSV);
  assert.equal(new Date(r[2].ms).toISOString(), '2026-09-19T18:00:00.000Z');
});

test('0 mm medido é preservado, não descartado', () => {
  const r = lerDadosRede(CSV);
  assert.equal(r[1].valor, 0);
});

test('sensores que não são chuva são ignorados', () => {
  const csv = CSV + '\n130190203A;Colônia;ITACOATIARA;AM;-3.1;-58.4;2026-09-19 18:10:00;nivel;250;1;0';
  assert.equal(lerDadosRede(csv).length, 3);
});

test('vírgula decimal é aceita', () => {
  const csv = `cod.estacao;datahora;sensor;valor\nA;2026-09-19 17:00:00;chuva;2,5`;
  assert.equal(lerDadosRede(csv)[0].valor, 2.5);
});

test('a ordem das colunas não importa', () => {
  const csv = `valor;sensor;datahora;cod.estacao\n3.1;chuva;2026-09-19 17:00:00;XYZ`;
  const r = lerDadosRede(csv);
  assert.equal(r[0].cod, 'XYZ');
  assert.equal(r[0].valor, 3.1);
});

test('alerta em JSON vira erro com a mensagem do CEMADEN', () => {
  assert.throws(
    () => lerDadosRede('[{"Alerta":"O parâmetro token não pode ser vazio!"}]', 'AC'),
    /token não pode ser vazio/,
  );
});

test('resposta JSON de verdade continua funcionando', () => {
  const j = JSON.stringify([{ codestacao: 'A', datahora: '2026-09-19 17:00:00', valor: 1.5 }]);
  assert.equal(lerDadosRede(j)[0].valor, 1.5);
});

test('resposta vazia não é erro', () => {
  assert.deepEqual(lerDadosRede(''), []);
  assert.deepEqual(lerDadosRede('   \n  '), []);
});

test('texto sem cabeçalho nem JSON vira erro legível', () => {
  assert.throws(() => lerDadosRede('Servico temporariamente indisponivel', 'AM'), /inesperada.*AM/s);
});

test('linhas truncadas não derrubam o parser', () => {
  const csv = `cod.estacao;datahora;sensor;valor\nA;2026-09-19 17:00:00;chuva;1\nLIXO\n;;;\nB;2026-09-19 17:00:00;chuva;2`;
  assert.equal(lerDadosRede(csv).length, 2);
});

test('minutosAteExpirar lê o exp do JWT', () => {
  const payload = Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 600 })).toString('base64url');
  const min = minutosAteExpirar(`eyJhbGciOiJIUzI1NiJ9.${payload}.x`);
  assert.ok(min > 9 && min <= 10, `esperava ~10 min, veio ${min}`);
});

test('token ilegível não quebra', () => {
  assert.equal(minutosAteExpirar('nao-e-jwt'), null);
});

test('exp no passado é lido, mas é só informação', () => {
  // A PED já emitiu token com exp vencido que funcionou: o número existe
  // para o relatório, e nunca para bloquear a chamada.
  const p = Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) - 300 })).toString('base64url');
  const min = minutosAteExpirar(`eyJhbGciOiJIUzI1NiJ9.${p}.x`);
  assert.ok(min < 0, `esperava negativo, veio ${min}`);
});

test('token sem exp não vira zero nem erro', () => {
  const p = Buffer.from(JSON.stringify({ iss: 'br.gov.cemaden' })).toString('base64url');
  assert.equal(minutosAteExpirar(`eyJhbGciOiJIUzI1NiJ9.${p}.x`), null);
});

// ---------------------------------------------------------------------------
// lerMilimetros — ausência e sentinelas nunca viram 0 medido
// ---------------------------------------------------------------------------

import { lerMilimetros, inicioDaHora } from './cemaden.mjs';

test('campo vazio não vira 0 — Number("") é 0 e isso seria "não choveu"', () => {
  assert.equal(lerMilimetros(''), null);
  assert.equal(lerMilimetros('   '), null);
  assert.equal(lerMilimetros(null), null);
  assert.equal(lerMilimetros(undefined), null);
});

test('sentinelas negativas viram ausência, não chuva', () => {
  assert.equal(lerMilimetros('-99.9'), null);
  assert.equal(lerMilimetros(-1), null);
  assert.equal(lerMilimetros('-0.5'), null);
});

test('texto não numérico vira ausência', () => {
  assert.equal(lerMilimetros('ND'), null);
  assert.equal(lerMilimetros('null'), null);
});

test('zero medido continua sendo zero', () => {
  assert.equal(lerMilimetros('0'), 0);
  assert.equal(lerMilimetros(0), 0);
  assert.equal(lerMilimetros('0,0'), 0);
});

test('vírgula decimal e espaços em volta', () => {
  assert.equal(lerMilimetros(' 2,5 '), 2.5);
  assert.equal(lerMilimetros('2.5'), 2.5);
});

test('CSV com coluna de valor vazia não publica chuva zero', () => {
  const csv = `cod.estacao;datahora;sensor;valor\nA;2026-09-19 17:00:00;chuva;\nB;2026-09-19 17:00:00;chuva;ND`;
  const r = lerDadosRede(csv);
  assert.equal(r.length, 2);
  assert.equal(r[0].valor, null);
  assert.equal(r[1].valor, null);
});

test('inicioDaHora trunca em UTC', () => {
  assert.equal(inicioDaHora(Date.UTC(2026, 8, 20, 13, 59, 58)), Date.UTC(2026, 8, 20, 13, 0, 0));
  assert.equal(inicioDaHora(Date.UTC(2026, 8, 20, 14, 0, 2)), Date.UTC(2026, 8, 20, 14, 0, 0));
});
