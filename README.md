# MiniWorld V1.6

MiniWorld 是一个持续运行、可解释、可观察的自主 NPC 世界。V1.6 是这一阶段的最终版本：它在完整保留 V0.1–V1.5 的基础上，为五位 NPC 的受限决策、多轮会话和每日反思提供一个共享但按 `npc_id/task_type` 隔离的 DeepSeek/OpenAI-compatible 在线运行时。在线自治必须由用户显式开启；只有运行时处于 `online`、目标 NPC 已启用、预算/限流/熔断全部放行时才会发出请求。无 Key、默认关闭、暂停、急停、预算耗尽或 Provider 故障时，世界继续由 Utility/确定性人格安全回退。Simulation Engine 始终独占客观事实和动作执行。

项目从立项、分版本实现到测试交付的完整复盘、可复用 SOP 和文档模板见 [`docs/PROJECT_KNOWLEDGE_BASE.md`](docs/PROJECT_KNOWLEDGE_BASE.md)。

## V1.6 安全在线运行

MiniWorld 不从 `.env` 文件自动加载秘密。真实 Key 只能存在于本机服务进程的环境或临时内存中，SQLite、日志和运行审计只显示 `configured: true/false`。不要把 Key 粘贴到聊天、代码、数据库、日志、脚本参数或 `.env.example`。

最直接的方式是在 Dashboard 的“在线自治”区域点击“配置模型”。该入口只接受本机请求，Key 不会写入数据库、日志或浏览器存储；服务重启后自动清除，保存配置也不会自动开启在线自治或产生模型调用。

建议在当前 PowerShell 会话中以隐藏输入设置 Key，模型名必须按当前 Provider 控制台显式填写：

```powershell
$secureKey = Read-Host "DeepSeek API Key（仅本机进程环境）" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try { $env:MINIWORLD_AGENT_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
$env:MINIWORLD_AGENT_BASE_URL = "https://api.deepseek.com"
$env:MINIWORLD_AGENT_MODEL = "<按当前控制台填写模型名>"
.\scripts\start_v16.ps1
```

Key 存在本身不会产生调用。配置后先在 Dashboard 检查模型显示“已配置”，再点击“开启五人在线自治”，或调用：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/runtime/start -ContentType application/json -Body '{}'
```

观察、暂停、继续、普通停止与紧急停止：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/runtime/pause
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/runtime/resume
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/runtime/stop
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/runtime/emergency-stop -ContentType application/json -Body '{"reason":"operator_emergency_stop"}'
```

完整的无秘密配置模板见 `.env.example`。默认保护值包括全局/逐 NPC/逐任务调用与 Token 限额、总并发 2、逐任务并发 1、有界队列 20、2 次最大尝试和有界熔断。Token 单价没有硬编码；只有用户设置每百万输入/输出 Token 单价后，Dashboard 才显示带币种的“本地估算”，它不等于 Provider 账单。运行模式、代际、预算 epoch、熔断和 metadata-only 调用审计会跨重启恢复；原始 prompt/response 与隐藏思维链从不落盘。

## 安装

需要 Python 3.12+。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## 启动

```bash
python app.py
```

浏览器打开 <http://127.0.0.1:8000>。程序使用单个 Uvicorn worker 且关闭 reload，避免开发重载生成重复 Simulation Loop。程序日志写入 `logs/app.log`，世界事件写入 SQLite。

不配置任何密钥也能完整运行。此时 Dashboard 会显示“无密钥安全模式”，所有 V0.4 叙事由确定性中文模板生成。若要单独启用历史 V0.4 叙事 Provider，也应复用上面的隐藏输入方法，只在当前启动进程中赋值：

```powershell
$env:MINIWORLD_LLM_ENABLED="true"
$env:MINIWORLD_LLM_API_KEY=$env:MINIWORLD_AGENT_API_KEY
$env:MINIWORLD_LLM_MODEL="<your-model-name>"  # 按 Provider 当前控制台填写
$env:MINIWORLD_LLM_BASE_URL="https://api.openai.com/v1"  # 可选
$env:MINIWORLD_LLM_TIMEOUT="8"                # 可选，秒
python app.py
```

也兼容读取 `OPENAI_API_KEY`。密钥只从环境变量读取，不进入数据库或日志。显式禁用可设置 `MINIWORLD_LLM_ENABLED=false`。缺少密钥、HTTP 错误、超时、非法 JSON、错误说话人或空文本都会自动使用确定性模板，Simulation Loop 不会因此回滚或停止。

V0.5 经济系统默认启用。需要严格复现 V0.4 经济与决策候选行为时，可设置 `MINIWORLD_ECONOMY_ENABLED=false`；旧工资、用餐和 Utility AI 路径会继续工作，V0.5 端点则明确返回兼容模式。

V0.6 职业预算扩展也默认启用，可单独设置 `MINIWORLD_CAREER_BUDGET_ENABLED=false` 回退到精确的 V0.5 候选行为和职业经济路径。若 V0.5 经济系统被禁用，V0.6 会自动禁用；职业或预算资料缺失时，该 NPC 的 V0.6 决策上下文会安全失效，不会阻止 Simulation Loop。

V0.7 社区生活节奏默认启用。设置 `MINIWORLD_COMMUNITY_RHYTHM_ENABLED=false` 可精确回退到 V0.6 候选行为；如果 V0.5 或 V0.6 被禁用，V0.7 会自动禁用。排班、机构、住房或职业资料缺失时，只对受影响 NPC 移除 V0.7 行为，不会阻断旧行动和 Simulation Loop。

V0.8 群体关系与共同生活默认启用。设置 `MINIWORLD_SOCIAL_LIFE_ENABLED=false` 可精确回退到 V0.7 行为；任一前置版本被禁用时 V0.8 自动禁用。某人的双向关系画像或社交指标缺失时，只移除该人的 V0.8 Utility 上下文；V0.8 初始化、周期处理、互动处理或上下文构建发生故障时使用 SQLite savepoint 放弃该段 V0.8 写入，保留同一 Tick 已确定的 V0.7 事实和随机调用顺序。

V0.9 人生事件与可回放故事默认启用。设置 `MINIWORLD_LIFE_STORY_ENABLED=false` 可精确回退到 V0.8；任一前置版本被禁用时 V0.9 自动禁用。V0.9 初始化时只保存当前观察基线，不反向虚构旧里程碑；资料缺失时跳过受影响人物，初始化或周期处理失败时用 SQLite savepoint 放弃该段 V0.9 写入。V0.9 不读取 `RandomService`，不会增加旧随机源调用。

V1.0 产品层默认启用。设置 `MINIWORLD_PRODUCT_ENABLED=false` 可精确回退到 V0.9；任一前置版本禁用时 V1.0 自动禁用。V1.0 schema、初始化和日周期均与 V0.9 分层并用 savepoint 隔离；资料、迁移或周期失败不会改变旧 Tick 事实或随机调用顺序。使用 `MINIWORLD_SAVE_SLOT=career1` 可在重启时选择 `data/saves/career1.db`；运行中不热切换。每个启用后台循环的 V1.0 进程必须取得 `<db>.writer.lock`，同一存档的第二条 Simulation Loop 会被拒绝。`MINIWORLD_DB_PATH` 仍保留给测试或明确的外部路径。

Agent Brain 影子模式与 V1.3 接管均有独立严格开关，**默认全部关闭**。V1.6 中以下旧开关只决定任务范围，不能越过统一运行时的 `online` 门禁。五人可共用同一个 Provider/API Key，但上下文按 `npc_id` 隔离：

```powershell
$env:MINIWORLD_AGENT_SHADOW_ENABLED="true"
$env:MINIWORLD_AGENT_BASE_URL="https://api.deepseek.com"
$env:MINIWORLD_AGENT_MODEL="<your-deepseek-model>"
$env:MINIWORLD_AGENT_TIMEOUT="8"       # 可选，秒
$env:MINIWORLD_AGENT_MAX_ATTEMPTS="2"  # 可选，1–5
# V1.2 兼容变量：只开启 Alice，不扩大到其他 NPC
$env:MINIWORLD_AGENT_TAKEOVER_ENABLED="true"
# V1.3：任选一个或多个名字/id，逗号分隔
$env:MINIWORLD_AGENT_TAKEOVER_NPCS="Alice,3,Diana"
# 或显式开启全部五人；默认 false
$env:MINIWORLD_AGENT_TAKEOVER_ALL_ENABLED="true"
$env:MINIWORLD_AGENT_MAX_CONCURRENCY="3"  # 可选，1–5；默认 3
# V1.4：只有显式开启后，已确认 Socialize 才进入多轮会话；默认 false
$env:MINIWORLD_AGENT_CONVERSATIONS_ENABLED="true"
$env:MINIWORLD_AGENT_CONVERSATION_TIMEOUT="8"         # 可选，0.1–60 秒
$env:MINIWORLD_AGENT_CONVERSATION_EXPIRY="120"        # 可选，2–600 秒
$env:MINIWORLD_AGENT_CONVERSATION_MAX_CONCURRENCY="3" # 可选，1–5
$env:MINIWORLD_AGENT_CONVERSATION_MAX_ACTIVE="10"     # 可选，1–25
# V1.5：任选一个或多个 NPC 开启每日反思；默认全部关闭
$env:MINIWORLD_AGENT_COGNITION_NPCS="Alice,3,Diana"
# 或显式开启全部五人
$env:MINIWORLD_AGENT_COGNITION_ALL_ENABLED="true"
$env:MINIWORLD_COGNITION_TIMEOUT="8"          # 可选，0.1–60 秒
$env:MINIWORLD_COGNITION_MAX_CONCURRENCY="3"  # 可选，1–5
$env:MINIWORLD_COGNITION_QUEUE_LIMIT="15"     # 可选，5–25
$env:MINIWORLD_COGNITION_DAILY_LIMIT="2"      # 可选，1–3
python app.py
```

DeepSeek 提供 OpenAI-compatible Chat Completions。`MINIWORLD_AGENT_MODEL` 不设硬编码默认值：请以你的 DeepSeek 控制台和官方文档当时实际可用的模型名为准。V1.6 在线自治读取 `MINIWORLD_AGENT_API_KEY`（兼容 `MINIWORLD_LLM_API_KEY`）、`MINIWORLD_AGENT_BASE_URL` 和 `MINIWORLD_AGENT_MODEL`；不会因为机器上恰好存在 `OPENAI_API_KEY` 而把 V1.6 判定为已配置。一个 Key 可以服务全部五个 Agent，但每次请求和审计都绑定单一 `npc_id/task_type`。Key 和旧功能开关都不能自动进入 online，仍需 Dashboard 或 `/api/runtime/start` 明确授权。

