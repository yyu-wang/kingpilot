"""M9-T08：知识库摄入、分级/DLP 门禁与增量更新测试。"""

import json
import shutil
from pathlib import Path

import pytest

from config.rag_rules import RAG_MAX_FILE_BYTES
from governance.rag.ingest import RagIngestor
from infrastructure.exceptions import RagError
from observability.audit import AuditLogger, JsonlAuditSink

_FIXTURES = Path(__file__).parent / "fixtures" / "rag"


def _ingestor(tmp_path: Path, *, name: str = "knowledge") -> tuple[RagIngestor, Path, Path]:
    source = tmp_path / name
    if not source.exists():
        shutil.copytree(_FIXTURES, source)
    index_dir = tmp_path / "rag_index"
    return RagIngestor(source_root=source, index_dir=index_dir), source, index_dir


def test_initial_ingest(tmp_path: Path) -> None:
    ingestor, _, _ = _ingestor(tmp_path)
    report = ingestor.refresh()

    assert report.added == 3
    assert report.updated == 0
    assert report.removed == 0
    assert report.unchanged == 0
    assert report.chunks > 0
    assert report.written is True
    assert ingestor.index_path.exists()
    assert [(item.path, item.reason) for item in report.skipped] == [
        ("sensitive/internal-contacts.md", "level_denied")
    ]


def test_repeat_ingest_is_idempotent(tmp_path: Path) -> None:
    ingestor, _, _ = _ingestor(tmp_path)
    ingestor.refresh()
    before = ingestor.index_path.read_text(encoding="utf-8")

    report = ingestor.refresh()

    assert report.unchanged == 3
    assert report.written is False
    assert ingestor.index_path.read_text(encoding="utf-8") == before


def test_update_document_reindexes(tmp_path: Path) -> None:
    ingestor, source, _ = _ingestor(tmp_path)
    ingestor.refresh()
    path = source / "public" / "sales-order-guide.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "客户信用额度不足", "客户信用额度不足，需要财务复核"
        ),
        encoding="utf-8",
    )

    report = ingestor.refresh()

    assert report.updated == 1
    assert report.unchanged == 2
    assert report.written is True
    hits = ingestor.load_index().search("财务复核")
    assert hits
    assert hits[0].path == "public/sales-order-guide.md"


def test_delete_document_removes_chunks(tmp_path: Path) -> None:
    ingestor, source, _ = _ingestor(tmp_path)
    ingestor.refresh()
    (source / "public" / "inventory-guide.txt").unlink()

    report = ingestor.refresh()

    assert report.removed == 1
    assert ingestor.load_index().search("盘点差异") == []
    assert {document.path for document in ingestor.load_index().documents} == {
        "public/sales-order-guide.md",
        "internal/purchase-order-guide.md",
    }


def test_dlp_rejects_document_with_pii(tmp_path: Path) -> None:
    ingestor, source, _ = _ingestor(tmp_path)
    bad = source / "public" / "contacts.md"
    bad.write_text("# 联系人\n\n手机号 13800138000。", encoding="utf-8")

    report = ingestor.refresh()

    assert any(
        item.path == "public/contacts.md" and item.reason == "sensitive_content"
        for item in report.skipped
    )
    assert "13800138000" not in ingestor.index_path.read_text(encoding="utf-8")
    assert len(ingestor.load_index().documents) == 3  # 其他文档不受影响


def test_too_large_file_is_skipped(tmp_path: Path) -> None:
    ingestor, source, _ = _ingestor(tmp_path)
    (source / "public" / "big.md").write_text("a" * (RAG_MAX_FILE_BYTES + 1), encoding="utf-8")

    report = ingestor.refresh()

    assert any(
        item.path == "public/big.md" and item.reason == "too_large" for item in report.skipped
    )


def test_non_utf8_file_is_skipped(tmp_path: Path) -> None:
    ingestor, source, _ = _ingestor(tmp_path)
    (source / "public" / "binary.md").write_bytes(b"\xff\xfe\x00\x00")

    report = ingestor.refresh()

    assert any(
        item.path == "public/binary.md" and item.reason == "unsupported_encoding"
        for item in report.skipped
    )


def test_hidden_file_and_symlink_are_skipped(tmp_path: Path) -> None:
    ingestor, source, _ = _ingestor(tmp_path)
    (source / ".hidden.md").write_text("# 隐藏文档", encoding="utf-8")
    (source / "public" / "link.md").symlink_to(source / "public" / "sales-order-guide.md")

    report = ingestor.refresh()

    assert any(
        item.path == "public/link.md" and item.reason == "symlink" for item in report.skipped
    )
    paths = {document.path for document in ingestor.load_index().documents}
    assert ".hidden.md" not in paths
    assert "public/link.md" not in paths


def test_missing_source_raises(tmp_path: Path) -> None:
    ingestor = RagIngestor(source_root=tmp_path / "missing", index_dir=tmp_path / "rag_index")
    with pytest.raises(RagError) as exc:
        ingestor.refresh()
    assert exc.value.code == "rag_source_missing"


def test_index_can_be_rebuilt_after_delete(tmp_path: Path) -> None:
    ingestor, _, _ = _ingestor(tmp_path)
    ingestor.refresh()
    ingestor.index_path.unlink()

    report = ingestor.refresh()

    assert report.written is True
    assert report.added == 3
    assert ingestor.load_index().search("审核销售订单")


def test_audit_event_has_no_document_content(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    ingestor, _, _ = _ingestor(tmp_path)
    audited = RagIngestor(
        source_root=ingestor.source_root,
        index_dir=tmp_path / "rag_index",
        audit=AuditLogger(JsonlAuditSink(audit_path)),
    )

    audited.refresh()
    first_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(first_lines) == 1
    event = json.loads(first_lines[0])
    assert event["event_type"] == "rag.indexed"
    assert event["detail"]["added"] == 3
    assert "销售订单" not in first_lines[0]

    audited.refresh()  # 无变化不写审计
    assert len(audit_path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_source_root_change_rebuilds_index(tmp_path: Path) -> None:
    first, _, _ = _ingestor(tmp_path, name="knowledge-a")
    first.refresh()

    source_b = tmp_path / "knowledge-b"
    (source_b / "public").mkdir(parents=True)
    (source_b / "public" / "faq.md").write_text(
        "# 常见问题\n\n## 发票\n\n发票在审核后开具。", encoding="utf-8"
    )
    second = RagIngestor(source_root=source_b, index_dir=tmp_path / "rag_index")

    report = second.refresh()

    assert report.added == 1
    assert report.removed == 3
    assert {document.path for document in second.load_index().documents} == {"public/faq.md"}
