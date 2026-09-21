/**
 * Funções puras do ciclo dos rios: grade horária, tendência e a decisão de
 * quando o ciclo está vencido.
 *
 * Sem rede, testado em `rios.test.mjs`.
 */

/** Passo e tamanho da série publicada: 48 h de hora em hora. */
export const PASSO_MS = 60 * 60 * 1000;
export const SLOTS = 48;

/** Ausência de medição naquela hora — diferente de 0 cm, que é leitura válida. */
export const SEM_DADO = null;

/** Cadência do ciclo: de hora em hora, não de 15 em 15 min (ver `rios.mjs`). */
export const CADENCIA_MS = 55 * 60 * 1000;

/** Faixa plausível de cota, em cm. Fora disso é erro de sensor, não rio. */
export const COTA_MIN = -500;
export const COTA_MAX = 6000;

export function cotaPlausivel(cm) {
  return typeof cm === 'number' && Number.isFinite(cm) && cm >= COTA_MIN && cm <= COTA_MAX;
}

/** O controle de qualidade da própria ANA aprovou esta leitura? */
export function aprovada(cq) {
  return typeof cq === 'string' && /aprovad/i.test(cq);
}

/**
 * O ciclo está vencido?
 *
 * **Falha ABERTO, de propósito.** Qualquer dúvida — publicação anterior
 * ausente (estreia), JSON corrompido, carimbo ilegível, relógio do runner
 * divergindo para o futuro — resulta em RODAR. O erro de rodar à toa custa uma
 * rodada de um minuto; o erro de não rodar é a camada congelar para sempre
 * sem ninguém perceber, porque o carimbo velho nunca mais vence.
 */
export function venceu(geradoAnterior, agora = Date.now(), cadenciaMs = CADENCIA_MS) {
  const t = typeof geradoAnterior === 'string' ? Date.parse(geradoAnterior) : Number(geradoAnterior);
  if (!Number.isFinite(t)) return true;
  // Carimbo no futuro é relógio errado, não dado fresco.
  if (t > agora + 5 * 60 * 1000) return true;
  return agora - t >= cadenciaMs;
}

/**
 * Alinha o fim da janela na hora cheia. Assim todas as estações compartilham
 * a mesma grade, e o `t0` do arquivo vale para todas.
 */
export function fimDaGrade(agora = Date.now()) {
  return Math.floor(agora / PASSO_MS) * PASSO_MS;
}

export function t0Da(fim) {
  return fim - (SLOTS - 1) * PASSO_MS;
}

/**
 * Leituras de 15 em 15 min → uma linha de 48 valores horários.
 *
 * Para cada hora, vale a leitura mais próxima dentro de meia hora. Interpolar
 * inventaria medição; pegar a primeira da hora jogaria fora a mais próxima.
 *
 * Leitura reprovada pelo controle de qualidade da ANA **entra na série** — um
 * buraco no meio do gráfico é pior para quem olha do que um valor bruto — mas
 * é marcada, e quem decide cor e tendência usa só as aprovadas.
 */
export function gradeHoraria(leituras, fim = fimDaGrade()) {
  const t0 = t0Da(fim);
  const v = new Array(SLOTS).fill(SEM_DADO);
  const bruta = new Array(SLOTS).fill(false);
  const melhor = new Array(SLOTS).fill(Infinity);

  for (const l of leituras ?? []) {
    if (!l || !Number.isFinite(l.ms) || !cotaPlausivel(l.nivel)) continue;
    const i = Math.round((l.ms - t0) / PASSO_MS);
    if (i < 0 || i >= SLOTS) continue;
    const dist = Math.abs(l.ms - (t0 + i * PASSO_MS));
    if (dist > PASSO_MS / 2) continue;
    // Empate (duas leituras à mesma distância): a mais recente vence.
    if (dist > melhor[i]) continue;
    melhor[i] = dist;
    v[i] = Math.round(l.nivel);
    bruta[i] = !aprovada(l.cqNivel);
  }

  return { t0, passoMin: PASSO_MS / 60_000, slots: SLOTS, v, bruta };
}

/** Último valor não vazio da linha, com o índice. */
export function ultimoDa(v) {
  for (let i = v.length - 1; i >= 0; i--) {
    if (v[i] !== SEM_DADO && v[i] !== undefined) return { i, cm: v[i] };
  }
  return null;
}

/**
 * Variação em cm nas últimas `horas`, olhando a grade.
 *
 * Devolve `null` quando falta uma das duas pontas: dizer "estável" porque não
 * temos o passado seria inventar estabilidade.
 */
