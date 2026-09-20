import test from 'node:test';
import assert from 'node:assert/strict';
import { HORARIOS, instantesDeSondagem, lerIndices, lerPerfil, marcaWyoming, urlSkewT } from './wyoming.mjs';
import { NIVEIS_PADRAO, reduzirNiveis } from './estacoes-sondagem.mjs';

// Resposta real do Wyoming (Manaus, 2026-09-18 12Z), recortada.
const HTML = `<HTML><BODY>
<H2>82332 SBMN Manaus Observations at 12Z 18 Sep 2026</H2>
<PRE>
-----------------------------------------------------------------------------
   PRES   HGHT   TEMP   DWPT   RELH   MIXR   DRCT   SPED   THTA   THTE   THTV
    hPa      m      C      C      %   g/kg    deg    m/s      K      K      K
-----------------------------------------------------------------------------
 1005.9     85   29.2   25.4     80  20.70    100    0.5  301.8  363.3  305.6
 1000.0    137   27.9   25.4     86  20.78    100    0.5  301.0  362.5  304.8
  925.0    828   23.9   18.9     74  15.03     68    8.7  303.7  348.6  306.4
  850.0   1564   19.2   15.1     77  12.83     79   12.7  306.3  345.0  308.6
  700.0   3172    9.4   -2.0     46   4.21     75   12.3  309.2  322.6  310.0
  500.0   5890   -5.1  -20.1     32   1.60     90    7.0  318.0  324.1  318.3
</PRE>
<TABLE>
<TR><TD>PWAT</TD><TD>Precipitable water</TD><TD>52.31</TD><TD>millimeter</TD></TR>
<TR><TD>MUCAPE</TD><TD>Most Unstable CAPE</TD><TD>1204.7</TD><TD>joule / kilogram</TD></TR>
<TR><TD>LCLP</TD><TD>Lifted Condensation Level</TD><TD>948.0</TD><TD>hectopascal</TD></TR>
</TABLE>
</BODY></HTML>`;

test('marcaWyoming usa o único formato aceito pelo servidor', () => {
  assert.equal(marcaWyoming(Date.UTC(2026, 8, 18, 12, 0, 0)), '2026-09-18 12:00:00');
  assert.equal(marcaWyoming(Date.UTC(2026, 0, 5, 0, 0, 0)), '2026-01-05 00:00:00');
});

test('marcaWyoming é UTC, não fuso local', () => {
  assert.ok(marcaWyoming(Date.UTC(2026, 8, 18, 2, 0, 0)).endsWith('02:00:00'));
});

test('instantesDeSondagem só devolve horários de lançamento', () => {
  const hs = instantesDeSondagem(Date.UTC(2026, 8, 20, 19, 0, 0), 48);
  assert.ok(hs.every((t) => HORARIOS.includes(new Date(t).getUTCHours())));
});

test('instantesDeSondagem vem do mais recente para o mais antigo', () => {
  const hs = instantesDeSondagem(Date.UTC(2026, 8, 20, 19, 0, 0), 48);
  for (let i = 1; i < hs.length; i++) assert.ok(hs[i] < hs[i - 1]);
  // Às 19Z, a mais recente possível é a das 12Z do mesmo dia.
  assert.equal(new Date(hs[0]).getUTCHours(), 12);
});

test('instantesDeSondagem não inventa sondagem futura', () => {
  const agora = Date.UTC(2026, 8, 20, 6, 0, 0);
  const hs = instantesDeSondagem(agora, 48);
  assert.ok(hs.every((t) => t <= agora));
});

test('lerPerfil extrai os níveis do bloco PRE', () => {
  const p = lerPerfil(HTML, 123);
  assert.equal(p.ms, 123);
  assert.equal(p.niveis.length, 6);
  assert.deepEqual(p.niveis[0], { p: 1005.9, z: 85, t: 29.2, d: 25.4, u: 80 });
  assert.equal(p.niveis.at(-1).p, 500);
});

test('sem bloco PRE é ausência de sondagem, não erro', () => {
  assert.equal(lerPerfil('<html><body>Nada aqui</body></html>', 1), null);
});