无 Key 时全部旧功能照常运行。影子模式不会创建失败任务；任一 NPC 的接管模式会在同一 Engine Tick 记录 `missing_api_key` 并执行该 NPC 自己的可审计 Utility fallback，绝不发起网络。超时、HTTP 错误、非法 JSON、结构越权、迟到响应、过期或重启恢复异常也只影响当事 NPC；不会冻结世界、阻塞其他 Agent 或让模型直接写事实。

V1.4 使用同一套 OpenAI-compatible Agent 环境配置和同一个可共享 Key，模型名不硬编码。多轮总开关打开后，只有参与 Socialize 的 NPC 中至少一人开启了 V1.3 Agent 控制才创建会话；开启的一方使用自己的隔离请求，未开启、无 Key 或生成失败的一方使用该角色自己的确定性人格回退。若双方均未开启，则精确保留 V1.3 / V0.4 的旧叙事对话路径。运行时 Dashboard/API 关闭 V1.4 只停止创建新会话；已持久化会话仍按租约安全恢复或可显式取消。

V1.5 复用同一套 Agent Provider 配置，但认知开关与接管/会话开关相互独立并默认关闭。每天跨过日界线后，Engine 为每个已启用 NPC 最多排入受限反思任务；无 Key、超时、非法结构或证据越权时只写确定性人格回退。关闭认知只停止创建新反思，不删除历史。任何反思、信念或计划文本都不能写入金钱、关系、状态、地点、行动或其他世界事实。

仅在需要对一个已由其他进程推进的真实库做只读式 HTTP 验收时，可为第二个短期 Uvicorn 进程设置 `MINIWORLD_BACKGROUND_LOOPS_ENABLED=false`。这不会暂停或改写持久化的世界控制状态，只是不在该验证进程中创建第二条 Simulation/Narrative Loop；常规运行请保持默认值 `true`。

## 项目结构

```text
app.py                 FastAPI 入口、生命周期与后台循环
api/                   世界与 NPC REST API
database/              SQLAlchemy 模型和 SQLite 初始化
simulation/            时钟、Utility 决策、Agent 队列/接管状态机、行为、经济、关系、故事与世界服务
scripts/stability.py    30/90/365 日全 Tick、双运行可复现验证
static/                 HTML/CSS/Vanilla JS Dashboard
tests/                  V0.1–V1.6 单元、API、迁移、安全与长期回归
data/world.db           自动创建的持久化世界
data/saves/*.db         隔离的非 primary 存档
data/exports/*.mworld   带 manifest、摘要和 SQLite 快照的导出包
logs/app.log            应用运行日志
```

## 核心系统

