"""End-to-end check over the real stdio transport Cherry will use.

Spawns the server exactly as the MCP config does, then speaks MCP JSON-RPC:
initialize -> tools/list -> tools/call (cache hit, so no extra API spend).
"""

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

CMD = ["uv", "run", "--project", str(Path(__file__).resolve().parent), "ask-expert"]

proc = subprocess.Popen(
    CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)

q: "queue.Queue[str]" = queue.Queue()


def reader() -> None:
    for line in proc.stdout:
        q.put(line)


threading.Thread(target=reader, daemon=True).start()


def send(msg: dict) -> None:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(timeout: float = 60.0):
    try:
        return json.loads(q.get(timeout=timeout))
    except queue.Empty:
        return None


def rpc(mid: int, method: str, params: dict, timeout: float = 60.0):
    send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
    while True:
        msg = recv(timeout)
        if msg is None:
            return None
        if msg.get("id") == mid:
            return msg


init = rpc(1, "initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "stdio-check", "version": "0"},
})
print("INITIALIZE:", "ok" if init and "result" in init else f"FAILED {init}")
server = (init or {}).get("result", {}).get("serverInfo", {})
print("  serverInfo:", server)

send({"jsonrpc": "2.0", "method": "notifications/initialized"})

tools = rpc(2, "tools/list", {})
names = [t["name"] for t in (tools or {}).get("result", {}).get("tools", [])]
print("TOOLS:", names)
if names:
    first = tools["result"]["tools"][0]
    print("  desc chars:", len(first.get("description") or ""))
    print("  required:", first.get("inputSchema", {}).get("required"))

call = rpc(3, "tools/call", {
    "name": "consult_expert",
    "arguments": {
        "objective": "算出 7*191 的结果，并给出可回代检验的中间步骤。",
        "context": "纯算术题，无外部依赖。",
        "output_format": "一行等式给出结果，另起一行给出可回代检验的验算等式。",
        "trigger": "stdio 传输链路验证",
        "question_type": "math_reasoning",
    },
}, )
text = ""
if call and "result" in call:
    for block in call["result"].get("content", []):
        if block.get("type") == "text":
            text = block["text"]
print("CALL ok:", bool(text))
print("  is_cached_frame:", "重复提问" in text)
print("  has_frame_tail:", "处理要求" in text)
print("  head:", text[:120].replace("\n", " / "))

# 真实流式调用走完整协议链路（非缓存）：验证 stream=True 在 MCP 传输层可用。
# 用一个全新的 code_debug 问题，既覆盖类型模板注入，又不与上面重复。
stream_call = rpc(5, "tools/call", {
    "name": "consult_expert",
    "arguments": {
        "objective": "这段 Python 代码为什么会抛 UnboundLocalError？",
        "context": (
            "代码如下：\n"
            "def f(flag):\n"
            "    if flag:\n"
            "        total = 1\n"
            "    return total\n"
            "调用 f(False) 时报错。报错原文：UnboundLocalError: "
            "local variable 'total' referenced before assignment"
        ),
        "output_format": "指出根因所在行 → 解释作用域规则 → 给出两处最小改法 → 给出验证命令。",
        "trigger": "stdio 传输层真实流式调用验证",
        "question_type": "code_debug",
        "attempts": "已尝试在 return 前打印 total，仍然报同样错误；Python 3.12，环境无第三方包。",
    },
}, timeout=420.0)  # 流式长调用，需远大于默认 60s
stext = ""
if stream_call and "result" in stream_call:
    for block in stream_call["result"].get("content", []):
        if block.get("type") == "text":
            stext = block["text"]
print("STREAM CALL ok:", bool(stext))
print("  not_cached:", "重复提问" not in stext)
print("  not_failed:", "咨询失败" not in stext)
print("  has_frame_tail:", "处理要求" in stext)
print("  has_process_note:", "[过程]" in stext)
print("  mentions_cause:", "UnboundLocalError" in stext or "作用域" in stext)
print("  head:", stext[:160].replace("\n", " / "))

reject = rpc(4, "tools/call", {
    "name": "consult_expert",
    "arguments": {
        "objective": "修复报错",
        "context": "执行时报错。",
        "output_format": "给修复方案。",
        "trigger": "P0 校验的传输层验证",
        "question_type": "code_debug",
    },
})
rtext = ""
if reject and "result" in reject:
    for block in reject["result"].get("content", []):
        if block.get("type") == "text":
            rtext = block["text"]
print("REJECT via transport:", "[咨询未执行]" in rtext and "attempts" in rtext)
print("  head:", rtext[:100].replace("\n", " / "))

proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()

err = proc.stderr.read()[-800:]
if err.strip():
    print("STDERR tail:", err)

ok = (
    bool(names)
    and "重复提问" in text
    and "处理要求" in text
    and bool(stext)
    and "咨询失败" not in stext
    and "处理要求" in stext
    and "[过程]" in stext
)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
