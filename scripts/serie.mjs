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
    // Leitura sem valor não escreve célula nenhuma: a fatia continua
    // SEM_DADO. Gravar 0 aqui transformaria silêncio do sensor em "mediu e
    // não choveu", que é a inversão que esta base inteira existe para evitar.
    if (!Number.isFinite(l.valor)) continue;
    const atual = v[i][s];
    v[i][s] = atual === SEM_DADO ? l.valor : atual + l.valor;
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
      // `h.ms` é o INÍCIO da hora, e a chuva dela cai em [h, h+1h). A janela
      // compara pelo FIM: comparar pelo início incluía a hora que só começa em
      // `fim` — chuva ainda no futuro daquela fatia — e excluía a que começa
      // em `inicio`, que está dentro da janela.
      const fimDaHora = h.ms + 3_600_000;
      if (fimDaHora > inicio && fimDaHora <= fim) {
        soma += h.mm;
        n++;
      }
    }
    if (n > 0) linha[s] = soma;
  }
  return linha;
}

/**
 * Arredonda para 1 casa decimal, preservando `SEM_DADO`.
 *
 * O piso em 0 não é cosmético: `-0.96` arredondaria para `-1`, que é o próprio
 * marcador de ausência — uma medição viraria "sem dado" por coincidência
 * numérica. Chuva acumulada não é negativa, então 0 é o piso correto.
 */
export function arredondar(v) {
  return v.map((linha) =>
    linha.map((x) => (x === SEM_DADO ? SEM_DADO : Math.max(0, Math.round(x * 10) / 10))),
  );
}

/**
 * Copia da grade anterior as linhas que este ciclo não conseguiu preencher.
 *
 * Uma UF que responde CSV só com cabeçalho devolve zero leituras sem erro, e
 * todas as estações dela iriam a SEM_DADO nas 144 fatias — o ciclo publicaria
 * uma grade estritamente pior que a anterior. Aqui a linha antiga é deslocada
 * para a janela nova e reaproveitada.
 *
 * Só entra onde não há NENHUMA medição nova: onde o ciclo trouxe dado, o dado
 * novo manda.
 */
export function herdarVazias(atual, codigos, anterior, slots = SLOTS) {
  if (!anterior || !Number.isFinite(anterior.t0)) return { herdadas: 0, v: atual.v };
  const desloc = Math.round((atual.t0 - anterior.t0) / PASSO_MS);
  const linhaAntes = new Map(anterior.codigos.map((c, i) => [c, i]));

  let herdadas = 0;
  const v = atual.v.map((linha, i) => {
    if (linha.some((x) => x !== SEM_DADO)) return linha;
    const j = linhaAntes.get(codigos[i]);
    if (j === undefined) return linha;
    const origem = anterior.v[j] ?? [];
    const nova = linhaVazia(slots);
    let achou = false;
    for (let s = 0; s < slots; s++) {
      const o = s + desloc;
      if (o >= 0 && o < origem.length && origem[o] !== SEM_DADO) {
        nova[s] = origem[o];
        achou = true;
      }
    }
    if (!achou) return linha;
    herdadas++;
    return nova;
  });
  return { herdadas, v };
}

/**
 * Quantas FATIAS têm ao menos uma estação reportando.
 *
 * É o sinal de que a grade tem história, e não de que a rede está boa: uma
 * grade recém-criada tem 1 ou 2 fatias ocupadas, enquanto uma preenchida tem
 * quase todas. Contar células em vez de fatias confundiria "sem história" com
 * "muitas estações fora do ar", e faria o preenchimento inicial disparar toda
 * vez que a rede do CEMADEN estivesse degradada.
 */
export function fatiasComDado(v, slots = SLOTS) {
  let n = 0;
  for (let s = 0; s < slots; s++) {
    if (v.some((linha) => linha?.[s] !== undefined && linha[s] !== SEM_DADO)) n++;
  }
  return n;
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