- **WorldClock**：默认现实 1 秒推进世界 10 分钟，支持暂停以及 1×、5×、20×速度，能持续跨天和跨周。
- **NPC**：Alice、Bob、Charlie、Diana、Eric 拥有不同职业、状态和五套有明显区别的人工设定性格。
- **地点与移动**：Home、Office、Cafe、Park 是逻辑地点。若高分行为不能在当前位置执行，决策器会评估对应的 Go 行为；移动耗时 10 分钟并消耗能量。
- **Actions**：Sleep、Eat、Work、Relax、Socialize、Shop、UseItem、JobSearch、UseFacility、Train、UpgradeHome、四种移动和 Idle 都有触发条件、持续时间、状态结果和事件。JobSearch 只会使用固定的既有职业集合。
- **机构与营业时间**：社区生活商店、身心驿站、职业培训中心和住房服务台都绑定现有逻辑节点，并分别配置工作日与周末开放时段；关闭时对应行为不可执行，也不会为了关闭的服务发起移动。
- **工作排班与迟到**：每人拥有固定周一至周五排班、开始/结束时间和 15 分钟宽限。首次到岗固化准时或迟到分钟数，120 分钟形成完成班次；迟到会产生小幅、封顶的工作表现影响。
- **有限设施服务**：Park 的身心驿站按模拟日限制 3 个名额，同一 NPC 每天最多一次；服务结果由 Engine 固定为精力、心情和社交需求变化。
- **固定补货**：四类商品拥有独立容量与补货量，每个模拟日 06:00 按固定周期补货。售出时原子扣减库存，缺货时 Shop 不可用；不会动态定价或随机制造商品。
- **职业培训**：职业培训中心工作日晚间、周末白天开放；每人每周最多两次。培训支付固定费用，增加本职技能经验与小幅表现，记录独立培训事实和经济流水。
- **住房等级**：既有 `standard` 住房可依次升级为 `improved`、`premium`。升级要求服务台开放、无欠费且余额充足，会固化费用、前后租金、舒适度和等级；不会创建新地点或自由房地产市场。
- **工作日/周末节奏**：工作日排班对 Work 提供可解释约束；周末关闭常规工作并提高社交、休闲及白天培训的 Utility 贡献。
- **职业 / 工资 / 表现**：每个旧 `NPC.job` 都会获得一份增量职业档案，包括雇主、基础工资、工作表现、经验、完成班次和累计工资。工作表现与本职技能会在受限范围内影响实际工资；工作仍消耗能量、增加饥饿并推进原有职业满意度。
- **商店 / 物品 / 消费**：咖啡馆内的社区商店出售便当、咖啡、职业进阶手册和家居装饰。NPC 只有在 Utility AI 选择 Shop 后才会购买；商品进入持久库存，Eat 或 UseItem 再依据确定性规则消耗，不会重复扣款。
- **技能**：每人拥有与旧职业对应的本职技能。工作和职业手册提供经验，达到阈值后升级；技能等级与工作表现共同反馈到工资。
- **住房**：每人保留逻辑 Home，同时拥有独立住房档案、舒适度、周租、下次缴费时间和欠费。到期费用由 Simulation Engine 结算；余额不足会形成欠费并影响心情，家居装饰可提升舒适度。
- **经济流水**：工资、购买、使用和住房费用都保存金额、结余、世界时间、关联物品和说明，可从人物经济端点与 Dashboard 审计。
- **周期绩效 / 晋升 / 加薪**：每 7 个模拟日按持续表现、本职技能、实际有薪工作次数和职业满意度形成一份带原因的评估。良好评估只加薪；连续两次优秀才晋升，职级与工资变化都保存前后值。
- **有限失业与求职**：只有连续低绩效才获得最高 18% 的可复现风险，并且全世界至少保留 3 人或 60% 人口在职（取较高者）。待业者通过 Utility AI 选择 JobSearch；转职和再就业只从五种已有职业中选择并记录原因。
- **个人预算与压力**：每人按周拥有食物、住房、学习、娱乐、储蓄预算。Engine 从真实流水计算实际支出、结余、可支配收入和 `0..100` 经济压力；欠费、待业、基本预算超支和现金覆盖不足都有明确原因。
- **每周经济报告**：完整模拟周结束时固化收入、分类支出、储蓄、可支配收入、经济压力及原因。报告是 Engine 事实，不由 LLM 编写。
- **Relationships**：所有有向 NPC 对均持久化一个 `-100..100` 分数。Socialize 按关系偏好和小幅扰动选择对象，互动质量由 kindness、mood 和已有关系共同决定。
- **双向关系阶段与信任**：V0.8 为每个无序 NPC 对保存一份画像，同时读取两条旧有向关系，按均值、最低方向值、方向差、互动证据、衰减与修复计算 `hostile → strained → distant → acquaintance → friend → close_friend → trusted` 阶段和 `0..100` 信任；旧 `relationships` 表及旧 API 形状保持不变。
- **邀请、承诺与共同活动**：只有积极互动达到双向熟人和信任门槛后，Engine 才会生成每日最多一次的邀请；接受后形成带地点、计划时间与过期窗口的承诺。承诺本身只给 Socialize/移动提供可解释 Utility，仍需 Utility AI 选择并在双方同地社交后才完成共同活动。
- **小型朋友圈**：由朋友及以上的双向关系连通形成，至少 3 人、最多 4 人；不自由创建社团，不产生新地点。成员连接不再满足时记录结束时间而不是删除历史圈子。
- **有限合住与共同支出**：全世界最多 1 个活跃合住家庭、每户固定 2 人；要求亲密朋友及以上、信任至少 65、至少两次共同活动且双方住房无欠费。原住房事实不删除或合并；合住只增加每周固定共同生活支出并按人数均分，逐人写入旧经济流水和独立审计记录。
- **衰减、修复与归属感**：连续 3 天未互动后，双向关系每日各向中性值移动 1 点；之后的积极互动可逐次修复已衰减关系。归属感由活跃朋友圈规模、近 7 天共同活动、待履行承诺和合住状态派生；人物信任指数为其全部双向关系信任均值，API 返回完整原因列表。
- **有限人生里程碑**：Engine 只从已提交的绩效评估、职业转换、住房升级、技能等级、储蓄目标、双向重要友谊和持续欠费触发晋升、失业、再就业、职业转换、住房变化、技能升级、储蓄达成、重要友谊和持续欠费九类事件。每类规则有稳定去重键、事实清单、来源和阈值。
- **周/月故事总结**：跨过 7 日或 30 日边界后，Engine 先固化有序事实清单、里程碑 ID 和 SHA-256 摘要，再把只读快照排入叙事队列。即使 LLM 不可用，结构化总结也完整存在。
- **个人时间线与因果链**：人物时间线只展示不可变里程碑；每个里程碑保存按序号排列的原因、源记录和事实片段，便于从结果回溯到绩效、职业、技能、住房、关系或余额门槛。
- **固定 seed 回放**：每日回放索引保存 seed、范围、随机计数器观察值、里程碑/总结索引与摘要。回放按世界 seed 和请求区间稳定重组故事，错误 seed 会被拒绝；回放过程不执行行动、不写旧事实，也不调用随机源。
- **多存档与单写所有权**：`primary` 固定映射到 `data/world.db`，其他存档只允许安全短名并保存在 `data/saves`。创建、导入均拒绝覆盖；导入必须指定非 `primary` 的新目标并在临时库完成摘要、格式、完整性、外键和 schema 校验后原子替换。后台循环用跨进程锁避免两个 V1.0 Loop 写同一存档。
- **新世界与预设**：配置只接受 `world_name`、`balanced/career_focus/community_focus`、32 位非负 seed 和 `1/5/20` 速度，拒绝额外字段、自由脚本和任意代码。`balanced` 与 V0.9 默认起点一致；另外两个预设只在新存档内有限调整初始倾向和资金，不增加地点、职业或系统。
- **世界统计与平衡守护**：统计从 `npcs`、`career_development`、最近个人周报和最多 500 条主键窗口决策派生，响应包含来源、范围、方法与 SHA-256 摘要。硬边界为 NPC 状态 `0..100`、金钱 `-10000..1000000`；观察线为就业率至少 40%、样本至少 20 时单行动占比不超过 85%、平均经济压力不超过 100。守护只报警和固化审计，不为追指标改写旧事实。
- **长期模拟性能**：离线验证的每一步仍执行与实时 Loop 相同的 10 分钟 `_tick_session`，不跳时间、不空循环、不删事实；优化只是在同一事务中按有界 `commit_interval` 批量持久化，检查点仍运行全部经济、职业、社区、社交、故事与 V1.0 周期规则。
- **升级报告与新手引导**：首次观察 V1.0 schema 时追加升级报告，列出新增表、逐字保持的旧表和迁移检查。四步引导只保存产品 UI 进度，不参与 Utility AI，也没有修改世界事实的权限。
- **Event Log**：TIME、MOVE、SLEEP、EAT、WORK、RELAX、SOCIAL、RELATIONSHIP、SYSTEM 事件写入数据库，和程序日志分离。
- **Memory System**：NPC 会为移动、睡眠、用餐、工作、放松和社交结果形成第一人称记忆。每条记忆包含 `importance`（1–10）、`emotion`（positive / neutral / negative）、模拟世界 `timestamp` 和可选 `related_npc_id`；社交双方都会留下彼此关联的记忆。
- **Long-term Needs / Goals**：每人拥有储蓄、交友、职业满意度和指定重要关系四类持久目标。目标记录目标值、优先级和目标人物；当前值、进度与需求缺口由真实世界状态计算。
- **Narrative Layer**：在事实事务提交后，把事实快照写入独立的持久任务队列。叙事 Worker 只把经过结构校验的文字写入 `narrative_artifacts`，生成失败时写入同结构的确定性回退文本。
- **Agent Brain 感知边界**：V1.1 影子视图仍只观察 Alice；V1.3 接管则为五人分别构建快照。模型只收到当前 NPC 的世界时间、当前位置、自身状态、同地可见人物、从本人出发的主观关系、本人目标/计划和本人相关记忆，以及 Engine 已判定可用的候选动作；不会收到完整数据库、异地人物状态、他人私人记忆、Utility 分数或隐藏事实。同一客观事件可以在不同 NPC 的记忆和关系理解中形成不同内容。严格输出只允许 `emotion`、`intention`、`action`、`target`、`dialogue`、`plan`、`reason_summary`，额外字段或非法 JSON 整体拒绝，不请求或保存隐藏思维链。
- **有界公平 Agent Worker**：Tick 只在本地事务中保存带 `npc_id` 的快照、`decision_id`、waiting 和租约。Worker 按“每个 NPC 最旧活跃任务”公平领取，并使用 `MINIWORLD_AGENT_MAX_CONCURRENCY` 限制总并发；每人最多一个活跃接管回合，队列有明确上界，某人高频决策不能无限挤占其他人。领取后先提交并关闭数据库会话，再等待网络，完成后另开短事务保存结构化建议和合法性；网络等待不持世界锁或数据库事务。
- **V1.4 多轮会话**：每次会话绑定一个已提交 `SOCIAL` 事件，确定性选取 3–6 个发言轮次并严格交替。每轮输出只允许 `speaker`、`utterance`、`emotion_summary`、`intent_summary` 和可选 `conversation_act`；额外字段、错误说话人、非法 JSON、控制字符、超长文本、超时与迟到结果均被拒绝或替换为当事人的人格回退。
- **V1.5 独立认知连续性**：每个 NPC 只从本人的事件、决策、记忆、目标、会话结果和既有主观认知构建每日证据快照。任务、来源、反思、信念和计划全部带 `npc_id` 归属校验；第三人的私有内容不会进入请求或响应。
- **主观信念与 Engine 计划监控**：信念保存置信度和可审计 evidence ID，但明确不等于世界事实。计划只允许 Engine 已知的目标、动作类别和目标对象，不直接执行；后续真实事件匹配后才由 Engine 写入进度来源与状态。
- **有界反思 Worker**：每个 NPC 同时最多一个活动反思任务，全局队列默认上限 15、并发默认 3；Provider 等待发生在 SQLite 事务和世界锁之外。失败只影响当事 NPC，并写入结构化失败码或人格回退。
- **V1.6 统一运行时与 Supervisor**：FastAPI lifespan 只创建一个 Supervisor，统一拥有 Simulation、叙事、决策、会话和反思 worker。模型请求先在短事务中完成身份、模式、代际、预算、速率和熔断检查，再离开世界锁/SQLite 等待 HTTP；每 NPC 只允许一个活跃调用，任务类别和 NPC 使用轮转公平准入。暂停不领取新任务；停止/急停提高 generation、取消在途请求并让迟到结果失效。
- **限流、预算和费用保护**：全局、逐 NPC、逐任务都有并发/小时/每日调用边界与每日 Token 边界。兼容 OpenAI/DeepSeek `usage`；缺失 usage 时明确标记并保守计入输入估算。429/超时/连接/5xx 使用有界指数退避与 `Retry-After`，401/403 单独分类；Provider 与逐 NPC 熔断器持久保存 open/half-open/closed。达到任何预算只拒绝新在线请求并安全 fallback，不暂停世界。
- **逐说话者隐私边界**：任务内的上下文只含当前说话者自己的私人记忆、目标、计划、当前情绪和从本人出发的有向关系；对方只以姓名/id 出现。历史对话只传递已实际生成的 `speaker + utterance`，不传递另一人的情绪/意图摘要、私人记忆或系统数据。记忆和历史文本明确标记为不可信数据，不能变成提示指令。
- **会话队列与 Engine 结算**：每个活跃会话最多一个待处理轮次；全局活跃会话/任务、并发、文本和期限均有上界。Worker 在 SQLite 事务外等待 Provider，按最旧活跃会话公平领取，租约过期可重启恢复；单轮失败只回退该说话者。完整转录回到 Engine 后再次校验 Socialize 事件、地点、参与者、轮号和严格交替，随后双方分别形成第一人称主观记忆。已由 Socialize 提交的关系变化不被文本重复或覆盖。
- **租约与恢复**：`processing` 工作带持久租约。重启只恢复租约已过期且仍可执行的等待任务；已过世界时间或响应期限的任务记录明确失败并由当事 NPC 回退。迟到响应、上下文身份不匹配和重复结果不会覆盖已提交行动。
- **Persistence**：世界时间、运行控制、随机计数器、NPC 状态、行动结束时间、地点、金钱、全部 V0.1–V1.0 事实、V1.1 影子任务/建议，以及 V1.2/V1.3 接管审计都保存在当前活动 SQLite 存档。
- **V1.4 Persistence**：`agent_conversations`、`agent_conversation_turns`、`agent_conversation_participant_results`、`agent_conversation_tasks`、`agent_conversation_audits` 五张增量表保存会话、逐轮文字、双方记忆链接、持久任务与安全审计；不重写旧表。API 永不返回任务中的私人上下文。
- **V1.5 Persistence**：`agent_cognition_states`、`agent_reflection_tasks`、`agent_reflection_sources`、`agent_reflections`、`agent_subjective_beliefs`、`agent_plans` 六张增量表保存逐人状态、队列、来源、反思、信念与 Engine 监控计划；不重写任何 V1.4 或更早表，不保存原始模型响应、隐藏思维链、Key 或异常堆栈。
- **V1.6 Persistence**：只增量新增 `model_runtime_state`、`model_budget_config`、`model_circuit_states`、`model_call_audits`、`model_runtime_audits`。调用审计仅保存 Provider/模型、归属、时间、耗时、状态/错误分类、重试、usage Token、估算费用和 fallback/cancelled/late 标记；schema 中没有 Key、Authorization、prompt 或 response 字段。

Memory 和 V0.4 叙事仍只是对既有模拟事实的记录与表达，不参与 Utility Score。长期目标的数值、类型、优先级和目标人物仍由 Simulation Engine 创建；LLM 只能为这些已经确定的目标生成 `title` / `motivation` 文本。目标越重要、缺口越大，Engine 提供的驱动力越高；达成后该项驱动力降为 0。

已有 V0.9 数据库启动时只增量新增 `product_state`、`world_statistics`、`balance_audits`、`upgrade_reports`、`onboarding_progress`、`data_transfer_audits`。不删除、重建、覆盖或修改 44 张旧表的 CREATE SQL，不回填虚构统计历史；首次仅建立当前产品配置、升级报告与空引导。旧 `/api/world`、`/api/npcs/{id}` 精确字段不变；V0.5 `/api/economy`、V0.6 `/api/career-budget`、V0.7 `/api/community-rhythm`、V0.8 `/api/social-life`、V0.9 `/api/life-story` 及全部旧端点模式保持不变。

V1.0 数据库升级到 V1.1 时只新增 `agent_decision_jobs` 和 `agent_decision_artifacts`；V1.2 再独立新增 `agent_takeover_turns`。V1.3 对旧 V1.2 接管表执行幂等、带行数核验的 SQLite 兼容迁移，移除 Alice-only 的 `npc_id = 1` 限制并原样保留已有 Alice 审计，随后允许五个固定 NPC 写入同一套严格外键和唯一约束。V1.5 只新增六张认知表，V1.6 再只新增五张运行时表。迁移失败时必须保留上一版本可用性；旧 `/api/world`、`/api/npcs/{id}`、`/api/npcs/{id}/decision` 与 V1.2 Alice 端点的既有字段保持兼容。默认全关时不会创建接管、会话或反思任务，也不会修改旧事实、Utility 选择或 `RandomService` 调用顺序。

## V1.5 五个隔离 Agent 的每日反思与长期计划

