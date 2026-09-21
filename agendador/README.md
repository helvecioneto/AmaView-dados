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

## Segurança

- O token fica em `/etc/amaview/token`, modo **600**, lido só pelo root.
- Nunca entra em argumento de linha de comando: argumentos de processo são
  visíveis para qualquer usuário da máquina.
- A permissão do PAT é a mínima que funciona — *Actions: Read and write* num
  repositório só. Esse token não lê código privado nem publica nada; ele só
  aperta este botão.
- Se vazar, revogue em <https://github.com/settings/tokens?type=beta>. O pior
  que alguém faz com ele é disparar ciclos de dados públicos.

