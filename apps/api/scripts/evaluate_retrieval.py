"""Run a retrieval evaluation dataset against a repository indexed in the local database.

    python scripts/evaluate_retrieval.py evaluation/datasets/code-mind.v1.json
    python scripts/evaluate_retrieval.py DATASET --repository owner/name --k 8 --json report.json

The dataset names its repository and the commit its expectations were written for; index that
commit first (POST /api/v1/repositories with "ref" set to the SHA). Vector search uses the
embedding provider from .env (EMBEDDING_PROVIDER=ollama with bge-m3); without one, only the
keyword configuration runs. Nothing is written to the database and no answer model is called.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from codemind.core.config import settings
from codemind.db.models import SERVABLE_SNAPSHOT_STATUSES, Repository, RepositorySnapshot
from codemind.db.session import get_session_factory
from codemind.evaluation.retrieval import evaluate, load_dataset, report_json, report_table
from codemind.providers import get_embedding_provider
from codemind.retrieval.answer import MAX_EVIDENCE_ITEMS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--repository", help="owner/name; defaults to the dataset's repository")
    ap.add_argument("--commit", help="snapshot commit; defaults to the dataset's commit_sha")
    ap.add_argument("--k", type=int, default=MAX_EVIDENCE_ITEMS, help="evidence items per question")
    ap.add_argument("--json", type=Path, help="also write the full report here")
    ap.add_argument("--verbose", action="store_true", help="print each case's misses")
    args = ap.parse_args()

    dataset = load_dataset(args.dataset)
    owner, _, name = (args.repository or dataset.repository).partition("/")
    commit = args.commit or dataset.commit_sha
    with get_session_factory()() as db:
        repo = db.scalar(
            select(Repository).where(
                func.lower(Repository.owner) == owner.lower(),
                func.lower(Repository.name) == name.lower(),
            )
        )
        if repo is None:
            print(f"{owner}/{name} is not imported. Import and index it first.", file=sys.stderr)
            return 2
        query = select(RepositorySnapshot).where(
            RepositorySnapshot.repository_id == repo.id,
            RepositorySnapshot.status.in_(SERVABLE_SNAPSHOT_STATUSES),
        )
        if commit:
            query = query.where(RepositorySnapshot.commit_sha == commit)
        snapshot = db.scalar(query.order_by(RepositorySnapshot.indexed_at.desc()).limit(1))
        if snapshot is None:
            print(
                f"No indexed snapshot of {owner}/{name}"
                + (f" at {commit}. Index that commit with ref={commit}." if commit else "."),
                file=sys.stderr,
            )
            return 2

        embedder = get_embedding_provider(settings)
        results = evaluate(
            db,
            snapshot.id,
            dataset,
            embedder=embedder,
            k=args.k,
            token_budget=settings.answer_context_tokens,
        )

    print(f"{dataset.name} v{dataset.version}: {len(dataset.cases)} cases on {owner}/{name}")
    print(
        f"commit {snapshot.commit_sha}, k={args.k}, embeddings={getattr(embedder, 'model_name', None)}"
    )
    print(report_table(results))
    if args.verbose:
        for result in results:
            for case in result.cases:
                if case.missed:
                    print(f"  [{result.config}] {case.case_id}: missed {', '.join(case.missed)}")
    if args.json:
        report = report_json(
            dataset,
            results,
            commit_sha=snapshot.commit_sha,
            k=args.k,
            embedding_model=getattr(embedder, "model_name", None),
            ran_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
