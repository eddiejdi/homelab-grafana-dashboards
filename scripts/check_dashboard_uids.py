#!/usr/bin/env python3
"""Falha se dois dashboards visíveis ao provider do Grafana compartilharem o mesmo uid.

Duas duplicatas de uid dentro do path de um provider fazem o Grafana emitir

    dashboards provisioning provider has no database write permissions because of duplicates

e congelar o provisionamento de TODOS os dashboards daquele provider: os deploys
continuam gravando em disco e a API continua servindo as versões antigas, em
silêncio. Foi exatamente o que aconteceu entre 22/07 e 24/07 de 2026.

Modos de uso:

  --repo dashboards
      Checa apenas os arquivos do repositório (usado no CI de pull request).

  --repo dashboards --provider-dir /home/.../provisioning/dashboards
      Checa o estado PROJETADO do provider: os arquivos que já existem lá,
      sobrepostos pelos arquivos que o rsync deste repo vai gravar. Roda antes
      do rsync, então uma colisão aborta o deploy sem sujar o servidor.

A varredura replica a semântica do Grafana (pkg/services/provisioning/dashboards/
file_reader.go): recursiva, apenas arquivos terminados em `.json`, ignorando
diretórios cujo nome começa com ponto. Por isso `x.json.bak-20260724` e
`x.json.disabled.20260723` não contam — mas `.../trading/x.json` conta.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def iter_dashboards(root: Path) -> list[Path]:
    """Lista os .json que o Grafana enxergaria sob `root`."""
    found: list[Path] = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.json")):
        if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_file():
            found.append(path)
    return found


def read_uid(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"❌ {path}: JSON inválido ({exc})", file=sys.stderr)
        raise SystemExit(1) from exc
    uid = data.get("uid")
    return uid if isinstance(uid, str) and uid else None


def projected_layout(repo_dir: Path, provider_dir: Path | None) -> dict[str, Path]:
    """Mapeia caminho-final → arquivo-fonte depois que o rsync deste repo rodar."""
    layout: dict[str, Path] = {}

    if provider_dir is not None:
        for existing in iter_dashboards(provider_dir):
            layout[str(existing.relative_to(provider_dir))] = existing

    for source in iter_dashboards(repo_dir):
        # rsync "$SRC/" "$DEST/" preserva a hierarquia de categorias.
        layout[str(source.relative_to(repo_dir))] = source

    return layout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="dashboards", type=Path,
                        help="diretório de dashboards deste repositório")
    parser.add_argument("--provider-dir", type=Path, default=None,
                        help="path do provider no servidor; ativa a checagem do estado projetado")
    args = parser.parse_args()

    if not args.repo.is_dir():
        print(f"❌ diretório não encontrado: {args.repo}", file=sys.stderr)
        return 1

    layout = projected_layout(args.repo, args.provider_dir)

    by_uid: dict[str, list[str]] = defaultdict(list)
    missing: list[str] = []
    for rel, source in sorted(layout.items()):
        uid = read_uid(source)
        if uid is None:
            missing.append(rel)
            continue
        by_uid[uid].append(rel)

    for rel in missing:
        print(f"⚠️  {rel}: sem campo 'uid' — o Grafana vai gerar um a cada deploy")

    duplicates = {uid: paths for uid, paths in by_uid.items() if len(paths) > 1}
    if duplicates:
        scope = "no provider (estado projetado)" if args.provider_dir else "no repositório"
        print(f"\n❌ uid duplicado {scope} — deploy abortado:", file=sys.stderr)
        for uid, paths in sorted(duplicates.items()):
            print(f"\n  uid '{uid}' aparece em {len(paths)} arquivos:", file=sys.stderr)
            for rel in paths:
                origin = "repo" if layout[rel].is_relative_to(args.repo) else "servidor"
                print(f"    - {rel}  ({origin}: {layout[rel]})", file=sys.stderr)
        print(
            "\nDois arquivos com o mesmo uid sob o path do provider congelam o "
            "provisionamento inteiro.\nEleja uma fonte de verdade e remova a outra cópia. "
            "Backups vão para\n/home/homelab/monitoring/grafana/dashboard_quarantine/ — nunca "
            "para dentro do path do provider.",
            file=sys.stderr,
        )
        return 1

    print(f"✅ {len(by_uid)} uids únicos em {len(layout)} dashboards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
