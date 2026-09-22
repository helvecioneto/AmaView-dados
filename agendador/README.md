# Agendador externo

O agendamento do GitHub Actions **não é confiável**. Medido neste repositório:
pedindo um ciclo a cada 15 minutos, o intervalo real entre disparos foi de
**135 minutos em média** (mediana 153). Um pedido anterior de 10 minutos não
disparou uma única vez em 1h42.

Isto aqui é o conserto: uma máquina com relógio de verdade aperta o botão do
`workflow_dispatch` na hora certa. Um timer por workflow:

| Unidade | Quando | Workflow |
|---|---|---|
| `amaview-chuva.timer` | a cada 15 min | `chuva.yml` |
| `amaview-sondagem.timer` | 07:22, 10:22, 19:22 e 22:22 UTC | `sondagem.yml` |

A sondagem entrou depois: dependia só do `schedule` do GitHub e, em
21/09/2026, passou 12 h sem atualizar.

O `schedule` do workflow continua ligado como **rede de segurança** — se esta
máquina cair, os disparos irregulares do GitHub ainda mantêm o dado vivo, e o
AmaView avisa quando ele envelhece.

## Por que systemd, e não cron

A máquina nem tem `cron` instalada, mas isso é detalhe. O timer do systemd é
melhor para este trabalho:

- `Persistent=true` **recupera o disparo perdido** depois de reinício ou queda
  de rede, que é exatamente o que o GitHub não faz;
- o log vai para o journald (`journalctl -u amaview-chuva -u amaview-sondagem`),
  com código de saída de cada execução;
- `RandomizedDelaySec` desloca do minuto cheio, que é quando todo mundo agenda.

## Uso

Tudo pelo `amaview`, que fica em `~` e em `/usr/local/bin`:

| Comando | O que faz |
|---|---|
| `amaview` | situação: timers, token, últimas execuções e frescor do dado |
| `amaview instalar` | clona/atualiza, instala as unidades e liga os timers |
| `amaview token` | grava o token (pede na tela, sem eco) |
| `amaview disparar [workflow]` | dispara um ciclo agora (`chuva.yml`, ou `sondagem.yml`) |
| `amaview logs [n]` | últimas execuções |
| `amaview parar` | desliga os timers |
| `amaview remover` | desinstala (mantém o token) |

### Primeira vez

```sh
~/amaview instalar
~/amaview token      # cola o PAT; ele testa e liga os timers sozinho
```

O PAT vem de <https://github.com/settings/tokens?type=beta>, com acesso só a
este repositório e permissão **Actions: Read and write**.

### Atualizar

```sh
amaview instalar     # git pull + reinstala as unidades e o próprio script, e religa os timers
```

## Blocos do GOES-19

A mesma máquina corta os JPEGs do setor NSA do [NOAA/STAR](https://www.star.nesdis.noaa.gov/goes/sector.php?sat=G19&sector=nsa)
em blocos de 512 px (larguras 3600 e 7200), para o AmaView baixar e decodificar
só a parte visível da imagem. No zoom 6, por exemplo, a tela mostra 0,8% do
quadro de 7200 px: ~0,4 MB em blocos contra 7,3 MB do arquivo inteiro.

- **Sem recompressão.** O corte é feito nos coeficientes do JPEG (`tjTransform`
  da TurboJPEG, o mesmo do `jpegtran -crop`): os pixels são os do arquivo da
  NOAA. Os 135 blocos de um quadro de 7200 px saem numa chamada (~0,2 s).
- **Sobra de 16 px** do vizinho em cada lado do bloco (múltiplo do MCU, então
  ainda sem perda). O AmaView desenha só o miolo de 512 px; a suavização ao
  ampliar lê a sobra e não aparecem emendas. Custa ~13% a mais de bytes.
- **Pré-corte.** A cada minuto, um HEAD no arquivo de 450 px de cada horário
  que falta nas últimas 6 h (grade de 10 min, sem baixar a listagem de 1,1 MB)
  diz se o STAR o publicou; publicado, todos os produtos dele são cortados na
  hora, e o produto que ainda não chegou é tentado de novo a cada minuto. O
  `latest.jpg` do STAR não serve de sinal: em 22/09/2026 o GOES-19 parou das
  09:50 às 13h (manutenção no solo da NOAA) e voltou soltando os quadros das
  12:00–12:20 sem mexer nele. O resto das últimas 48 h é preenchido do mais
  novo para o mais velho. Em regime: ~240 MB baixados do STAR a cada 10 min,
  ~85 GB em disco.
- **`/blocos/v1/ultimos`**: o horário mais recente já cortado de cada produto.
  O AmaView pergunta a cada minuto e recarrega as listagens quando há quadro
  novo — sem depender do `latest.jpg`.
- **Sob demanda.** O nginx serve o bloco do disco; se ainda não existe (horário
  fora da grade, pré-corte atrasado), o pedido cai no `blocos.py`, que corta o
  quadro na hora e devolve o bloco (~2 s, quase tudo download).
- **Se esta máquina cair**, o AmaView volta sozinho ao JPEG inteiro do STAR.

| Peça | Onde |
|---|---|
| `blocos/blocos.py` | serviço (só local, porta 8090), pré-corte e limpeza (> 49 h) |
| `blocos/amaview-blocos.service` | unidade systemd (usuário `amaview-blocos`, `/var/cache/amaview-blocos`) |
| `blocos/nginx-*.conf` | site do nginx: disco primeiro, `@blocos` no que falta; CORS e cache imutável |

URL: `/blocos/v1/nsa/{produto}/{AAAADDDHHMM}/{largura}/{linha}_{coluna}.jpg`
(dia juliano, UTC, como nos arquivos do STAR). Saúde: `/blocos/saude`.

O `amaview instalar` instala e atualiza tudo; `amaview` mostra a situação. O
HTTPS (o AmaView é servido por HTTPS e o navegador recusa blocos por HTTP) sai
do Let's Encrypt para `147-15-84-134.sslip.io`, uma vez, com
`amaview certificado`; o `certbot.timer` renova.

Testes (sem rede): `python3 -m unittest discover -s agendador/blocos`.

## Segurança

- O token fica em `/etc/amaview/token`, modo **600**, lido só pelo root.
- Nunca entra em argumento de linha de comando: argumentos de processo são
  visíveis para qualquer usuário da máquina.
- A permissão do PAT é a mínima que funciona — *Actions: Read and write* num
  repositório só. Esse token não lê código privado nem publica nada; ele só
  aperta este botão.
- Se vazar, revogue em <https://github.com/settings/tokens?type=beta>. O pior
  que alguém faz com ele é disparar ciclos de dados públicos.