export function variacao(v, horas) {
  const ultimo = ultimoDa(v);
  if (!ultimo) return null;
  const j = ultimo.i - horas;
  // A última leitura costuma estar uma hora atrás da ponta da grade (a ANA
  // publica com ~50 min de atraso), então `j` fica em −1 na variação de 48 h.
  // A busca tolerante abaixo já cuida de índices negativos; só desistimos se
  // nem com a folga de 3 h houver amostra.
  if (j < -3) return null;
  // Tolera um buraco: procura a amostra mais próxima até 3 h antes do alvo.
  for (let k = 0; k <= 3; k++) {
    for (const idx of [j - k, j + k]) {
      if (idx >= 0 && idx < v.length && v[idx] !== SEM_DADO && v[idx] !== undefined) {
        return ultimo.cm - v[idx];
      }
    }
  }
  return null;
}

/**
 * Limiar da tendência, em cm por 24 h.
 *
 * Abaixo disso a variação é ruído de sensor e oscilação de vento sobre a
 * régua, não o rio subindo ou descendo.
 */
export const LIMIAR_TENDENCIA_CM = 5;

export function tendenciaDe(delta24h) {
  if (delta24h === null || !Number.isFinite(delta24h)) return 'desconhecida';
  if (delta24h >= LIMIAR_TENDENCIA_CM) return 'subindo';
  if (delta24h <= -LIMIAR_TENDENCIA_CM) return 'descendo';
  return 'estavel';
}

// ---------------------------------------------------------------------------
// Referência histórica: por DIA DO ANO, não por mês
// ---------------------------------------------------------------------------

/**
 * Janela, em dias, em torno do dia do ano. ±15 dias dá um mês de amostras por
 * ano sem achatar a sazonalidade.
 */
export const JANELA_DOY = 15;

/**
 * Mínimo de anos para colorir uma estação.
 *
 * Os dias DENTRO de uma janela são fortemente autocorrelacionados — o rio de
 * ontem explica o de hoje —, então quem conta como amostra independente é o
 * ANO, não o dia. Com menos de 8 anos, um percentil empírico é quase o
 * mínimo da amostra, e chamar isso de "anormal" seria ruído com cara de fato.
 */
export const MIN_ANOS_REFERENCIA = 8;

/** Dia do ano, 1 a 366. */
export function diaDoAno(ms) {
  const d = new Date(ms);
  const inicio = Date.UTC(d.getUTCFullYear(), 0, 1);
  return Math.floor((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - inicio) / 86_400_000) + 1;
}

/** Distância circular entre dois dias do ano (365/366 e 1 são vizinhos). */
export function distanciaDoy(a, b) {
  const d = Math.abs(a - b);
  return Math.min(d, 366 - d);
}

/** Percentil de uma lista JÁ ORDENADA, com interpolação linear. */
export function percentil(ordenada, p) {
  if (!ordenada.length) return null;
  if (ordenada.length === 1) return ordenada[0];
  const i = (ordenada.length - 1) * p;
  const b = Math.floor(i);
  const a = Math.ceil(i);
  return b === a ? ordenada[b] : ordenada[b] + (ordenada[a] - ordenada[b]) * (i - b);
}

/**
 * Referência para um dia do ano, a partir das médias diárias da estação.
 *
 * `dias` é `{ 'AAAA-MM-DD': cm }`. Devolve `null` quando não há anos
 * suficientes — e `null` tem de continuar significando "não sei", nunca
 * "normal": tratar ausência de referência como normalidade apagaria
 * justamente as estações sobre as quais não sabemos nada.
 *
 * Por que não a mediana do MÊS: o Negro varia de 10 a 20 cm por dia em
 * setembro, e a mediana mensal equivale ao dia 15. Comparar o dia 20 com ela
 * embute meio metro de "anomalia" que é só calendário — e na subida, entre
 * abril e maio, o sinal se inverte e pinta a bacia inteira de seca.
 */
