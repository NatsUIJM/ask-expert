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

# 端到端用例会把 _stream_consult 换成假实现；推理强度专项用例需要真实实现。
_REAL_STREAM_CONSULT = s._stream_consult
_REAL_RUN_EXPERT = s._run_expert


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

    print("== connection overrides ==")
    s.API_KEY, s.BASE_URL, s.MODEL = "env-key", "http://env/v1", "env-model"
    assert s._effective_conn() == ("http://env/v1", "env-key", "env-model")
    assert s._effective_conn(api_key="call-key") == (
        "http://env/v1",
        "call-key",
        "env-model",
    )
    assert s._effective_conn(base_url="http://call/v1") == (
        "http://call/v1",
        "env-key",
        "env-model",
    )
    assert s._effective_conn(model="call-model") == (
        "http://env/v1",
        "env-key",
        "call-model",
    )
    # 空白字符串按"未传"处理，回落环境变量
    assert s._effective_conn(api_key="  ") == ("http://env/v1", "env-key", "env-model")
    # 覆盖项参与去重键：同一问题换模型/端点不得命中同一缓存
    assert s._dedup_key(**kw, prior_consult_id="", model="call-model") != k1
    assert (
        s._dedup_key(**kw, prior_consult_id="", base_url="http://call/v1") != k1
    )
    # 参数齐全时不再要求环境变量
    s.API_KEY, s.BASE_URL, s.MODEL = "", "", ""
    assert s._missing_required_env() != []
    assert (
        s._missing_required_env(
            api_key="k", base_url="http://localhost:1/v1", model="m"
        )
        == []
    )

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

    async def fake_stream(prompt, ctx=None, on_partial=None, system_prompt=None, **kwargs):
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

    async def fake_audit_stream(prompt, ctx=None, on_partial=None, system_prompt=None, **kwargs):
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

    print("== attachments ==")
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="ask-expert-att-"))
    (tmpdir / "notes.md").write_text("# 笔记\n关键结论 A", encoding="utf-8")
    (tmpdir / "paper.tex").write_text("\\section{方法}", encoding="utf-8")
    (tmpdir / "blob.pdf").write_bytes(b"%PDF-1.4")
    (tmpdir / "binary.txt").write_bytes(b"abc\x00def")
    (tmpdir / "gbk.txt").write_bytes("中文".encode("gbk"))
    (tmpdir / "huge.md").write_bytes(b"a" * (s.MAX_ATTACHMENT_BYTES + 1))

    block, err = s._load_attachments(
        [str(tmpdir / "notes.md"), str(tmpdir / "paper.tex")]
    )
    assert err == "" and "关键结论 A" in block and "\\section{方法}" in block
    assert block.count("<<<ATTACHMENT") == 2

    assert s._load_attachments(None) == ("", "")
    assert s._load_attachments([]) == ("", "")

    _, err = s._load_attachments([str(tmpdir / "blob.pdf")])
    assert "不支持的附件格式" in err
    _, err = s._load_attachments([str(tmpdir / "binary.txt")])
    assert "NUL" in err
    _, err = s._load_attachments([str(tmpdir / "gbk.txt")])
    assert "UTF-8" in err
    _, err = s._load_attachments([str(tmpdir / "huge.md")])
    assert "500KB" in err
    _, err = s._load_attachments([str(tmpdir / "missing.md")])
    assert "不存在" in err
    # 两个文件合计超限（各 300KB）
    (tmpdir / "a.md").write_bytes(b"a" * (300 * 1024))
    (tmpdir / "b.md").write_bytes(b"b" * (300 * 1024))
    _, err = s._load_attachments([str(tmpdir / "a.md"), str(tmpdir / "b.md")])
    assert "500KB" in err

    # 带附件的端到端：内容进入提示词，且附件参与去重
    att_captured: list[str] = []

    async def fake_att_stream(prompt, ctx=None, on_partial=None, system_prompt=None, **kwargs):
        att_captured.append(prompt)
        result = s._StreamResult()
        result.answer_parts.append("结论")
        result.finish_reason = "stop"
        result.chunks = 1
        result.ttft_ms = 1
        result.think_chars = 1
        return result, 0

    s._stream_consult = fake_att_stream
    s.API_KEY, s.BASE_URL, s.MODEL = "test", "http://localhost:1/v1", "test-model"

    async def run_att() -> None:
        import re

        out = await s._consult(
            objective="attachment end-to-end",
            context="context",
            output_format="format",
            trigger="test",
            attachments=[str(tmpdir / "notes.md")],
        )
        assert "关键结论 A" in att_captured[-1]
        assert "【附件】" in att_captured[-1]
        assert "<<<ATTACHMENT 1:" in att_captured[-1]
        match = re.search(r"consult_id=([0-9a-f]+)", out)
        assert match is not None
        s._consult_path(match.group(1)).unlink()

    asyncio.run(run_att())
    for f in tmpdir.iterdir():
        f.unlink()
    tmpdir.rmdir()

    print("== tool schema ==")

    async def schema() -> None:
        tools = await s.mcp.list_tools()
        items = tools.values() if isinstance(tools, dict) else tools
        names = {getattr(tool, "name", "") for tool in items}
        print("tools", names)
        assert "consult_expert" in names and "audit_artifact" in names
        assert "get_expert_task_status" in names and "list_expert_tasks" in names
        for tool in items:
            name = getattr(tool, "name", "")
            schema_obj = (
                getattr(tool, "inputSchema", None)
                or getattr(tool, "parameters", {})
                or {}
            )
            props = schema_obj.get("properties", {})
            required = set(schema_obj.get("required", []))
            if name == "consult_expert":
                assert "prior_consult_id" in props
            if name == "audit_artifact":
                assert "checklist" in props and "prior_audit_id" in props
            if name.startswith(("consult_expert", "audit_artifact")):
                for opt in ("api_key", "base_url", "model", "attachments"):
                    assert opt in props, (name, opt)
                    assert opt not in required, (name, opt)
                att_schema = props["attachments"]
                assert att_schema.get("type") == "array" or any(
                    branch.get("type") == "array"
                    for branch in att_schema.get("anyOf", [])
                )

    asyncio.run(schema())

    print("== 出站请求携带 high、不携带 token 上限 ==")
    s._stream_consult = _REAL_STREAM_CONSULT
    assert s.PROMPT_VERSION == "2026-11-10.3"
    assert s.REASONING_EFFORT == "high", s.REASONING_EFFORT
    # 用假客户端捕获 _stream_consult 组装的 kwargs，不发网络请求。
    seen: list[dict] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            seen.append(kwargs)
            payload = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="OK", reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

            class _OneShot:
                def __aiter__(self):
                    async def gen():
                        yield payload

                    return gen()

            return _OneShot()

    class _FakeClient:
        chat = SimpleNamespace(completions=_FakeCompletions())

    s._get_client = lambda **kwargs: _FakeClient()
    s.MIN_CALL_INTERVAL = 0.0
    result, retries = asyncio.run(
        s._stream_consult("probe", api_key="k", base_url="http://x/v1", model="m")
    )
    assert seen[0].get("reasoning_effort") == "high", seen
    assert seen[0].get("temperature") == s.TEMPERATURE, seen
    forbidden = {"max_tokens", "max_completion_tokens"}
    assert not (forbidden & set(seen[0])), seen
    assert result.answer == "OK" and retries == 0
    print("request keys:", sorted(seen[0].keys()))

    print("== 带可选参数 400：自动去掉参数重试 ==")
    from openai import APIStatusError

    def _status_400():
        return APIStatusError(
            "unsupported parameter",
            response=SimpleNamespace(status_code=400, headers={}, request=None),
            body=None,
        )

    class _RejectOptional:
        def __init__(self, reject_keys):
            self.reject_keys = set(reject_keys)
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.reject_keys & set(kwargs):
                raise _status_400()
            payload = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="fallback OK", reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

            class _OneShot:
                def __aiter__(self):
                    async def gen():
                        yield payload

                    return gen()

            return _OneShot()

    # 只拒绝 temperature：第一次带两个参数，重试时只剩 reasoning_effort
    rej = _RejectOptional(["temperature"])
    s._get_client = lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=rej))
    r, n = asyncio.run(
        s._stream_consult("p", api_key="k", base_url="http://x/v1", model="m")
    )
    assert len(rej.calls) == 2, rej.calls
    assert "temperature" in rej.calls[0] and "temperature" not in rej.calls[1]
    assert rej.calls[1].get("reasoning_effort") == "high"
    assert r.answer == "fallback OK" and n == 0  # 降级不消耗常规重试预算
    print("temperature dropped, effort kept; calls:", [sorted(c) for c in rej.calls])

    # 两个参数都拒绝：逐个降级，最后裸请求成功，仍不计入重试预算
    rej2 = _RejectOptional(["temperature", "reasoning_effort"])
    s._get_client = lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=rej2))
    r2, n2 = asyncio.run(
        s._stream_consult("p2", api_key="k", base_url="http://x/v1", model="m")
    )
    assert len(rej2.calls) == 3, rej2.calls
    assert "temperature" not in rej2.calls[2] and "reasoning_effort" not in rej2.calls[2]
    assert r2.answer == "fallback OK" and n2 == 0
    print("both dropped, retries:", n2)

    # 降级用完后仍 400：应抛出，不无限循环
    rej3 = _RejectOptional(["temperature", "reasoning_effort", "model"])
    s._get_client = lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=rej3))
    try:
        asyncio.run(s._stream_consult("p3", api_key="k", base_url="http://x/v1", model="m"))
        raise AssertionError("expected 400 to propagate")
    except APIStatusError:
        print("terminal 400 propagates after downgrades")

    # 恢复默认客户端，供后续用例使用
    s._get_client = lambda **kwargs: _FakeClient()

    print("== background task: submit -> status -> result ==")
    import re as _re

    async def run_background_case() -> None:
        gate = asyncio.Event()

        async def fake_run_expert(**kwargs):
            cid = kwargs.get("consult_id_override", "")
            (s.PARTIAL_DIR / f"{cid}.txt").write_text("部分输出", encoding="utf-8")
            await gate.wait()
            try:
                (s.PARTIAL_DIR / f"{cid}.txt").unlink()
            except Exception:
                pass
            return (
                f"[外部强模型咨询结果 · 测试 · consult_id={cid} · status=answered]\n答案正文"
            )

        s._run_expert = fake_run_expert
        s.API_KEY, s.BASE_URL, s.MODEL = "k", "http://fake/v1", "fake-model"

        # 1) wait_seconds=0：立即返回任务号
        out = await s._consult(
            objective="background task test", context="c", output_format="f",
            trigger="t", background=True, wait_seconds=0,
        )
        assert "已提交" in out and "task_id=task_" in out, out
        tid = _re.search(r"task_[0-9a-f]{10}", out).group(0)
        await asyncio.sleep(0)  # 让 runner 真正开始

        # 2) 运行中：状态 running，报告已输出字数
        st = await s._get_task_status_text(tid, wait_seconds=0)
        assert "状态 running" in st and "已输出约 4 字" in st, st
        print("running status ok:", st.splitlines()[0])

        # 3) 长轮询：任务完成后立即返回完整结果
        gate.set()
        st2 = await s._get_task_status_text(tid, wait_seconds=3)
        assert "状态 done" in st2 and "答案正文" in st2, st2
        print("done status ok:", st2.splitlines()[0])

        # 4) 未知 / 非法 task_id：不报错、不穿越
        assert "未找到任务" in await s._get_task_status_text("task_0000000000")
        assert "未找到任务" in await s._get_task_status_text("../../etc/passwd")

        # 5) 状态落盘 + 列表可找回
        assert s._task_path(tid).exists()
        assert tid in s._list_tasks_text()
        print("persisted + listed:", tid)

        # 6) 宽限期内完成：直接返回结果，不返回任务号
        async def fake_fast(**kwargs):
            return "[外部强模型咨询结果 · 测试 · consult_id=x · status=answered]\n即时答案"

        s._run_expert = fake_fast
        out2 = await s._consult(
            objective="background fast test", context="c", output_format="f",
            trigger="t", background=True, wait_seconds=3,
        )
        assert "即时答案" in out2 and "task_id=" not in out2, out2
        print("inline grace ok")

        # 7) 审计路径同样支持后台模式
        out3 = await s._audit(
            artifact="工件", checklist="S1 引用", background=True, wait_seconds=3,
        )
        assert "即时答案" in out3
        print("audit background ok")

        # 8) 默认参数确实是异步（工具 schema 默认值）
        tools = await s.mcp.list_tools()
        consult = next(t for t in tools if t.name == "consult_expert")
        schema_obj = (
            getattr(consult, "inputSchema", None)
            or getattr(consult, "parameters", {})
            or {}
        )
        props = schema_obj.get("properties", {})
        assert props["background"].get("default") is True
        assert props["wait_seconds"].get("default") == s.DEFAULT_WAIT_SECONDS
        print("tool defaults ok:", props["background"].get("default"),
              props["wait_seconds"].get("default"))

    asyncio.run(run_background_case())

    print("== 推理长度达到上限：返回推理链 / 超长落盘 ==")
    s._run_expert = _REAL_RUN_EXPERT
    s.API_KEY, s.BASE_URL, s.MODEL = "k", "http://fake/v1", "fake-model"

    async def run_truncation_case() -> None:
        calls: list[dict] = []

        async def fake_stream(prompt, ctx=None, on_partial=None, system_prompt=None, **kwargs):
            r = s._StreamResult()
            if "truncation long" in str(kwargs.get("prompt", "")) or "truncation long" in prompt:
                # 超长推理链（>= 20000 字）
                r.think_parts.append("推理" * 12000)
            else:
                # 短推理链（< 20000 字），正文为空
                r.think_parts.append("推理链片段A\n推理链片段B")
            r.think_chars = len(r.think_text)
            r.finish_reason = "length"
            r.chunks = 3
            return r, 0

        s._stream_consult = fake_stream

        # 情形一：短推理链内联返回
        out1 = await s._consult(
            objective=f"truncation short {s._new_consult_id()}",
            context="c", output_format="f", trigger="t",
        )
        assert "未产出答案" in out1 and "推理链片段A" in out1 and "推理链片段B" in out1, out1
        assert "REASONING>>>" in out1 and "临时文件" not in out1
        print("short chain inlined, chars:", len(out1))

        # 情形二：finish_reason=length 但有部分正文时也附推理链
        async def fake_with_answer(prompt, ctx=None, on_partial=None, system_prompt=None, **kwargs):
            r = s._StreamResult()
            r.think_parts.append("部分推理")
            r.think_chars = len("部分推理")
            r.answer_parts.append("部分正文")
            r.finish_reason = "length"
            r.chunks = 2
            return r, 0

        s._stream_consult = fake_with_answer
        out2 = await s._consult(
            objective=f"truncation with answer {s._new_consult_id()}",
            context="c", output_format="f", trigger="t",
        )
        assert "部分正文" in out2 and "部分推理" in out2, out2
        print("partial answer + chain ok")

        # 情形三：>= 20000 字落盘并返回绝对路径
        s._stream_consult = fake_stream
        out3 = await s._consult(
            objective=f"truncation long {s._new_consult_id()}",
            context="c", output_format="f", trigger="t",
        )
        assert "未产出答案" in out3 and "已保存到临时文件" in out3, out3
        import re as _re2
        m = _re2.search(r"(/[^\s]+\.md)", out3)
        assert m, out3
        path = Path(m.group(1))
        assert path.exists() and path.stat().st_size >= 20000, (path, path.stat().st_size)
        assert "推理" in path.read_text(encoding="utf-8")[:200]
        assert "推理链片段" not in out3  # 超长时不内联正文
        print("long chain saved:", path, path.stat().st_size, "bytes")
        path.unlink()  # 清理测试产物

    asyncio.run(run_truncation_case())

    print("ALL OFFLINE TESTS PASS")


if __name__ == "__main__":
    main()
