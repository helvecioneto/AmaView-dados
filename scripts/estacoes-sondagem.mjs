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
 * Níveis de pressão guardados, em hPa.
 *
 * O perfil bruto tem centenas de níveis; para um gráfico de algumas centenas
 * de pixels, guardar todos é desperdício de banda de quem abre o app. Estes
 * são os obrigatórios da meteorologia — o suficiente para a forma das duas
 * curvas — e o parser ainda mantém os níveis significativos de umidade.
 */
export const NIVEIS_PADRAO = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100];

/**
 * Reduz o perfil aos níveis que importam.
 *
 * Mantém: o nível de superfície (o primeiro), os padrões mais próximos, e os
 * níveis em que a umidade muda bruscamente — que é onde está a informação
 * sobre camadas secas e úmidas, justamente o que as duas curvas mostram.
 */
export function reduzirNiveis(niveis, padroes = NIVEIS_PADRAO) {
  if (!niveis?.length) return [];
  const escolhidos = new Set();

  // Superfície sempre entra: é a base de tudo que se lê no perfil.
  escolhidos.add(0);

  for (const alvo of padroes) {
    let melhor = -1;
    let dist = Infinity;
    niveis.forEach((n, i) => {
      const d = Math.abs(n.p - alvo);
      if (d < dist && d <= 15) {
        dist = d;
        melhor = i;
      }
    });
    if (melhor >= 0) escolhidos.add(melhor);
  }

  // Quebras de umidade: onde a diferença T−Td muda mais de 8 °C de um nível
  // para o outro, há uma camada nova, e omiti-la achataria o perfil.
  for (let i = 1; i < niveis.length; i++) {
    const a = niveis[i - 1];
    const b = niveis[i];
    if (a.t === null || a.d === null || b.t === null || b.d === null) continue;
    if (Math.abs(b.t - b.d - (a.t - a.d)) > 8) escolhidos.add(i);
  }

  return [...escolhidos].sort((x, y) => x - y).map((i) => niveis[i]);
}
