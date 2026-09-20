/**
 * Montagem da grade temporal publicada — funções puras (testadas em
 * `serie.test.mjs`).
 *
 * A grade é densa: uma linha por estação, uma coluna por fatia de 10 minutos,
 * cobrindo 24 h (144 fatias). Denso em vez de esparso porque a chuva é um
 * fenômeno raro no tempo — a maioria das células é 0, e zeros repetidos somem
 * no gzip. Medido: ~13 KB comprimido contra ~33 KB do formato esparso, com
 * indexação O(1) no navegador em vez de uma busca.
 *
 * `SEM_DADO` (-1) distingue "a estação não reportou" de "choveu 0 mm", que é
 * uma diferença real: a primeira é silêncio do sensor, a segunda é medição.
 */

export const PASSO_MS = 10 * 60_000;
export const SLOTS = 144;
export const SEM_DADO = -1;

/** Fatia de 10 min imediatamente anterior ou igual a `ms`. */
export function alinhar(ms) {
  return Math.floor(ms / PASSO_MS) * PASSO_MS;
}

/** Instante da primeira fatia de uma grade que termina em `fim`. */
export function inicioDaGrade(fim, slots = SLOTS) {
  return alinhar(fim) - (slots - 1) * PASSO_MS;
}

/** Índice da fatia de `ms` numa grade que começa em `t0`; -1 se está fora. */
export function indiceDaFatia(t0, ms, slots = SLOTS) {
  const i = Math.round((alinhar(ms) - t0) / PASSO_MS);
  return i >= 0 && i < slots ? i : -1;
}

/** Linha nova, toda sem dado. */
export function linhaVazia(slots = SLOTS) {
  return new Array(slots).fill(SEM_DADO);
}

/**
 * Grade a partir das leituras brutas da PED (caminho com token).
 *
 * Cada leitura da PED é a chuva medida no intervalo que termina no seu
 * carimbo de tempo, então ela cai na fatia do próprio carimbo. Quando mais de
 * uma leitura cai na mesma fatia, os valores são somados (é chuva acumulada,
 * não uma medida instantânea).
 *
 * @param codigos ordem das linhas (códigos de estação)
 * @param leituras `{ cod, ms, valor }`
 */
export function gradeDeLeituras(codigos, leituras, fim, slots = SLOTS) {
  const t0 = inicioDaGrade(fim, slots);
  const linha = new Map(codigos.map((c, i) => [c, i]));
  const v = codigos.map(() => linhaVazia(slots));

  for (const l of leituras) {
    const i = linha.get(l.cod);
    if (i === undefined) continue;
    const s = indiceDaFatia(t0, l.ms, slots);
    if (s < 0) continue;
    const atual = v[i][s];
    const novo = Number.isFinite(l.valor) ? l.valor : 0;
    v[i][s] = atual === SEM_DADO ? novo : atual + novo;
  }
  return { t0, v };
}

/**
 * Desloca a grade anterior para uma nova janela e grava o instantâneo atual na
 * última fatia (caminho sem token).
 *
 * É o que permite ao AmaView animar a chuva mesmo sem credencial: cada ciclo
 * do workflow acrescenta uma coluna e descarta a mais antiga, de modo que a
 * série se forma sozinha ao longo das horas. Não há invenção de dado — cada
 * coluna é uma medição real do CEMADEN, com o instante em que foi publicada.
 *
 * @param anterior grade publicada no ciclo passado (ou null na 1ª execução)
 * @param codigos ordem das linhas desejada agora
 * @param valores Map código → valor do instantâneo
 */
export function deslocarEGravar(anterior, codigos, valores, fim, slots = SLOTS) {
  const t0 = inicioDaGrade(fim, slots);
  const antes = anterior && Number.isFinite(anterior.t0) ? anterior : null;
  // Quantas fatias a janela andou desde a grade anterior.
  const desloc = antes ? Math.round((t0 - antes.t0) / PASSO_MS) : 0;
  const linhaAntes = antes ? new Map(antes.codigos.map((c, i) => [c, i])) : null;

  const v = codigos.map((cod) => {
    const linha = linhaVazia(slots);
    if (antes && linhaAntes.has(cod)) {
      const origem = antes.v[linhaAntes.get(cod)] ?? [];
      // Copia o que sobreviveu ao deslocamento da janela.
      for (let s = 0; s < slots; s++) {
        const o = s + desloc;
        if (o >= 0 && o < origem.length) linha[s] = origem[o];
      }
    }
    const atual = valores.get(cod);
    if (Number.isFinite(atual)) linha[slots - 1] = atual;
    return linha;
  });

  return { t0, v };
}

/**
 * Acumulado de 24 h em cada fatia, a partir de uma série HORÁRIA.
 *
 * Usado para preencher a grade na primeira execução do modo aberto. Para a
 * fatia que termina em T, soma as horas cujo INÍCIO cai em (T − 24 h, T] — a
 * mesma grandeza que o instantâneo publicado pelo CEMADEN, para que as
 * colunas preenchidas e as acrescentadas depois signifiquem a mesma coisa.
 *
 * Uma fatia sem nenhuma hora conhecida no período vira `SEM_DADO`, nunca 0:
 * não há como afirmar que não choveu numa janela que não foi medida.
 *
 * @param horas `[{ ms, mm }]` com `ms` no início da hora (UTC)
 */
export function acum24hPorFatia(horas, t0, slots = SLOTS) {
  const linha = linhaVazia(slots);
  if (!horas?.length) return linha;
  const ordenadas = [...horas].sort((a, b) => a.ms - b.ms);

  for (let s = 0; s < slots; s++) {
    const fim = t0 + s * PASSO_MS;
    const inicio = fim - 24 * 3_600_000;
    let soma = 0;
    let n = 0;
    for (const h of ordenadas) {
      if (h.ms > inicio && h.ms <= fim) {
        soma += h.mm;
        n++;
      }
    }
    if (n > 0) linha[s] = soma;
  }
  return linha;
}

/** Arredonda para 1 casa decimal, preservando `SEM_DADO`. */
export function arredondar(v) {
  return v.map((linha) => linha.map((x) => (x === SEM_DADO ? SEM_DADO : Math.round(x * 10) / 10)));
}

/** Quantas células têm medição (para o relatório do workflow). */
export function contarMedidas(v) {
  let com = 0;
  let chuva = 0;
  for (const linha of v) {
    for (const x of linha) {
      if (x === SEM_DADO) continue;
      com++;
      if (x > 0) chuva++;
    }
  }
  return { com, chuva, total: v.length * (v[0]?.length ?? 0) };
}
