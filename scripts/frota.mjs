/**
 * Funções puras do ciclo das embarcações: classificação de privacidade,
 * acúmulo de trilha, poda do cadastro e grade de escuta.
 *
 * Tudo que decide O QUE É PUBLICADO mora aqui, sem rede, e é testado em
 * `navios.test.mjs`. A separação importa mais nesta camada que nas outras:
 * um erro aqui não deixa um gráfico feio, publica a posição de alguém que
 * não deveria estar publicada.
 */

/** Fontes que o pipeline aceita re-servir. Qualquer outra é descartada. */
export const FONTES_ACEITAS = ['aishub', 'aisstream'];

/** Janela da trilha publicada. */
export const TRILHA_MS = 24 * 60 * 60 * 1000;

/** Depois disso um MMSI sai do cadastro: não foi mais ouvido, não é mais notícia. */
export const CADASTRO_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/** Células da grade de escuta e por quanto tempo uma célula "lembra". */
export const CELULA_GRAU = 0.25;
export const ESCUTA_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** Dois pontos mais próximos que isso são a mesma parada — não viram amostra nova. */
export const DEDUPE_M = 100;

/** Acima disso, dois pontos da trilha não se ligam: o barco sumiu no meio. */
export const LACUNA_MS = 30 * 60 * 1000;

/** Comprimento a partir do qual um barco de pesca é frota comercial. */
export const PESCA_COMERCIAL_M = 24;

// ---------------------------------------------------------------------------
// Privacidade: o que pode ser publicado, e como
// ---------------------------------------------------------------------------

/**
 * Mensagens de posição e de estático da **Classe B**. Tudo o mais que o AIS
 * manda como posição é Classe A.
 */
const MSG_CLASSE_B = /ClassB|^StaticDataReport$/i;

export function ehClasseB(msgType) {
  return typeof msgType === 'string' && MSG_CLASSE_B.test(msgType);
}

/**
 * O que fazer com uma embarcação. O critério é **quem é obrigado a
 * transmitir**, não o tamanho do barco.
 *
 * - `completo`: nome, MMSI, ficha e trilha.
 * - `anonimo`: só ponto, família e rumo. Sem MMSI, sem nome, sem trilha, sem
 *   persistir entre ciclos.
 * - `omitir`: não entra no arquivo.
 *
 * A **classe do transponder** é o discriminador, e não o tipo declarado:
 *
 * - **Classe A** é obrigatória por convenção (SOLAS e, no Brasil, a NORMAM da
 *   Marinha) para a frota comercial. Quem transmite em Classe A o faz por
 *   dever legal, e o dado é institucional. Medido em 20/09/2026: 77 das 78
 *   embarcações da Amazônia são Classe A — inclusive BERTOLINI VI e os JOSE
 *   GUILHERME, comboios de carga que declaram tipo 0.
 * - **Classe B** é o transponder barato e voluntário do barco pequeno. Aí sim
 *   há chance de a embarcação estar ligada a uma pessoa identificável.
 *
 * A primeira versão desta função usava só o tipo declarado, e com isso
 * anonimizava 35 das 40 embarcações brasileiras — apagando justamente a frota
 * regional que é o assunto da camada — porque tipo 0 ("não declarado") é o
 * padrão de fábrica de meio mundo. O tipo diz o que o barco é; a classe diz se
 * ele tinha escolha de estar ali.
 */
export function classificar(tipo, comprimentoM, msgType = null) {
  const t = Number.isFinite(tipo) ? Math.trunc(tipo) : 0;
  const classeB = ehClasseB(msgType);
  const grande = Number.isFinite(comprimentoM) && comprimentoM >= PESCA_COMERCIAL_M;

  // Vela e recreio: fora, sempre, em qualquer classe.
  if (t === 36 || t === 37) return 'omitir';

  // Pesca: comercial só com porte de 24 m ou mais.
  if (t === 30) return grande ? 'completo' : 'anonimo';

  // Tipos comerciais declarados: reboque, comboio, dragagem, militar, serviço,
  // e as dezenas 2 e 4–9 (efeito solo, rápidas, passageiros, carga, tanque).
  const dezena = Math.trunc(t / 10);
  const tipoComercial = (t >= 31 && t <= 35) || dezena === 2 || (dezena >= 4 && dezena <= 9);
  if (tipoComercial) return 'completo';

  // Tipo não declarado: decide a classe do transponder.
  if (classeB && !grande) return 'anonimo';
  return 'completo';
}

/** Comprimento total a partir das dimensões do AIS (proa + popa). */
export function comprimentoDe(dim) {
  if (!dim) return null;
  const a = Number(dim.A);
  const b = Number(dim.B);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  const c = a + b;
  return c > 0 ? c : null;
}

/** Boca (bombordo + boreste). */
export function boca(dim) {
  if (!dim) return null;
  const c = Number(dim.C);
  const d = Number(dim.D);
  if (!Number.isFinite(c) || !Number.isFinite(d)) return null;
  const l = c + d;
  return l > 0 ? l : null;
}

// ---------------------------------------------------------------------------
// Trilha
// ---------------------------------------------------------------------------

/** Metros entre dois pontos (equirretangular — sobra precisão nesta escala). */
export function distanciaM(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const rad = Math.PI / 180;
  const x = (lon2 - lon1) * rad * Math.cos(((lat1 + lat2) / 2) * rad);
  const y = (lat2 - lat1) * rad;
  return Math.sqrt(x * x + y * y) * R;
}

export function arredondar(n, casas = 4) {
  const f = 10 ** casas;
  return Math.round(n * f) / f;
}

