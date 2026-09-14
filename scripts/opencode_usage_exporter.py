#!/usr/bin/env python3
"""
opencode_usage_exporter — Prometheus exporter para o uso das conexões do OpenCode.

Fonte de uso real: ~/.local/share/opencode/opencode.db (SQLite, modo read-only).
Fonte de limites/renovação:
  - OpenRouter (dinâmico): GET https://openrouter.ai/api/v1/auth/key
  - Demais conexões: config estático em ~/.config/opencode/usage-limits.json

Métricas expostas (todas com label connection):
  opencode_connection_connected          gauge  0/1 (conexão configurada no auth.json)
  opencode_connection_messages_total     counter nº de mensagens assistant por conexão
  opencode_connection_cost_total         counter custo em USD acumulado (soma do DB)
  opencode_connection_tokens_input_total counter
  opencode_connection_tokens_output_total counter
  opencode_connection_tokens_cache_read_total counter
  opencode_connection_messages_limit     gauge  limite de requests no período (-1 = ilimitado/desconhecido)
  opencode_connection_limit_remaining    gauge  restante no limite (-1 = N/A)
  opencode_connection_limit_period       gauge  0=daily 1=weekly 2=monthly 3=nenhum
  opencode_connection_cost_period        gauge  custo reportado pelo provider (labels connection,period)
  opencode_connection_renewal_epoch      gauge  unix epoch da próxima renovação (0 = sem info)
  opencode_connection_free_tier          gauge  0/1 provider é free tier
  opencode_usage_scrape_success          gauge  0/1 última coleta do DB OK
  opencode_usage_db_size_bytes           gauge  tamanho do opencode.db

Dependências: apenas stdlib. Servidor HTTP próprio em /metrics.

Uso:
  python3 opencode_usage_exporter.py [--port 9998]
Env:
  OPENCODE_USAGE_PORT        porta HTTP (default 9998)
  OPENCODE_USAGE_BIND        bind (default 0.0.0.0)
  OPENCODE_USAGE_DB          caminho do opencode.db (default ~/.local/share/opencode/opencode.db)
  OPENCODE_USAGE_AUTH        caminho do auth.json (default ~/.local/share/opencode/auth.json)
  OPENCODE_USAGE_CONFIG      caminho da config de limites (default ~/.config/opencode/usage-limits.json)
  OPENCODE_USAGE_REFRESH     intervalo em s p/ re-buscar APIs de provider (default 300)
  OPENCODE_USAGE_DISABLE_NET define 1 para desativar chamadas de rede (só DB + config)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# provider_id (campo providerID do opencode.db) -> nome da conexão exibida
DEFAULT_PROVIDER_MAP = {
    "opencode": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "github-copilot": "GitHub Copilot",
    "xai-oauth": "xAI Grok",
    "openrouter": "OpenRouter",
    "traycer-openrouter": "OpenRouter",
    "openai": "OpenAI",
    "huggingface": "HuggingFace",
    "ollama": "Ollama (local)",
    "ollama-gpu1": "Ollama (local)",
    "ollama-nas": "Ollama (local)",
}

PERIOD_CODE = {"daily": 0, "weekly": 1, "monthly": 2, "none": 3, "unknown": 3}


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


class ProviderFetcher:
    """Busca dinâmica de limites/renovação nas APIs dos providers."""

    OPENROUTER_URL = "https://openrouter.ai/api/v1/auth/key"

    def __init__(self, auth_path: str, refresh: int, disable_net: bool):
        self.auth_path = auth_path
        self.refresh = refresh
        self.disable_net = disable_net
        self.lock = threading.Lock()
        self.cache: dict = {}
        self.last_fetch: float = 0

    def _authorization(self) -> dict:
        try:
            with open(self.auth_path, encoding="utf-8") as f:
                auth = json.load(f)
        except Exception:
            return {}
        # Mapeia credencial amorfa do auth.json para os nomes de conexão usados aqui
        out = {}
        if isinstance(auth.get("openrouter"), dict):
            out["OpenRouter"] = auth["openrouter"].get("key")
        return out

    def _fetch_from_cache(self, conn: str) -> dict:
        if self.disable_net:
            return {}
        with self.lock:
            if time.time() - self.last_fetch < self.refresh and conn in self.cache:
                return self.cache[conn]
        data = {}
        try:
            tokens = self._authorization()
            key = tokens.get(conn)
            if conn == "OpenRouter" and key:
                req = urllib.request.Request(
                    self.OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {key}", "User-Agent": "opencode-usage-exporter/1.0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = json.load(resp)
                d = payload.get("data", {})
                limit = d.get("limit")
                data = {
                    "limit": limit if isinstance(limit, (int, float)) else None,
                    "limit_remaining": d.get("limit_remaining"),
                    "limit_reset": d.get("limit_reset"),
                    "is_free_tier": bool(d.get("is_free_tier", False)),
                    "usage_daily": d.get("usage_daily"),
                    "usage_weekly": d.get("usage_weekly"),
                    "usage_monthly": d.get("usage_monthly"),
                }
        except Exception:
            data = {}
        with self.lock:
            self.cache[conn] = data
            self.last_fetch = time.time()
        return data


class UsageCollector:
    """Agrega uso a partir do opencode.db (read-only) e da config de limites."""

    def __init__(self, db_path: str, config_path: str, auth_path: str, refresh: int, disable_net: bool):
        self.db_path = db_path
        self.config_path = config_path
        self.auth_path = auth_path
        self.fetcher = ProviderFetcher(auth_path, refresh, disable_net)

    # -- config -----------------------------------------------------------
    def _load_config(self) -> dict:
        cfg = {"providers": {}, "merge": {}}
        try:
            with open(self.config_path, encoding="utf-8") as f:
                user = json.load(f)
            cfg["providers"].update(user.get("providers", {}))
            cfg["merge"].update(user.get("merge", {}))
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return cfg

    def _provider_map(self, cfg: dict) -> dict:
        m = dict(DEFAULT_PROVIDER_MAP)
        m.update({k: v for k, v in cfg.get("merge", {}).items() if v})
        return m

    # -- uso real do DB ---------------------------------------------------
    def collect_db(self) -> dict:
        """Retorna por conexão: messages, cost, tokens_input/output/cache_read (totais)."""
        agg: dict[str, dict] = defaultdict(lambda: {
            "messages": 0, "cost": 0.0,
            "tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0,
        })
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.execute("PRAGMA query_only = 1")
        try:
            cur = con.cursor()
            for (row,) in cur.execute("SELECT data FROM message"):
                try:
                    d = json.loads(row)
                except Exception:
                    continue
                pid = d.get("providerID")
                if not pid:
                    continue
                a = agg[pid]
                a["messages"] += 1
                cost = d.get("cost")
                if isinstance(cost, (int, float)) and cost > 0:
                    a["cost"] += float(cost)
                tok = d.get("tokens") or {}
                ti = tok.get("input") or 0
                to = tok.get("output") or 0
                cache = (tok.get("cache") or {}).get("read") or 0
                a["tokens_input"] += ti if isinstance(ti, (int, float)) else 0
                a["tokens_output"] += to if isinstance(to, (int, float)) else 0
                a["tokens_cache_read"] += cache if isinstance(cache, (int, float)) else 0
        finally:
            con.close()
        return agg

    # -- conexões configuradas (auth.json) --------------------------------
    def collect_connected(self) -> set:
        conns = set()
        try:
            with open(self.auth_path, encoding="utf-8") as f:
                auth = json.load(f)
            merged = {
                "OpenCode Zen": any(isinstance(auth.get("opencode"), dict) and auth["opencode"].get("key") for _ in [0]),
                "OpenCode Go": any(isinstance(auth.get("opencode-go"), dict) and auth["opencode-go"].get("key") for _ in [0]),
                "GitHub Copilot": bool(auth.get("github-copilot")),
                "xAI Grok": bool(auth.get("xai-oauth")),
                "OpenRouter": bool(auth.get("openrouter")),
                "OpenAI": bool(auth.get("openai")),
                "HuggingFace": bool(auth.get("huggingface")),
            }
            for name, ok in merged.items():
                if ok:
                    conns.add(name)
        except Exception:
            pass
        return conns


def _fmt_label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def render_metrics(c: UsageCollector) -> tuple[str, bool]:
    ok = True
    lines: list[str] = []
    try:
        db_agg = c.collect_db()
    except Exception:
        db_agg = {}
        ok = False

    cfg = c._load_config()
    pmap = c._provider_map(cfg)
    cfgs = cfg.get("providers", {})
    connected = c.collect_connected()

    site = int(time.time())

    # Reagrupa providers -> conexão (merge)
    conn_agg: dict[str, dict] = defaultdict(lambda: {
        "messages": 0, "cost": 0.0,
        "tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0,
    })
    for pid, a in db_agg.items():
        name = pmap.get(pid, pid)
        for k in ("messages", "cost", "tokens_input", "tokens_output", "tokens_cache_read"):
            conn_agg[name][k] += a[k]

    all_conns = set(conn_agg) | set(cfgs) | connected

    lines.append("# HELP opencode_connection_messages_total Mensagens (requests) do modelo por conexao (acumulado do opencode.db).")
    lines.append("# TYPE opencode_connection_messages_total counter")
    lines.append("# HELP opencode_connection_cost_total Custo em USD acumulado por conexao (soma da coluna cost no opencode.db).")
    lines.append("# TYPE opencode_connection_cost_total counter")
    lines.append("# HELP opencode_connection_tokens_input_total Tokens de entrada acumulados por conexao.")
    lines.append("# TYPE opencode_connection_tokens_input_total counter")
    lines.append("# HELP opencode_connection_tokens_output_total Tokens de saida acumulados por conexao.")
    lines.append("# TYPE opencode_connection_tokens_output_total counter")
    lines.append("# HELP opencode_connection_tokens_cache_read_total Tokens de cache read acumulados por conexao.")
    lines.append("# TYPE opencode_connection_tokens_cache_read_total counter")
    lines.append("# HELP opencode_connection_connected Conexao configurada no auth.json do opencode (1) ou nao (0).")
    lines.append("# TYPE opencode_connection_connected gauge")
    lines.append("# HELP opencode_connection_messages_limit Limite de requests no periodo por conexao. -1 = ilimitado/desconhecido.")
    lines.append("# TYPE opencode_connection_messages_limit gauge")
    lines.append("# HELP opencode_connection_limit_remaining Restante no limite atual. -1 = N/A.")
    lines.append("# TYPE opencode_connection_limit_remaining gauge")
    lines.append("# HELP opencode_connection_limit_period Periodo do limite: 0=daily 1=weekly 2=monthly 3=none/unknown.")
    lines.append("# TYPE opencode_connection_limit_period gauge")
    lines.append("# HELP opencode_connection_free_tier Provider em free tier (1) ou nao (0).")
    lines.append("# TYPE opencode_connection_free_tier gauge")
    lines.append("# HELP opencode_connection_renewal_epoch Unix epoch da proxima renovacao do limite. 0 = sem info.")
    lines.append("# TYPE opencode_connection_renewal_epoch gauge")
    lines.append("# HELP opencode_connection_cost_period Custo reportado pelo provider no periodo (labels connection, period=daily|weekly|monthly).")
    lines.append("# TYPE opencode_connection_cost_period gauge")

    for conn in sorted(all_conns):
        agg = conn_agg.get(conn, {})
        L = _fmt_label(conn)
        lines.append(f'opencode_connection_messages_total{{connection="{L}"}} {int(agg.get("messages", 0))}')
        cost = float(agg.get("cost", 0.0))
        lines.append(f'opencode_connection_cost_total{{connection="{L}"}} {cost:.6f}')
        lines.append(f'opencode_connection_tokens_input_total{{connection="{L}"}} {int(agg.get("tokens_input", 0))}')
        lines.append(f'opencode_connection_tokens_output_total{{connection="{L}"}} {int(agg.get("tokens_output", 0))}')
        lines.append(f'opencode_connection_tokens_cache_read_total{{connection="{L}"}} {int(agg.get("tokens_cache_read", 0))}')
        lines.append(f'opencode_connection_connected{{connection="{L}"}} {1 if conn in connected else 0}')

        ccfg = cfgs.get(conn, {})
        dyn = c.fetcher._fetch_from_cache(conn)
        limit = ccfg.get("limit")
        if limit is None:
            limit = dyn.get("limit")
        if limit is None and not dyn:
            limit = -1
        if limit is None:
            limit = -1
        limit = float(limit) if isinstance(limit, (int, float)) else -1.0
        lines.append(f'opencode_connection_messages_limit{{connection="{L}"}} {limit:g}')

        rem = ccfg.get("limit_remaining")
        if rem is None:
            rem = dyn.get("limit_remaining")
        if rem is None:
            rem = -1
        rem = float(rem) if isinstance(rem, (int, float)) else -1.0
        lines.append(f'opencode_connection_limit_remaining{{connection="{L}"}} {rem:g}')

        period = ccfg.get("period", "none")
        lines.append(f'opencode_connection_limit_period{{connection="{L}"}} {PERIOD_CODE.get(str(period), 3)}')

        free = 1 if dyn.get("is_free_tier") else (1 if ccfg.get("free") else 0)
        lines.append(f'opencode_connection_free_tier{{connection="{L}"}} {free}')

        renewal = ccfg.get("renewal")
        if isinstance(renewal, (int, float)):
            renewal_epoch = float(renewal)
        elif isinstance(renewal, str) and renewal:
            import datetime as _dt
            try:
                renewal_epoch = _dt.datetime.fromisoformat(renewal).timestamp()
            except ValueError:
                renewal_epoch = 0.0
        else:
            renewal_epoch = float(dyn.get("limit_reset") or 0.0)
        lines.append(f'opencode_connection_renewal_epoch{{connection="{L}"}} {renewal_epoch:g}')

        for period in ("daily", "weekly", "monthly"):
            val = ccfg.get(f"usage_{period}")
            if val is None:
                val = dyn.get(f"usage_{period}")
            if val is None:
                continue
            lines.append(
                f'opencode_connection_cost_period{{connection="{L}",period="{period}"}} {float(val):.6f}'
            )

    try:
        size = os.path.getsize(c.db_path)
    except Exception:
        size = 0
    lines.append("# HELP opencode_usage_db_size_bytes Tamanho do arquivo opencode.db.")
    lines.append("# TYPE opencode_usage_db_size_bytes gauge")
    lines.append(f"opencode_usage_db_size_bytes {size}")
    lines.append("# HELP opencode_usage_scrape_success 1 se a ultima coleta do opencode.db teve sucesso.")
    lines.append("# TYPE opencode_usage_scrape_success gauge")
    lines.append(f"opencode_usage_scrape_success {1 if ok else 0}")
    lines.append(f"opencode_usage_scrape_timestamp {site}")
    return "\n".join(lines) + "\n", ok


class Handler(BaseHTTPRequestHandler):
    collector: UsageCollector = None  # type: ignore

    def do_GET(self):  # noqa: N802
        if self.path == "/metrics":
            body, ok = render_metrics(self.collector)
            status = 200 if ok else 500
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())
        elif self.path in ("/", "/health"):
            body = b"opencode_usage_exporter OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # silêncio
        return


def main() -> None:
    ap = argparse.ArgumentParser(description="Prometheus exporter de uso das conexões do OpenCode")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OPENCODE_USAGE_PORT", "9998")))
    ap.add_argument("--bind", default=os.environ.get("OPENCODE_USAGE_BIND", "0.0.0.0"))
    args = ap.parse_args()

    db = os.environ.get("OPENCODE_USAGE_DB", "~/.local/share/opencode/opencode.db")
    auth = os.environ.get("OPENCODE_USAGE_AUTH", "~/.local/share/opencode/auth.json")
    cfg = os.environ.get("OPENCODE_USAGE_CONFIG", "~/.config/opencode/usage-limits.json")
    refresh = int(os.environ.get("OPENCODE_USAGE_REFRESH", "300"))
    disable = os.environ.get("OPENCODE_USAGE_DISABLE_NET", "0") == "1"

    collector = UsageCollector(_expand(db), _expand(cfg), _expand(auth), refresh, disable)
    Handler.collector = collector

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"opencode_usage_exporter ouvindo em http://{args.bind}:{args.port}/metrics")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()