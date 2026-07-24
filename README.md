# homelab-grafana-dashboards

Repositório único para todos os dashboards Grafana do homelab Eddie.

Deploy automático via GitHub Actions ao fazer push em `main`.

## Estrutura

```
dashboards/
├── trading/        # BTC, Clear B3, Multi-Coin, relatórios
├── infrastructure/ # NAS, Squid, DHCP, Akash, Central
├── storage/        # Tape LTO, Storj
├── agents/         # Neural Network, Banking, WhatsApp, Copilot, Tunnels
└── security/       # Authentik, Secrets Agent
provisioning/
└── dashboards.yml  # Config de provisionamento Grafana (subpastas por categoria)
```

## Deploy

| Evento | Ação |
|--------|------|
| Push em `main` (arquivos `dashboards/**` ou `provisioning/**`) | Deploy automático via self-hosted runner |
| `workflow_dispatch` | Deploy manual; `full_sync=true` remove arquivos deletados |
| Pull Request | Valida JSON + campos obrigatórios |

O Grafana recarrega os dashboards automaticamente a cada 30s — sem restart necessário.

## Adicionando um dashboard

1. Exporte o JSON do Grafana (Share → Export → Save to file)
2. Coloque na categoria correta em `dashboards/<categoria>/nome.json`
3. Abra PR — a action valida o JSON, os campos obrigatórios e os uids antes do merge
4. Após merge em `main`, o deploy é automático

## uid é único — e o provider é compartilhado

O provider `eddie-dashboards` aponta para a raiz de
`/home/homelab/monitoring/grafana/provisioning/dashboards/` e **varre
recursivamente**. Se dois arquivos quaisquer sob esse path tiverem o mesmo `uid`,
o Grafana emite

```
dashboards provisioning provider has no database write permissions because of duplicates
```

e **congela o provisionamento de todos os dashboards** do provider. O sintoma é
traiçoeiro: os deploys continuam gravando em disco e a API continua servindo as
versões antigas, sem erro nenhum. Foi o que aconteceu entre 22/07 e 24/07 de 2026.

Duas regras:

- **Nunca deixe backups dentro do path do provider** — nada de `*.bak*` ou
  `*.disabled.*` ali. O destino é
  `/home/homelab/monitoring/grafana/dashboard_quarantine/`.
- **Este repo não é o único que grava no provider.** O `eddie-auto-dev` deploya na
  raiz do path (via `scripts/deploy_btc_trading_profiles.sh` e afins). Um
  dashboard só pode ter uma fonte de verdade.

O guard `scripts/check_dashboard_uids.py` roda nos dois momentos:

| Onde | Comando | O que pega |
|------|---------|-----------|
| PR (`validate.yml`) | `--repo dashboards` | uid duplicado dentro deste repo |
| Deploy (`deploy.yml`, antes do rsync) | `--repo … --provider-dir …` | colisão com o que já está no servidor, venha de onde vier |

Se o guard falhar, o deploy aborta **antes** de escrever qualquer coisa.

### Dashboards que pertencem ao eddie-auto-dev

Estes **não** vivem aqui — a fonte de verdade é o repo `eddie-auto-dev`, que os
deploya na raiz do path do provider junto com os exporters e queries de que
dependem:

| uid | Fonte |
|-----|-------|
| `btc-trading-monitor` | `eddie-auto-dev:grafana/dashboards/btc-trading-monitor.json` |
| `nas-rpa4all-omv` | `eddie-auto-dev:grafana/dashboards/nas-rpa4all-omv.json` |
| `tape-component-quality-v1` | `eddie-auto-dev:monitoring/grafana/provisioning/dashboards/tape-component-quality-v1.json` |

## Servidor

- Host: `192.168.15.2`
- Provisioning path: `/home/homelab/monitoring/grafana/provisioning/dashboards/`
- Quarentena de backups: `/home/homelab/monitoring/grafana/dashboard_quarantine/`
- Grafana: `https://grafana.rpa4all.com`