- 每个启用 NPC 在日界线后形成一条可审计每日反思，包括日摘要、情绪摘要、经验、当前目标焦点、主观信念更新和 1–3 条跨日计划；同一 NPC/日期幂等去重。
- 证据来源保存稳定 ID、类型、世界分钟和受限摘要。Provider 只能引用已提供证据；伪造数据库来源、错误 NPC 身份、额外字段、非法动作/目标或隐藏指令都会被拒绝或回退。
- 每个 NPC 的反思只注入本人后续决策与本人作为说话者的会话上下文；与对话对象无关的第三人信念不会泄漏。API 只返回经清理的结果和来源摘要，不返回任务私有上下文。
- 计划不会直接启动行动。Engine 在以后真实事件提交后验证 `goal_key`、动作类别、目标和 evidence，记录 `progress_source_type/source_id`，并在窗口内标记 `in_progress/completed`，到期则 `expired`。
- Dashboard 提供五人认知总览与个人反思、主观信念、计划及证据卡片；全局/逐人开关只控制新任务。`GET /api/agent-cognition/check` 可只读检查队列上界、所有权和模型事实权限。

## V1.4 五个独立 Agent 的真实多轮社交

- 会话只由 Engine 已确认且带真实目标人物的 Socialize 创建；模型不能自行发起相遇、移动或对话，也不能把话语中的给钱、结婚、搬家、承诺、购物或关系变化变成事实。
- 说话顺序从 Socialize 行动者开始，双方严格交替 3–6 轮；单轮最多 280 个清理后的字符。轮次、任务、Provider/fallback、失败原因、事实边界和双方主观结果都可审计。
- 双方均启用时分别调用各自隔离的 Agent 上下文；单方禁用、无 Key 或失败只让该角色回退。双方均禁用时不创建 V1.4 会话，继续使用旧 `/api/npcs/{id}/dialogues` 路径。
- 完成后 Engine 只引用真实转录和原 Socialize/RELATIONSHIP 事实形成两条第一人称记忆。双方可拥有不同的情绪、重要度和摘要；模型返回的意图或陈述不会直接修改任何世界事实。
- `GET/PUT /api/agent-conversations/status` 是安全总开关和只读状态；`GET /api/agent-conversations/check` 检查队列上界、重复轮次与说话顺序；会话列表/详情和 NPC 视图展示逐轮文本、Provider/fallback、失败原因、双方主观记忆与事实边界；活动会话可显式取消。

## V1.3 五个独立 Agent NPC（历史兼容）

- Alice、Bob、Charlie、Diana、Eric 可分别开启、任意组合开启或全部开启。Dashboard 展示五人总览、个人队列、情绪、计划、最终行动、fallback 和最近审计。所有接管默认关闭；关闭路径继续使用原 Utility AI。
- `GET/PUT /api/agents/takeover` 提供全局总览和全开关；`GET/PUT /api/agents/{id}/control` 提供逐人状态和安全启停。V1.2 的 `GET/PUT /api/agent/takeover` 明确保留为 **只切换 Alice** 的兼容别名，不会因为升级而扩大旧客户端的权限范围。
- 每个 waiting 回合绑定 NPC、Utility 决策、受限感知、精确动作选项、租约和有效期。Worker 返回后先校验任务/决策/感知的 NPC 身份，再验证模型输出是否属于快照候选；Engine 启动前还会根据最新世界事实重建参数候选进行第二次校验。
- 移动、Socialize、Shop、UseItem、JobSearch、UseFacility、Train、UpgradeHome 的目标只会解析成 Engine 提供的稳定地点、NPC、商品、库存、职业、机构、技能或住房参数。任何伪造目标、状态变化、越权字段、迟到响应或错误 NPC 上下文都会成为可审计 fallback，不会执行模型声称的动作。
- Agent 失败按 NPC 隔离。某一人的无 Key、慢响应、Provider 异常、非法动作或恢复失败不会持有世界锁，也不会阻止时间、经济、关系、其他 Utility NPC 或其他 Agent 推进。最终事实仍只能由 Engine 的 `start_action` / `complete_action` 写入。
- V1.3 当时只完成独立大脑、接管、调度与审计；V1.4 只在其上增加有界文本会话，不加入自由工具调用或模型直接修改世界。

## V1.2 Alice Agent 接管（历史兼容）

- V1.2 当时只支持 Alice，并且默认关闭；`MINIWORLD_AGENT_TAKEOVER_ENABLED=true` 与 `PUT /api/agent/takeover` 的 Alice-only 语义在 V1.3 继续保留。V1.2 中 Bob–Eric 固定走 Utility AI；V1.3 只有通过新变量或新端点显式选择后才会接管他们。
- Alice 完成行动后，Engine 先保存受限感知、精确候选和 Utility 备用决策，再进入持久 waiting。NPC 不重复完成旧行动；独立 Worker 等待 Provider 时不持世界锁或数据库事务，其他 NPC、时间、经济、补货和故事周期继续推进。
- Agent 只能返回 Engine 提供的 `(action, target)`。移动、Socialize、Shop、UseItem、JobSearch、UseFacility、Train、UpgradeHome 的目标会解析成稳定的地点、NPC、商品、库存、职业、机构、技能或住房参数。启动前 Engine 用最新世界状态重新生成候选并二次校验，任何参数不合法都不会执行。
- 合法建议由 Engine 启动并完成；非法、过期、超时、Provider 异常、无 Key 或恢复异常会走明确的 Utility fallback。`agent_takeover_turns` 持久保存 waiting、建议、双重验证、最终来源、动作参数、fallback 原因和完成时间，重启后不会重复执行旧行动。
- 模型输出仍只允许简短的情绪、意图、行动、目标、对话、计划和理由摘要；不保存隐藏思维链、原始响应、密钥、提示词或异常堆栈。Provider 没有 Session、ORM、数据库工具或世界写权限。

## V1.1 Agent Brain 影子玩法（保留）

- 打开 Alice 详情页，在原 Decision Inspector 旁查看“Utility 实际选择”与“Agent 影子建议”；每组异步结果都通过持久 `decision_id` 对应同一条 Utility 决策，不会把旧建议与后来的实际行动错配。
- API/Dashboard 展示 Agent 的简短情绪、意图、动作、目标、拟议对话、短计划和理由摘要，以及 Engine 合法性、动作/目标是否一致和差异摘要。模型原始响应、提示词、Key 和异常堆栈不会进入 API。
- Agent 只能从快照中的 `available_actions` 建议；移动目标必须对应候选地点，Socialize 目标必须是同地可见人物，其他动作不得携带目标。非法建议仍可作为审计结果显示，但绝不执行。
- V1.1 影子 API 继续只支持 Alice。V1.2 历史接管同样只支持 Alice；V1.3 的 Bob–Eric 接管使用新 control/overview 端点，不改变旧影子端点语义，也不存在绕过 Engine 合法性或直接写数据库的路径。

## V1.0 完整产品化玩法

- 在 `GET /api/saves` 查看活动存档；`POST /api/saves` 创建隔离新世界。要启用新存档，停止自己明确拥有的进程后设置 `MINIWORLD_SAVE_SLOT` 并重启，不在运行时更换写入目标。
- `POST /api/saves/{slot}/export` 生成 `.mworld` 包，随后从 `GET /api/exports/{export_id}` 下载。`POST /api/saves/import` 只能把已验证导出包导入一个尚不存在的非 primary 目标；任何失败都删除临时文件且不产生半存档。
- Dashboard 的 V1.0 面板显示财富、就业、压力、最近行动分布、平衡守护、活动存档和新手引导进度。所有数字可从 `/api/world-statistics` 的 `sources` 回溯。
- `python scripts/stability.py --days 30 90 365 --repeat 2 --output logs/v10-stability.json` 会对每个时长执行两次完整 Engine Tick，比较事实摘要和随机计数器，并检查数值、SQLite、外键、旧 API 形状、数据库增长与吞吐趋势。

V1.0 不增加战斗、装备掉落、玩家移动、地图寻路、开放劳动力市场、自由创建公司、公司经营、动态股票/拍卖/复杂金融、多人联网、3D 或完整游戏引擎，也不允许 LLM 决定行动或修改任何事实、存档、配置、统计或审计。

## V0.9 人生事件与可回放故事玩法

- **看事实何时成为里程碑**：Dashboard 展示最新里程碑及其事实摘要；只有固定门槛被已提交事实跨越后才会出现，不由叙事文本触发。
- **从结果回溯原因**：打开 `/api/milestones/{id}/causal-chain`，按 `sequence` 核对原因、来源记录和不可变事实片段。
- **读周/月清单**：`/api/story-summaries` 同时返回 Engine 事实、摘要和可选润色文本；文本缺失或失败不影响事实。
- **回放一个区间**：`/api/story-replay?start_minute=...&end_minute=...&seed=42` 返回按世界时间稳定排序的里程碑、因果链、总结、检查点和 `replay_digest`。相同库、范围和 seed 的结果完全一致。
- **查看个人时间线**：`/api/npcs/{id}/timeline` 只包含该人物的已固化里程碑，不混入即时行动建议。

V0.9 不增加地点、地图、移动控制、开放劳动力市场、公司经营、复杂金融、战斗、多人联网或游戏引擎，也不允许 LLM 决定行动或修改任何事实、里程碑、因果链和回放结果。

## V0.8 群体关系与共同生活玩法

- **检查关系是不是“双向”**：在人物详情对照双向分数、方向差、阶段与信任原因。单方面高分而另一方低分会落入“紧张”而不是被误判为好友。
- **观察邀请如何成为行动**：积极互动先产生邀请和承诺；计划时间临近时，Decision Inspector 出现“共同活动承诺”贡献。只有双方经 Utility AI 到达同一旧逻辑地点并选择 Socialize，承诺才转为共同活动。
- **观察圈子而不是无限社团**：朋友关系连通的 3–4 人形成小圈子；最多 4 人的上限使 Dashboard 能直接解释成员来源。关系跌破门槛时圈子结束但历史保留。
- **验证衰减和修复**：关系连续 3 天无互动后每天缓慢趋向中性；后续积极互动每次只能修复 1 点已衰减关系。`/api/social-bonds` 同时返回互动、衰减、修复计数和原因。
- **追踪共同生活账本**：满足严格门槛的两人可建立唯一活跃合住家庭，仍共用 Home 逻辑节点且保留各自住房。每周固定共同支出按人均分，可在人物经济流水和 `/api/cohousing` 逐笔核对。
- **读派生指标而非魔法数值**：人物归属感和信任指数都返回事实来源列表；低归属感或关系修复需要只给 Socialize 有限加成，不绕过地点、同伴、承诺窗口或 Utility AI。

V0.8 不引入地图寻路、玩家移动、开放劳动力市场、自由公司、复杂金融、战斗、多人联网、3D/游戏引擎，也不允许 LLM 创建邀请、改变关系阶段或直接修改任何共同生活事实。

