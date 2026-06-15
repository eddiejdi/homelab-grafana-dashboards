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
3. Abra PR — a action valida o JSON antes do merge
4. Após merge em `main`, o deploy é automático

## Servidor

- Host: `192.168.15.2`
- Provisioning path: `/home/homelab/monitoring/grafana/provisioning/dashboards/`
- Grafana: `https://grafana.rpa4all.com`
