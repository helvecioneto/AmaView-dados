/**
 * Estações de radiossonda da Amazônia Legal.
 *
 * Lista fixa, e não baixada: são 15 e mudam de década em década. Extraída da
 * lista de estações do IGRA/NOAA, filtrada pela caixa da Amazônia Legal e
 * conferida contra o Wyoming (as 6 testadas responderam).
 *
 * `wmo` é o identificador aceito pelo Wyoming; `icao` fica de reserva, porque
 * o endpoint aceita os dois.
 */

export const ESTACOES_SONDAGEM = [
  { wmo: 82022, icao: 'SBBV', nome: 'Boa Vista', uf: 'RR', lat: 2.849, lon: -60.6943 },
  { wmo: 82026, icao: 'SBTS', nome: 'Tiriós', uf: 'PA', lat: 2.22, lon: -55.93 },
  { wmo: 82099, icao: 'SBMQ', nome: 'Macapá', uf: 'AP', lat: 0.05, lon: -51.0667 },
  { wmo: 82107, icao: 'SBUA', nome: 'São Gabriel da Cachoeira', uf: 'AM', lat: -0.1167, lon: -66.9667 },
  { wmo: 82193, icao: 'SBBE', nome: 'Belém', uf: 'PA', lat: -1.3833, lon: -48.4833 },
  { wmo: 82244, icao: 'SBSN', nome: 'Santarém', uf: 'PA', lat: -2.4333, lon: -54.7167 },
  { wmo: 82332, icao: 'SBMN', nome: 'Manaus', uf: 'AM', lat: -3.15, lon: -59.9833 },
  { wmo: 82411, icao: 'SBTT', nome: 'Tabatinga', uf: 'AM', lat: -4.25, lon: -69.93 },
  { wmo: 82532, icao: 'SBMY', nome: 'Manicoré', uf: 'AM', lat: -5.8167, lon: -61.2833 },
  { wmo: 82705, icao: 'SBCZ', nome: 'Cruzeiro do Sul', uf: 'AC', lat: -7.5833, lon: -72.7667 },
  { wmo: 82824, icao: 'SBPV', nome: 'Porto Velho', uf: 'RO', lat: -8.7667, lon: -63.9167 },
  { wmo: 82917, icao: 'SBRB', nome: 'Rio Branco', uf: 'AC', lat: -9.87, lon: -67.9 },
  { wmo: 82965, icao: 'SBAT', nome: 'Alta Floresta', uf: 'MT', lat: -9.8667, lon: -56.1 },
  { wmo: 83208, icao: 'SBVH', nome: 'Vilhena', uf: 'RO', lat: -12.7, lon: -60.1 },
];

/**
 * Níveis padrão da meteorologia, em hPa: entram sempre que existem.
 */
export const NIVEIS_PADRAO = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100];

/** O gráfico (Skew-T Log-P) vai da superfície a 100 hPa: acima disso nada é publicado. */
export const P_TOPO = 100;
/** Teto de níveis por perfil. O bruto tem ~270 até 100 hPa. */
export const MAX_NIVEIS = 80;

/** Distância mínima (hPa) do último nível mantido, conforme a altura. */
export function passoEm(p) {
  if (p > 700) return 10;
  if (p > 300) return 15;
  return 10;
}

/** Quanto a temperatura e o orvalho precisam ter mudado para uma virada de tendência contar (°C). */
const VIRADA_T = 0.5;
const VIRADA_D = 2;
/** Salto da depressão do orvalho (T−Td) entre níveis vizinhos que marca camada nova (°C). */
const QUEBRA_UMIDADE = 8;

/** O nível `i` é um extremo local de `campo` (a curva vira ali)? */
function vira(niveis, i, campo) {
  const a = niveis[i - 1]?.[campo];
  const b = niveis[i]?.[campo];
  const c = niveis[i + 1]?.[campo];
  if (a == null || b == null || c == null) return false;
  return (b - a) * (c - b) < 0;
}

/**
 * Afina o perfil para o Skew-T: até `max` níveis entre a superfície e 100 hPa.
 *
 * O bruto tem centenas de níveis; o gráfico tem algumas centenas de pixels.
 * Mas 12–24 níveis (a primeira versão) não bastam para as áreas de CAPE e
 * CIN: a parcela cruza o ambiente entre dois níveis, e com eles a 100 hPa um
 * do outro a área desenhada era outra.
 *
 * Entram sempre: a superfície (o primeiro nível), os padrões que existem e o
 * último nível até 100 hPa. Entre eles, um nível entra quando
 * - se afastou do último mantido por `passoEm(p)` hPa; ou
 * - a temperatura (ou o orvalho) vira ali — base e topo de inversão — e já
 *   mudou `VIRADA_T` (`VIRADA_D`) °C desde o último mantido; ou
 * - a depressão do orvalho salta mais de 8 °C de um nível para o outro.
 *
 * Passando de `max`, o passo e os limiares das viradas crescem juntos até
 * caber. Nada é interpolado: todo nível publicado foi medido.
 */
export function reduzirNiveis(niveis, padroes = NIVEIS_PADRAO, max = MAX_NIVEIS) {
  if (!niveis?.length) return [];
  // A superfície fica mesmo numa estação (hipotética) acima de 100 hPa.
  const ate = niveis.filter((n, i) => i === 0 || n.p >= P_TOPO);

  const fixos = new Set([0, ate.length - 1]);
  for (const alvo of padroes) {
    let melhor = -1;
    let dist = Infinity;
    ate.forEach((n, i) => {
      const d = Math.abs(n.p - alvo);
      if (d < dist && d <= 15) {
        dist = d;
        melhor = i;
      }
    });
    if (melhor >= 0) fixos.add(melhor);
  }
  // Quebras de umidade: camada seca ou úmida nova.
  for (let i = 1; i < ate.length; i++) {
    const a = ate[i - 1];
    const b = ate[i];
    if (a.t === null || a.d === null || b.t === null || b.d === null) continue;
    if (Math.abs(b.t - b.d - (a.t - a.d)) > QUEBRA_UMIDADE) fixos.add(i);
  }

  const escolher = (folga) => {
    const out = [];
    let ultimo = null;
    ate.forEach((n, i) => {
      const entra =
        fixos.has(i) ||
        ultimo === null ||
        ultimo.p - n.p >= passoEm(n.p) * folga ||
        (vira(ate, i, 't') && Math.abs(n.t - (ultimo.t ?? n.t)) >= VIRADA_T * folga) ||
        (vira(ate, i, 'd') && Math.abs(n.d - (ultimo.d ?? n.d)) >= VIRADA_D * folga);
      if (!entra) return;
      out.push(n);
      ultimo = n;
    });
    return out;
  };

  let folga = 1;
  let out = escolher(folga);
  while (out.length > max && folga < 8) {
    folga *= 1.15;
    out = escolher(folga);
  }
  return out;
}
