# Agendador externo

O agendamento do GitHub Actions **não é confiável**. Medido neste repositório:
pedindo um ciclo a cada 15 minutos, o intervalo real entre disparos foi de
**135 minutos em média** (mediana 153). Um pedido anterior de 10 minutos não
disparou uma única vez em 1h42.

Isto aqui é o conserto: uma máquina com relógio de verdade aperta o botão do
`workflow_dispatch` na hora certa.

O `schedule` do workflow continua ligado como **rede de segurança** — se esta
máquina cair, os disparos irregulares do GitHub ainda mantêm o dado vivo, e o
AmaView avisa quando ele envelhece.

## Por que systemd, e não cron

A máquina nem tem `cron` instalada, mas isso é detalhe. O timer do systemd é
melhor para este trabalho:

- `Persistent=true` **recupera o disparo perdido** depois de reinício ou queda
  de rede, que é exatamente o que o GitHub não faz;
- o log vai para o journald (`journalctl -u amaview-chuva`), com código de
  saída de cada execução;
- `RandomizedDelaySec` desloca do minuto cheio, que é quando todo mundo agenda.

## Instalação

```sh
sudo mkdir -p /opt/amaview /etc/amaview
sudo git clone https://github.com/helvecioneto/AmaView-dados /opt/amaview \
  || sudo git -C /opt/amaview pull

# O token: crie um fine-grained PAT em
# https://github.com/settings/tokens?type=beta
# com acesso SÓ a este repositório e permissão "Actions: Read and write".
sudo install -m 600 /dev/null /etc/amaview/token
sudo nano /etc/amaview/token        # cole o token, uma linha, sem aspas

sudo cp /opt/amaview/agendador/amaview-chuva.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now amaview-chuva.timer
```

## Conferir

```sh
systemctl list-timers amaview-chuva.timer     # próximo disparo
journalctl -u amaview-chuva -n 20 --no-pager  # últimas execuções
sudo /opt/amaview/agendador/disparar.py chuva.yml   # disparo manual
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

## Atualizar

```sh
sudo git -C /opt/amaview pull
sudo cp /opt/amaview/agendador/amaview-chuva.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart amaview-chuva.timer
```