export function referenciaDoDia(dias, doy) {
  if (!dias) return null;
  const amostras = [];
  const anos = new Set();

  for (const [data, cm] of Object.entries(dias)) {
    if (!cotaPlausivel(cm)) continue;
    const ms = Date.parse(`${data}T12:00:00Z`);
    if (!Number.isFinite(ms)) continue;
    if (distanciaDoy(diaDoAno(ms), doy) > JANELA_DOY) continue;
    amostras.push(cm);
    anos.add(data.slice(0, 4));
  }

  if (anos.size < MIN_ANOS_REFERENCIA) return null;

  const o = amostras.sort((a, b) => a - b);
  return {
    // A amostra ordenada fica junto: é dela que sai a posição percentílica
    // exata, sem aproximar por quartis.
    amostra: o,
    anos: anos.size,
    n: o.length,
    min: o[0],
    p25: Math.round(percentil(o, 0.25)),
    p50: Math.round(percentil(o, 0.5)),
    p75: Math.round(percentil(o, 0.75)),
    max: o[o.length - 1],
  };
}

/**
 * Posição da cota de hoje na distribuição daquele dia do ano, de 0 a 100.
 *
 * É o número que torna estações comparáveis: **adimensional**. Um metro e
 * meio abaixo do normal é rotina no Negro, cuja amplitude anual passa de
 * 10 m, e é evento extremo num rio de 3 m de amplitude — em centímetros, os
 * dois casos pintariam a mesma cor.
 */
export function posicaoPercentil(cm, ref) {
  if (!cotaPlausivel(cm) || !ref?.amostra?.length) return null;
  let menores = 0;
  for (const v of ref.amostra) {
    if (v < cm) menores++;
    else break;
  }
  return Math.round((menores / ref.amostra.length) * 100);
}

/**
 * Classe da escala divergente, a partir da POSIÇÃO PERCENTÍLICA.
 *
 * Os cortes são os quartis: fora deles, a cota está entre as mais extremas
 * um quarto dos anos medidos — o que é dizível em português sem estatística
 * ("mais baixo que em 13 dos 15 anos medidos").
 */
export function classePorPercentil(p) {
  if (p === null || !Number.isFinite(p)) return null;
  if (p <= 10) return 'muitoAbaixo';
  if (p < 25) return 'abaixo';
  if (p <= 75) return 'normal';
  if (p < 90) return 'acima';
  return 'muitoAcima';
}

/** Diferença em cm entre a cota de hoje e a mediana daquele dia do ano. */
export function anomaliaCm(cm, ref) {
  if (!cotaPlausivel(cm) || !Number.isFinite(ref?.p50)) return null;
  return Math.round(cm - ref.p50);
}

/**
 * Extremos da série DESTA estação, no período medido.
 *
 * Deliberadamente NÃO se chamam "recorde histórico": a régua de Manaus tem
 * série desde 1902, e os 30,02 m de 2021 e os 12,11 m de 2024 que saem no
 * jornal são do porto, não desta estação telemétrica. Misturar as duas
 * séries produziria um "recorde" que nunca existiu. A interface diz
 * "desde 2014, nesta estação".
 */
export function extremosDe(dias) {
  if (!dias) return null;
  let max = null;
  let min = null;
  const anos = new Set();
  for (const [data, cm] of Object.entries(dias)) {
    if (!cotaPlausivel(cm)) continue;
    anos.add(data.slice(0, 4));
    if (!max || cm > max.cm) max = { cm, data };
    if (!min || cm < min.cm) min = { cm, data };
  }
  if (!max) return null;
  const ordenados = [...anos].sort();
  return { max, min, desde: Number(ordenados[0]), ate: Number(ordenados[ordenados.length - 1]), anos: anos.size };
}

// ---------------------------------------------------------------------------
// Herança entre ciclos
// ---------------------------------------------------------------------------

/**
 * Desloca uma série publicada na grade anterior para a grade de agora.
 *
 * `null` quando a publicação anterior é velha demais para alcançar a grade
 * nova, ou quando não sobra medição nenhuma depois do deslocamento — nesses
 * casos é melhor a estação sumir que mostrar dado de dias atrás.
 */
export function deslocarSerie(serieAnterior, t0Anterior, passoAnteriorMin, fimNovo) {
  if (!Array.isArray(serieAnterior) || serieAnterior.length !== SLOTS) return null;
  if (!Number.isFinite(t0Anterior)) return null;
  const passo = (Number(passoAnteriorMin) || 60) * 60_000;
  const fimAnterior = t0Anterior + (SLOTS - 1) * passo;
  const desloc = Math.round((fimNovo - fimAnterior) / PASSO_MS);
  if (desloc < 0 || desloc >= SLOTS) return null;
  const serie = [...serieAnterior.slice(desloc), ...new Array(desloc).fill(SEM_DADO)];
  return ultimoDa(serie) ? serie : null;
}
