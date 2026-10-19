"""RAG 知识库摄入 CLI（M9-T08，开发辅助，不进六门禁）。

用法：
    make rag-ingest                                   # 用 .env/Settings 默认目录
    make rag-ingest SOURCE=/tmp/kp-knowledge          # 指定知识源
    make rag-ingest QUERY="审核销售订单"               # 摄入后直接验证 top5
    uv run python scripts/rag_ingest.py --source ... --index-dir ... --json

退出码：0 成功 / 2 源目录或索引错误。
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from config.rag_rules import RAG_DEFAULT_TOP_K
from config.settings import get_settings
from governance.rag.ingest import RagIngestor
from infrastructure.exceptions import RagError
from models.rag import RagIngestReport
from observability.audit import AuditLogger, JsonlAuditSink

_SNIPPET_CHARS = 80


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KingPilot RAG 知识库摄入（M9-T08）")
    parser.add_argument("--source", default="", help="知识源目录（默认取 Settings）")
    parser.add_argument("--index-dir", default="", help="索引目录（默认取 Settings）")
    parser.add_argument("--query", default="", help="摄入后可选：直接检索验证")
    parser.add_argument("--top-k", type=int, default=RAG_DEFAULT_TOP_K, help="检索条数")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser.parse_args(argv)


def _render_report(report: RagIngestReport, index_path: Path) -> None:
    state = "已写入" if report.written else "无变化（跳过写入）"
    print(f"索引文件：{index_path}")
    print(
        f"新增 {report.added} / 更新 {report.updated} / 删除 {report.removed} / "
        f"未变 {report.unchanged} | 分块 {report.chunks} | {state}"
    )
    if report.skipped:
        print(f"跳过 {len(report.skipped)} 篇：")
        for skipped in report.skipped:
            print(f"  - {skipped.path} ({skipped.reason})")


def _render_hits(ingestor: RagIngestor, query: str, top_k: int) -> None:
    hits = ingestor.load_index().search(query, top_k=top_k)
    print(f"\n检索「{query}」（top {top_k}）：")
    if not hits:
        print("  （无命中）")
        return
    for rank, hit in enumerate(hits, start=1):
        snippet = " ".join(hit.content.split())[:_SNIPPET_CHARS]
        print(f"{rank}. [score={hit.score:.4f}] {hit.path} › {hit.heading} (#{hit.chunk_index})")
        print(f"   {snippet}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    source = Path(args.source) if args.source else settings.rag_source_dir
    index_dir = Path(args.index_dir) if args.index_dir else settings.rag_index_dir

    ingestor = RagIngestor(
        source_root=source,
        index_dir=index_dir,
        audit=AuditLogger(JsonlAuditSink(settings.audit_log_path)),
    )
    try:
        report = ingestor.refresh()
        hits = ingestor.load_index().search(args.query, top_k=args.top_k) if args.query else []
    except RagError as exc:
        print(f"RAG 摄入失败：{exc} (code={exc.code})", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "report": report.model_dump(mode="json"),
                    "index_path": str(ingestor.index_path),
                    "hits": [hit.model_dump(mode="json") for hit in hits],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    _render_report(report, ingestor.index_path)
    if args.query:
        _render_hits(ingestor, args.query, args.top_k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
