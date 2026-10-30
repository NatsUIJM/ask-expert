"""冒烟测试：① 工具 schema（模型实际看到的内容）② 真实咨询一次 ③ 重复提问去重。

运行：uv run python smoke_test.py
"""

import asyncio
import sys
from types import SimpleNamespace

from ask_expert.server import _StreamResult, _build_prompt, _consult, _consume_stream, mcp


def _chunk(content=None, reasoning=None, finish=None, usage=None):
    """构造与中转实际结构一致的 chunk（delta.content / delta.reasoning_content 分列）。"""
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice], usage=usage)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


async def test_stream_parsing() -> None:
    print("=== 0. 流式解析（离线假迭代器，不产生 API 费用）===")

    # 情形一：推理与答复分列两个字段，须分别累计
    chunks = [
        _chunk(reasoning="先算 7*191…"),
        _chunk(reasoning="…得 1337"),
        _chunk(content="7 * 191 = "),
        _chunk(content="1337"),
        _chunk(finish="stop", usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 99})),
    ]
    r = await _consume_stream(_FakeStream(chunks), _StreamResult())
    print("answer:", repr(r.answer), "| think_chars:", r.think_chars, "| chunks:", r.chunks)
    print("usage:", r.usage)
    ok1 = r.answer == "7 * 191 = 1337" and r.think_chars == 16 and r.chunks == 5
    print("SPLIT PARSE:", ok1)

    # 情形二：推理吃光预算、答案为空 —— 必须被识别为截断失败而非"正常空回答"
    r2 = await _consume_stream(
        _FakeStream([_chunk(reasoning="长推理" * 50), _chunk(finish="length")]),
        _StreamResult(),
    )
    print("truncated_without_answer:", r2.truncated_without_answer, "| answer empty:", not r2.answer)
    ok2 = r2.truncated_without_answer and not r2.answer

    # 情形三：正常空回答（未截断）—— 不应被误判为截断
    r3 = await _consume_stream(_FakeStream([_chunk(finish="stop")]), _StreamResult())
    ok3 = not r3.truncated_without_answer

    print("STREAM PARSE RESULT:", "PASS" if (ok1 and ok2 and ok3) else "FAIL")
    return ok1 and ok2 and ok3


async def dump_schema() -> None:
    tools = None
    for getter in ("list_tools", "get_tools"):
        fn = getattr(mcp, getter, None)
        if fn is None:
            continue
        try:
            tools = await fn()
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  ({getter} unavailable: {type(exc).__name__})")
    if not tools:
        print("SCHEMA DUMP: unavailable")
        return
    items = tools.values() if isinstance(tools, dict) else tools
    for tool in items:
        name = getattr(tool, "name", "?")
        desc = getattr(tool, "description", "") or ""
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "parameters", {}) or {}
        print(f"TOOL: {name}")
        print(f"DESC chars: {len(desc)}")
        print(f"DESC head: {desc[:160].replace(chr(10), ' / ')}")
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        print(f"PARAMS: {list(props.keys())}")
        print(f"REQUIRED: {schema.get('required') if isinstance(schema, dict) else '?'}")
        for key, val in props.items():
            pdesc = (val.get("description") or "")[:60].replace(chr(10), " ")
            print(f"  - {key}: {pdesc}")
        print()


async def main() -> int:
    print("=== 1. tool schema ===")
    await dump_schema()

    if not await test_stream_parsing():
        print("\nRESULT: FAIL (stream parsing)")
        return 1

    print("\n=== 2. real consultation (streaming) ===")
    answer = await _consult(
        objective="算出 7*191 的结果，并给出可回代检验的中间步骤。",
        context="纯算术题，无外部依赖。",
        output_format="一行等式给出结果，另起一行给出可回代检验的验算等式。",
        trigger="任务规格清晰但需要一次外部核对（冒烟测试用例）",
        question_type="math_reasoning",
    )
    print(answer[:700])
    print("...")
    print("FRAME OK:", "[外部强模型咨询结果" in answer and "处理要求" in answer)
    print("ANSWER OK:", "1337" in answer)
    print("PROCESS NOTE:", "[过程]" in answer)

    print("=== 3. dedup (identical call again) ===")
    again = await _consult(
        objective="算出 7*191 的结果，并给出可回代检验的中间步骤。",
        context="纯算术题，无外部依赖。",
        output_format="一行等式给出结果，另起一行给出可回代检验的验算等式。",
        trigger="重复提问测试",
        question_type="math_reasoning",
    )
    cached = "重复提问" in again
    print("CACHE HIT:", cached)
    print("SAME BODY:", "1337" in again)

    print("=== 4. code_debug 缺 attempts：应被拒（不发起出站调用）===")
    rejected = await _consult(
        objective="修复运行脚本时的 TypeError",
        context="执行 python main.py 时报错。",
        output_format="给出修复方案。",
        trigger="P0 校验路径测试",
        question_type="code_debug",
        attempts="",
    )
    print(rejected[:220])
    rejected_ok = "[咨询未执行]" in rejected and "attempts" in rejected
    print("REJECTED:", rejected_ok)

    print("=== 5. 类型模板注入（本地构造，不发起调用）===")
    code_prompt = _build_prompt("code_debug", "o", "c", "f", "a", "")
    other_prompt = _build_prompt("other", "o", "c", "f", "a", "")
    injected = "【回答要求】" in code_prompt and "根因" in code_prompt
    not_injected = "【回答要求】" not in other_prompt
    print("TEMPLATE INJECTED:", injected, "| other 不注入:", not_injected)

    ok = (
        "[外部强模型咨询结果" in answer
        and "处理要求" in answer
        and "1337" in answer
        and cached
        and rejected_ok
        and injected
        and not_injected
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