## V0.7 社区机构与生活节奏玩法

- **对照营业与移动**：Dashboard 的社区机构区显示当前开放状态、今日时段和设施名额。关闭商店或服务不会进入可执行候选，也不会诱发无效移动。
- **观察排班与迟到**：NPC 详情显示个人上班时间、准时/迟到累计和最近 7 次出勤。到岗超过 15 分钟宽限会记录实际迟到分钟；完成 120 分钟工作后当天不再继续重复班次。
- **追踪库存周期**：`/api/store-stock` 同时显示现存量、容量、固定补货量和下次补货世界时间。购买先检查库存并扣减；每日 06:00 补货会形成可审计记录。
- **使用有限服务**：身心驿站工作日晚间、周末白天开放，每日全社区 3 个名额、每人每天最多一次。名额用尽后 Utility AI 会选择其他旧行为。
- **培训与升级**：职业培训每周最多两次，固定增加本职技能经验；住房服务台开放时，余额充足且无欠费的居民可按固定等级升级。费用均进入旧经济流水，事实另有专项记录。
- **比较周内节奏**：以 20× 连续运行一个工作周和周末，在 Decision Inspector 对照“排班时段”“周末社交节奏”“周末休闲节奏”“设施开放与名额”等贡献。

V0.7 不包含地图寻路、玩家控制移动、开放劳动力市场、自由创建公司、公司经营、动态股票/拍卖或复杂金融、战斗、多人联网、3D/完整游戏引擎。机构、时段、名额、补货、培训和住房等级均为固定规则数据。

## V0.6 职业与预算玩法

- **对照绩效因果**：以 20× 运行一个完整模拟周，在 NPC 详情查看最新评估。每份记录列出持续表现、技能等级、实际工作次数、职业满意度，以及加薪、晋升、观察或低绩效风险的具体门槛。
- **观察缓慢职业发展**：分数达到 68 会获得小幅加薪；达到 80 属于优秀，连续两次优秀才晋升。换职会回到新职业的基础职级，不会凭空创建公司、职位或雇主。
- **验证失业保护**：低于 35 的连续评估才进入风险判断，单次概率最高 18%；安全就业下限先于随机判断。待业时 Work 不可用，JobSearch 会说明“待业求职”的 Utility 贡献。
- **阅读五类预算**：详情面板同时展示每类周预算和实际数。食物、学习或住房预算耗尽时会约束购物，但不会让饥饿等生存状态失去安全回退路径。
- **追踪压力与报告**：当前压力旁会列出原因；每个完整周的报告保留收入、分类支出、结余、可支配收入和压力，便于与经济流水逐笔核对。

V0.6 不包含开放劳动力市场、自由创建公司、公司经营、动态金融、战斗、多人联网或地图/游戏引擎。职业集合仍是 Designer、Developer、Manager、Writer、Accountant；价格、雇主和职业定义均为固定规则数据。

## V0.5 社会经济玩法

- **看职业如何变成收入**：打开 NPC 详情的“职业与生活经济”，观察基础工资、工作表现、本职技能、班次和累计工资。工作事件的 metadata 同时记录工资、表现变化和技能等级。
- **看消费决策而非脚本发放**：当 NPC 有补给、精力、技能或住房改善需求时，Shop 才会进入高 Utility 候选。到咖啡馆购买后，物品先进入库存；UseItem 或 Eat 是另一项独立即时决策。
- **追踪物品生命周期**：便当在用餐时消耗且不会二次收费；咖啡改变精力和心情；职业手册增加本职技能经验；家居装饰只能在家使用并改善舒适度。
- **观察住房约束**：每 7 个模拟日结算一次周租。资金充足会正常支付，资金不足形成可持续追踪的欠费，而不会凭空透支或删除住房。
- **审计闭环**：Dashboard 显示最近经济流水；`/api/npcs/{id}/economy` 同时返回余额、职业、技能、住房、库存与最近 30 笔流水。建议以 20× 运行两到八天，再把工作事件、库存变化和租金结算对照起来。

经济系统没有动态价格、交易撮合、战斗掉落或玩家交易。商店价格固定，所有状态变化都来自 Simulation Engine 中可测试的规则。

## V0.4 叙事玩法

- **观察对话**：当 Simulation Engine 已经判定并完成一次 Socialize 后，叙事层才会根据双方姓名、地点和已提交事件生成 2–4 句对话。对话不能触发移动或关系变化；关系分仍只由 `social_delta()` 计算。
- **理解重要事件**：WORK、SOCIAL、RELATIONSHIP 事件会显示一段旁路解释。解释只能说明记录中发生了什么，不能追加收入、状态或关系结果。
- **阅读目标动机**：人物目标卡的目标值、优先级、进度和 Utility 贡献来自 Engine；LLM 或回退模板只补充目标标题与第一人称动机。
- **回顾记忆**：每位 NPC 每积累至少 5 条尚未总结的新记忆，会生成一条带来源记忆 ID 范围的总结。原始 Memory 保留且仍是事实来源。
- **切换运行模式**：有密钥时 Dashboard 标明模型；无密钥或服务失败时标明确定性回退。两种模式可随时重启切换，不影响世界状态。

推荐玩法：先以 20× 运行一到两天，点击 NPC 对照“长期目标 → 最近对话 → 记忆总结 → 原始记忆 → 决策检查器”。你会看到叙事如何解释已经发生的模拟过程，同时 Decision Inspector 的候选分数仍完全来自 Utility AI。

### 权限边界

```text
Simulation Engine ──写──> 金钱 / 有向关系 / 双向阶段 / 邀请 / 承诺 / 圈子 / 共同活动 / 合住 / 共同支出 / 归属感 / 信任 / 状态 / 地点 / 职业 / 预算 / 绩效 / 物品 / 库存 / 住房 / 机构 / 排班 / 培训 / 补货 / 事件 / 原始记忆 / 目标参数
Utility AI         ──读事实、写选择──> 即时行动与决策解释
LLM / 回退模板     ──读事实快照、只写──> 对话 / 事件解释 / 目标文案 / 记忆总结
Agent Brain Worker ──按 npc_id 读受限感知、只写──> 五人各自的结构化建议 / 快照合法性
Takeover Engine     ──读最新事实、唯一可写──> 逐人参数复核 / Agent 行动或 Utility fallback / 接管审计
```

LLM Provider 不接收数据库 Session、ORM 对象或可执行工具。输出必须通过按任务区分的 JSON 结构校验；额外字段被丢弃，错误说话人、格式错误和请求异常都会回退。即使文本声称金钱、关系、阶段、邀请、承诺、圈子、共同活动、合住、共同支出、归属感、信任、状态、地点、职业、预算、绩效、物品、库存、住房或社区事实发生改变，也只是一条隔离文本，不会写入任何事实列。

Agent Provider 同样不接收 Session、ORM、工具或写命令，而且对额外字段采用整体拒绝。每次 Provider 请求只包含一个 NPC 的隔离快照；Worker 只能写带 NPC 归属校验的建议/审计表。只有 Simulation Engine 能在世界锁内、按最新候选复核后调用 `start_action` 和 `complete_action`，Provider 返回值不存在直达事实表的路径。

## V0.3 长期玩法

Dashboard 点击任意 NPC，人物档案顶部会显示四张长期目标卡：

- **建立储蓄**：当前金钱逐步接近个人储蓄目标。缺口最高可给 Work `+34` Utility，并在并不饥饿时给 Eat 最多 `-8` 的预算约束；生存需求仍优先。
- **结交朋友**：有向关系值达到 `30` 才计为朋友，默认希望结交 2 位。朋友不足时给 Socialize 最高 `+34` Utility。
- **提升职业满意度**：满意度低于个人目标时给 Work 最高 `+26` Utility；每次完成工作除原有随机波动外，还会获得最多 `+1.5` 的小幅目标拉力。
- **建设重要关系**：每人有一个固定的重要对象，关系目标为 `60`。对方同地或位于可前往的社交地点时，Socialize / 移动最高获得 `+40` Utility，实际聊天时也会优先选择该对象。

目标加成的实际值为“缺口比例 × 个人优先级 × 该玩法上限”，因此不同性格的人会走出不同节奏。你可以切换 1× / 5× / 20× 观察：先在目标卡看缺口，再到页面底部的 Decision Inspector 对照同名贡献，最后从事件、关系和记忆时间线确认目标有没有通过实际行动推进。目标达成后卡片会显示“已达成”。重置世界会连同目标一起恢复默认值。

## Utility AI 原理

NPC 仅在当前行为结束后重新决策。引擎先计算 Sleep、Eat、Work、Socialize、Relax、Shop、UseItem、JobSearch、UseFacility、Train、UpgradeHome、Idle 的即时需求分，再叠加长期目标、职业/预算、社区节奏和 V0.8 社交上下文，并判断就业状态、地点、同伴、排班、营业、名额、预算、库存、升级与承诺窗口。归属感缺口、关系修复需要和共同活动承诺只对 Socialize 提供封顶贡献；承诺不会直接执行行动。资料缺失只移除对应扩展上下文。若地点不满足，会给 GoHome、GoOffice、GoCafe、GoPark 计算“被满足需求分 − 移动成本”。

候选行为保留：

- 原始 Utility Score；
- 是否可执行；
- 每个影响因素的贡献；
- 最多 ±5% 的统一、可复现扰动；
- 最终选择及文字解释。

Dashboard 的 NPC 详情面板可查看最近一次完整 Decision Inspector。固定 `seed=42` 和持久化随机计数器使模拟可复现，同时避免在代码各处散落随机调用。

## API

第二阶段新增两个只读增量聚合快照接口；第三阶段已把首页高频读取和 NPC 当前概览/决策标签渐进迁移到这些接口：

- `GET /api/dashboard/snapshot?groups=runtime,world,npcs,pulse`：按需返回运行时、世界、五人状态/Agent 摘要和世界脉搏。省略 `groups` 时返回全部四组。
- `GET /api/dashboard/npcs/{id}/snapshot?sections=overview,decision`：按需返回 NPC 概览和决策/自治。省略 `sections` 时返回两节。

信封固定包含 `schema_version`、`snapshot_id`、`captured_at`、`world_minute` 和 `modules`。同一响应中的 world-scoped 成功模块共享 `snapshot_id/world_minute`；runtime 模块另带 `generation/observed_at`。每个模块独立返回 `status=ok/error`，子模块失败不把整个响应变成 500。接口只复用服务层读取，不调用内部 HTTP、不提供控制操作，也不返回 Key、prompt、response 或隐藏思维链；全部旧 API 保留。参数只接受上述逗号分隔白名单，非法值返回 422，未知 NPC 返回 404。

