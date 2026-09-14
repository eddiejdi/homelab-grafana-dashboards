# homelab-grafana-dashboards

Repositório único para todos os dashboards Grafana do homelab Eddie.

Deploy automático via GitHub Actions ao fazer push em `main`.

## Estrutura

```
dashboards/
├── trading/        # BTC, Clear B3, Multi-Coin, relatórios
├── infrastructure/ # NAS, Squid, DHCP, Akash, Central
├── storage/        # Tape LTO, Storj
├── agents/         # Neural Network, Banking, WhatsApp, Copilot, Tunnels, OpenCode
└── security/       # Authentik, Secrets Agent
provisioning/
└── dashboards.yml  # Config de provisionamento Grafana (subpastas por categoria)
scripts/            # Exporter Prometheus (uso das conexões do OpenCode)
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

## Dashboard: OpenCode — Conexões e Uso

Painel `dashboards/agents/opencode-connections-usage.json` mostra, por conexão
(OpenCode Zen, OpenCode Go, GitHub Copilot, xAI Grok, OpenRouter, OpenAI,
HuggingFace, Ollama local), o uso real (mensagens/custo/tokens), o limite e a
próxima data de renovação.

Os dados vêm de um exporter Prometheus que roda na workstation onde o opencode
grava o `opencode.db`:

### 1. Instalar o exporter na workstation

```bash
cp scripts/opencode_usage_exporter.py ~/homelab-grafana-dashboards/scripts/ 2>/dev/null || mkdir -p ~/homelab-grafana-dashboards/scripts && cp scripts/opencode_usage_exporter.py ~/homelab-grafana-dashboards/scripts/

# Testar direto
python3 scripts/opencode_usage_exporter.py --port 9998
curl http://127.0.0.1:9998/metrics
```

### 2. Configurar limites e renovação (opcional)

Copie `scripts/usage-limits.example.json` para `~/.config/opencode/usage-limits.json`
e preencha `limit`, `period` e `renewal` das conexões com limite fixo
(Copilot, Grok, OpenCode Go/Zen, ...). O OpenRouter é buscado dinamicamente
pela API (`limit`/`limit_reset`/custo diário/semanal/mensal).

### 3. systemd (opcional, rodar sempre)

```bash
cp scripts/opencode-usage-exporter.service.example ~/.config/systemd/user/opencode-usage-exporter.service
systemctl --user daemon-reload
systemctl --user enable --now opencode-usage-exporter
```

### 4. Adicionar o scrape no Prometheus do homelab

Adicione um job em `scrape_configs` apontando para a workstation
(IP atual: `192.168.15.137` via RJ45 / `192.168.15.114` via Wi-Fi):

```yaml
scrape_configs:
  - job_name: opencode-usage
    scrape_interval: 30s
    static_configs:
      - targets: ["192.168.15.137:9998"]
```

Após o push em `main`, o dashboard é deployado automaticamente na pasta
`Agents` do Grafana.
