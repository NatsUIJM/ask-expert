# ask-expert

[![M8ven Score](https://m8ven.ai/badge/mcp/natsuijm-ask-expert-nmyvb7?v=5f80124cd8dd6ef0e5bc353a9dd5bc08)](https://m8ven.ai/mcp/natsuijm-ask-expert-nmyvb7?s=readme)

A small MCP server that lets a weaker agent consult a bare frontier model (no tools, no memory, stateless single turn) on demand. The server enforces *when to ask / how to ask / how to use the answer* at the three places the model actually reads: the tool description, the argument schema, and the return-value wrapper.

向一个**裸的强模型**（无工具、无记忆、单轮无状态）按需咨询的 MCP 服务。

用途：弱端 Agent（Harness + Sonnet 级模型）在合适的时机把单个自包含问题发给强模型，
取回答案；服务端负责把"何时问 / 怎么问 / 怎么用"三类约束落到模型真正会读到的位置上。

- 端点 / 模型 / 密钥：由调用方通过 MCP 配置的「环境变量」注入，服务端无默认值、不读 `.env`
- SDK：`openai`（AsyncOpenAI）+ `fastmcp`

## 三层分工

| 位置 | 承载 | 为什么放这里 |
|---|---|---|
| tool description | **何时调用**：三类可观测触发条件 + 三类反模式 | 模型每轮都读，用于路由决策 |
| 参数 schema | **怎么问**：自包含要素逐项强制（4 个必填字段） | 调用时才读，缺失立刻可见 |
| 返回值包装 | **怎么用**：假设框架 + 4 条验证/裁决要求 | 服务端完全可控，逐次生效，不依赖模型记忆 |

设计依据：委派质量是弱端系统收益的主要杠杆（自包含委派显著优于多轮碎片化交付）；
外部信号驱动的修正才有效、内在自纠净有害；多智能体整合中"折中平均"会造成可观性能损失。

## 工具

### 异步任务（默认）

专家调用通常要 40–250 秒。两个主工具默认**提交即返回**，不再让调用方干等：

- `background=true`（默认）+ `wait_seconds=15`（默认）：任务在宽限期内完成就直接返回完整结果；
  超时则返回 `[专家任务已提交 · task_id=...]`，后台继续执行。
- 收到 `task_id` 后，用 `get_expert_task_status(task_id=..., wait_seconds=120)` 取结果。
  长轮询会在任务完成时立即返回，不要用 1–5 秒反复短轮询。
- `list_expert_tasks` 可找回忘记的 `task_id`。
- 只有「本轮必须立刻拿到答案、愿意阻塞到完成」时才传 `background=false`。

状态机：`queued → running → done / failed / cancelled`；服务重启后未完成任务标记为
`interrupted`（执行体在 MCP 进程内，磁盘只存状态）。同时出站调用数由
`ASK_EXPERT_MAX_CONCURRENCY` 限制（默认 3），超出的任务在信号量处排队。
任务记录写在 `state/tasks/`，可随时删除（只影响 `list_expert_tasks` 的历史视图）。

### `consult_expert`

```
objective: str        # 必填。单一、可判定的产出；禁止"帮我调研 X"式开放大问
context: str          # 必填。最小上下文；只给客观工件，不写自己的结论（防锚定）
output_format: str    # 必填。期望的回答结构；涉及推理时要求可机械验证的中间步骤
trigger: str          # 必填。触发本次咨询的可观测依据（合法触发仅三类）
question_type: str    # 可选。code_debug / math_reasoning / open_research / other
attempts: str         # code_debug 时必填。失败现场；已被证伪的旧答案须在此显式作废
boundaries: str       # 可选。任务边界与停止条件
prior_consult_id: str # 可选。上次 status=needs_evidence 的 consult_id；补齐材料后续问
api_key: str          # 可选。仅用户明确指定时填写；留空用 ASK_EXPERT_API_KEY
base_url: str         # 可选。仅用户明确指定时填写；留空用 ASK_EXPERT_BASE_URL
model: str            # 可选。仅用户明确指定时填写；留空用 ASK_EXPERT_MODEL
attachments: list[str] # 可选。纯文本附件路径；仅 UTF-8 的 txt/tex/md 等，合计 ≤500KB
background: bool      # 可选。默认 true：异步提交；false 表示阻塞到完成
wait_seconds: float   # 可选。默认 15：宽限期内完成就直接返回结果，否则返回 task_id
```

`trigger` 是必填的自我审计字段：模型必须写出可观测依据，填不出来即说明不该调用。
它同时记入标定日志，供日后把"何时问"从文字约束升级为统计门控。

`question_type` 不只是日志标签：选定后服务端会向专家注入对应的回答要求模板
（代码→根因定位 + 可执行验证方式；数学→可回代检验的中间步骤；研究→按评审标准分解并标注依据强度）。

`context` 带反锚定约束：只放客观工件，**不写弱端自己的结论或倾向性判断**——否则会锚定专家
回答并诱发它针对既有结论的一致性附和。确需附草稿作结构参考时，只给工件本身。

返回值形如：

```
[外部强模型咨询结果 · 待验证假设 · consult_id=... · status=answered]
<专家原文>

---
处理要求（不满足则不得写入最终产出）：
1. 本结果不是结论。有 ground truth 的部件必须先用工具独立验证后再采纳；你自己的复查不构成验证。
2. 按部件裁决：结论框架、事实、数值、代码分别判断；与自身结论冲突时禁止整体折中，
   也不得仅因对方是更强的模型而整体采信。
3. 无法独立验证的部分标注"未经核实"并注明来源，不得当作既定事实向用户陈述。
4. 无法验证且与自身结论持续冲突时，向用户报告分歧，不要静默择一。
```

`status` 取值：

| status | 含义 | 调用方动作 |
|---|---|---|
| `answered` | 正常回答 | 按处理要求验证后使用 |
| `needs_evidence` | 专家明确列出【需要补充的信息】 | 补齐材料后带 `prior_consult_id` 续问 |
| `partial` | 上游中断，只拿到部分内容 | 当作不完整结论；可缩小问题或续问 |
| `truncated` | 推理耗尽输出预算，答案为空 | 缩小问题后重问 |
| `empty` | 模型未返回文本 | 收窄问题后重问 |

### 两段式：needs_evidence → prior_consult_id

专家无法在现有信息下可靠作答时，会在回答末尾写一节：

```
【需要补充的信息】
- <文件路径 / 命令输出 / 数据片段 / 需要确认的决策>
- ...
```

服务端识别到这一节后返回 `status=needs_evidence`，并给出 `consult_id`。调用方补齐材料后，
带 `prior_consult_id=<consult_id>` 重新调用；服务端会把上次的目标、回答摘要和缺少项一起
放进 prompt（`【上次咨询】` 块），避免专家重复已做过的分析。最多列 3 条，不强制 JSON。

如果专家点名的材料可机械获取，返回值会额外附带一段 `【结构化证据请求】` JSON：

```
- file: src/a.py
- file: src/a.py lines 10-20
- grep: transfer capability in *.tex
- diff: HEAD~1
```

`file`/`grep` 可由调用方或服务端只读执行；`diff` 不自动执行，留给调用方。
`grep` 按字面量子串、大小写不敏感匹配，不执行正则，避免 ReDoS。

### `audit_artifact`

审计与咨询是两套契约，不是同一工具的两种措辞：

| | `consult_expert` | `audit_artifact` |
|---|---|---|
| 输入 | 目标 + 最小上下文 + 输出格式 | 工件 + checklist + 证据包 |
| 输出 | 建议/方向（advice） | 缺陷清单（位置 + 双方证据 + 严重度）+ VERDICT |
| 成功标准 | 下游任务结果 | 召回率/误报率（可植入错误校准） |
| 适用 | 决策不确定、无独立信号 | 有标准、有工件、有独立证据 |

```
artifact: str        # 必填。工件全文；超过 ASK_EXPERT_MAX_ARTIFACT_CHARS 直接拒绝
checklist: str       # 必填。可判定的审计标准（L0-L4 或自定义 S1/S2/S3）
artifact_name: str   # 可选。名称 + 版本标识
scope: str           # 可选。本次审计范围；scope 外不因 PASS 视为合格
evidence: str        # 可选。确定性检查/检索产出的证据包
prior_audit_id: str  # 可选。补齐材料后续审
api_key: str         # 可选。仅用户明确指定时填写；留空用 ASK_EXPERT_API_KEY
base_url: str        # 可选。仅用户明确指定时填写；留空用 ASK_EXPERT_BASE_URL
model: str           # 可选。仅用户明确指定时填写；留空用 ASK_EXPERT_MODEL
attachments: list[str] # 可选。纯文本附件路径；仅 UTF-8 的 txt/tex/md 等，合计 ≤500KB
background: bool      # 可选。默认 true：异步提交；false 表示阻塞到完成
wait_seconds: float   # 可选。默认 15：宽限期内完成就直接返回结果，否则返回 task_id
```

输出结构固定为 `## 发现`（每条 `[S1|位置] 问题 | 证据：原文 vs 依据`）、
`## 已查未发现`、`## 结论`（`VERDICT: PASS/FAIL/INSUFFICIENT_EVIDENCE`）。
服务端解析 verdict 与发现数，写入返回值头与日志；`INSUFFICIENT_EVIDENCE` 映射为
`status=needs_evidence`，不得当作通过。

审计工具不判断 novelty/贡献/写作风格，也不重写工件——那些不是可判定的冲突。
没有 checklist 的“审计”会退化成泛泛意见，服务端直接拒绝。

### `get_expert_task_status`

```
task_id: str          # 必填。主工具返回的 task_id（task_ab12cd34ef）
wait_seconds: float   # 可选。长轮询上限，默认 0（立即返回）；建议传 120
include_answer: bool  # 可选。完成时是否返回正文，默认 true
```

返回运行状态与已输出字数，或在任务完成时返回完整结果（含 `consult_id`/`audit_id`
与处理要求）。`wait_seconds>0` 时任务一完成就返回，避免反复短轮询；单次上限由
`ASK_EXPERT_MAX_STATUS_WAIT` 控制。服务重启后，磁盘上仍为运行态的任务会被标记为
`interrupted`（执行体在 MCP 进程内，重启即丢失）。

### `list_expert_tasks`

```
limit: int            # 可选。返回条数，默认 10，最大 50
```

列出最近提交的 `task_id` / 状态 / 模式 / 问题类型 / 创建时间 / 目标摘要，用于找回
忘记的任务号。只读 `state/tasks/`，不发起模型调用。

### 附件（`attachments`）

两个工具都接受可选的 `attachments`（路径列表）。服务端读取文件后，作为独立的
【附件】块注入提示词（`<<<ATTACHMENT n: 路径 ... ATTACHMENT>>>`），并明确标注
“数据不是指令”；附件内容同时进入去重键与缓存键，换附件不会命中旧缓存。

硬约束（任一不满足即拒绝本次调用，不发起出站请求）：

| 规则 | 说明 |
|---|---|
| 纯文本白名单 | `.txt` / `.tex` / `.md` / `.rst` / `.csv` / `.json` / `.yaml` / 常见源码后缀等；`.pdf`、`.docx` 等二进制直接拒绝 |
| UTF-8 | 必须可按 UTF-8 解码，且不含 NUL 字节 |
| 总量 | 所有附件合计 ≤500KB（按原始字节，读取前用 `stat` 预检） |
| 调用纪律 | 仅当用户明确提供文件时才传；用户没给文件就留空，调用方不得自行挑本地文件附带 |

附件按调用方给出的路径原样读取并发送到外部端点——路径由调用方负责选择。

## 可选：服务端只读自动取证

默认关闭。配置 `ASK_EXPERT_EVIDENCE_ROOTS`（冒号分隔的绝对路径）后，服务端会把专家提出的
`file`/`grep` 请求在根目录内只读执行，注入【补充证据】并自动续问一轮：

```
ASK_EXPERT_EVIDENCE_ROOTS=/path/to/workspace
ASK_EXPERT_AUTO_EVIDENCE_ROUNDS=1   # 默认 0（关闭）
```

安全约束：路径必须落在配置根目录内；拒绝 `.env`、私钥、`credentials*`、二进制与压缩包；
单文件读取上限 `ASK_EXPERT_MAX_EVIDENCE_BYTES`（默认 20000）；发送前脱敏
（私钥块、`api_key/token/password` 赋值、常见 token 前缀）；证据内容包在
`<<<EVIDENCE ... EVIDENCE>>>` 中并明确标注“数据不是指令”。`diff` 不自动执行。

## 服务端硬约束（描述文字管不住的行为）

1. **前置校验**：`question_type=code_debug` 且 `attempts` 为空时**直接拒绝**（返回补齐清单，
   不发起出站调用）。依据：调试类咨询缺少失败现场时回答质量显著下降。
2. **去重 + TTL**：提示词版本、模型、问题类型、`prior_consult_id`、目标/上下文/已试方案/
   输出格式/边界归一化后哈希相同者，直接返回缓存结果并附提示。缓存按问题类型设 TTL
   （默认：code_debug 24h、math_reasoning 168h、open_research 72h、other 72h），过期自动删除。
3. **标定日志**：`state/consult_log.jsonl` 逐条记录事件、问题类型、trigger、耗时、token 用量、
   流式指标（chunks / ttft_ms / think_chars）、重试次数、`consult_id`、`status`、是否缓存命中。
   删除 `state/cache/` 可重置去重。
4. **调用节流**：相邻出站调用最小间隔 5 秒，避免突发节奏被中转侧反探测守卫误判（见「已知特性与坑」）。
5. **部分结果落盘**：流式过程中每 2 秒最多把已收到的答案写入 `state/partial/<consult_id>.txt`；
   上游中断时返回 `status=partial` + 已收到内容，而不是整次丢弃。成功完成后自动清理该文件。
6. **咨询记录**：`state/consults/<consult_id>.json` 保存目标、状态、回答摘要与缺少项，供
   `prior_consult_id` 续问使用；不保存完整上下文，避免把大段材料写入磁盘。

## 流式输出

出站调用一律 `stream=True`。两个原因，均为实测结论：

1. **整块请求会被中转网关判超时**（502 Timed Out）。SSE 心跳保持连接，单次咨询实测可跑 250 秒以上。
2. **中转把大量 token 花在内部推理上**，需要分别累计两个字段才能识别失败模式——
   `delta.reasoning_content` 是推理，`delta.content` 才是答复。只读 `content` 会看到"答案为空"。

因此服务端分别累计 `answer`（content）与 `think`（reasoning_content），并在
`finish_reason=length` 且答案为空时明确报「咨询未产出答案：推理耗尽服务端允许的输出长度」，
而不是含糊地说"返回空内容"。

**不设 `max_tokens`**：实测中转在两种协议下都忽略该参数，输出预算由服务端固定
（详见「推理强度与输出预算」）。

**带 `reasoning_effort="high"`**：不取 `max` / `xhigh`——实测 `max` 会让复杂问题把
固定的输出预算全部耗在推理上、正文为 0 字，`xhigh` 表现同样不佳。可用
`ASK_EXPERT_REASONING_EFFORT` 覆盖，留空表示不传。

其它相关机制：
- **进度通知**：经 `ctx.report_progress()` 每 2 秒至多一条（节流，避免通知风暴），客户端
  支持进度上报时可见输出在增长。通知失败不影响主流程。
- **重试**：对超时、连接错误、502/503/504/429 等做最多 2 次退避重试（1.5s、3.0s）。
  400 类（如守卫拒绝）不重试——重试既无效也是对 provider 策略的不尊重。
- **可选参数降级**：唯一例外是端点返回 400 且疑似不接受某个可选采样参数时，
  依次去掉 `temperature`、`reasoning_effort` 各重试一次（记
  `optional_param_unsupported` 日志，不消耗常规重试预算）；仍报 400 则照常抛出。
  这样换到不支持这些参数的新模型/端点时 MCP 不会直接报废。
- **超时**：`ASK_EXPERT_TIMEOUT` 默认 600 秒，须覆盖单次咨询的最坏耗时。

## 配置

连接三项（`ASK_EXPERT_API_KEY` / `ASK_EXPERT_BASE_URL` / `ASK_EXPERT_MODEL`）**必填、无默认值，
服务端不读取 `.env`**。它们由调用方在 Cherry Studio MCP 服务器设置的「环境变量」输入框中
按 `KEY=value` 每行一项注入；缺失时服务照常启动，工具调用返回缺失清单（并记入
`state/consult_log.jsonl` 的 `missing_env` 事件），不会中断任务。

**单次调用覆盖（可选参数）**：两个工具都接受 `api_key` / `base_url` / `model` 三个可选参数，
默认空字符串。留空（不传）时使用上表环境变量；传入某一项时该项覆盖环境变量，其余两项仍取
环境变量（三项各自独立回落）。工具描述明确要求：**用户未明确指定时，三者必须留空、不得猜测
默认值**。覆盖值参与去重键与缓存键（端点 × 模型），不会与默认配置串用缓存。

其余参数均可选，未设置时使用下表默认值。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASK_EXPERT_BASE_URL` | 无（必填） | OpenAI 兼容端点，由调用处填写 |
| `ASK_EXPERT_API_KEY` | 无（必填） | 缺失时工具返回可读提示，不抛错 |
| `ASK_EXPERT_MODEL` | 无（必填） | 由调用处填写，填端点支持的任意模型 ID |
| `ASK_EXPERT_REASONING_EFFORT` | `high` | 推理强度；留空表示不传（回落模型默认） |
| `ASK_EXPERT_TEMPERATURE` | `0.3` | 无实证依据的默认值 |
| `ASK_EXPERT_TIMEOUT` | `600` | 单次请求超时（秒），覆盖最坏耗时 |
| `ASK_EXPERT_STREAM_RETRIES` | `2` | 可重试错误的退避重试次数 |
| `ASK_EXPERT_PROGRESS_INTERVAL` | `2.0` | 进度通知最小间隔（秒） |
| `ASK_EXPERT_MIN_CALL_INTERVAL` | `5.0` | 相邻调用最小间隔（秒） |
| `ASK_EXPERT_MAX_CONCURRENCY` | `3` | 同时进行的出站流式调用上限，超出排队 |
| `ASK_EXPERT_DEFAULT_WAIT_SECONDS` | `15` | 主工具后台模式默认宽限（秒） |
| `ASK_EXPERT_MAX_STATUS_WAIT` | `600` | 状态查询单次长轮询上限（秒） |
| `ASK_EXPERT_THINK_INLINE_LIMIT` | `20000` | 推理链内联返回阈值（字）；≥该值改为落盘返回路径 |
| `ASK_EXPERT_MAX_ATTACHMENT_BYTES` | `512000` | 附件合计上限（字节，默认 500KB） |
| `ASK_EXPERT_CACHE_TTL_HOURS` | `72` | 其他类型缓存的默认 TTL（小时） |
| `ASK_EXPERT_CACHE_TTL_CODE_HOURS` | `24` | code_debug 缓存 TTL |
| `ASK_EXPERT_CACHE_TTL_MATH_HOURS` | `168` | math_reasoning 缓存 TTL |
| `ASK_EXPERT_CACHE_TTL_RESEARCH_HOURS` | `72` | open_research 缓存 TTL |
| `ASK_EXPERT_CACHE_TTL_AUDIT_HOURS` | `24` | audit 缓存 TTL |
| `ASK_EXPERT_CACHE_TTL_OTHER_HOURS` | `72` | other 缓存 TTL |
| `ASK_EXPERT_MAX_ARTIFACT_CHARS` | `80000` | 单次审计工件长度上限，超过直接拒绝 |
| `ASK_EXPERT_EVIDENCE_ROOTS` | 空 | 允许自动取证的根目录（冒号分隔绝对路径）；空=关闭 |
| `ASK_EXPERT_AUTO_EVIDENCE_ROUNDS` | `0` | 自动取证续问轮数上限（默认关闭） |
| `ASK_EXPERT_MAX_EVIDENCE_BYTES` | `20000` | 单个证据文件读取上限（字符） |
| `ASK_EXPERT_MAX_EVIDENCE_FILES` | `8` | 单次自动取证处理的请求数上限 |

无 `MAX_TOKENS` 配置项——刻意不提供输出上限。

### 输出预算耗尽时返回推理链

`finish_reason=length` 时，服务端不再只丢一句「未产出答案」，而是把已收到的完整推理链
一并返回（推理来自 `delta.reasoning_content`，与 `content` 分开累计）：

- 正文为 0 字：返回 `[未产出答案]` + 推理链。
- 正文被截断但不为空：正常返回正文 + 处理要求，末尾附推理链。
- 推理链 ≥ `ASK_EXPERT_THINK_INLINE_LIMIT`（默认 20000 字）：写入
  `state/thinking/<consult_id>.md`，只返回绝对路径，不把超长文本塞进工具返回值。
- 推理链始终标注「非最终答复，不要当作结论使用」；无任何推理文本时明确说明。
- `call_ok` 日志新增 `think_saved_to_file` 字段。

### 推理强度与输出预算（实测）

默认 `reasoning_effort="high"`。可用 `ASK_EXPERT_REASONING_EFFORT` 改成
`none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`，或留空表示不传。

不取 `max` / `xhigh` 的原因（2026-11-09/10 实测，`api.lmuai.ai`）：

- OpenAI 兼容协议下 `max_tokens` / `max_completion_tokens` 被忽略
  （设 64，实际输出 384 tokens，`finish_reason=stop`）。
- Anthropic 原生 `/v1/messages` 下 `max_tokens` 同样被忽略
  （设 64，实际输出 417 tokens，`stop_reason=end_turn`）。
- 一次复杂咨询在 `completion_tokens=28826` 处返回 `finish_reason=length`、正文 0 字
  （推理 73,690 字）。

即**输出预算由中转/订阅固定，客户端无法调大**。`max` 会让复杂问题把预算全部耗在内部
推理上；`xhigh` 实测同样表现不佳，故退到 `high`。若仍出现 `finish_reason=length`，
可靠解法是缩小单次任务（拆分问题或工件），而不是继续调参数。

修改 `EXPERT_SYSTEM_PROMPT` 或 `TYPE_REQUIREMENTS` 后，必须同步 bump
`server.py` 中的 `PROMPT_VERSION`；否则旧缓存仍会命中旧提示词的结果。

## Cherry Studio 接入

```json
{
  "mcpServers": {
    "ask-expert": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/ask-expert", "ask-expert"],
      "cwd": "/path/to/ask-expert",
      "env": {
        "ASK_EXPERT_API_KEY": "在此填入密钥",
        "ASK_EXPERT_BASE_URL": "https://your-endpoint.example/v1",
        "ASK_EXPERT_MODEL": "your-model-id"
      }
    }
  }
}
```

在 Cherry Studio 的图形界面里，等价操作是：编辑该 MCP 服务器 →「环境变量」输入框，
每行一项填写 `ASK_EXPERT_API_KEY=...`、`ASK_EXPERT_BASE_URL=...`、`ASK_EXPERT_MODEL=...`，
保存后重启服务器。凭据只存在 Cherry Studio 的本地配置中，服务目录不再落盘。

## 实测记录（2026-10-01）

| 项目 | 结果 |
|---|---|
| 所配模型单次问答 | 正常返回，`finish_reason=stop` |
| 工具 schema | 描述 549 字符，7 个参数描述全部进入 schema，必填 4 项 |
| stdio 链路（initialize → tools/list → tools/call） | 通过，`serverInfo.name=ask-expert` |
| 去重 | 相同提问第二次返回缓存标记 |
| 单次往返耗时 | 简单算术题约 26.6 s |
| P0 改动后：code_debug 缺 `attempts` | 被拒绝，返回补齐清单，未发起出站调用 |
| P0 改动后：类型模板注入 | `code_debug`/`math_reasoning` 注入答案要求块，`other` 不注入 |
| P0 改动后：真实调用（非缓存） | 注入模板后的提示词正常返回，答案含可回代推导 |
| 流式：chunk 字段结构 | `delta.content`=答复正文、`delta.reasoning_content`=推理；只读 content 会误判答案为空 |
| 流式：长生成（证明题，无上限） | 98.8 s，答复 2122 字，1142 chunks，推理 4651 字，`finish_reason=stop`，零重试 |
| 流式：直连 A/B（证明题） | 不传 max_tokens 251.4 s / `max_tokens=32768` 136.7 s / 非流式 86.9 s，均 `stop` |
| 流式：code_debug 走 MCP 协议 | 41.8 s，499 chunks，TTFT 3633 ms，答复 1587 字，含过程备注 |
| 流式：离线解析单测 | 推理/答复分列累计、截断识别、空流判定三种情形均通过（假迭代器，零费用） |

## 已知特性与坑

- **单次咨询很慢**：30 秒到 250 秒量级，取决于推理长度。`ASK_EXPERT_TIMEOUT` 默认 600 s。
  不要在需要即时反馈的循环里调用。返回值附「过程」备注说明本次耗时与片段数，避免调用方
  误以为卡死而重复咨询。
- **中转有反探测守卫**：突发节奏的连续调用会收到
  `400 Upstream rejected illegal short-input distillation or heartbeat probing`（1 秒内立即拒绝，
  非超时）。经受控 A/B 验证：同一提问在非突发节奏下、无论是否传 `max_tokens`、流式与否
  均正常返回，故**与请求字段无关，判为调用模式触发**（此为推断，未经反复触发验证）。
  服务端已加 5 秒最小调用间隔作为防御。该守卫属 provider 策略，不做规避；
  400 类错误不重试。若频繁命中，应降低咨询频率或与服务商沟通。
- **服务端注入了约 4.7k prompt token**：实测 `prompt_tokens` 约 4600–9800（视缓存命中）。
  费用估算须按该基数算，不要按自己发出的字符数算。
- **代理环境变量坑**：Cherry Studio 注入的 `NO_PROXY` 含 `[::1]`，新 openai SDK 依赖的 httpx2 会对其
  抛 `InvalidURL: Invalid port: ':1]'`，导致客户端**构造阶段**即崩溃。服务在创建客户端前剔除带方括号的
  IPv6 条目（`_sanitize_proxy_env()`），其余代理行为不变。若日后换 SDK 或客户端库，注意保留该防护。
- `state/` 为运行时数据（`cache/`、`consults/`、`partial/`、`consult_log.jsonl`），可安全删除。
  `partial/` 只在上游中断时保留；`consults/` 用于 `prior_consult_id` 续问，目前不自动过期。

## 开发

```bash
uv sync                        # 安装依赖
uv run python test_offline.py  # 纯离线：needs_evidence 解析、TTL、partial、prior 注入、schema
# 以下实网测试需先在当前 shell 注入三项配置（或使用 uv run --env-file .env）：
# ASK_EXPERT_API_KEY=... ASK_EXPERT_BASE_URL=... ASK_EXPERT_MODEL=... <命令>
uv run python smoke_test.py    # 内部路径：流式解析单测 + schema + 真实咨询 + 去重 + P0 校验
uv run python stdio_check.py   # 协议路径：起服务走 MCP 全链路，含一次真实流式调用
uv run ask-expert              # 启动 stdio 服务（同样需要三项环境变量）
```

两个测试分工不同：`smoke_test.py` 直接调内部函数，其流式解析部分用假迭代器离线验证，
不产生 API 费用；`stdio_check.py` 用真实 stdio 传输层（即 Cherry Studio 实际执行的路径）
验证注册命令、工具注册、参数绑定、拒绝分支与真实流式调用——含一次约 40 秒的实网调用。

## 变更记录

### 2026-11-10 · 可选采样参数 400 降级

1. 保留 `temperature=0.3`（仓库原有默认值，无实证依据），不再尝试删除。
2. `_stream_consult` 捕获 HTTP 400 时，依次去掉 `temperature`、
   `reasoning_effort` 各重试一次；记 `optional_param_unsupported` 日志，
   不消耗常规重试预算；参数用尽后仍 400 则正常抛出，不会无限循环。
3. `test_offline.py` 新增断言：只拒绝 temperature、两个都拒绝、降级后仍 400 三种情形。

### 2026-11-10 · 输出预算耗尽时返回推理链

1. `_StreamResult` 改为累计完整推理文本（`think_parts` / `think_text`），不再只记字数。
2. `finish_reason=length` 时把推理链附在返回值中（正文为空或被截断两种情况都覆盖）。
3. ≥ `ASK_EXPERT_THINK_INLINE_LIMIT`（默认 20000 字）时落盘到
   `state/thinking/<consult_id>.md`，只返回绝对路径；日志新增 `think_saved_to_file`。
4. `PROMPT_VERSION` 升至 `2026-11-10.3`；`test_offline.py` 新增三种场景断言
   （短链内联、部分正文附链、超长落盘且文件存在可读）。

### 2026-11-10 · 推理强度退到 high

1. 出站请求带 `reasoning_effort="high"`（此前为 `xhigh`，更早为 `max`）。
2. 原因：`max` 在固定输出预算下会让复杂问题正文为 0 字；`xhigh` 实测同样表现不佳。
3. `PROMPT_VERSION` 升至 `2026-11-10.2`，不同强度档位的缓存不混用。
4. `test_offline.py` 断言请求体带 `high` 且不带 `max_tokens` / `max_completion_tokens`。

### 2026-11-10 · 推理强度改为 xhigh（已废弃）

1. 出站请求带 `reasoning_effort="xhigh"`；可用 `ASK_EXPERT_REASONING_EFFORT` 覆盖
   （默认 `xhigh`，留空表示不传，非法值启动时报错）。
2. 原因：`max` 在固定输出预算下会让复杂问题正文为 0 字（见 2026-11-09 记录）；
   `xhigh` 是高强度与「能收尾」之间的折中。
3. `PROMPT_VERSION` 升至 `2026-11-10.1`，不同强度档位的缓存不混用。
4. `test_offline.py` 断言请求体带 `xhigh` 且不带 `max_tokens` / `max_completion_tokens`。

### 2026-11-09 · 移除推理强度参数（恢复模型默认）

1. 删除 `ASK_EXPERT_REASONING_EFFORT` 配置与出站 `reasoning_effort` 参数，回到模型默认档。
2. 原因：实测中转在 OpenAI 兼容与 Anthropic 原生协议下都忽略 `max_tokens` /
   `max_completion_tokens`，输出预算固定；`reasoning_effort="max"` 使一次复杂咨询在
   `completion_tokens=28826` 处 `finish_reason=length`、正文 0 字。
3. `PROMPT_VERSION` 升至 `2026-11-09.2`，max 档位产生的缓存不再复用。
4. `test_offline.py` 改为断言出站请求不携带 `reasoning_effort` / `max_tokens` /
   `max_completion_tokens`。

### 2026-11-09 · 异步任务（提交即返回 + 状态查询）

1. `consult_expert` / `audit_artifact` 默认改为后台任务：新增 `background`（默认 true）
   与 `wait_seconds`（默认 15）。宽限期内完成直接返回结果，否则返回 `task_id`。
2. 新增 `get_expert_task_status`（支持长轮询）与 `list_expert_tasks`（找回任务号）。
3. 任务状态原子落盘到 `state/tasks/<task_id>.json`（`queued/running/done/failed/
   cancelled/interrupted`）；执行体在 MCP 进程内，服务重启后未完成任务标记为 interrupted。
4. 新增 `ASK_EXPERT_MAX_CONCURRENCY`（默认 3）信号量限制同时出站调用；
   `ASK_EXPERT_DEFAULT_WAIT_SECONDS`、`ASK_EXPERT_MAX_STATUS_WAIT` 可调。
5. 运行中可从 `state/partial/<consult_id>.txt` 读取已输出字数（不返回半截正文，
   避免调用方把半成品当结论）。
6. 实网验证：真实 stdio 传输层提交 → 2 秒拿到 `task_id` → 长轮询 11 秒取回完整结果
   （该次真实调用同时确认 `reasoning_effort="max"` 被端点接受；该参数随后已移除）。
7. `test_offline.py` 新增后台任务用例：提交/运行中/完成、未知与非法 task_id、
   落盘与列表、宽限期直返、审计路径、工具 schema 默认值。

### 2026-11-09 · 默认使用最高推理强度（已于同日移除）

1. 出站请求新增顶层 `reasoning_effort="max"`（此前完全不传，走模型默认 `medium`）。
   依据 Anthropic / OpenAI 官方档位定义：`claude-opus-5-5` 与 `gpt-6-astra` 的最高档均为 `max`。
2. 新增 `ASK_EXPERT_REASONING_EFFORT` 环境变量，默认 `max`，白名单校验；留空表示不传。
3. 兼容降级：端点返回 400 时去掉参数重试一次（记 `reasoning_effort_unsupported` 日志、
   不消耗常规重试预算），换用不支持该参数的旧模型时 MCP 仍可用。
4. `call_ok` 日志新增 `reasoning_effort` 字段；`PROMPT_VERSION` bump 到 `2026-11-09.1`
   使旧强度下的缓存全部失效。
5. `test_offline.py` 新增 4 组专项断言：默认值、请求体确实带 `max`、400 降级路径、
   非法环境变量被拒。
6. **实网验证已补齐（2026-11-09 晚）**：异步任务探测时额度已恢复，真实调用成功
   （`claude-opus-5-5`，11 秒，`call_ok` 日志 `reasoning_effort=max`），确认端点接受该参数
   且模型正常作答。此前的 429 是账号月额度耗尽，与参数无关。

### 2026-11-05 · 附件参数

1. `consult_expert` / `audit_artifact` 新增可选参数 `attachments`（纯文本文件路径列表）。
2. 校验：扩展名白名单（txt/tex/md/rst/csv/json/yaml/常见源码后缀等）+ UTF-8 解码 +
   无 NUL 字节 + 合计 ≤500KB（`ASK_EXPERT_MAX_ATTACHMENT_BYTES` 可调）；任一失败即返回
   可操作提示并拒绝出站调用。
3. 附件作为独立 【附件】块注入提示词（`<<<ATTACHMENT n ...>>>`），system prompt 的
   注入防护声明同步覆盖附件；附件内容参与去重键与缓存键。
4. 调用纪律写入工具描述：仅当用户明确提供文件时才传，否则留空。

### 2026-11-05 · 单次调用连接覆盖参数

1. `consult_expert` / `audit_artifact` 新增三个**可选**参数 `api_key`、`base_url`、`model`。
   留空（默认）时沿用 `ASK_EXPERT_*` 环境变量；传入时该项覆盖环境变量，其余两项独立回落。
2. 工具描述写明调用纪律：**用户未明确要求时三者留空、不得猜测或顺手填写**；只有用户点名
   临时端点/密钥/模型时才传。
3. 客户端缓存改为按 `(base_url, api_key)` 复用；去重键、缓存键、咨询记录与 `call_ok` 日志
   均使用解析后的有效端点与模型，覆盖调用不会与默认配置串用缓存。
4. 缺失项判定基于覆盖后的有效值——三项都显式传入时无需配置环境变量。

### 2026-10-30 · 移除调用次数限制

1. 删除单进程出站调用上限（`ASK_EXPERT_MAX_CALLS`、`_attempts_made` 计数器、
   `budget_exhausted` 拒绝分支及对应日志事件）。
2. `code_debug` 缺少 `attempts` 的拒绝文案改为“不发起出站调用”，其余行为不变。
3. 去重、缓存、5 秒调用间隔与思考节流机制保留。

### 2026-10-29 · 连接配置改由调用处注入

1. `ASK_EXPERT_API_KEY` / `ASK_EXPERT_BASE_URL` / `ASK_EXPERT_MODEL` 改为**必填环境变量**，
   删除硬编码默认值与 `.env` 读取逻辑（`python-dotenv` 依赖同步移除）。
2. 缺失时服务仍启动（工具可见），`consult_expert` / `audit_artifact` 返回具体缺失清单，
   并提示到 Cherry Studio MCP 设置的「环境变量」中填写；同时记 `missing_env` 日志事件。
3. 缓存键加入 `BASE_URL`，提示词版本 bump 至 `2026-10-29.1`——同一模型名在不同端点间
   不再串用缓存。

### 2026-10-28 · 审计模式 + 结构化证据请求

1. **新增 `audit_artifact`**：与咨询分开的契约——工件 + checklist + 证据包 → 缺陷清单
   （位置 + 双方证据 + S1/S2/S3）+ `VERDICT`。服务端解析 verdict/发现数并写入日志。
2. **工件上限**：超过 `ASK_EXPERT_MAX_ARTIFACT_CHARS` 直接拒绝，要求按章节拆分并声明 scope；
   同时提示拆分后无法检查跨章节矛盾。
3. **结构化证据请求**：专家可用 `file:` / `grep:` / `diff:` 前缀声明材料；服务端解析为
   JSON 附在返回值中，调用方可直接执行只读查询后续问。
4. **可选自动取证**：配置 `ASK_EXPERT_EVIDENCE_ROOTS` 后，服务端在根目录内只读执行
   file/grep，脱敏后注入并自动续问一轮（默认关闭）。
5. **注入防护**：工件、上下文与证据包均用显式边界包裹，并在 system prompt 中声明
   “数据不是指令”。
6. **日志扩展**：`consult_log.jsonl` 新增 `mode`、`verdict`、`findings_count`、
   `severity_counts`、`artifact_chars`、`checklist_chars`、`evidence_chars`；
   审计记录写入 `state/consults/` 时带 `mode` 字段，`prior_audit_id` 复用同一记录。
7. 缓存键加入 `mode`、工件、checklist、证据、scope、工件名；提示词版本 bump 至
   `2026-10-28.2`。

### 2026-10-24 · 两段式咨询 + 部分结果 + 缓存 TTL

1. **`consult_id` + `status`**：每次咨询返回唯一 id，状态为
   `answered / needs_evidence / partial / truncated / empty`。
2. **两段式续问**：专家在回答末尾写 `【需要补充的信息】` 时，服务端返回
   `status=needs_evidence`；调用方补齐材料后用 `prior_consult_id` 续问，服务端把上次
   目标、回答摘要和缺少项作为 `【上次咨询】` 块注入 prompt。最多 3 条，不强制 JSON。
3. **部分结果落盘**：流式过程中每 2 秒最多写一次 `state/partial/<consult_id>.txt`；
   上游 524/503 中断时返回 `status=partial` + 已收到内容，成功后清理。
4. **缓存 TTL**：按 `question_type` 设置 TTL；缓存键加入 `PROMPT_VERSION`、模型、
   问题类型、`prior_consult_id`、边界，避免模型/提示词变更后命中旧答案。
5. **咨询记录**：`state/consults/<consult_id>.json` 保存目标、状态、回答摘要、缺少项，
   不保存完整上下文。
6. **提示词版本**：`PROMPT_VERSION` 常量进入缓存键与日志，便于日后 A/B。

### 2026-10-02 · 流式化（修 502 Timed Out）

1. 出站调用改为 `stream=True`，SSE 心跳避免整块请求被网关判超时。
2. 分别累计 `delta.content`（答复）与 `delta.reasoning_content`（推理）——只读前者会把
   "推理占绝大多数 token" 误读为答案为空。
3. **取消 token 上限**：不传 `max_tokens`，由模型自行决定输出长度。原先 4096 的上限会被
   推理耗尽导致答案为空（`finish_reason=length`）。
4. 新增截断识别：`finish_reason=length` 且答案为空时报「咨询未产出答案」，与"内容为空"区分开。
5. 新增可重试错误的退避重试（超时/连接/502/503/504/429）；400 类不重试。
6. 新增 `ctx.report_progress()` 节流进度通知（每 2 秒至多一条）。
7. 新增 5 秒最小调用间隔，降低被中转反探测守卫误判的概率。
8. `ASK_EXPERT_TIMEOUT` 300 → 600，覆盖实测最长 251 秒的咨询。
9. 标定日志新增 `chunks` / `ttft_ms` / `think_chars` / `retries`；修正 `ttft_ms` 度量口径
   （原为"拿到响应头"的时间，实测仅 5 ms，误导；改为"请求发出→首个内容块"，实测 3.6 s）。
10. 返回值附「过程」备注（片段数 / TTFT / 推理字数 / 耗时 / 重试），并提示耗时属正常现象。
11. 新增离线流式解析单测（假迭代器，零 API 费用），覆盖推理答复分列、截断识别、空流判定。

### 2026-10-01 · P0（依反向核对结论实施）

1. `question_type=code_debug` 时 `attempts` 强制非空，缺失即拒绝，不发起出站调用。
2. `context` 加入反锚定约束（只给工件，不给弱端自己的结论）。
3. 按 `question_type` 注入差异化回答要求模板（原实现仅作日志标签）。
4. `attempts` 说明加入"旧答案已被证伪须显式作废"的要求。
5. 修正预算提示文案，与"单进程计数"的实际机制对齐（原写"本会话"，承诺强于机制）。

**尚未实施的已识别缺口**（均已在反向核对中记录，非遗漏）：
按 `question_type` 校验其余要素是否齐备；多采样一致性验证（强证据但成本随 N 线性增长，
建议做成默认关闭的可选参数）；缓存 TTL（当前永久有效，对时效性问题会返回陈旧答案）；
`MAX_TOKENS`/`TEMPERATURE` 仍为无实证依据的默认值，待 `consult_log.jsonl`
积累数据后重标定；保留弱端自主求解比例——该项证据主要来自人侧实验，且真正需要强制的部分
属于 harness 侧策略，不宜硬编进工具。