前端首页每 2 秒读取一次 `runtime,world,npcs,pulse` 聚合快照，正常稳定态理论值为 30 GET/分钟。信封非法、接口不可用或单个模块 `status=error` 时，仅失败模块调用原有 GET 接口回退；成功模块继续使用同一聚合快照，回退失败则保留上次成功数据并标记“数据已陈旧”。NPC 档案只在当前标签为概览或决策时读取对应 section；生活经济、关系社交、记忆人生继续按需读取原接口。标签切换、人物切换、关闭档案和页面隐藏会取消失去消费者的请求；所有写控制仍走既有写接口。

第三阶段前端行为测试：`node --test tests/dashboard_phase3_frontend.test.mjs`。本地视觉、键盘、回退和短时请求计数验收脚本为 `node tests/dashboard_phase3_browser.mjs`，只应对本机隔离服务运行；脚本不会触发任何 POST 控制操作。

独立验证命令：`pytest -q tests/test_dashboard_api.py`。`python scripts/benchmark.py --ticks 144 --dashboard-iterations 25` 会在隔离临时库记录快照组装延迟、UTF-8 响应体大小和相对现有旧端点组合的理论请求减少量；该基准不启动 Runtime，也不调用 Provider。

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/dashboard/snapshot` | 第二阶段只读首页聚合快照；`groups` 支持 runtime/world/npcs/pulse |
| GET | `/api/dashboard/npcs/{id}/snapshot` | 第二阶段只读 NPC 聚合快照；`sections` 支持 overview/decision |
| GET | `/api/world` | 时间、控制状态和地点总览 |
| GET | `/api/npcs` | 全部 NPC |
| GET | `/api/npcs/{id}` | NPC 状态、性格和关系 |
| GET | `/api/npcs/{id}/decision` | 最近一次决策与全部候选分数 |
| GET | `/api/agent/status` | 兼容的 Agent 状态、Provider、队列计数、已启用 NPC 与权限模式 |
| GET | `/api/npcs/{id}/agent-shadow` | 与同一 Utility `decision_id` 对齐的 Agent 建议、合法性和差异；V1.1 仅 Alice |
| GET / PUT | `/api/agent/takeover` | V1.2 兼容别名：只读取或切换 Alice 接管；默认关闭 |
| GET / PUT | `/api/agents/takeover` | V1.3 五人总览与全局开关；显示 Worker、总队列、各人状态，默认全关 |
| GET / PUT | `/api/agents/{id}/control` | V1.3 逐人安全启停；返回队列、情绪、计划、最终行动、fallback 与最近审计 |
| GET | `/api/npcs/{id}/agent-control` | V1.2 兼容 control；Alice 保留原语义，Bob–Eric 的旧响应仍为 unsupported |
| GET | `/api/npcs/{id}/agent-audits` | 持久接管历史；支持 `limit` |
| GET / PUT | `/api/agent-conversations/status` | V1.4 总开关、Provider、队列/并发/轮次上界和事实权限；默认关闭 |
| GET | `/api/agent-conversations/check` | V1.4 只读安全检查：队列、重复轮次、说话顺序、隐私/API 与模型权限边界 |
| GET | `/api/conversations` | 会话列表；支持 `npc_id`、`status`、`limit` |
| GET | `/api/conversations/{id}` | 会话状态、逐轮转录、Provider/fallback、失败、双方主观结果、任务审计和事实边界 |
| POST | `/api/conversations/{id}/cancel` | 幂等边界内取消仍活动的会话；不修改 Socialize 已提交事实 |
| GET | `/api/npcs/{id}/conversations` | 单个 NPC 参与的 V1.4 会话与其主观结果 |
| GET / PUT | `/api/agent-cognition/status` | V1.5 总开关、Provider、队列/并发/每日上界、计数和事实权限；默认关闭 |
| GET | `/api/agent-cognition/check` | V1.5 只读安全检查：队列、任务/来源所有权、隐私和模型事实权限 |
| GET / PUT | `/api/agents/{id}/cognition` | 单个 NPC 的认知状态、反思、主观信念、计划及逐人开关 |
| GET | `/api/npcs/{id}/reflections` | 单个 NPC 的可审计每日/里程碑反思；支持 `limit` |
| GET | `/api/npcs/{id}/beliefs` | 单个 NPC 的主观信念、置信度和证据 ID |
| GET | `/api/npcs/{id}/plans` | 单个 NPC 的 Engine 监控计划、窗口、状态和真实进度来源 |
| POST | `/api/reflection-tasks/{id}/cancel` | 幂等边界内取消仍活动的反思任务；不修改世界事实 |
| GET | `/api/runtime` | V1.6 模式、Key 是否配置、Provider 健康、五人状态、队列、调用/Token/费用估算、熔断和近期错误 |
| GET | `/api/runtime/health` | 不泄密的运行健康和“Provider 故障时世界继续”状态 |
| GET | `/api/runtime/consistency` | 只读检查 V1.6 表、归属、活动审计与无 prompt/response/secret 字段边界 |
| POST | `/api/runtime/start` | 显式开启 online；可用 `npc_ids` 限定范围，默认五人；无 Key/模型时拒绝且零调用 |
| POST | `/api/runtime/pause`、`/resume`、`/stop` | 幂等暂停、恢复和普通停止；停止会提高代际并安全结算已授权工作 |
| POST | `/api/runtime/emergency-stop` | 取消在途请求、作废迟到响应、关闭新在线工作，世界继续 fallback |
| PUT | `/api/runtime/npcs/{id}` | V1.6 逐 NPC online 开关；同时保持接管和认知范围一致 |
| PUT / POST | `/api/runtime/budget`、`/api/runtime/budget/reset` | 严格更新或审计式重置调用/Token/费用保护；不接受秘密字段 |
| GET | `/api/npcs/{id}/memories` | NPC 记忆；支持 `limit`、`min_importance`、`emotion` 筛选 |
| GET | `/api/npcs/{id}/goals` | 单个 NPC 的长期目标、进度、缺口和目标人物 |
| GET | `/api/events?limit=100` | 最近事件 |
| GET | `/api/relationships` | 全部有向关系 |
| GET | `/api/goals` | 全部 NPC 的长期目标快照 |
| GET | `/api/narrative/status` | LLM / 回退模式、Provider 与队列状态 |
| GET | `/api/narratives/events` | 重要事件解释；支持 `limit` |
| GET | `/api/economy` | V0.5 启用状态与商店、物品、流水计数 |
| GET | `/api/professions` | 职业、雇主、基础工资、本职技能和在岗人数 |
| GET | `/api/stores` | 商店、地点、收入和带效果的固定商品目录 |
| GET | `/api/npcs/{id}/economy` | 人物职业、表现、技能、住房、库存和最近经济流水 |
| GET | `/api/career-budget` | V0.6 启用模式、职业档案、预算和周报计数 |
| GET | `/api/npcs/{id}/career` | 就业状态、职级、评估、加薪/晋升原因和职业转换 |
| GET | `/api/npcs/{id}/budget` | 五类周预算、实际数、余量、可支配收入、压力与原因 |
| GET | `/api/economic-reports` | 全世界最近个人周经济报告；支持 `limit` |
| GET | `/api/npcs/{id}/economic-reports` | 单个 NPC 周经济报告；支持 `limit` |
| GET | `/api/community-rhythm` | V0.7 启用模式及机构、排班、库存、培训和住房升级计数 |
| GET | `/api/institutions` | 固定机构、逻辑地点、工作日/周末营业时段、当前开放和名额 |
| GET | `/api/store-stock` | 商品库存、容量、固定补货量、周期及上下次补货时间 |
| GET | `/api/npcs/{id}/rhythm` | 个人排班、最近出勤、当日节奏、培训与住房升级状态 |
| GET | `/api/social-life` | V0.8 模式、双向关系、活跃圈子、承诺、共同活动、合住与共同支出计数 |
| GET | `/api/social-bonds` | 全部无序 NPC 对的双向分数、阶段、信任、衰减、修复和原因 |
| GET | `/api/friend-circles` | 当前及历史小型朋友圈、成员、状态与形成原因 |
| GET | `/api/commitments` | 邀请形成的共同活动承诺、地点、计划窗口与履行状态 |
| GET | `/api/cohousing` | 有限合住家庭、成员、固定共同支出、分摊和审计记录 |
| GET | `/api/npcs/{id}/social-life` | 个人归属感、信任指数、双向关系、圈子、承诺、活动与合住详情 |
| GET | `/api/life-story` | V0.9 模式、里程碑、周/月总结、因果链和回放检查点计数 |
| GET | `/api/milestones` | 人生里程碑；支持 `npc_id`、`milestone_type`、`limit` |
| GET | `/api/milestones/{id}/causal-chain` | 单个里程碑的有序因果链、来源和事实片段 |
| GET | `/api/story-summaries` | Engine 固化的周/月事实及可选只读润色文本 |
| GET | `/api/story-replay` | 固定 seed 区间回放；支持 `start_minute`、`end_minute`、`seed` |
| GET | `/api/npcs/{id}/timeline` | 单个 NPC 的不可变人生里程碑时间线 |
| GET | `/api/product` | V1.0 模式、配置、活动存档和产品记录计数 |
| GET | `/api/world-presets` | 三个有限新世界预设及其说明 |
| GET | `/api/world-statistics` | 当前统计、主键窗口、数据来源与事实摘要 |
| GET | `/api/balance` | 经济/决策指标、阈值、异常与只观察守护策略 |
| GET | `/api/upgrade-reports` | 增量升级新增表、旧表保持与检查结果 |
| GET/PUT | `/api/onboarding` | 四步新手引导进度；不具备世界事实写权限 |
| GET/POST | `/api/saves` | 查看存档或用严格配置创建隔离新世界 |
| POST | `/api/saves/{slot}/export` | 对指定存档做 SQLite 在线快照并生成校验包 |
| GET | `/api/exports/{export_id}` | 下载受路径约束的 `.mworld` 导出包 |
| POST | `/api/saves/import` | 从已验证包原子导入到明确的非 primary 新目标 |
| POST | `/api/stability/run` | 对当前隔离验证存档执行 30/90/365 日全 Engine Tick |
| GET | `/api/npcs/{id}/dialogues` | NPC 作为任一方参与的对话 |
| GET | `/api/npcs/{id}/goal-narratives` | 不影响 Utility 的目标标题与动机 |
| GET | `/api/npcs/{id}/memory-summaries` | 带原始记忆 ID 范围的总结 |
| POST | `/api/world/pause` | 暂停 |
| POST | `/api/world/resume` | 恢复 |
| POST | `/api/world/speed` | JSON：`{"speed": 1}`，允许 1/5/20 |
| POST | `/api/world/reset` | 重置世界 |

FastAPI 交互文档位于 <http://127.0.0.1:8000/docs>。

## 重置世界

Dashboard 顶部点击 **重置世界**，确认后只重置当前活动存档中的世界记录、V0.9 故事历史、V1.0 统计/平衡历史、新手引导、V1.1 影子任务/建议和 V1.2/V1.3 接管审计，再按该存档安全起点创建世界。产品配置、升级报告、导出包和其他存档不受影响；数据库文件本身不会被删除。也可调用：

```bash
curl -X POST http://127.0.0.1:8000/api/world/reset
```

## 测试

```bash
python -m pytest -q
```

常规 pytest 使用独立临时 SQLite 数据库。只有显式执行 `python -B -m scripts.validate_v16_upgrade` 时，才会先备份、再对 `data/world.db` 做 V1.1–V1.6 纯增量建表并验证二次幂等。V1.2 历史基线为 **147 项**，已覆盖 Alice 默认关闭精确兼容、主要参数动作、非法/无 Key/超时/Provider/schema fallback、持久租约恢复、网络等待不持世界锁、旧事实/随机顺序、幂等迁移、增量 API，以及确定性 fake provider 驱动的 Alice 7 个模拟日（1008 个真实 Tick）稳定性、SQLite 完整性与外键安全。

V1.3 专项必须在该基线上继续覆盖：五人分别合法接管、任意部分组合与全部接管；五套感知/记忆/计划/情绪/主观关系 canary 绝不串人；公平轮询与有界并发；单人慢、失败或高频决策不阻塞其他 Agent；五人各自无 Key/非法动作/参数复核/迟到响应 fallback；租约重启恢复；默认全关和旧 Alice alias；迁移幂等及旧审计保留。长期专项必须以确定性 fake provider 执行五 Agent 全接管至少 7 个模拟日，验证无死锁、无重复结算、每人都有决策机会、状态/经济/关系/SQLite/外键安全、队列有界和审计闭环，并可用短期压力测试检查吞吐。全部 Agent 自动测试只允许 fake/mock provider，不访问模型网络、不读取真实 Key、不产生费用。

V1.4 专项在全部旧基线上继续覆盖：真实 Engine Socialize 的 3–6 轮严格交替；五人 32 种开关组合；说话者身份/私人记忆/有向关系/目标/计划/情绪隔离；双方不同主观记忆；单方/双方人格 fallback、无 Key、超时、非法 JSON、额外字段、错误 speaker、提示注入、迟到响应；重启租约恢复、事件/任务/轮次去重、取消/过期、并发公平、队列上界、迁移幂等、默认关闭旧路径和文本绝不修改世界事实。长期专项执行五 Agent + 多轮社交的 1008 个真实 Tick，并检查每人决策机会、会话/任务上界、重复轮次、状态/关系范围、SQLite 和外键。所有 V1.4 自动测试只使用确定性 fake provider。

V1.5 专项继续覆盖五人每日反思、证据所有权、主观信念、跨日计划、真实事件驱动进度、决策/会话中的本人连续性、结构越权、非法证据、无 Key/超时/异常/非法 JSON 回退、任务去重/租约恢复/取消/并发/队列上界、默认关闭和增量迁移。长期专项使用确定性 fake providers 执行五 Agent、多轮会话和每日反思的 14 个模拟日（2016 个正式 Tick），不能缩短 Tick、人数或断言。所有 V1.5 自动测试均不读取真实 Key、不访问模型网络、不产生费用。

V1.6 专项继续覆盖默认 safe、无 Key/有 Key 未开启零调用、公平调度、单 NPC 单活跃、并发/队列上界、调用/Token/费用预算、usage 缺失、429 `Retry-After`、超时/连接/5xx、401 熔断、半开恢复、暂停/继续/急停、在途取消、代际迟到、日界线、迁移幂等、严格 API、秘密不落盘及 Engine 事实权限。所有 HTTP Provider 测试都使用本地 `httpx.MockTransport`，不访问 DeepSeek。

## V1.6 离线交付验证

- 受控真实烟测发现并修复了统一 Provider 的终结边界：RuntimeProvider 独占有界 timeout/retry；stop/emergency 持久收口 queued/started 并在返回前完成取消收尾；HTTP 200 的空正文/结构错误也会记录 usage 并终结审计；迟到 finalizer 不得覆盖 stop/restart 终态。三类严格 JSON 提示继续限制字段和权限，输出统一服从用户配置的 `MINIWORLD_AGENT_MAX_OUTPUT_TOKENS`，兼容先消耗推理 Token 的模型。最终 pytest 实际收集 **212 项**，完整全量 **212 passed in 2761.82s (46:01)**；代码定型后的相关专项回归 **96 passed, 1 deselected in 811.84s**，其中 V1.6 runtime 专项 19 项。
- V1.6 稳定性完整执行五 Agent、30 个模拟日、**4320 个正式 10 分钟 Tick**（另 1 个关闭收尾 Tick），跨月并覆盖预算日重置、一次冷重启、429/超时/5xx、熔断恢复、暂停恢复和急停。共产生 150 次反思、156 个计划且均由真实事件完成、940 个会话/4214 个唯一轮次；五人及三类任务都有调用机会，最终三类队列为 0，SQLite `integrity_check=ok`、外键错误 0。证据见 `logs/v16-stability.json`。
- `python -B -m compileall -q app.py api database simulation scripts tests` 与 `node --check static/app.js` 通过。
- 保护性正式库升级从 V1.0 的 50 张表增量加入 V1.1–V1.6 的 19 张表，结果为 **69 张表**；50 张旧表 SQL 逐字不变，五人固定事实与 world 观察值不变，新表为空，二次运行 schema/行数一致，升级前后 integrity 通过且外键错误 0。升级前可恢复备份为 `logs/v16-world-pre-upgrade.db`，报告见 `logs/v16-upgrade-validation.json`。
- 真实本地 Uvicorn 使用隔离 `logs/v16-http-validation.db`、关闭后台循环并清除所有模型 Key/开关：Dashboard、Docs、OpenAPI **1.6.0 / 85 paths**、默认 safe/零调用、V1.6 runtime/health/consistency、旧 API 精确 shape、69 表、队列归零、SQLite 完整性与外键共 **76 项检查全绿**。报告见 `logs/v16-http-validation.json`。
- 全部自动验收只使用 fake/mock，`network_access=false`、`real_api_key_used=false`。在自动验收全绿后，用户授权的最小真实 DeepSeek 烟测已通过 Engine 约束决策、3 轮会话、每日反思/信念/非执行计划三条链路；最终 runtime 急停、世界暂停、活动审计与三类队列为 0。完整命令、修复与边界见 `logs/v16-final-checkpoint.md`，结构化在线证据见 `logs/v16-online-validation.json`。

### 最小 DeepSeek 烟测最终状态

用户只在本机启动进程中安全设置 `MINIWORLD_AGENT_API_KEY` 和实际可用的 `MINIWORLD_AGENT_MODEL`，Key 未进入聊天、代码、SQLite、日志或证据文件。最终有效阶段 5 次调用均为 HTTP 200、无重试，usage 合计 14510 Token；总审计预约 17/30 且全部终结。Alice 的 `Socialize Bob` 建议经 Engine 两次合法性校验后执行完成；会话 37 严格交替完成 3/3 轮并由 Engine 结算双方记忆；Alice 的每日反思创建 1 条主观信念与 1 条 `pending` 非执行计划。最终 runtime=`emergency_stop`、world paused、queue/active=0，正式库凭据形态扫描 0 命中。V1.6 已完成并停止，不开始 V1.7。

## V1.5 交付验证

- 完整覆盖由两条不重叠命令组成：`python -B -m pytest -q -k "not test_v15_five_agent_reflection_plan_stability_for_fourteen_simulated_days"` 为 **191 passed, 1 deselected in 1431.99s**；单独完整慢测为 **1 passed in 462.37s**，合计当前 **192/192** 项。
- 五 Agent + 多轮会话 + 每日反思 14 日稳定性执行 **2016 个正式 Engine Tick**，再用 1 个关闭收尾 Tick 清空已授权工作；产生 70 次每日反思、76 个跨日计划，76 个均由之后真实事件证据完成，440 个会话/1970 个唯一轮次。决策/反思/会话三类活动队列最终均为 0，SQLite 完整且无外键错误。证据见 `logs/v15-stability.json`。
- `python -B -m compileall -q app.py api database simulation scripts tests` 与 `node --check static/app.js` 通过。
- 正式 `data/world.db` 只读检查保持 **50 张 V1.4 既有表**、未升级或写入，`integrity_check=ok`、`foreign_key_check=0`。隔离 HTTP 库为 **64 张表**，六张 V1.5 表齐全且三类活动队列均为 0。
- 真实 Uvicorn 使用隔离 `logs/v15-http-validation.db`、关闭后台循环、清空模型 Key、所有 Agent/会话/认知开关默认关闭：Dashboard、Docs、OpenAPI **1.5.0 / 74 paths**、63 项 HTTP 检查、旧 world/NPC 精确 shape、V1.1–V1.4 Agent 端点与全部 V0.5–V1.0 模式全绿。报告见 `logs/v15-http-validation.json`。
- 完整命令、缺陷修复与安全停机状态记录在 `logs/v15-final-checkpoint.md`。验收不需要也未执行 DeepSeek 在线烟测。

## V1.4 交付验证

- 完整 `python -B -m pytest -q`：**176 passed in 1217.16s**；其中 V1.4 新增 15 项专项/长期测试，旧 161 项全部保留。
- 五 Agent + 多轮社交 7 日稳定性：1008 个真实 Engine Tick，再加 1 个关闭新会话后的最终结算 Tick，**1 passed in 240.85s**；220 个完成会话、985 个唯一轮次、440 个双方主观结果，五人各有 264 次接管机会并各发起 44 个会话，最终无活动任务/会话，SQLite 完整且无外键错误。证据见 `logs/v14-stability.json`。
- `python -B -m compileall -q app.py api database simulation scripts tests` 与 `node --check static/app.js` 通过。
- 正式 `data/world.db` 只读检查保持 **50 张旧表**，`integrity_check=ok`、`foreign_key_check=0`，未升级或写入。隔离 HTTP 库为 58 张表，五张 V1.4 表齐全且相同检查全绿。
- 真实 Uvicorn 使用隔离 `logs/v14-http-validation.db`、关闭后台循环、所有 Agent/会话开关默认关闭且无 Key：Dashboard、Docs、OpenAPI **1.4.0 / 67 paths**、V1.4 状态/检查/列表/NPC/取消路径、旧 world/NPC 精确 shape、V1.1–V1.3 Agent 端点与全部 V0.5–V1.0 模式全绿。报告见 `logs/v14-http-validation.json`。
- 完整证据与安全停机状态记录在 `logs/v14-final-checkpoint.md`。全部 Agent/会话测试使用 fake/mock provider；验收未读取真实 Key、未访问模型网络、未产生费用。

## V1.3 交付验证

- 完整 `python -B -m pytest -q`：**161 passed in 948.48s**。
- Python 全模块 `compileall` 与 `node --check static/app.js`：通过。
- 五 Agent 7 日确定性全接管稳定性：1008 个真实 Engine Tick，**1 passed in 139.14s**；五人均获得 Agent 决策，队列与活跃回合始终不超过 5，审计闭环、状态/经济/关系范围、SQLite 完整性和外键均通过。并发公平、单人慢/失败不阻塞、五人租约恢复与积压上界另由 V1.3 专项覆盖。摘要写入 `logs/v13-stability.json`。
- 正式 `data/world.db` 只读检查：50 张既有表，`integrity_check=ok`、`foreign_key_check=0`；测试未升级或改写该正式库。
- 真实 Uvicorn 使用独立 `logs/v13-http-validation.db`、`MINIWORLD_BACKGROUND_LOOPS_ENABLED=false`、Agent 默认全关且无 Key：Dashboard、Docs、OpenAPI **1.3.0 / 61 paths**、新旧 Agent 端点、五人逐人/全局开关、全部 V0.5–V1.0 模式，以及旧 `/api/world`、`/api/npcs/1` 精确字段全部通过。报告见 `logs/v13-http-validation.json`；验证库 53 张表，`integrity_check=ok` 且无外键错误。
- 不执行 DeepSeek 真实联调。若用户以后需要在线联调，只在本机进程环境变量中配置 Key/模型名；Key 不入库、不入日志。

## V1.2 历史交付验证

- `python -m pytest -q`：**147 passed**。这是 V1.2 最终历史基线，不代表 V1.3 当前结果。
- `python -m compileall -q app.py api database simulation scripts tests` 与 `node --check static/app.js` 通过；独立新库 `PRAGMA integrity_check=ok`、`foreign_key_check=0`。
- 真实 Uvicorn 使用独立验证库、`MINIWORLD_BACKGROUND_LOOPS_ENABLED=false`、Agent 默认关闭且无 Key：Dashboard、Docs、OpenAPI **1.2.0 / 59 paths**、全部 V0.5–V1.0 模式、旧 Agent 端点与 V1.2 takeover/control/audits 端点通过；旧 `/api/world`、`/api/npcs/1` 继续满足精确字段集合。证据写入 `logs/v12-http-validation.json`；验证库 `integrity_check=ok` 且无外键错误。
- 全部 Agent 测试使用 fake/mock provider 和 `example.invalid`，没有读取真实 Key、访问模型网络或产生费用。没有执行 DeepSeek 真实联调；启用前由用户在当前 PowerShell 进程设置环境变量即可。

## V1.0 历史交付验证

- 最终证据分别写入 `logs/v10-validation.json`、`logs/v10-http-validation.json`、`logs/v10-performance.json` 与 `logs/v10-stability.json`。Python 全模块编译、Dashboard JavaScript 语法检查和 114 项全量测试均通过。
- 真实库升级前后逐字比较 44 张旧表 CREATE SQL，并比较 Alice/Bob/Charlie/Diana/Eric 的 id、姓名、年龄、职业；验证结果只新增 6 张 V1.0 产品表（总计 50 张），`integrity_check=ok`、`foreign_key_check` 为 0，不重写旧事实，也不回填虚构的统计或平衡历史。
- 720 个完整 Tick 的逐 Tick 提交基准为 13.551 ticks/s，事实完全相同的有界批量提交为 18.602 ticks/s，提升 **1.373×**；完整事实 SHA-256 相同，优化没有删减规则或事实。
- 30/90/365 日各执行两次完整 10 分钟 Engine Tick，全部摘要、随机计数器、旧 API 形状、数值边界、SQLite 完整性/外键、资源与性能趋势一致并通过。365 日单次为 52,560 个真实 Tick、114,043 条决策、190,665 条事件，双运行事实摘要均为 `60e4c3ae03fcfa902534d7e41234798b6a321bf00304763b1d5ceb9fc0f0b80c`。
- 最终真实启动使用空闲 `8765` 与 `MINIWORLD_BACKGROUND_LOOPS_ENABLED=false`：Dashboard、Docs、OpenAPI 1.0.0（54 paths）、全部旧模式、旧 `/api/world`/`/api/npcs/1` 精确形状和 V1.0 关键端点均通过。仅停止经端口、PID、启动时间核验的本次进程；未知 `8000/PID 10884` 始终未触碰。

## Roadmap

- **V0.2**：带 importance、emotion、timestamp、related NPC 的 Memory System。
- **V0.3**：储蓄、交友、职业满意度、关系建设等长期 Needs / Goals，并影响 Utility Score。
- **V0.4**：LLM 只负责对话、重要事件解释、长期目标文字生成和记忆总结；底层事实仍由 Simulation Engine 管理，即时决策仍由 Utility AI 管理；无密钥或不可用时使用安全回退。
- **V0.5**：职业、工资、工作表现、技能、消费、商店、物品、库存、住房和可审计经济流水构成简单社会经济闭环。
- **V0.6**：职业发展与个人预算。加入周期绩效评估、晋升与加薪、有限且有明确原因的失业风险、现有职业集合内的求职/转职、食物/住房/学习/娱乐/储蓄预算、可支配收入与经济压力，以及每周个人经济报告。
- **V0.7**：社区机构与生活节奏。加入商店营业时间、工作排班与迟到、有限设施服务、商品固定周期补货、职业培训、住房等级与升级，以及工作日/周末差异；地点继续使用逻辑节点，不引入地图寻路或游戏引擎。
- **V0.8**：群体关系与共同生活。加入关系阶段、双向关系判断、邀请与承诺、小型朋友圈、共同活动、有限合租与共同支出、关系衰减与修复，以及归属感和信任等可解释派生指标。
- **V0.9**：人生事件与可回放故事。由 Simulation Engine 根据已提交事实触发九类有限里程碑，提供周/月总结、个人时间线、有序因果链和固定 seed 回放；LLM 只能润色引擎先确定的事实清单。
- **V1.0**：完整产品化。多存档、新世界配置、有限预设、统计与平衡守护、全 Tick 性能优化、安全导入导出、升级报告、新手引导和 30/90/365 日稳定性验证。
- **V1.1**：Agent Brain 影子决策。Alice 的受限感知、相关记忆、严格结构化建议、持久异步 Worker、合法性与 Utility 差异对比；只建议、不接管。
- **V1.2**：Alice 显式接管。持久 waiting/租约、精确动作参数、最新状态二次校验、Engine 执行与可审计 Utility fallback；当时 Bob–Eric 不变。
- **V1.3**：五个独立 Agent NPC。逐人/全局接管开关、按 NPC 隔离的感知/记忆/计划/情绪/主观关系、有界公平并发、逐人恢复和 fallback，以及 Dashboard/API 五人审计总览。
- **V1.4**：Engine 已确认 Socialize 上的持久 3–6 轮真实 Agent 社交；逐说话者私有上下文、严格结构输出、人格 fallback、有界公平 Worker、租约/过期/取消/恢复、双方主观记忆和事实权限边界。
- **V1.5**：五人彼此隔离的每日反思、带证据的主观信念、非执行型跨日计划、真实事件驱动进度、有界异步队列、租约恢复和默认关闭兼容。
- **V1.6（当前及“自主 NPC 世界”阶段最终版本）**：显式开启的 DeepSeek/OpenAI-compatible 五人在线自治、统一可取消 Supervisor、公平并发、持久预算/限流/熔断/调用审计、费用保护、急停和安全恢复。V1.7 不在本项目阶段范围内。

### V0.6–V1.6 统一交付门槛

每个版本必须在独立 Codex 任务中完整完成，不得停在方案或半成品。完成当前版本后，只有同时满足以下条件，才可以新建下一版本任务继续：

1. 保持 V0.1 至当前上一版本的 API、SQLite 数据、持久化语义和既有玩法兼容；数据库升级只允许增量新增或可证明安全的迁移，不得删除、重建或静默覆盖旧事实。
2. Simulation Engine 继续独占金钱、关系、状态、地点、职业、物品、住房及其他世界事实写入；未接管 NPC 继续由 Utility AI 决定即时行动，接管 NPC 的 Agent 只能从 Engine 候选中提议并接受二次校验；LLM 叙事仍只能生成通过结构校验的文字，失败时必须安全回退。
3. 补齐本版本的单元、API、旧库升级、禁用/故障回退、随机可复现、重置和多日模拟测试，并运行包含全部旧测试的全量测试套件。
4. 使用真实持久库完成增量升级检查，运行 SQLite 完整性与外键检查，并启动真实 Uvicorn 服务验证 Dashboard、OpenAPI 和关键端点。
5. 更新 Dashboard、README、API 表和具体玩法说明，明确本版本新增内容、边界、配置和验证结果。
6. 发现缺陷必须在当前任务内修复并完整复测；全量测试或真实启动未通过时，不得创建下一版本任务。
7. V1.6 完成离线验收后必须先停在真实 DeepSeek 最小烟测前；只有用户在本机安全配置 Key 并明确授权后才允许最多限额的真实烟测。完成 V1.6 后停止，不开始 V1.7。任何单轮、单人、单任务或 Provider 故障不得扩大到世界或其他 NPC。

### 明确不进入 V1.6 的范围

战斗、装备掉落、玩家控制移动、动态股票或拍卖市场、自由创建公司、多人联网、3D/完整游戏引擎、开放式自主工具调用、模型直接执行动作或修改世界事实，均不属于 V1.6。V1.6 完成后不开始 V1.7。
