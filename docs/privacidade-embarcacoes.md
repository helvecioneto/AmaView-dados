# Embarcações (AIS) — privacidade, licença e como pedir remoção

Este repositório republica posições de embarcações recebidas por **AIS**
(*Automatic Identification System*) na Amazônia Legal, para a camada "Barcos"
do [AmaView](https://helvecioneto.github.io/AmaView/).

---

## Como pedir a remoção de uma embarcação

**Escreva para o mantenedor do repositório** (ou abra uma
[issue](https://github.com/helvecioneto/AmaView-dados/issues)) informando o
**MMSI** da embarcação e a sua relação com ela.

O MMSI entra em [`dados/suprimidos.json`](../dados/suprimidos.json) e, a partir
do ciclo seguinte (até 15 minutos), a embarcação deixa de ser publicada aqui —
posição, nome, ficha e trilha. O pedido vale para qualquer embarcação, inclusive
comercial, e não precisa de justificativa.

O que este repositório **não** consegue fazer é tirar a embarcação do ar: o AIS
é uma transmissão de rádio aberta, e outras dezenas de sites a recebem. A
remoção aqui é a remoção desta republicação.

---

## O que é publicado, e o que não é

A regra não é o tamanho do barco: é **quem é obrigado a transmitir**.

| Frota | O que vai ao ar |
|---|---|
| **Classe A** (porte obrigatório por convenção — carga, tanque, passageiros, rebocadores, comboios, serviço) | Tudo: MMSI, nome, tipo, dimensões, destino declarado, ETA e trilha de 24 h |
| **Classe B** sem tipo declarado e com menos de 24 m | Só um ponto no mapa: **sem MMSI, sem nome, sem trilha**, e sem guardar nada entre ciclos |
| **Pesca** com menos de 24 m (ou sem porte conhecido) | Idem: ponto anônimo |
| **Vela (tipo 36) e recreio (tipo 37)** | **Nada.** Não são publicados, em nenhuma classe e em nenhum tamanho |
| Qualquer uma, a pedido | Nada (lista de supressão) |

O corte é feito no **pipeline**, antes de escrever o arquivo: o que não é
publicado não fica guardado em lugar nenhum. E o cadastro esquece qualquer MMSI
que não seja ouvido há 30 dias.

O raciocínio: uma embarcação comercial transmite porque a lei manda, e o dado é
institucional — quem opera o comboio de balsas que sobe o Amazonas não tem
expectativa de privacidade sobre a rota comercial. Um barco pequeno com
transponder voluntário é outra coisa: o MMSI pode levar a uma pessoa
identificável, e um rastro de 24 h é um perfil de deslocamento. É a mesma linha
que a Noruega adota nos seus dados abertos de AIS.

---

## Licença e crédito

Os dados vêm do [aiscast / Open Waters](https://openwaters.io/ais/)
(`ais.openwaters.io`), que agrega fontes com licenças **diferentes**. O aiscast
é explícito ao dizer que as licenças **não se fundem**: cada evento carrega a
sua fonte, e o crédito é por fonte.

Por isso os arquivos publicados trazem `attribution` como **objeto**, uma
entrada por fonte, e cada embarcação carrega o campo `f` com a fonte dela.
Quem consumir estes arquivos deve exibir o crédito de cada fonte presente,
sem concatenar. Hoje as fontes na Amazônia são:

```
aishub    → Open Waters AIS (https://openwaters.io/ais/). AISHub (https://www.aishub.net)
aisstream → Open Waters AIS (https://openwaters.io/ais/). aisstream.io
```

O pipeline só aceita essas duas (`FONTES_ACEITAS` em `scripts/frota.mjs`).
Qualquer outra é descartada — inclusive receptores voluntários (`udp:*`), cujo
agregado o aiscast publica sob **ODbL**, que traria obrigação de
*share-alike* sobre o JSON derivado. Se um dia fizer sentido incluí-los, é
preciso acrescentar a licença ODbL ao diretório `navios/`.

O código deste repositório é MIT. Os **dados** não são cobertos por essa
licença: seguem os termos de cada fonte.

---

## Limites de uso

O AmaView não serve para navegação. As posições são as recebidas, com atraso de
até um ciclo (15 min) e cobertura irregular, e o destino e a ETA são
**declarados pela própria embarcação** — digitados à mão na ponte, muitas vezes
desatualizados.

---

## Pendências do mantenedor

Duas coisas que dependem de uma decisão humana:

1. ~~**Token pessoal da openwaters**~~ — **feito em 21/09/2026.** Está no secret
   `OPENWATERS_TOKEN` deste repositório, com nível `personal`: 400 graus² de
   área (contra 100 do anônimo), 50 mensagens por segundo e limites contados
   por token em vez de por endereço IP — o que importa porque o runner do
   GitHub Actions tem IP compartilhado.

   Não há formulário nem conta: o token sai de um POST em `/v1/keys` com uma
   chave pública Ed25519, e não expira (`exp: 0`).

   ```sh
   node -e '
     const { generateKeyPairSync } = require("node:crypto");
     const { publicKey } = generateKeyPairSync("ed25519");
     const der = publicKey.export({ type: "spki", format: "der" });
     const pubkey = der.subarray(der.length - 32).toString("base64url");
     fetch("https://ais.openwaters.io/v1/keys", {
       method: "POST",
       headers: { "Content-Type": "application/json" },
       body: JSON.stringify({ pubkey }),
     }).then((r) => r.json()).then((j) => console.log(j.token));
   '
   ```

   Para revogar, escreva para hello@openwaters.io. Gerar outro token é só
   repetir o comando — eles não são vinculados a nenhuma identidade.

2. **E-mail para hello@openwaters.io** — o `docs/limits.md` deles pede esse
   contato em caso de dúvida de tier. Rascunho:

   > Olá. Sou pesquisador e mantenho o AmaView
   > (https://helvecioneto.github.io/AmaView/), um portal público e sem fins
   > lucrativos que mostra a Amazônia Legal quase em tempo real. Acabei de
   > acrescentar uma camada de embarcações que consome o aiscast.
   >
   > O consumo é fixo e não cresce com o número de visitantes: um ciclo a cada
   > 15 minutos faz 4 requisições a `/v1/vessels` (quatro caixas somando
   > ~46 graus²) e mantém um WebSocket em `/v1/stream` por até 90 s. O
   > navegador dos visitantes nunca fala com vocês — republicamos um JSON
   > derivado no GitHub Pages, com o `attribution` de cada fonte exibido na
   > interface, no popup e nos vídeos exportados.
   >
   > Duas perguntas: (1) este uso cabe no tier Pessoal? (2) há alguma restrição
   > para re-redistribuir os eventos com `source: aisstream`, já que a tabela
   > de licenças indica que os termos deles não são publicados?
