/**
 * Monta a REFERÊNCIA HISTÓRICA de cada estação de rio: máximo e mínimo já
 * registrados, e a distribuição do nível mês a mês.
 *
 * Sem isso, a camada mostra "2069 cm" — um número que não diz nada a quem não
 * é hidrólogo. Com isso, ela diz **"1,4 m abaixo do normal para setembro"** e
 * **"a 8,6 m do recorde de seca de outubro de 2024"**, que é a pergunta que o
 * cidadão amazônico realmente faz.
 *
 * Roda RARAMENTE e à mão, não no workflow: o resultado é praticamente
 * estático (um recorde muda uma vez por década) e a coleta baixa centenas de
 * megabytes de um serviço público. O arquivo gerado é commitado no
 * repositório.
 *
 *   node scripts/rios-climatologia.mjs [ano_inicial] [saida.json]
 *
 * Retomável: se o arquivo de saída já existir, as estações já processadas são
 * puladas. Uma queda de rede no meio não joga fora o que já foi baixado.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { emLotes, leituras } from './ana.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const ANO_INICIAL = Number(process.argv[2]) || 2014;
const SAIDA = process.argv[3] || join(RAIZ, 'dados', 'rios-climatologia.json');
const ANO_FINAL = new Date().getUTCFullYear();

/**
 * Estações com referência histórica, por NOME.
 *
 * Os códigos são resolvidos contra `dados/rios-estacoes.json` em vez de
 * escritos à mão: código de estação decorado é um jeito silencioso de
 * publicar o rio errado com o nome certo.
 *
 * Não são todas as 154: cada estação custa ~12 requisições de um ano inteiro
 * (~1,5 MB cada) e o valor marginal cai rápido. Estas são as que contam a
 * história — capitais, confluências e os portos por onde a cheia e a seca
 * chegam ao noticiário.
 */
export const CURADAS_NOMES = [
  'MANAUS',
  'MANACAPURU',
  'ITACOATIARA',
  'ÓBIDOS',
  'COARI',
  'TABATINGA',
  'BARCELOS',
  'MOURA',
  'SANTA ISABEL',
  'CUCUÍ',
  'CARACARAÍ',
  'BOA VISTA',
  'PORTO VELHO',
  'ABUNÃ',
  'GUAJARÁ-MIRIM',
  'ITAITUBA',
  'ALTAMIRA',
  'MARABÁ',
  'MACAPÁ',
  'ORIXIMINÁ',
  'RIO BRANCO',
  'SENA MADUREIRA',
  'XAPURI',
  'JI-PARANÁ',
  'ALMEIRIM',
  'PORTO DE MOZ',
  'LARANJAL DO JARI',
  'BERURI',
  'MANOEL URBANO',
  'ARIQUEMES',
  'SANTARÉM',
  'VITÓRIA DO XINGU',
  'CONCEIÇÃO DO ARAGUAIA',
  'MUCAJAÍ',
  'SERRINHA',
  'PRINCIPE DA BEIRA',
];

function inicioAno(ano) {
  return Date.UTC(ano, 0, 1);
}
function fimAno(ano) {
  return Math.min(Date.UTC(ano, 11, 31, 23, 59), Date.now());
}

/** Percentil de uma lista JÁ ORDENADA (interpolação linear). */
export function percentil(ordenada, p) {
  if (ordenada.length === 0) return null;
  if (ordenada.length === 1) return ordenada[0];
  const i = (ordenada.length - 1) * p;
  const baixo = Math.floor(i);
  const alto = Math.ceil(i);
  if (baixo === alto) return ordenada[baixo];
  return ordenada[baixo] + (ordenada[alto] - ordenada[baixo]) * (i - baixo);
}

/**
 * Resume as leituras de uma estação.
 *
 * Usa a MÉDIA DIÁRIA, e não as leituras de 15 em 15 minutos: um pico de
 * sensor de um quarto de hora não é um recorde de cheia, e sem essa
 * agregação um único valor espúrio viraria "o maior nível já registrado".
 */
