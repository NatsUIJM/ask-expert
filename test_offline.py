"""离线测试：不产生 API 费用。

运行：uv run python test_offline.py
覆盖：needs_evidence 标记解析、consult_id/status 包装、缓存 TTL、部分结果保存、
consult 记录与 prior_consult_id 注入、工具 schema。
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask_expert.server as s


def main() -> None:
    print("== extract missing ==")
    body, items = s._extract_missing_info(
        "结论正文\n\n【需要补充的信息】\n- 文件 A\n- 运行命令 B\n"
    )
    print(repr(body), items)
    assert body == "结论正文" and items == ["文件 A", "运行命令 B"]
    assert s._extract_missing_info("普通回答") == ("普通回答", [])
    assert s._extract_missing_info("回答\n【需要补充的信息】\n") == (
        "回答\n【需要补充的信息】\n",
        [],
    )

    print("== frame ==")
    frame = s._frame(
        "正文", consult_id="abc123", status="needs_evidence", missing_info=["文件 A"]
    )
    print(frame[:180].replace("\n", " | "))
    assert "consult_id=abc123" in frame and "status=needs_evidence" in frame
    assert "prior_consult_id=abc123" in frame

    print("== dedup key ==")
    kw = dict(
        question_type="math_reasoning",
        objective="o",
        context="c",
        attempts="a",
        output_format="f",
        boundaries="b",
    )
    k1 = s._dedup_key(**kw, prior_consult_id="")
    k2 = s._dedup_key(**kw, prior_consult_id="")
    k3 = s._dedup_key(**kw, prior_consult_id="p1")
    print(k1, k2, k3)
    assert k1 == k2 and k1 != k3

    print("== cache ttl ==")
    old = s.CACHE_DIR / "testttl_old.json"
    new = s.CACHE_DIR / "testttl_new.json"
    old.write_text(
        json.dumps({"answer": "old", "created_at": "2020-01-01T00:00:00+08:00"}),
        encoding="utf-8",
    )
    new.write_text(
        json.dumps({"answer": "new", "created_at": s._now_iso()}), encoding="utf-8"
    )
    assert s._cache_get("testttl_old", "code_debug") is None
    assert not old.exists()
    assert s._cache_get("testttl_new", "code_debug")["answer"] == "new"
    new.unlink()

    print("== partial saver ==")
    saver = s._PartialSaver("testpartial")
    saver.update("hello")
    assert saver.best == "hello"
    saver.flush()
    assert saver.path.read_text(encoding="utf-8") == "hello"
    saver.clear()
    assert not saver.path.exists()

    print("== stream failure keeps partial ==")

    def chunk(text: str):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=text, reasoning_content=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )

    class BadStream:
        def __aiter__(self):
            async def gen():
                yield chunk("第一段")
                yield chunk("第二段")
                raise RuntimeError("upstream 524")

            return gen()

    async def run_stream() -> None:
        saver2 = s._PartialSaver("testpartial2")
        try:
            await s._consume_stream(BadStream(), s._StreamResult(), on_partial=saver2.update)
        except RuntimeError as exc:
            print("caught", exc)
        saver2.flush()
        print("best", saver2.best)
        assert saver2.best == "第一段第二段"
        saver2.clear()

    asyncio.run(run_stream())

    print("== prior block ==")
    cid = "testconsult"
    s._record_consult(
        consult_id=cid,
        objective="目标 X",
        question_type="math_reasoning",
        status="needs_evidence",
        answer="上次结论",
        missing_info=["文件 A"],
        prior_consult_id="",
    )
    prior = s._prior_block(cid)
    print(prior)
    assert "目标 X" in prior and "上次结论" in prior and "文件 A" in prior
    s._consult_path(cid).unlink()

    print("== consult end-to-end needs_evidence ==")
    captured: list[str] = []

    async def fake_stream(prompt, ctx=None, on_partial=None, system_prompt=None):
        captured.append(prompt)
        result = s._StreamResult()
        result.answer_parts.append("现有结论\n【需要补充的信息】\n- 文件 A")
        result.finish_reason = "stop"
        result.chunks = 1
        result.ttft_ms = 1
        result.think_chars = 10
        if on_partial is not None:
            on_partial(result.answer)
        return result, 0

    s._stream_consult = fake_stream
    s.API_KEY, s.BASE_URL, s.MODEL = "test", "http://localhost:1/v1", "test-model"

    async def run_consult() -> None:
        import re

        first = await s._consult(
            objective="unique objective for needs-evidence test",
            context="context",
            output_format="format",
            trigger="test",
        )
        print(first[:180].replace("\n", " | "))
        assert "status=needs_evidence" in first
        match = re.search(r"consult_id=([0-9a-f]+)", first)
        assert match is not None
        cid = match.group(1)
        second = await s._consult(
            objective="unique objective for needs-evidence test 2",
            context="context 2",
            output_format="format",
            trigger="test",
            prior_consult_id=cid,
        )
        assert "【上次咨询】" in captured[-1]
        assert f"id: {cid}" in captured[-1]
        print("prior block injected into second prompt")
        s._consult_path(cid).unlink()

    asyncio.run(run_consult())

    print("== evidence request parsing ==")
    reqs = s._parse_evidence_requests(
        [
            "file: src/a.py",
            "file: src/a.py lines 10-20",
            "grep: transfer capability in *.tex",
            "diff: HEAD~1",
            "文件 A",
            "运行命令 B",
        ]
    )
    print(reqs)
    assert reqs[0] == {"type": "file", "path": "src/a.py"}
    assert reqs[1]["line_start"] == 10 and reqs[1]["line_end"] == 20
    assert reqs[2]["type"] == "grep" and reqs[2]["glob"] == "*.tex"
    assert reqs[3]["type"] == "diff"
    assert len(reqs) == 4

    print("== secret redaction ==")
    red = s._redact_secrets("api_key = sk-ABCDEFGHIJKLMNOPQRSTUVWX\npassword: hunter2hunter2")
    print(red)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in red
    assert "hunter2hunter2" not in red

    print("== audit answer parsing ==")
    verdict, count, sev = s._parse_audit_answer(
        "## 发现\n- [S1|第3节] a | 证据：x vs y\n- [S2|表2] b | 证据：m vs n\n"
        "## 结论\nVERDICT: FAIL\n理由"
    )
    print(verdict, count, sev)
    assert verdict == "FAIL" and count == 2 and sev == {"S1": 1, "S2": 1, "S3": 0}

    print("== audit frame ==")
    frame_a = s._frame(
        "正文", consult_id="audit1", status="answered", mode="audit",
        verdict="FAIL", findings_count=2,
        evidence_requests=[{"type": "file", "path": "a.tex"}],
    )
    print(frame_a[:200].replace("\n", " | "))
    assert "审计结果" in frame_a and "audit_id=audit1" in frame_a
    assert "verdict=FAIL" in frame_a and "结构化证据请求" in frame_a
    assert "prior_audit_id=audit1" in frame_a

    print("== evidence resolution (read-only, redacted) ==")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        secret_file = root / "notes.txt"
        secret_file.write_text(
            "needle here\napi_key = sk-ZYXWVUTSRQPONMLKJIHGF\n", encoding="utf-8"
        )
        old_roots = s.EVIDENCE_ROOTS
        s.EVIDENCE_ROOTS = [root]
        try:
            text, prov = s._resolve_evidence(
                [
                    {"type": "file", "path": str(secret_file)},
                    {"type": "grep", "pattern": "needle", "glob": "*.txt"},
                    {"type": "file", "path": str(root / ".." / "outside.txt")},
                ]
            )
            print(prov)
            assert "needle here" in text
            assert "sk-ZYXWVUTSRQPONMLKJIHGF" not in text
            assert any(item.get("status") == "rejected" for item in prov)
        finally:
            s.EVIDENCE_ROOTS = old_roots

    print("== audit end-to-end ==")

    async def fake_audit_stream(prompt, ctx=None, on_partial=None, system_prompt=None):
        captured.append(prompt)
        result = s._StreamResult()
        result.answer_parts.append(
            "## 发现\n- [S1|第3节] 正文 3.2% vs 表2 2.8% | 证据：3.2 vs 2.8\n"
            "## 已查未发现\n- 已查 1 项：编号\n## 结论\nVERDICT: FAIL\n理由"
        )
        result.finish_reason = "stop"
        result.chunks = 1
        result.ttft_ms = 1
        result.think_chars = 10
        return result, 0

    s._stream_consult = fake_audit_stream
    s.API_KEY, s.BASE_URL, s.MODEL = "test", "http://localhost:1/v1", "test-model"

    async def run_audit() -> None:
        suffix = s._new_consult_id()
        out = await s._audit(
            artifact=f"正文 3.2% · {suffix}",
            checklist="L2 数值一致性",
            artifact_name=f"test-paper-{suffix}",
        )
        print(out[:220].replace("\n", " | "))
        assert "status" not in out.split("]")[0] or "answered" in out
        assert "verdict=FAIL" in out and "findings=1" in out
        assert "【审计标准】" in captured[-1]
        assert "L2 数值一致性" in captured[-1]
        import re
        match = re.search(r"audit_id=([0-9a-f]+)", out)
        assert match is not None
        s._consult_path(match.group(1)).unlink()

    asyncio.run(run_audit())

    print("== tool schema ==")

    async def schema() -> None:
        tools = await s.mcp.list_tools()
        items = tools.values() if isinstance(tools, dict) else tools
        names = {getattr(tool, "name", "") for tool in items}
        print("tools", names)
        assert "consult_expert" in names and "audit_artifact" in names
        for tool in items:
            name = getattr(tool, "name", "")
            schema_obj = (
                getattr(tool, "inputSchema", None)
                or getattr(tool, "parameters", {})
                or {}
            )
            props = schema_obj.get("properties", {})
            if name == "consult_expert":
                assert "prior_consult_id" in props
            if name == "audit_artifact":
                assert "checklist" in props and "prior_audit_id" in props

    asyncio.run(schema())
    print("ALL OFFLINE TESTS PASS")


if __name__ == "__main__":
    main()
