"""交互式 CLI（M3-T07）：多轮对话 + 本地会话持久化。

一次进程 = 一个会话的多轮对话；每轮答复后立即落盘（崩溃不丢历史），
下次以同一 ``--session`` 启动即可续聊（三层记忆从磁盘恢复）。

设计要点：
- ``run_chat_loop`` 注入读写函数：单测无需真终端，5 轮续聊可确定性验证
- 不 import ``sandbox``：生产入口只认 ``.env`` 配置；本地沙箱演示走
  ``scripts/chat.py``（测试设施不进 wheel，见 sandbox/__init__.py 约定）
- 幻觉告警显性提示（衔接 M3-T06）：``hallucination_warning`` 为 True 时
  提醒用户核对原始数据，不阻断会话
"""

import argparse
import asyncio
from collections.abc import Callable, Sequence
from uuid import uuid4

from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.providers import get_provider
from agent.session_store import SessionStore
from agent.tools import build_default_registry
from config.settings import Settings, get_settings
from governance.online_sampler import OnlineSampler
from governance.prompt_registry import PromptRegistry
from infrastructure.exceptions import KingPilotError, SessionStoreError
from infrastructure.logger import setup_logging
from infrastructure.secrets import build_secret_provider
from infrastructure.tenancy import tenant_paths
from kingdee.client import KingdeeClient
from models.tenant import DEFAULT_TENANT
from observability.alerts import AlertEngine
from observability.audit import AuditLogger, JsonlAuditSink
from observability.tracing import TraceStore
from safety.engine import SafetyLayer
from safety.human_in_loop import CliConfirmationIO

_EXIT_COMMANDS = frozenset({"/exit", "/quit"})
_DEFAULT_SESSION = "default"


async def run_chat_loop(
    core: AgentCore,
    memory: MemoryStore,
    store: SessionStore,
    *,
    read_input: Callable[[str], str],
    write_output: Callable[[str], None],
    sampler: OnlineSampler | None = None,
) -> None:
    """多轮对话循环：读输入 → 跑主循环 → 打印答复 → 落盘（含在线抽样）。

    Args:
        core: 已注入同一 ``memory`` 的主循环。
        memory: 当前会话记忆（每轮由 core 写回）。
        store: 会话持久化存储（每轮答复后 ``save``；崩溃不丢已完成轮次）。
        read_input: 读一行输入（生产为 ``input``；测试注入脚本序列）。
        write_output: 输出一行文本（生产为 ``print``；测试注入收集器）。
        sampler: 在线抽样器；None = 关闭（M8-T02）。
    """
    write_output(f"KingPilot 多轮对话已启动（会话 {memory.session_id}），输入 /exit 退出。")
    while True:
        try:
            raw = read_input("你> ")
        except EOFError:
            write_output("")
            break
        text = raw.strip()
        if not text:
            continue
        if text in _EXIT_COMMANDS:
            break
        try:
            response = await core.run(text)
        except KingPilotError as exc:
            # 单轮失败不终结会话：历史已落盘，用户可继续或排查后重试
            write_output(f"处理失败：{exc}")
            continue
        write_output(f"助手> {response.content}")
        if sampler is not None:
            # 在线抽样是旁路：写入失败只告警，不影响会话（M8-T02）
            sampler.maybe_sample(response, session_id=memory.session_id)
        if response.hallucination_warning:
            # 措辞保守：含派生汇总（如合计=各项相加）时也会提示，属检测口径限制（M7 补聚合核对）
            write_output("（提示：部分内容未能与工具原始结果直接核对，请留意）")
        store.save(memory)
    write_output(f"会话已保存：{store.path_for(memory.session_id)}")


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    """CLI 入口：解析参数 → 加载会话 → 启动多轮对话。

    ``settings`` 可注入（``scripts/chat.py`` 用它指向本地沙箱）；
    不传则走 ``get_settings()``（生产路径，读 .env）。
    """
    setup_logging()
    args = _build_parser().parse_args(argv)
    resolved = settings or get_settings()
    store = SessionStore(tenant_paths(resolved, DEFAULT_TENANT).session_root)

    if args.list:
        _print_sessions(store)
        return 0

    session_id = uuid4().hex[:12] if args.new else args.session
    try:
        memory = store.load(session_id)
    except SessionStoreError as exc:
        print(f"会话加载失败：{exc}")
        return 1
    try:
        return asyncio.run(_run_session(resolved, store, memory))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


async def _run_session(settings: Settings, store: SessionStore, memory: MemoryStore) -> int:
    """建立 Provider/Client 并跑完整个会话；资源在 finally 统一释放。"""
    # 审计先于 Provider：预算降档/耗尽事件也要进统一审计流（M7-T04）
    paths = tenant_paths(settings, DEFAULT_TENANT)
    audit = AuditLogger(JsonlAuditSink(paths.audit_path))
    audit.subscribe(AlertEngine().observe)
    secrets = build_secret_provider(settings)
    try:
        prompts = PromptRegistry(settings.prompts_dir)
        provider = get_provider(
            settings,
            audit=audit,
            prompts=prompts,
            rollout_key=DEFAULT_TENANT,
            secrets=secrets,
        )
    except KingPilotError as exc:
        print(f"LLM 初始化失败：{exc}（请检查 KINGPILOT_LLM_API_KEY / prompts 目录）")
        return 2
    try:
        async with KingdeeClient(settings, secrets=secrets) as client:
            registry = build_default_registry()
            safety = SafetyLayer(registry, client, CliConfirmationIO(), audit=audit)
            trace_store = TraceStore(paths.trace_dir)
            sampler = (
                OnlineSampler(
                    paths.sample_dir,
                    sample_rate=settings.online_sample_rate,
                    max_per_day=settings.online_sample_max_per_day,
                )
                if settings.online_sample_rate > 0
                else None
            )
            core = AgentCore(
                provider,
                registry,
                client,
                settings,
                memory=memory,
                safety=safety,
                trace_store=trace_store,
                audit=audit,
            )
            await run_chat_loop(
                core, memory, store, read_input=input, write_output=print, sampler=sampler
            )
    finally:
        await provider.aclose()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kingpilot-chat", description="KingPilot 多轮对话 CLI（会话落盘可续聊）"
    )
    parser.add_argument(
        "--session",
        default=_DEFAULT_SESSION,
        help=f"会话 ID（默认 {_DEFAULT_SESSION}；同名会话自动续聊）",
    )
    parser.add_argument("--new", action="store_true", help="创建随机 ID 的新会话")
    parser.add_argument("--list", action="store_true", help="列出本地会话后退出")
    return parser


def _print_sessions(store: SessionStore) -> None:
    sessions = store.list_sessions()
    if not sessions:
        print(f"（{store.root} 下暂无会话）")
        return
    for info in sessions:
        print(f"{info.session_id}  {info.rounds} 轮  {info.updated_at}")