export function resumir(linhas) {
  const porDia = new Map();
  for (const l of linhas) {
    if (l.nivel === null || !Number.isFinite(l.nivel)) continue;
    // Níveis absurdos são erro de sensor, não rio.
    if (l.nivel < -500 || l.nivel > 6000) continue;
    const d = new Date(l.ms);
    const chave = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
    const a = porDia.get(chave) ?? { soma: 0, n: 0, mes: d.getUTCMonth() };
    a.soma += l.nivel;
    a.n++;
    porDia.set(chave, a);
  }

  const dias = [...porDia.entries()].map(([data, a]) => ({ data, mes: a.mes, cm: a.soma / a.n }));
  if (dias.length === 0) return null;

  let max = dias[0];
  let min = dias[0];
  const porMes = Array.from({ length: 12 }, () => []);
  for (const d of dias) {
    if (d.cm > max.cm) max = d;
    if (d.cm < min.cm) min = d;
    porMes[d.mes].push(d.cm);
  }

  const meses = porMes.map((lista) => {
    if (lista.length < 30) return null; // menos de um mês de dias: não é normal climática
    const o = [...lista].sort((a, b) => a - b);
    return {
      n: o.length,
      p10: Math.round(percentil(o, 0.1)),
      p50: Math.round(percentil(o, 0.5)),
      p90: Math.round(percentil(o, 0.9)),
    };
  });

  const anos = [...new Set(dias.map((d) => d.data.slice(0, 4)))].sort();

  return {
    dias: dias.length,
    anos: [Number(anos[0]), Number(anos[anos.length - 1])],
    max: { cm: Math.round(max.cm), data: max.data },
    min: { cm: Math.round(min.cm), data: min.data },
    meses,
  };
}

async function main() {
  const estacoes = JSON.parse(await readFile(join(RAIZ, 'dados', 'rios-estacoes.json'), 'utf8')).estacoes;
  const porCod = new Map(estacoes.map((e) => [e.cod, e]));

  let acumulado = { gerado: null, periodo: [ANO_INICIAL, ANO_FINAL], estacoes: {} };
  if (existsSync(SAIDA)) {
    acumulado = JSON.parse(await readFile(SAIDA, 'utf8'));
    console.log(`retomando: ${Object.keys(acumulado.estacoes).length} estações já prontas`);
  }

  const curadas = CURADAS_NOMES.map((nome) => {
    const e = estacoes.find((x) => x.nome === nome);
    if (!e) console.warn(`  [aviso] "${nome}" não está na lista de estações — ignorada`);
    return e?.cod ?? null;
  }).filter(Boolean);

  const faltando = curadas.filter((c) => !acumulado.estacoes[c]);
  console.log(`${faltando.length} estações a processar, ${ANO_INICIAL}–${ANO_FINAL}\n`);

  for (const cod of faltando) {
    const nome = porCod.get(cod)?.nome ?? cod;
    const anos = [];
    for (let a = ANO_INICIAL; a <= ANO_FINAL; a++) anos.push(a);

    const t0 = Date.now();
    const { ok, erros } = await emLotes(
      anos,
      async (ano) => leituras(cod, inicioAno(ano), fimAno(ano), { timeoutMs: 90_000 }),
      { concorrencia: 2 },
    );

    const todas = ok.flat();
    const resumo = resumir(todas);
    if (!resumo) {
      console.log(`  ${nome.padEnd(22)} SEM DADO (${erros.length} erros)`);
      acumulado.estacoes[cod] = null;
    } else {
      acumulado.estacoes[cod] = resumo;
      const comMes = resumo.meses.filter(Boolean).length;
      console.log(
        `  ${nome.padEnd(22)} ${String(resumo.dias).padStart(5)} dias · ${resumo.anos[0]}–${resumo.anos[1]} · ` +
          `máx ${resumo.max.cm} (${resumo.max.data}) · mín ${resumo.min.cm} (${resumo.min.data}) · ` +
          `${comMes}/12 meses · ${((Date.now() - t0) / 1000).toFixed(0)} s${erros.length ? ` · ${erros.length} erros` : ''}`,
      );
    }

    acumulado.gerado = new Date().toISOString();
    await writeFile(SAIDA, JSON.stringify(acumulado));
  }

  const prontas = Object.values(acumulado.estacoes).filter(Boolean).length;
  console.log(`\nOK — ${prontas} estações com referência histórica → ${SAIDA}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error(`FALHOU: ${e.message}`);
    process.exitCode = 1;
  });
}
