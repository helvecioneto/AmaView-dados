/**
 * Baixa o histórico da telemetria da ANA e guarda a **média diária** de cada
 * estação. É o insumo da climatologia.
 *
 * Fica separado do cálculo de propósito: baixar 154 estações × 13 anos leva
 * quase uma hora e ~3 GB de um serviço público. O método estatístico ainda vai
 * mudar; o dado baixado, não. Guardando as médias diárias, recalcular a
 * referência é instantâneo e não custa nada a ninguém.
 *
 * Média DIÁRIA, e não as leituras de 15 em 15 min: um pico de sensor de um
 * quarto de hora não é um recorde de cheia, e sem essa agregação um único
 * valor espúrio viraria "o maior nível já registrado".
 *
 *   node scripts/rios-dias.mjs [ano_inicial]
 *
 * Retomável: o que já foi baixado é pulado. Salva a cada estação concluída.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { emLotes, leituras } from './ana.mjs';
import { cotaPlausivel } from './hidro.mjs';

const RAIZ = join(dirname(fileURLToPath(import.meta.url)), '..');
const ANO_INICIAL = Number(process.argv[2]) || 2014;
const ANO_FINAL = new Date().getUTCFullYear();
const SAIDA = join(RAIZ, 'dados', 'rios-dias.json');

/** Média diária das leituras de um ano, como `{ 'AAAA-MM-DD': cm }`. */
export function mediasDiarias(linhas) {
  const acc = new Map();
  for (const l of linhas ?? []) {
    if (!l || !Number.isFinite(l.ms) || !cotaPlausivel(l.nivel)) continue;
    const d = new Date(l.ms);
    const chave = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
    const a = acc.get(chave) ?? { soma: 0, n: 0 };
    a.soma += l.nivel;
    a.n++;
    acc.set(chave, a);
  }
  const saida = {};
  for (const [dia, a] of acc) saida[dia] = Math.round(a.soma / a.n);
  return saida;
}

async function main() {
  const estacoes = JSON.parse(await readFile(join(RAIZ, 'dados', 'rios-estacoes.json'), 'utf8')).estacoes;

  let acumulado = { gerado: null, periodo: [ANO_INICIAL, ANO_FINAL], estacoes: {} };
  if (existsSync(SAIDA)) {
    acumulado = JSON.parse(await readFile(SAIDA, 'utf8'));
    console.log(`retomando: ${Object.keys(acumulado.estacoes).length} estações já baixadas`);
  }

  const faltando = estacoes.filter((e) => !acumulado.estacoes[e.cod]);
  console.log(`${faltando.length} de ${estacoes.length} estações a baixar, ${ANO_INICIAL}–${ANO_FINAL}\n`);

  // Uma tarefa por (estação, ano), todas na mesma fila: assim a concorrência
  // de 3 fica sempre cheia, mesmo quando uma estação responde devagar.
  const tarefas = [];
  for (const e of faltando) {
    for (let ano = ANO_INICIAL; ano <= ANO_FINAL; ano++) tarefas.push({ e, ano });
  }

  const porEstacao = new Map();
  const t0 = Date.now();

  const { erros } = await emLotes(
    tarefas,
    async ({ e, ano }) => {
      const inicio = Date.UTC(ano, 0, 1);
      const fim = Math.min(Date.UTC(ano, 11, 31, 23, 59), Date.now());
      const l = await leituras(e.cod, inicio, fim, { timeoutMs: 120_000, tentativas: 1 });
      const dias = mediasDiarias(l);
      const atual = porEstacao.get(e.cod) ?? {};
      Object.assign(atual, dias);
      porEstacao.set(e.cod, atual);
      return true;
    },
    {
      concorrencia: 3,
      aoAndar: (f, n) => {
        const min = (Date.now() - t0) / 60_000;
        const falta = (min / f) * (n - f);
        console.log(`  ${f}/${n} (${min.toFixed(0)} min, faltam ~${falta.toFixed(0)} min) · ${porEstacao.size} estações`);
      },
    },
  );

  for (const [cod, dias] of porEstacao) {
    if (Object.keys(dias).length > 0) acumulado.estacoes[cod] = dias;
  }
  acumulado.gerado = new Date().toISOString();
  await writeFile(SAIDA, JSON.stringify(acumulado));

  const total = Object.values(acumulado.estacoes).reduce((s, d) => s + Object.keys(d).length, 0);
  console.log(
    `\nOK — ${Object.keys(acumulado.estacoes).length} estações, ${total} dias · ` +
      `${erros.length} requisições falharam · ${((Date.now() - t0) / 60_000).toFixed(0)} min`,
  );
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error(`FALHOU: ${e.message}`);
    process.exitCode = 1;
  });
}