/**
 * Acrescenta a posição de agora à trilha e poda o que passou de 24 h.
 *
 * A amostra carrega o INSTANTE ABSOLUTO, não um índice de grade: a janela
 * desliza a cada ciclo, e uma trilha indexada por slot andaria para trás
 * sozinha a cada publicação.
 *
 * Um ponto que mal se mexeu não vira amostra nova — só atualiza o instante da
 * última. Sem isso, um navio atracado gastaria 96 amostras idênticas.
 */
export function mesclarTrilha(anterior, ponto, agora = Date.now()) {
  const pontos = Array.isArray(anterior) ? anterior.filter(valida) : [];
  const corte = agora - TRILHA_MS;
  const vivos = pontos.filter((p) => p[0] >= corte).sort((a, b) => a[0] - b[0]);

  if (!ponto || !Number.isFinite(ponto.lat) || !Number.isFinite(ponto.lon)) return vivos;

  const nova = [
    ponto.ms,
    arredondar(ponto.lat),
    arredondar(ponto.lon),
    Number.isFinite(ponto.sog) ? Math.round(ponto.sog * 10) / 10 : null,
  ];

  const ultima = vivos[vivos.length - 1];
  if (ultima) {
    if (nova[0] <= ultima[0]) return vivos; // amostra repetida ou fora de ordem
    if (distanciaM(ultima[1], ultima[2], nova[1], nova[2]) < DEDUPE_M) {
      // Mesma posição: move o instante em vez de empilhar um ponto igual.
      vivos[vivos.length - 1] = [nova[0], ultima[1], ultima[2], nova[3]];
      return vivos;
    }
  }
  vivos.push(nova);
  return vivos;
}

function valida(p) {
  return Array.isArray(p) && p.length >= 3 && Number.isFinite(p[0]) && Number.isFinite(p[1]) && Number.isFinite(p[2]);
}

// ---------------------------------------------------------------------------
// Cadastro (o estático acumulado)
// ---------------------------------------------------------------------------

/**
 * Funde o que chegou agora no que já sabíamos, campo a campo.
 *
 * Campo novo só substitui o antigo se vier preenchido: um ciclo em que o
 * navio transmitiu só a posição não pode apagar o destino que ele declarou
 * meia hora atrás. E cada bloco guarda `d`, o instante em que foi declarado —
 * é o que permite à interface dizer "destino declarado em 11/9" em vez de
 * apresentar dado de nove dias como se fosse de agora.
 */
export function mesclarEstatico(anterior, novo, agora = Date.now()) {
  const base = anterior && typeof anterior === 'object' ? { ...anterior } : {};
  let mudou = false;

  for (const [chave, valor] of Object.entries(novo ?? {})) {
    if (valor === null || valor === undefined || valor === '' || valor === 0) continue;
    if (base[chave] !== valor) mudou = true;
    base[chave] = valor;
  }

  if (mudou || base.d === undefined) base.d = agora;
  return base;
}

/** Tira do cadastro quem não é ouvido há 30 dias. */
export function podarCadastro(cadastro, agora = Date.now()) {
  const corte = agora - CADASTRO_TTL_MS;
  const saida = {};
  for (const [mmsi, v] of Object.entries(cadastro ?? {})) {
    const visto = Number(v?.v ?? v?.d);
    if (Number.isFinite(visto) && visto >= corte) saida[mmsi] = v;
  }
  return saida;
}

// ---------------------------------------------------------------------------
// Grade de escuta — onde o AIS de fato é ouvido
// ---------------------------------------------------------------------------

/**
 * A pergunta que esta grade responde é "rio vazio ou sem recepção?".
 *
 * O AIS terrestre só alcança perto do receptor. Sem isso desenhado, um trecho
 * de rio sem ponto no mapa se lê como "não há navegação", que é falso — e é o
 * erro de leitura mais provável desta camada inteira.
 *
 * Cada célula guarda o instante da última recepção. Sete dias de memória
 * transformam recepções esparsas num contorno estável.
 */
export function chaveCelula(lat, lon) {
  const i = Math.floor(lat / CELULA_GRAU);
  const j = Math.floor(lon / CELULA_GRAU);
  return `${i},${j}`;
}

export function celulaParaCaixa(chave) {
  const [i, j] = chave.split(',').map(Number);
  return [
    arredondar(i * CELULA_GRAU, 3),
    arredondar(j * CELULA_GRAU, 3),
    arredondar((i + 1) * CELULA_GRAU, 3),
    arredondar((j + 1) * CELULA_GRAU, 3),
  ];
}

export function mesclarEscuta(anterior, posicoes, agora = Date.now()) {
  const corte = agora - ESCUTA_TTL_MS;
  const celulas = {};
  for (const [k, t] of Object.entries(anterior ?? {})) {
    const ms = Number(t);
    if (Number.isFinite(ms) && ms >= corte) celulas[k] = ms;
  }
  for (const p of posicoes ?? []) {
    if (!Number.isFinite(p?.lat) || !Number.isFinite(p?.lon)) continue;
    celulas[chaveCelula(p.lat, p.lon)] = agora;
  }
  return celulas;
}

// ---------------------------------------------------------------------------
// Atribuição
// ---------------------------------------------------------------------------

/**
 * Junta os objetos `attribution` de várias respostas SEM concatenar os textos.
 *
 * O aiscast é explícito: "licenses are never merged". Cada fonte tem termos
 * próprios, então o crédito é por fonte e a interface mostra só as fontes
 * presentes no que está na tela.
 */
export function juntarAtribuicao(...objetos) {
  const saida = {};
  for (const o of objetos) {
    if (!o || typeof o !== 'object') continue;
    for (const [fonte, texto] of Object.entries(o)) {
      if (typeof texto === 'string' && texto.trim()) saida[fonte] = texto.trim();
    }
  }
  return saida;
}