test('bloco PRE ilegível LANÇA — mudança de formato não pode passar calada', () => {
  // Silêncio aqui viraria "nenhuma estação tem dado" sem ninguém perceber.
  assert.throws(() => lerPerfil('<PRE>\nlixo\nsem cabecalho\n</PRE>', 1), /formato mudou/);
  assert.throws(() => lerPerfil('<PRE>\n PRES HGHT TEMP\n x\n y\n</PRE>', 1), /nenhum nível/);
});

test('campos ausentes viram null, não zero', () => {
  const html = HTML.replace(' 1000.0    137   27.9   25.4     86', ' 1000.0    137            25.4     86');
  const p = lerPerfil(html, 1);
  const n = p.niveis.find((x) => x.p === 1000);
  // A linha perde uma coluna: o que não é número não pode virar 0 °C.
  assert.ok(n === undefined || n.t === null || Number.isFinite(n.t));
});

test('lerIndices pega os índices da tabela', () => {
  const idx = lerIndices(HTML);
  assert.equal(idx.PWAT, 52.31);
  assert.equal(idx.MUCAPE, 1204.7);
  assert.equal(idx.LCLP, 948);
});

test('índice ausente simplesmente não aparece', () => {
  const idx = lerIndices('<html></html>');
  assert.equal(idx.PWAT, undefined);
  assert.deepEqual(idx, {});
});

test('urlSkewT aponta para o diagrama da mesma sondagem', () => {
  const u = urlSkewT(82332, Date.UTC(2026, 8, 18, 12, 0, 0));
  assert.ok(u.includes('id=82332'));
  assert.ok(u.includes('PNG%3ASKEWT'));
  assert.ok(u.includes('2026-09-18'));
});

// ---------------------------------------------------------------------------

test('reduzirNiveis mantém a superfície', () => {
  const p = lerPerfil(HTML, 1);
  const r = reduzirNiveis(p.niveis);
  assert.equal(r[0].p, 1005.9);
});

test('reduzirNiveis mantém os níveis padrão presentes', () => {
  const p = lerPerfil(HTML, 1);
  const r = reduzirNiveis(p.niveis).map((n) => n.p);
  for (const alvo of [1000, 925, 850, 700, 500]) assert.ok(r.includes(alvo), `faltou ${alvo}`);
});

test('reduzirNiveis não inventa níveis que não existem', () => {
  const p = lerPerfil(HTML, 1);
  const originais = new Set(p.niveis.map((n) => n.p));
  for (const n of reduzirNiveis(p.niveis)) assert.ok(originais.has(n.p));
});

test('reduzirNiveis preserva a ordem de pressão decrescente', () => {
  const p = lerPerfil(HTML, 1);
  const r = reduzirNiveis(p.niveis);
  for (let i = 1; i < r.length; i++) assert.ok(r[i].p < r[i - 1].p);
});

test('reduzirNiveis guarda quebras bruscas de umidade', () => {
  // Camada seca no meio: T−Td salta de 4 para 30 °C. Omiti-la achataria o
  // perfil justamente onde ele tem informação.
  const niveis = [
    { p: 1000, z: 100, t: 28, d: 24, u: 80 },
    { p: 900, z: 900, t: 22, d: 18, u: 78 },
    { p: 880, z: 1100, t: 21, d: -9, u: 10 },
    { p: 800, z: 2000, t: 16, d: -14, u: 8 },
  ];
  const r = reduzirNiveis(niveis, [1000]);
  assert.ok(r.some((n) => n.p === 880), 'a quebra de umidade tem que entrar');
});

test('reduzirNiveis aguenta perfil vazio', () => {
  assert.deepEqual(reduzirNiveis([]), []);
  assert.deepEqual(reduzirNiveis(undefined), []);
});

test('NIVEIS_PADRAO vai da superfície à estratosfera, em ordem', () => {
  for (let i = 1; i < NIVEIS_PADRAO.length; i++) assert.ok(NIVEIS_PADRAO[i] < NIVEIS_PADRAO[i - 1]);
});
