const locations = [
  { name: "Home", label: "家" },
  { name: "Office", label: "办公室" },
  { name: "Cafe", label: "咖啡馆" },
  { name: "Park", label: "公园" },
];

const actionLabels = {
  Sleep: "睡觉", Eat: "吃饭", Work: "工作", Relax: "放松",
  Socialize: "社交", Shop: "购物", UseItem: "使用物品", JobSearch: "求职/转职",
  UseFacility: "使用社区设施", Train: "职业培训", UpgradeHome: "升级住房", GoHome: "前往家", GoOffice: "前往办公室",
  GoCafe: "前往咖啡馆", GoPark: "前往公园", Idle: "发呆",
};
const locationLabels = { Home: "家", Office: "办公室", Cafe: "咖啡馆", Park: "公园" };
const jobLabels = { Designer: "设计师", Developer: "开发工程师", Manager: "经理", Writer: "作家", Accountant: "会计师" };
const fieldLabels = {
  energy: "能量", hunger: "饥饿", mood: "心情", social_need: "社交需求",
  work_satisfaction: "工作满意度", extroversion: "外向程度", kindness: "友善程度",
  ambition: "进取心", risk_tolerance: "风险偏好", discipline: "自律性",
};
const eventTypeLabels = {
  TIME: "时间", MOVE: "移动", SLEEP: "睡觉", EAT: "吃饭", WORK: "工作",
  RELAX: "放松", SOCIAL: "社交", RELATIONSHIP: "关系", SHOP: "购物",
  ITEM: "物品", HOUSING: "住房", CAREER_REVIEW: "绩效", CAREER_TRANSITION: "转职",
  CAREER_SEARCH: "求职", ECONOMIC_REPORT: "周报", SYSTEM: "系统",
  ATTENDANCE: "出勤", SHIFT: "班次", RESTOCK: "补货", FACILITY: "设施",
  TRAINING: "培训", HOUSING_UPGRADE: "住房升级", SOCIAL_COMMITMENT: "社交承诺",
  JOINT_ACTIVITY: "共同活动", COHOUSING: "合住", SHARED_EXPENSE: "共同支出",
};
const skillLabels = { design: "设计", programming: "编程", management: "管理", writing: "写作", accounting: "会计", professional: "职业技能" };
const transactionLabels = { wage: "工资", purchase: "消费", consume: "使用", rent: "住房", training: "培训", housing_upgrade: "住房升级", joint_activity: "共同活动", shared_expense: "共同支出" };
const stageLabels = { hostile: "敌对", strained: "紧张", distant: "疏远", acquaintance: "熟人", friend: "朋友", close_friend: "亲密朋友", trusted: "信赖伙伴" };
const milestoneLabels = { promotion: "晋升", housing_change: "住房变化", skill_upgrade: "技能升级", savings_achieved: "储蓄达成", important_friendship: "重要友谊", persistent_arrears: "持续欠费", unemployment: "失业", reemployment: "再就业", career_change: "职业转换" };
const emotionLabels = { positive: "积极", neutral: "中性", negative: "消极" };
const goalValueLabels = {
  savings: value => `$${Number(value).toFixed(0)}`,
  friendship: value => `${Number(value).toFixed(0)} 位`,
  career_satisfaction: value => Number(value).toFixed(0),
  relationship: value => Number(value).toFixed(0),
};
const weekdayLabels = {
  Monday: "星期一", Tuesday: "星期二", Wednesday: "星期三", Thursday: "星期四",
  Friday: "星期五", Saturday: "星期六", Sunday: "星期日",
};
const oldContributionLabels = {
  "Low energy": "能量不足", "Night time": "夜间加成", Discipline: "自律性",
  Hunger: "饥饿程度", "Can afford food": "餐费承受能力", "Meal-time rhythm": "用餐时段",
  Ambition: "进取心", "Work satisfaction": "工作满意度", "Working hours": "工作时段",
  "Low energy penalty": "低能量惩罚", "Hunger penalty": "高饥饿惩罚",
  "Social need": "社交需求", Extroversion: "外向程度", Mood: "当前心情",
  "Low mood": "心情低落", "Risk tolerance": "风险偏好", Baseline: "基础分",
  Contentment: "满足感", "Location ready": "地点条件满足", Continuity: "行动连续性",
  "Travel cost": "移动成本", "Recent move cooldown": "近期移动冷却",
  "Low energy travel penalty": "低能量移动惩罚",
};

const state = {
  world: null,
  npcs: null,
  runtime: null,
  selectedNpc: null,
  agentOverview: null,
  eventFilter: "all",
  events: [],
  eventNarratives: [],
  lastFocusedElement: null,
  runtimeConfigLastFocus: null,
  npcRecord: null,
};
const $ = (selector) => document.querySelector(selector);
const { HOME_GROUPS, SnapshotResolver } = window.MiniWorldDashboardSnapshots;

const requestPool = new Map();
const requestScopes = new Map();
const renderSignatures = new Map();
let requestSequence = 0;
const diagnostics = {
  startedAt: Date.now(),
  requests: 0,
  deduplicated: 0,
  aborted: 0,
  failed: 0,
  byPath: {},
  activeByPath: {},
  maxActiveByPath: {},
  timings: {},
  active: () => requestPool.size,
  snapshot() {
    return {
      startedAt: this.startedAt,
      requests: this.requests,
      deduplicated: this.deduplicated,
      aborted: this.aborted,
      failed: this.failed,
      active: requestPool.size,
      byPath: { ...this.byPath },
      maxActiveByPath: { ...this.maxActiveByPath },
      timings: Object.fromEntries(Object.entries(this.timings).map(([path, values]) => [path, {
        count: values.length,
        averageMs: Math.round(values.reduce((sum, value) => sum + value, 0) / values.length),
        maxMs: Math.round(Math.max(...values)),
      }])),
    };
  },
};
window.__MINIWORLD_DIAGNOSTICS__ = diagnostics;

function registerScope(scope, key) {
  if (!requestScopes.has(scope)) requestScopes.set(scope, new Set());
  requestScopes.get(scope).add(key);
}

function releaseScopeKey(scope, key) {
  const keys = requestScopes.get(scope);
  if (!keys) return;
  keys.delete(key);
  if (!keys.size) requestScopes.delete(scope);
}

function abortScope(scope) {
  const keys = requestScopes.get(scope);
  if (!keys) return;
  [...keys].forEach(key => {
    const entry = requestPool.get(key);
    if (!entry) return;
    entry.scopes.delete(scope);
    if (!entry.scopes.size) {
      diagnostics.aborted += 1;
      entry.controller.abort();
    }
  });
  requestScopes.delete(scope);
}

async function api(path, options = {}, scope = "global") {
  const method = String(options.method || "GET").toUpperCase();
  const dedupe = method === "GET";
  const key = dedupe ? `${method}:${path}` : `${method}:${path}:${requestSequence += 1}`;
  const existing = requestPool.get(key);
  if (dedupe && existing) {
    diagnostics.deduplicated += 1;
    existing.scopes.add(scope);
    registerScope(scope, key);
    return existing.promise;
  }

  const controller = new AbortController();
  const externalSignal = options.signal;
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  const requestOptions = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    signal: controller.signal,
  };
  const entry = { controller, scopes: new Set([scope]), promise: null };
  const requestStartedAt = performance.now();
  diagnostics.requests += 1;
  diagnostics.byPath[path] = (diagnostics.byPath[path] || 0) + 1;
  diagnostics.activeByPath[path] = (diagnostics.activeByPath[path] || 0) + 1;
  diagnostics.maxActiveByPath[path] = Math.max(diagnostics.maxActiveByPath[path] || 0, diagnostics.activeByPath[path]);
  registerScope(scope, key);

  entry.promise = fetch(path, requestOptions)
    .then(async response => {
      if (!response.ok) {
        let detail = null;
        try { detail = (await response.json()).detail; }
        catch (_) { detail = null; }
        const message = typeof detail === "object" ? (detail.message || detail.code) : detail;
        const error = new Error(message || `请求失败：${response.status}`);
        error.status = response.status;
        error.code = typeof detail === "object" ? detail.code : null;
        throw error;
      }
      return response.json();
    })
    .catch(error => {
      if (error.name !== "AbortError") diagnostics.failed += 1;
      throw error;
    })
    .finally(() => {
      const timings = diagnostics.timings[path] || [];
      timings.push(performance.now() - requestStartedAt);
      diagnostics.timings[path] = timings.slice(-120);
      diagnostics.activeByPath[path] = Math.max(0, (diagnostics.activeByPath[path] || 1) - 1);
      requestPool.delete(key);
      [...entry.scopes].forEach(entryScope => releaseScopeKey(entryScope, key));
    });
  requestPool.set(key, entry);
  return entry.promise;
}

function setHtml(target, html, signatureKey = null) {
  if (!target) return false;
  const key = signatureKey || target.id || target.className;
  if (renderSignatures.get(key) === html && target.innerHTML === html) return false;
  const focused = document.activeElement?.closest?.("[data-focus-key]")?.dataset.focusKey || null;
  target.innerHTML = html;
  renderSignatures.set(key, html);
  if (focused) target.querySelector(`[data-focus-key="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  return true;
}

function freshness(id, status, message) {
  const target = $(`#${id}`);
  if (!target) return;
  target.dataset.status = status;
  target.textContent = message;
}

function moduleFreshness(id, entry) {
  const target = $(`#${id}`);
  if (!target || !entry) return;
  target.dataset.status = entry.status;
  const time = entry.updatedAt ? new Date(entry.updatedAt).toLocaleTimeString("zh-CN", { hour12: false }) : "尚未成功";
  if (entry.status === "ok") target.textContent = entry.source === "aggregate" ? "数据同步正常" : "兼容读取正常";
  else if (entry.status === "stale") target.textContent = `暂时无法更新 · 显示 ${time} 的数据`;
  else target.textContent = "暂时无法获取数据";
}

async function settleRequests(entries, scope) {
  const results = await Promise.allSettled(entries.map(([, path, options]) => api(path, options || {}, scope)));
  return Object.fromEntries(entries.map(([name], index) => [name, results[index]]));
}

function initials(name) { return name.slice(0, 2).toUpperCase(); }
function actionLabel(action) { return actionLabels[action] || action; }
function locationLabel(location) { return locationLabels[location] || location; }
function fieldLabel(field) { return fieldLabels[field] || field.replaceAll("_", " "); }
function contributionLabel(label) {
  if (oldContributionLabels[label]) return oldContributionLabels[label];
  const match = label.match(/^Need for (.+)$/);
  return match ? `目标行为需求（${actionLabel(match[1])}）` : label.replace(/\((Sleep|Eat|Work|Relax|Socialize|Shop|UseItem|JobSearch|UseFacility|Train|UpgradeHome|Idle)\)/g, (_, action) => `（${actionLabel(action)}）`);
}
function worldLabel(world) {
  return `第 ${world.day} 天 · ${weekdayLabels[world.weekday] || world.weekday} · ${world.time}`;
}
function translateDecisionSummary(summary) {
  const fixed = {
    "Recover energy at Home": "在家睡觉以恢复能量",
    "Reduce hunger at Home or Cafe": "在家或咖啡馆用餐以降低饥饿",
    "Earn money at the Office": "前往办公室工作并获得收入",
    "Meet another NPC nearby": "与当前位置的其他 NPC 互动",
    "Recover mood and some energy": "放松以恢复心情和部分能量",
    "Wait briefly before reconsidering": "短暂等待后重新考虑行动",
    "Waiting for the first decision tick": "正在等待第一次决策 Tick",
  };
  if (fixed[summary]) return fixed[summary];
  const travel = summary.match(/^Travel to (Home|Office|Cafe|Park) to enable (Sleep|Eat|Work|Relax|Socialize|Shop|UseItem|Idle)$/);
  return travel ? `前往${locationLabel(travel[1])}，以便${actionLabel(travel[2])}` : summary;
}
function translateEventDescription(description) {
  const fixed = {
    "MiniWorld initialized": "MiniWorld 世界初始化完成",
    "World paused": "世界已暂停",
    "World resumed": "世界已继续运行",
  };
  if (fixed[description]) return fixed[description];
  let match = description.match(/^World speed set to (\d+)x$/);
  if (match) return `世界速度已设为 ${match[1]}×`;
  match = description.match(/^(.+) moved from (Home|Office|Cafe|Park) to (Home|Office|Cafe|Park)$/);
  if (match) return `${match[1]} 从${locationLabel(match[2])}移动到了${locationLabel(match[3])}`;
  match = description.match(/^(.+) woke up feeling more rested$/);
  if (match) return `${match[1]} 睡醒了，精力有所恢复`;
  match = description.match(/^(.+) finished a meal$/);
  if (match) return `${match[1]} 吃完了一顿饭`;
  match = description.match(/^(.+) completed a work session and earned \$(\d+)$/);
  if (match) return `${match[1]} 完成了一段工作，获得 $${match[2]}`;
  match = description.match(/^(.+) finished relaxing$/);
  if (match) return `${match[1]} 结束了放松休息`;
  match = description.match(/^(.+) could not find anyone to talk with$/);
  if (match) return `${match[1]} 没有找到可以聊天的人`;
  match = description.match(/^(.+) talked with (.+)$/);
  if (match) return `${match[1]} 与 ${match[2]} 聊了聊天`;
  match = description.match(/^(.+) → (.+) relationship ([+-]?\d+)$/);
  if (match) return `${match[1]} → ${match[2]} 的关系值变化 ${match[3]}`;
  match = description.match(/^(.+) spent a quiet moment idle$/);
  if (match) return `${match[1]} 安静地发了一会儿呆`;
  match = description.match(/^Day (\d+) began: (.+)$/);
  if (match) return `第 ${match[1]} 天开始了：${weekdayLabels[match[2]] || match[2]}`;
  return description;
}
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }

function renderRuntime(runtime) {
  const target = $("#runtime-overview");
  if (!runtime) {
    setHtml(target, '<article class="economy-disabled"><strong>运行时暂不可用</strong><span>世界仍按 Utility fallback 继续；可稍后重试。</span></article>', "runtime-overview");
    target.setAttribute("aria-busy", "false");
    return;
  }
  state.runtime = runtime;
  target.dataset.mode = runtime.mode || "safe";
  const modeLabels = { safe: "安全离线", online: "运行中", paused: "已暂停", emergency_stop: "已紧急停止" };
  const budget = runtime.budget || {};
  const used = budget.used || {};
  const limits = budget.limits || {};
  const cost = budget.cost || {};
  const thinking = Object.entries(runtime.npcs || {}).filter(([, value]) => value.online_thinking).map(([id]) => agentNpcName(id)).join("、") || "当前无人等待模型";
  const fallback = Object.entries(runtime.npcs || {}).filter(([, value]) => value.fallback_reason).map(([id, value]) => `${agentNpcName(id)}：${value.fallback_reason}`).slice(0, 3).join("；") || "没有近期本地回退";
  const online = runtime.mode === "online";
  const paused = runtime.mode === "paused";
  const configured = Boolean(runtime.configured);
  const canEmergencyStop = online || paused;
  const primaryAction = online ? "pause" : paused ? "resume" : "start";
  const primaryLabel = online ? "暂停在线自治" : paused ? "继续在线自治" : "开启五人在线自治";
  const guidance = !configured
    ? "当前未配置在线模型，NPC 会继续使用本地规则运行。点击“配置模型”可为本次服务临时连接模型。"
    : online
      ? "五位 NPC 可以在预算和安全门禁内请求在线模型；世界事实仍只由 Simulation Engine 决定。"
      : paused
        ? "已暂停领取新的模型任务；世界继续使用本地规则运行。"
        : runtime.mode === "emergency_stop"
          ? "在线模型调用已停止，在途结果已作废；世界继续使用本地规则运行。"
          : "在线自治尚未开启；世界继续使用本地规则运行。";
  const diagnosticsOpen = target.querySelector(".runtime-diagnostics")?.open ? " open" : "";
  setHtml(target, `<div class="runtime-head">
    <div class="runtime-title">
      <div class="runtime-title-line"><h3>在线自治</h3><span class="runtime-state" data-mode="${escapeHtml(runtime.mode || "safe")}">${escapeHtml(modeLabels[runtime.mode] || runtime.mode)}</span></div>
      <p id="runtime-guidance">${escapeHtml(guidance)}</p>
    </div>
    <div class="runtime-actions">
      ${configured
        ? `<button type="button" aria-describedby="runtime-guidance" data-focus-key="runtime-primary" data-runtime-action="${primaryAction}">${primaryLabel}</button>`
        : '<button type="button" aria-describedby="runtime-guidance" data-focus-key="runtime-config" data-runtime-config-open="true">配置模型</button>'}
      ${configured && !canEmergencyStop ? '<button class="secondary" type="button" aria-describedby="runtime-guidance" data-focus-key="runtime-config" data-runtime-config-open="true">模型设置</button>' : ""}
      ${canEmergencyStop ? '<button class="danger" type="button" aria-describedby="runtime-guidance" data-focus-key="runtime-emergency" data-runtime-action="emergency-stop">紧急停止在线自治</button>' : ""}
    </div>
  </div>
  <details class="runtime-diagnostics"${diagnosticsOpen}>
    <summary><span><strong>用量与诊断</strong><small>今日 ${used.calls || 0}/${limits.calls_per_day || 0} 次 · ${used.total_tokens || 0}/${limits.total_tokens_per_day || 0} Token</small></span></summary>
    <div class="runtime-grid">
      <article><small>当前模型连接</small><strong>${online ? "允许调用在线模型" : "当前未调用在线模型"}</strong><p>${configured ? `模型 ${escapeHtml(runtime.provider?.model || "未配置")}` : "未配置模型 Key"}</p></article>
      <article><small>任务状态</small><strong>${escapeHtml(thinking)}</strong><p>并发 ${runtime.queue?.active || 0}/${runtime.queue?.max_concurrency || 0} · 等待 ${runtime.queue?.depth || 0}/${runtime.queue?.limit || 0}</p></article>
      <article><small>今日模型用量</small><strong>${used.calls || 0}/${limits.calls_per_day || 0} 次</strong><p>${used.total_tokens || 0}/${limits.total_tokens_per_day || 0} Token</p></article>
      <article><small>费用估算</small><strong>${cost.pricing_configured ? `${Number(cost.amount || 0).toFixed(6)} ${escapeHtml(cost.currency || "")}` : "未配置单价"}</strong><p>仅供参考，不代表实际账单；达到预算后自动使用本地规则</p></article>
    </div>
    <p class="runtime-fallback"><strong>近期本地回退：</strong>${escapeHtml(fallback)}</p>
  </details>`, "runtime-overview");
  target.setAttribute("aria-busy", "false");
}

const agentPhaseLabels = {
  idle: "空闲", waiting: "等待 Agent", ready: "等待 Engine 复核",
  agent_executing: "Agent 行动执行中", fallback_executing: "Utility 回退执行中",
  completed: "已完成", disabled: "已关闭", unavailable: "不可用",
};

function agentPhaseLabel(value) { return agentPhaseLabels[value] || value || "空闲"; }
function agentNpcName(npcId, fallback = null) {
  const control = state.agentOverview?.npcs?.find(item => Number(item.npc_id) === Number(npcId));
  return control?.npc_name || fallback || `NPC ${npcId}`;
}

function renderAgentOverview(overview) {
  const target = $("#agent-overview");
  if (!overview || !Array.isArray(overview.npcs)) {
    target.innerHTML = '<article class="economy-disabled"><strong>Agent 总览暂不可用</strong><span>世界模拟与基础决策仍可继续运行。</span></article>';
    return;
  }
  state.agentOverview = overview;
  const worker = overview.worker || {};
  const provider = overview.provider || {};
  const cards = overview.npcs.map(control => {
    const queue = control.queue || {};
    const final = control.final || control.turn?.final;
    const plan = control.plan || control.turn?.agent?.plan || [];
    const audit = (control.recent_audits || [])[0];
    const finalSource = final?.source === "agent" ? "Agent" : final?.source === "utility_fallback" ? "Utility fallback" : "等待决策";
    const finalAction = final?.action ? actionLabel(final.action) : "尚无最终行动";
    const fallback = control.fallback || final?.fallback_reason_code;
    return `<article class="agent-fleet-card ${control.enabled ? "enabled" : ""}">
      <div class="agent-fleet-title"><strong>${escapeHtml(control.npc_name || `NPC ${control.npc_id}`)}</strong><span class="agent-fleet-state">${escapeHtml(control.enabled ? agentPhaseLabel(control.status) : "已关闭")}</span></div>
      <div class="agent-fleet-row"><span>队列 ${Number(queue.depth || 0)} / ${Number(queue.limit || 1)}</span><span>${control.emotion ? `情绪 ${escapeHtml(control.emotion)}` : "独立上下文"}</span></div>
      <p class="agent-fleet-final"><strong>${escapeHtml(finalAction)}</strong>${escapeHtml(finalSource)}${fallback ? ` · 回退 ${escapeHtml(fallback)}` : ""}</p>
      <p class="agent-fleet-plan">${plan.length ? `计划：${plan.map(escapeHtml).join(" → ")}` : "计划：等待下一次有效 Agent 决策"}</p>
      <p class="agent-fleet-audit">${audit ? `最近审计 #${audit.id} · ${escapeHtml(agentPhaseLabel(audit.state))}` : "最近审计：暂无"}</p>
      <button class="takeover-toggle ${control.enabled ? "active" : ""}" type="button" data-focus-key="agent-${control.npc_id}" data-agent-toggle="npc" data-npc-id="${control.npc_id}" data-enabled="${control.enabled ? "true" : "false"}">${control.enabled ? "关闭此 Agent" : "开启此 Agent"}</button>
    </article>`;
  }).join("");
  const enabledCount = overview.enabled_npc_ids?.length || 0;
  setHtml(target, `
    <div class="agent-overview-head">
      <div><h3>各自感知、各自记忆、各自回退。</h3><p>五个独立 Agent NPC</p></div>
      <div class="agent-overview-actions"><span class="agent-worker-summary">${provider.available ? `${escapeHtml(provider.provider || "Provider")} · ${escapeHtml(provider.model || "已配置模型")}` : `安全模式 · ${escapeHtml(provider.reason || "provider_unavailable")}`}<br>并发 ${Number(worker.max_concurrency || 0)} · 队列 ${Number(worker.queue_depth || 0)} / ${Number(worker.queue_limit || 5)} · ${worker.bounded === false ? "积压超限" : "有界"}</span><button id="agent-global-toggle" class="takeover-toggle ${overview.global_enabled ? "active" : ""}" type="button" data-focus-key="agent-global" data-agent-toggle="global" data-enabled="${overview.global_enabled ? "true" : "false"}">${overview.global_enabled ? "关闭全部 Agent" : `开启全部 Agent（当前 ${enabledCount}/5）`}</button></div>
    </div>
    <div class="agent-fleet-grid">${cards}</div>`, "agent-overview");
  renderNpcOverview();
}

function renderConversationOverview(status, conversations) {
  const target = $("#conversation-overview");
  if (!status) {
    target.innerHTML = '<article class="economy-disabled"><strong>多轮会话状态暂不可用</strong><span>世界模拟与基础对话仍可继续运行。</span></article>';
    return;
  }
  const bounds = status.bounds || {};
  const provider = status.provider || {};
  const cards = (conversations || []).slice(0, 6).map(item => {
    const last = item.turns?.[item.turns.length - 1];
    const fallbackCount = (item.turns || []).filter(turn => turn.fallback_used).length;
    return `<article class="conversation-card">
      <div class="conversation-meta"><strong>${escapeHtml(item.actor.name)} ↔ ${escapeHtml(item.target.name)}</strong><span>${escapeHtml(item.status)}</span></div>
      <p>${last ? `<strong>${escapeHtml(last.speaker.name)}</strong> ${escapeHtml(last.utterance)}` : "等待第一轮发言"}</p>
      <div class="conversation-meta"><span>${Number(item.completed_turn_count)} / ${Number(item.target_turn_count)} 轮 · ${escapeHtml(item.location)}</span><span>${fallbackCount ? `${fallbackCount} 轮回退` : "Provider"}</span></div>
    </article>`;
  }).join("");
  setHtml(target, `<div class="agent-overview-head">
      <div><h3>逐人上下文隔离，Engine 独占事实。</h3><p>持久多轮社交</p></div>
      <div class="agent-overview-actions"><span class="agent-worker-summary">${provider.available ? `${escapeHtml(provider.provider || "Provider")} · ${escapeHtml(provider.model || "已配置模型")}` : `人格回退 · ${escapeHtml(provider.reason || "未配置")}`}<br>队列 ${Number(bounds.queue_depth || 0)} / ${Number(bounds.queue_limit || 10)} · 并发 ${Number(bounds.max_concurrency || 0)} · 3–6 轮</span><button class="takeover-toggle ${status.enabled ? "active" : ""}" type="button" data-conversation-toggle="true" data-enabled="${status.enabled ? "true" : "false"}">${status.enabled ? "关闭新多轮会话" : "开启多轮会话"}</button></div>
    </div>
    ${cards ? `<div class="conversation-grid">${cards}</div>` : `<p class="empty">${status.enabled ? "等待下一次 Engine 已确认的 Socialize。" : "默认关闭；双方均未启用时继续使用基础叙事对话。"}</p>`}`, "conversation-overview");
}

function renderCognitionDetail(cognition) {
  if (!cognition) return '<p class="empty memory-empty">认知状态不可用；世界模拟与多轮会话仍可继续运行。</p>';
  const latest = cognition.reflections?.[0];
  const beliefs = cognition.subjective_beliefs || [];
  const plans = cognition.plans || [];
  const activePlans = plans.filter(item => ["pending", "in_progress"].includes(item.status));
  const toggle = `<button class="takeover-toggle ${cognition.enabled ? "active" : ""}" type="button" data-focus-key="npc-cognition-toggle" data-cognition-toggle="npc" data-npc-id="${cognition.npc_id}" data-enabled="${cognition.enabled ? "true" : "false"}">${cognition.enabled ? "关闭此人的新反思" : "开启此人的每日反思"}</button>`;
  const reflection = latest ? `<article class="agent-choice agent"><small>第 ${latest.reflection_day} 天 · ${escapeHtml(latest.provider)}${latest.fallback_used ? ` · 回退 ${escapeHtml(latest.failure_reason || "fallback")}` : ""}</small><strong>${escapeHtml(latest.goal_focus)}</strong><p>${escapeHtml(latest.day_summary)}</p><p>${escapeHtml(latest.emotion_summary)}</p><div class="cognition-evidence">证据 ${latest.sources.map(item => escapeHtml(item.source_id)).join(" · ") || "无"}</div></article>` : '<p class="empty memory-empty">等待第一个日界线后的反思。</p>';
  return `${toggle}<div class="agent-control-grid">${reflection}<article class="agent-choice"><small>主观信念 · 不等于事实</small>${beliefs.slice(0, 5).map(item => `<p><strong>${escapeHtml(item.target)}</strong> ${escapeHtml(item.belief)} · ${Math.round(Number(item.confidence) * 100)}%<br><span class="cognition-evidence">${item.evidence_ids.map(escapeHtml).join(" · ")}</span></p>`).join("") || "暂无"}</article><article class="agent-choice"><small>Engine 跟踪计划 · 不直接执行</small>${activePlans.slice(0, 5).map(item => `<p><strong>${escapeHtml(actionLabel(item.action_category))}</strong> ${escapeHtml(item.description)} · ${escapeHtml(item.status)} · 第 ${item.window_start_day}–${item.window_end_day} 天<br><span class="cognition-evidence">${escapeHtml(item.progress_evidence?.source_type || "等待事实")} ${escapeHtml(item.progress_evidence?.source_id || "")}</span></p>`).join("") || "暂无未完成计划"}</article></div>`;
}

function renderCognitionOverview(status, cognitions) {
  const target = $("#cognition-overview");
  if (!status) {
    target.innerHTML = '<article class="economy-disabled"><strong>认知状态暂不可用</strong><span>世界模拟与多轮会话仍可继续运行。</span></article>';
    return;
  }
  const provider = status.provider || {}; const bounds = status.bounds || {};
  const cards = (cognitions || []).map(item => {
    const latest = item?.reflections?.[0];
    const active = (item?.plans || []).filter(plan => ["pending", "in_progress"].includes(plan.status));
    return `<article class="agent-fleet-card ${item?.enabled ? "enabled" : ""}"><div class="agent-fleet-title"><strong>${escapeHtml(item?.npc_name || `NPC ${item?.npc_id}`)}</strong><span class="agent-fleet-state">${item?.enabled ? "每日反思开启" : "已关闭"}</span></div><div class="agent-fleet-row"><span>最近第 ${Number(item?.last_reflected_day || 0)} 天</span><span>${active.length} 条未完成计划</span></div><p class="agent-fleet-final"><strong>${escapeHtml(item?.current_goal_focus || "等待 goal_focus")}</strong>${latest ? escapeHtml(latest.day_summary) : "尚无反思"}</p><p class="agent-fleet-plan">主观信念 ${Number(item?.subjective_beliefs?.length || 0)} 条 · 证据 ${Number(latest?.sources?.length || 0)} 项${latest?.fallback_used ? ` · 回退 ${escapeHtml(latest.failure_reason || "fallback")}` : ""}</p><button class="takeover-toggle ${item?.enabled ? "active" : ""}" type="button" data-cognition-toggle="npc" data-npc-id="${item?.npc_id}" data-enabled="${item?.enabled ? "true" : "false"}">${item?.enabled ? "关闭新反思" : "开启新反思"}</button></article>`;
  }).join("");
  setHtml(target, `<div class="agent-overview-head"><div><h3>每日反思、主观信念与 Engine 跟踪计划。</h3><p>五人独立认知</p></div><div class="agent-overview-actions"><span class="agent-worker-summary">${provider.available ? `${escapeHtml(provider.provider)} · ${escapeHtml(provider.model)}` : `人格回退 · ${escapeHtml(provider.reason || "未配置")}`}<br>队列 ${Number(bounds.queue_depth || 0)} / ${Number(bounds.queue_limit || 15)} · 并发 ${Number(bounds.max_concurrency || 0)} · 事实仅 Engine</span><button class="takeover-toggle ${status.global_enabled ? "active" : ""}" type="button" data-cognition-toggle="global" data-enabled="${status.global_enabled ? "true" : "false"}">${status.global_enabled ? "关闭全部新反思" : "开启全部每日反思"}</button></div></div><div class="agent-fleet-grid">${cards}</div>`, "cognition-overview");
}

async function fetchAgentControl(npcId, scope = "global") {
  try { return await api(`/api/agents/${npcId}/control`, {}, scope); }
  catch (error) {
    if (error.name === "AbortError") throw error;
    const legacy = await api(`/api/npcs/${npcId}/agent-control`, {}, scope);
    return { npc_id: Number(npcId), ...legacy };
  }
}

function renderWorld(world) {
  const worldNpcs = Object.values(world.locations || {}).flat();
  state.world = { ...world, npcs: worldNpcs };
  if (!state.npcs) state.npcs = worldNpcs;
  $("#world-clock").textContent = worldLabel(world);
  $("#pause-button").textContent = world.paused ? "继续" : "暂停";
  $("#status-text").textContent = world.paused ? "模拟已暂停" : `正以 ${world.speed}× 运行`;
  $(".status").classList.toggle("paused", world.paused);
  document.querySelectorAll(".speed").forEach(button => {
    const active = Number(button.dataset.speed) === world.speed;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const locationHtml = locations.map(location => {
    const npcs = world.locations[location.name] || [];
    const cards = npcs.length ? npcs.map(npc => `
      <button class="npc-card" type="button" data-npc-id="${npc.id}" data-focus-key="location-npc-${npc.id}">
        <span class="avatar">${initials(npc.name)}</span>
        <span><span class="npc-name">${escapeHtml(npc.name)}</span><span class="npc-action">${escapeHtml(actionLabel(npc.current_action))}</span></span>
        <span class="energy-mini">能量 ${Math.round(npc.states.energy)}</span>
      </button>`).join("") : '<p class="empty">这里暂时没有人</p>';
    return `<article class="location"><div class="location-head"><h3>${location.label}</h3><span class="location-icon" aria-hidden="true">${locationIcon(location.name)}</span></div><div class="residents">${cards}</div></article>`;
  }).join("");
  setHtml($("#locations"), locationHtml, "locations");
  $("#locations").setAttribute("aria-busy", "false");
  renderNpcOverview();
}

function locationIcon(name) {
  const paths = {
    Home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V21h13V9.5"/><path d="M9 21v-7h6v7"/>',
    Office: '<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M8 7h2M14 7h2M8 11h2M14 11h2M8 15h2M14 15h2M10 21v-3h4v3"/>',
    Cafe: '<path d="M5 8h11v5.5A5.5 5.5 0 0 1 10.5 19 5.5 5.5 0 0 1 5 13.5Z"/><path d="M16 10h2.5a2.5 2.5 0 0 1 0 5H16M7 4v2M11 4v2M15 4v2"/>',
    Park: '<path d="M12 3c-3.5 0-6 2.7-6 6.1 0 2.7 1.6 4.8 4 5.7V21h4v-6.2c2.4-.9 4-3 4-5.7C18 5.7 15.5 3 12 3Z"/><path d="M8.5 11.5 12 15l3.5-3.5"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths[name] || paths.Home}</svg>`;
}

function renderNpcOverview() {
  const target = $("#npc-overview");
  const npcs = state.npcs || state.world?.npcs;
  if (!target || !npcs) return;
  const controls = new Map((state.agentOverview?.npcs || []).map(item => [Number(item.npc_id), item]));
  const cards = [...npcs].sort((a, b) => Number(a.id) - Number(b.id)).map(npc => {
    const control = controls.get(Number(npc.id)) || {};
    const final = control.final || control.turn?.final;
    const fallback = control.fallback || final?.fallback_reason_code || null;
    const source = final?.source === "agent" ? "Agent" : final?.source === "utility_fallback" ? "Utility fallback" : control.enabled ? agentPhaseLabel(control.status) : "Utility AI";
    const warning = fallback || control.status === "waiting";
    const metrics = [
      ["能量", npc.states.energy], ["饥饿", npc.states.hunger], ["心情", npc.states.mood], ["社交", npc.states.social_need],
    ];
    return `<button class="npc-observation-card" type="button" data-npc-id="${npc.id}" data-focus-key="overview-npc-${npc.id}">
      <span class="npc-card-head"><span class="avatar">${initials(npc.name)}</span><span><strong>${escapeHtml(npc.name)}</strong><small>${escapeHtml(jobLabels[npc.job] || npc.job)} · ${escapeHtml(locationLabel(npc.current_location))}</small></span><span class="source-chip ${warning ? "warning" : control.enabled ? "info" : ""}">${escapeHtml(source)}</span></span>
      <span class="npc-current"><span>当前行为</span><strong>${escapeHtml(actionLabel(npc.current_action))}</strong></span>
      <span class="npc-metric-grid">${metrics.map(([label, value]) => `<span><small>${label}</small><strong>${Math.round(Number(value))}</strong><i class="mini-track"><i style="width:${Math.max(0, Math.min(100, Number(value)))}%"></i></i></span>`).join("")}</span>
      <span class="npc-card-note ${warning ? "warning" : ""}">${fallback ? `需要关注：${escapeHtml(fallback)}` : control.enabled ? "在线自治已启用，Engine 保留最终复核。" : "安全路径：当前由 Utility AI 行动。"}</span>
    </button>`;
  }).join("");
  setHtml(target, cards, "npc-overview");
  target.setAttribute("aria-busy", "false");
}

function renderEvents(events, narratives = []) {
  state.events = events || [];
  state.eventNarratives = narratives || [];
  const groups = {
    people: new Set(["SOCIAL", "RELATIONSHIP", "SOCIAL_COMMITMENT", "JOINT_ACTIVITY", "COHOUSING"]),
    economy: new Set(["WORK", "SHOP", "ITEM", "HOUSING", "CAREER_REVIEW", "CAREER_TRANSITION", "CAREER_SEARCH", "ECONOMIC_REPORT", "ATTENDANCE", "SHIFT", "RESTOCK", "FACILITY", "TRAINING", "HOUSING_UPGRADE", "SHARED_EXPENSE"]),
    system: new Set(["TIME", "SYSTEM", "MOVE", "SLEEP", "EAT", "RELAX"]),
  };
  const visible = state.eventFilter === "all" ? state.events : state.events.filter(event => groups[state.eventFilter]?.has(event.event_type));
  const explanations = new Map(narratives.map(item => [item.event_id, item]));
  $("#event-count").textContent = `显示 ${visible.length} 条`;
  const html = visible.length ? visible.map(event => {
    const narrative = explanations.get(event.id);
    const explanation = narrative ? `<small class="event-explanation">${escapeHtml(narrative.content.text)} · ${narrative.fallback_used ? "确定性回退" : "LLM"}</small>` : "";
    return `<article class="event"><time class="event-time">${escapeHtml(event.world_time)}</time><span class="event-type">${escapeHtml(eventTypeLabels[event.event_type] || event.event_type)}</span><div><strong>${event.npc_name ? escapeHtml(event.npc_name) : "世界"}</strong><p>${escapeHtml(translateEventDescription(event.description))}</p>${explanation}</div></article>`;
  }).join("") : '<p class="empty memory-empty">当前筛选下还没有事件。</p>';
  setHtml($("#events"), html, "events");
  $("#events").setAttribute("aria-busy", "false");
}

function renderEconomy(status, stores, professions) {
  const target = $("#economy-overview");
  if (!status.enabled) {
    target.innerHTML = '<article class="economy-disabled"><strong>经济系统未启用</strong><span>职业与消费功能不可用；基础行动模拟继续运行。</span></article>';
    return;
  }
  const store = stores[0];
  target.innerHTML = `
    <div class="economy-heading"><div><h3>收入流向生活，再反馈到能力。</h3><p>社会经济</p></div><span class="counter">${status.transactions} 笔流水</span></div>
    <div class="economy-grid">
      <article class="economy-card"><small>职业与工资</small><strong>${professions.length} 种职业</strong><p>${professions.map(item => `${escapeHtml(item.label)} $${Number(item.base_wage).toFixed(0)}`).join(" · ")}</p></article>
      <article class="economy-card"><small>社区商店 · ${escapeHtml(locationLabel(store?.location || "Cafe"))}</small><strong>${store?.items.length || 0} 种物品</strong><p>${(store?.items || []).map(item => `${escapeHtml(item.name)} $${Number(item.price).toFixed(0)}`).join(" · ")}</p></article>
      <article class="economy-card"><small>闭环状态</small><strong>工资 → 消费 → 成长</strong><p>工作表现与技能影响工资；物品可补给状态、提升技能或改善住房。</p></article>
    </div>`;
}

function renderCareerOverview(status, reports) {
  const target = $("#career-overview");
  if (!status.enabled) {
    target.innerHTML = '<article class="economy-disabled"><strong>职业与预算功能未启用</strong><span>职业和预算资料不会进入决策；基础经济模拟继续运行。</span></article>';
    return;
  }
  const recent = reports.slice(0, 5);
  const averagePressure = recent.length ? recent.reduce((sum, row) => sum + Number(row.economic_pressure), 0) / recent.length : 0;
  target.innerHTML = `
    <div class="economy-heading"><div><h3>每周评估工作，也核对生活账本。</h3><p>职业发展与预算</p></div><span class="counter">${status.reports} 份周报</span></div>
    <div class="career-grid">
      <article class="economy-card"><small>周期档案</small><strong>${status.careers} 位居民</strong><p>绩效评估给出分数与原因；连续优秀才能晋升，低绩效风险受就业人数安全下限约束。</p></article>
      <article class="economy-card"><small>个人预算</small><strong>5 类周预算</strong><p>食物 · 住房 · 学习 · 娱乐 · 储蓄；预算余量与经济压力以有限权重进入 Utility AI。</p></article>
      <article class="economy-card"><small>最近周报平均压力</small><strong>${averagePressure.toFixed(1)} / 100</strong><p>${recent.length ? `来自最近 ${recent.length} 份个人周报` : "首个完整模拟周结束后生成报告"}</p></article>
    </div>`;
}

function renderCommunityOverview(status, institutions, stock) {
  const target = $("#community-overview");
  if (!status.enabled) {
    target.innerHTML = '<article class="economy-disabled"><strong>社区生活功能未启用</strong><span>营业、排班、设施、补货、培训和住房升级不可用；职业与预算功能继续运行。</span></article>';
    return;
  }
  const openCount = institutions.filter(item => item.open).length;
  const stockCount = stock.reduce((sum, item) => sum + Number(item.quantity), 0);
  target.innerHTML = `
    <div class="economy-heading"><div><h3>机构有钟点，生活有工作日与周末。</h3><p>社区机构与生活节奏</p></div><span class="counter">${openCount} / ${institutions.length} 营业中</span></div>
    <div class="community-grid">
      ${institutions.map(item => `<article class="economy-card"><small>${escapeHtml(locationLabel(item.location))} · ${escapeHtml(item.type)}</small><strong>${escapeHtml(item.name)}</strong><p>${item.open ? "当前开放" : "当前关闭"} · 今日 ${escapeHtml(item.today_hours)}${item.daily_capacity ? ` · 名额 ${item.capacity_used_today}/${item.daily_capacity}` : ""}</p></article>`).join("")}
      <article class="economy-card"><small>固定周期补货</small><strong>${stockCount} 件在库</strong><p>${stock.map(item => `${escapeHtml(item.item_name)} ${item.quantity}/${item.capacity}`).join(" · ")}</p></article>
    </div>`;
}

function renderSocialOverview(status, bonds, circles, commitments, households) {
  const target = $("#social-overview");
  if (!status.enabled) {
    target.innerHTML = '<article class="economy-disabled"><strong>群体关系功能未启用</strong><span>关系阶段、邀请、共同活动与合住不可用；社区生活功能继续运行。</span></article>';
    return;
  }
  const trusted = bonds.filter(item => ["friend", "close_friend", "trusted"].includes(item.stage)).length;
  target.innerHTML = `
    <div class="economy-heading"><div><h3>承诺要履行，信任有证据，关系也会随时间变化。</h3><p>群体关系与共同生活</p></div><span class="counter">${status.joint_activities} 次共同活动</span></div>
    <div class="social-grid">
      <article class="economy-card"><small>双向关系阶段</small><strong>${trusted} / ${bonds.length} 对朋友关系</strong><p>同时读取两个方向的关系值，并显示差异、信任、衰减与修复原因。</p></article>
      <article class="economy-card"><small>邀请与承诺</small><strong>${status.planned_commitments} 项待履行</strong><p>${commitments.slice(0, 3).map(item => `${escapeHtml(item.npc_low_name)} + ${escapeHtml(item.npc_high_name)} · ${locationLabel(item.location)}`).join(" · ") || "积极互动达到门槛后才会生成邀请"}</p></article>
      <article class="economy-card"><small>小型朋友圈</small><strong>${status.active_circles} 个活跃圈子</strong><p>${circles.filter(item => item.active).map(item => item.members.map(member => escapeHtml(member.npc_name)).join("、")).join(" · ") || "至少 3 人以朋友阶段关系连通，单圈最多 4 人"}</p></article>
      <article class="economy-card"><small>有限合住</small><strong>${status.active_households} 个家庭</strong><p>${households.filter(item => item.active).map(item => `${item.residents.map(member => escapeHtml(member.npc_name)).join(" + ")} · 周共同支出 $${Number(item.weekly_shared_cost).toFixed(0)}`).join(" · ") || "全世界最多 1 个双人合住家庭；需亲密关系、信任、共同活动且无欠费"}</p></article>
    </div>`;
}

function renderStoryOverview(status, milestones, summaries, replay) {
  const target = $("#story-overview");
  if (!status.enabled) {
    target.innerHTML = '<article class="economy-disabled"><strong>人生故事功能未启用</strong><span>人生里程碑、周期总结与回放不可用；群体关系功能继续运行。</span></article>';
    return;
  }
  const latest = milestones.slice(0, 3);
  const latestSummary = summaries[0];
  target.innerHTML = `
    <div class="economy-heading"><div><h3>事实先固化，故事才被讲述。</h3><p>人生事件与可回放故事</p></div><span class="counter">${status.milestones} 项里程碑</span></div>
    <div class="story-grid">
      <article class="economy-card"><small>最近人生事件</small><strong>${latest.length ? escapeHtml(milestoneLabels[latest[0].milestone_type] || latest[0].milestone_type) : "等待事实触发"}</strong><p>${latest.map(item => escapeHtml(item.title)).join(" · ") || "晋升、住房、技能、储蓄、友谊、欠费及职业转换均有固定门槛"}</p></article>
      <article class="economy-card"><small>周 / 月总结</small><strong>${status.weekly_summaries} / ${status.monthly_summaries}</strong><p>${latestSummary ? escapeHtml(latestSummary.narrative_text || "结构化事实已固化，叙事文本生成中") : "跨过完整周期后生成 Engine 事实清单"}</p></article>
      <article class="economy-card"><small>决策因果链</small><strong>${status.causal_links} 条证据</strong><p>每个里程碑保留有序原因、来源记录、规则与事实摘要。</p></article>
      <article class="economy-card"><small>固定 seed 回放</small><strong>${status.replay_checkpoints} 个检查点</strong><p>Seed ${replay.seed ?? "—"} · ${replay.replay_digest ? `摘要 ${escapeHtml(replay.replay_digest.slice(0, 12))}` : "尚无回放范围"}</p></article>
    </div>`;
}

function renderProductOverview(status, statistics, balance, onboarding) {
  const target = $("#product-overview");
  if (!status.enabled) {
    target.innerHTML = '<article class="economy-disabled"><strong>产品管理功能未启用</strong><span>多存档、统计、平衡审计和新手引导不可用；人生故事功能继续运行。</span></article>';
    return;
  }
  const metrics = statistics.metrics || {};
  const decisions = metrics.decisions || {};
  const completed = (onboarding.completed_steps || []).length;
  const balanceLabels = { healthy: "守护正常", warning: "需要关注", critical: "越过硬边界" };
  target.innerHTML = `
    <div class="economy-heading"><div><h3>存档隔离、来源可追溯、长期运行可验证。</h3><p>当前世界 · ${escapeHtml(status.world_name)}</p></div><span class="counter">${escapeHtml(status.active_slot || "当前")} 存档</span></div>
    <div class="product-grid">
      <article class="economy-card"><small>世界财富</small><strong>$${Number(metrics.money?.average || 0).toFixed(2)} / 人</strong><p>合计 $${Number(metrics.money?.total || 0).toFixed(2)} · 来源：npcs 当前已提交事实</p></article>
      <article class="economy-card"><small>就业与压力</small><strong>${metrics.employment_rate == null ? "等待资料" : `${(Number(metrics.employment_rate) * 100).toFixed(0)}% 就业`}</strong><p>最近经济压力 ${metrics.economic_pressure_average == null ? "尚无周报" : Number(metrics.economic_pressure_average).toFixed(1)}</p></article>
      <article class="economy-card"><small>决策平衡 · 最近 ${decisions.window || 0} 次</small><strong>${escapeHtml(actionLabel(decisions.top_action || "等待决策"))}</strong><p>最高行动占比 ${(Number(decisions.top_action_share || 0) * 100).toFixed(1)}% · 熵 ${Number(decisions.entropy_bits || 0).toFixed(2)} bits</p></article>
      <article class="economy-card"><small>平衡守护</small><strong>${balanceLabels[balance.status] || balance.status}</strong><p>${(balance.violations || []).length} 项异常 · 新手引导 ${completed}/4 · 只审计，不为追指标改写旧事实</p></article>
    </div>`;
}

function metric(label, value, personality = false) {
  const numeric = Math.round(Number(value) * (personality ? 100 : 1));
  return `<div class="metric ${personality ? "personality" : ""}"><span>${escapeHtml(label)}</span><span class="track"><span class="fill" style="width:${Math.max(0, Math.min(100, numeric))}%"></span></span><strong>${numeric}</strong></div>`;
}

function renderAgentShadow(shadow) {
  if (shadow.status === "unavailable") return '<p class="empty memory-empty">Agent 影子接口暂不可用；Utility AI 不受影响。</p>';
  if (!shadow.supported) return '<p class="empty memory-empty">影子建议目前仅支持 Alice。</p>';
  if (!shadow.enabled) return '<p class="empty memory-empty">影子模式默认关闭。Utility AI 继续独占真实行动。</p>';
  if (shadow.status === "waiting") return '<p class="empty memory-empty">正在等待 Alice 的下一次重新决策。</p>';
  const utility = shadow.utility || {};
  const utilityCard = `<article class="agent-choice utility"><small>Utility 实际选择 · 已执行</small><strong>${escapeHtml(actionLabel(utility.action || "等待"))}</strong><p>${escapeHtml(translateDecisionSummary(utility.reason_summary || ""))}</p></article>`;
  if (shadow.status === "pending" || shadow.status === "processing") {
    return `<div class="agent-shadow-grid">${utilityCard}<article class="agent-choice pending"><small>Agent 影子建议</small><strong>异步处理中</strong><p>世界不会等待模型；本 Tick 已按 Utility 结果继续。</p></article></div>`;
  }
  if (shadow.status === "failed") {
    return `<div class="agent-shadow-grid">${utilityCard}<article class="agent-choice failed"><small>Agent 影子建议</small><strong>安全回退</strong><p>未产生可用建议（${escapeHtml(shadow.error_code || "provider_error")}）；真实行动未改变。</p></article></div>`;
  }
  const agent = shadow.agent || {};
  const validation = shadow.validation || {};
  const comparison = shadow.comparison || {};
  const legalLabel = validation.legal ? "合法建议" : `非法建议 · ${validation.reason_code || "校验失败"}`;
  const matchLabel = comparison.same_action ? "与 Utility 一致" : "与 Utility 不同";
  return `<div class="agent-shadow-badges"><span class="${validation.legal ? "legal" : "illegal"}">${escapeHtml(legalLabel)}</span><span>${escapeHtml(matchLabel)}</span></div>
    <div class="agent-shadow-grid">${utilityCard}<article class="agent-choice agent"><small>Agent 影子建议 · 不执行</small><strong>${escapeHtml(actionLabel(agent.action || "—"))}${agent.target ? ` → ${escapeHtml(locationLabel(agent.target))}` : ""}</strong><p>${escapeHtml(agent.reason_summary || "")}</p></article></div>
    <div class="agent-explanation"><p><strong>情绪</strong>${escapeHtml(agent.emotion || "—")}</p><p><strong>意图</strong>${escapeHtml(agent.intention || "—")}</p>${agent.dialogue ? `<p><strong>拟议对话</strong>${escapeHtml(agent.dialogue)}</p>` : ""}<p><strong>短计划</strong>${(agent.plan || []).map(escapeHtml).join(" → ")}</p><p><strong>差异</strong>${escapeHtml(comparison.difference_summary || "—")}</p></div>`;
}

function renderAgentControl(control) {
  if (!control.supported) return '<p class="empty memory-empty">该 NPC 始终由 Utility AI 行动。</p>';
  const turn = control.turn;
  const npcName = agentNpcName(control.npc_id);
  const queue = control.queue || { depth: 0, limit: 1 };
  const toggle = `<button class="takeover-toggle ${control.enabled ? "active" : ""}" type="button" data-focus-key="npc-control-toggle" data-agent-toggle="npc" data-npc-id="${control.npc_id}" data-enabled="${control.enabled ? "true" : "false"}">${control.enabled ? `关闭 ${escapeHtml(npcName)} Agent` : `开启 ${escapeHtml(npcName)} Agent`}</button>`;
  if (!turn) return `${toggle}<div class="agent-control-meta"><span>Agent 状态<strong>${control.enabled ? "已开启" : "已关闭"}</strong></span><span>个人队列<strong>${Number(queue.depth || 0)} / ${Number(queue.limit || 1)}</strong></span><span>上下文<strong>按 NPC 隔离</strong></span></div><p class="empty memory-empty">${control.enabled ? "接管已开启，等待下一次行动边界。" : "默认关闭；当前继续使用 Utility AI。"}</p>`;
  const utility = turn.utility || {};
  const agent = turn.agent || {};
  const final = control.final || turn.final;
  const phase = agentPhaseLabel(turn.state);
  const finalText = final ? `${actionLabel(final.action || "—")}${final.target ? ` → ${final.target}` : ""}` : "尚未决定";
  const source = final?.source === "agent" ? "Agent 执行" : final?.source === "utility_fallback" ? "Utility fallback" : "等待";
  const reason = control.fallback || final?.fallback_reason_code || turn.validation?.execution?.reason_code || turn.validation?.snapshot?.reason_code || "—";
  const plan = control.plan || agent.plan || [];
  const audits = control.recent_audits || [];
  return `${toggle}<div class="agent-shadow-badges"><span>${escapeHtml(phase)}</span><span>${escapeHtml(source)}</span></div>
    <div class="agent-control-meta"><span>个人队列<strong>${Number(queue.depth || 0)} / ${Number(queue.limit || 1)}</strong></span><span>情绪<strong>${escapeHtml(control.emotion || agent.emotion || "等待")}</strong></span><span>最近审计<strong>${audits.length} 条</strong></span></div>
    <div class="agent-control-grid"><article class="agent-choice utility"><small>Utility 建议</small><strong>${escapeHtml(actionLabel(utility.action || "—"))}</strong></article><article class="agent-choice agent"><small>Agent 建议</small><strong>${escapeHtml(actionLabel(agent.action || "—"))}${agent.target ? ` → ${escapeHtml(agent.target)}` : ""}</strong><p>${escapeHtml(agent.reason_summary || "异步处理中")}</p></article><article class="agent-choice final"><small>最终真实行动</small><strong>${escapeHtml(finalText)}</strong><p>验证/回退原因：${escapeHtml(reason)}</p></article></div>
    <ul class="agent-plan-list">${plan.length ? plan.map((item, index) => `<li><strong>计划 ${index + 1}</strong> · ${escapeHtml(item)}</li>`).join("") : "<li>尚无 Agent 计划。</li>"}</ul>
    <ul class="agent-audit-list">${audits.length ? audits.map(item => `<li>审计 #${item.id} · ${escapeHtml(agentPhaseLabel(item.state))} · ${escapeHtml(item.final?.source || item.worker_state || "等待")}${item.final?.fallback_reason_code ? ` · ${escapeHtml(item.final.fallback_reason_code)}` : ""}</li>`).join("") : "<li>尚无接管审计。</li>"}</ul>`;
}

async function legacyOpenNpc(npcId) {
  const [npc, decision, memories, goals, dialogues, conversations, goalNarratives, memorySummaries, economy, career, budget, reports, rhythm, socialLife, timeline, agentShadow, agentControl, cognition] = await Promise.all([
    api(`/api/npcs/${npcId}`),
    api(`/api/npcs/${npcId}/decision`),
    api(`/api/npcs/${npcId}/memories?limit=20`),
    api(`/api/npcs/${npcId}/goals`),
    api(`/api/npcs/${npcId}/dialogues?limit=10`),
    api(`/api/npcs/${npcId}/conversations?limit=10`).catch(() => []),
    api(`/api/npcs/${npcId}/goal-narratives`),
    api(`/api/npcs/${npcId}/memory-summaries?limit=5`),
    api(`/api/npcs/${npcId}/economy`),
    api(`/api/npcs/${npcId}/career`),
    api(`/api/npcs/${npcId}/budget`),
    api(`/api/npcs/${npcId}/economic-reports?limit=4`),
    api(`/api/npcs/${npcId}/rhythm`),
    api(`/api/npcs/${npcId}/social-life`),
    api(`/api/npcs/${npcId}/timeline?limit=30`),
    api(`/api/npcs/${npcId}/agent-shadow`).catch(() => ({ status: "unavailable", supported: true })),
    fetchAgentControl(npcId).catch(() => ({ status: "unavailable", supported: true, npc_id: Number(npcId), enabled: false, turn: null })),
    api(`/api/agents/${npcId}/cognition`).catch(() => null),
  ]);
  state.selectedNpc = npcId;
  const candidates = decision.candidates || [];
  const maxScore = Math.max(1, ...candidates.filter(item => item.available).map(item => item.score));
  const decisionHtml = decision.chosen_action ? `
    <div class="decision-box"><small>选择时间：第 ${decision.world_day} 天 · ${decision.world_time}</small><div class="decision-choice">${escapeHtml(actionLabel(decision.chosen_action))}</div>
      ${candidates.map(item => `<div class="candidate ${item.available ? "" : "unavailable"}"><span>${escapeHtml(actionLabel(item.action))}</span><span class="track"><span class="fill" style="width:${item.available ? Math.max(0, item.score / maxScore * 100) : 0}%"></span></span><strong>${item.available ? item.score.toFixed(1) : "不可用"}</strong></div>`).join("")}
      <ul class="reason-list"><li><strong>${escapeHtml(translateDecisionSummary(decision.reason.summary))}</strong><span></span></li>${Object.entries(decision.reason.top_contributions || {}).map(([key, value]) => `<li><span>${escapeHtml(contributionLabel(key))}</span><strong>${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(1)}</strong></li>`).join("")}</ul>
    </div>` : `<div class="decision-box">${escapeHtml(translateDecisionSummary(decision.reason.summary))}</div>`;
  const memoriesHtml = memories.length ? memories.map(memory => `
    <article class="memory ${escapeHtml(memory.emotion)}">
      <div class="memory-meta"><time>第 ${memory.world_day} 天 · ${memory.world_time}</time><span>${escapeHtml(emotionLabels[memory.emotion] || memory.emotion)}</span><strong>重要度 ${memory.importance}</strong></div>
      <p>${escapeHtml(memory.content)}</p>
      ${memory.related_npc_name ? `<small>关联人物 · ${escapeHtml(memory.related_npc_name)}</small>` : ""}
    </article>`).join("") : '<p class="empty memory-empty">还没有形成记忆</p>';
  const goalNarrativeMap = new Map(goalNarratives.map(item => [item.goal_id, item]));
  const goalsHtml = goals.map(goal => {
    const format = goalValueLabels[goal.type] || (value => Number(value).toFixed(0));
    const targetPerson = goal.target_npc_name ? ` · ${escapeHtml(goal.target_npc_name)}` : "";
    const narrative = goalNarrativeMap.get(goal.id);
    return `<article class="goal ${goal.status}">
      <div class="goal-head"><strong>${escapeHtml(narrative?.content.title || goal.label)}${targetPerson}</strong><span>${goal.status === "completed" ? "已达成" : `优先级 ${Math.round(goal.priority * 100)}`}</span></div>
      <div class="goal-values"><span>${format(goal.current_value)} / ${format(goal.target_value)}</span><span>目标驱动力 ${Math.round(goal.need_score)}</span></div>
      <span class="track"><span class="fill" style="width:${goal.progress}%"></span></span>
      ${narrative ? `<p class="goal-motivation">${escapeHtml(narrative.content.motivation)}</p>` : '<p class="goal-motivation pending">叙事生成中；目标数值已由模拟引擎确定。</p>'}
    </article>`;
  }).join("");
  const dialoguesHtml = dialogues.length ? dialogues.map(dialogue => `
    <article class="dialogue">
      <div class="narrative-meta">第 ${Math.floor(dialogue.created_minute / 1440) + 1} 天 · ${dialogue.fallback_used ? "确定性回退" : "LLM"}</div>
      ${dialogue.content.lines.map(line => `<p><strong>${escapeHtml(line.speaker)}</strong>${escapeHtml(line.text)}</p>`).join("")}
    </article>`).join("") : '<p class="empty memory-empty">还没有发生可记录的对话</p>';
  const conversationsHtml = conversations.length ? conversations.map(conversation => `
    <article class="dialogue">
      <div class="narrative-meta">会话 #${conversation.id} · ${escapeHtml(conversation.status)} · ${conversation.completed_turn_count}/${conversation.target_turn_count} 轮</div>
      ${conversation.turns.map(turn => `<p><strong>${escapeHtml(turn.speaker.name)}</strong>${escapeHtml(turn.utterance)} <small>${escapeHtml(turn.provider)}${turn.fallback_used ? ` · 回退 ${escapeHtml(turn.failure_reason || "fallback")}` : ""}</small></p>`).join("")}
      ${conversation.participant_results.map(item => `<small>${escapeHtml(item.npc_name)} 的主观记忆：${escapeHtml(item.subjective_summary)}</small>`).join("<br>")}
      <small>事实边界：文本只读；关系/金钱/承诺/地点/行动均由 Engine 决定。</small>
    </article>`).join("") : '<p class="empty memory-empty">还没有多轮会话</p>';
  const summariesHtml = memorySummaries.length ? memorySummaries.map(summary => `
    <article class="summary"><div class="narrative-meta">记忆 ${summary.source_memory_start_id}–${summary.source_memory_end_id} · ${summary.fallback_used ? "确定性回退" : "LLM"}</div><p>${escapeHtml(summary.content.text)}</p></article>`).join("") : '<p class="empty memory-empty">积累 5 条新记忆后会生成总结</p>';
  const economyHtml = economy.enabled ? `
    <div class="economic-facts">
      <div class="fact"><small>基础时薪</small><strong>$${Number(economy.employment.base_wage).toFixed(2)}</strong></div>
      <div class="fact"><small>工作表现</small><strong>${Number(economy.employment.performance).toFixed(1)}</strong></div>
      <div class="fact"><small>完成班次</small><strong>${economy.employment.shifts_completed}</strong></div>
      <div class="fact"><small>累计工资</small><strong>$${Number(economy.employment.total_earnings).toFixed(2)}</strong></div>
    </div>
    <div class="housing-line"><strong>${escapeHtml(economy.housing.name)}</strong><span>舒适度 ${Number(economy.housing.comfort).toFixed(0)} · 周租 $${Number(economy.housing.weekly_rent).toFixed(2)}${economy.housing.arrears ? ` · 欠费 $${Number(economy.housing.arrears).toFixed(2)}` : ""}</span></div>
    <div class="skill-list">${economy.skills.map(skill => `<div class="skill"><span>${escapeHtml(skillLabels[skill.skill_key] || skill.skill_key)} Lv.${skill.level}</span><span class="track"><span class="fill" style="width:${skill.next_level_experience ? Math.min(100, skill.experience / skill.next_level_experience * 100) : 100}%"></span></span><strong>${Number(skill.experience).toFixed(0)} XP</strong></div>`).join("")}</div>
    <div class="inventory-list">${economy.inventory.length ? economy.inventory.map(item => `<span>${escapeHtml(item.name)} ×${item.quantity}</span>`).join("") : '<span class="muted-chip">库存为空</span>'}</div>
    <div class="transaction-list">${economy.transactions.length ? economy.transactions.slice(0, 8).map(tx => `<div><span>${escapeHtml(transactionLabels[tx.kind] || tx.kind)} · ${escapeHtml(tx.description)}</span><strong class="${tx.amount >= 0 ? "income" : "expense"}">${tx.amount >= 0 ? "+" : ""}$${Number(tx.amount).toFixed(2)}</strong></div>`).join("") : '<p class="empty memory-empty">还没有经济流水</p>'}</div>` : '<p class="empty memory-empty">经济系统未启用；基础行动模拟继续运行</p>';
  const budgetLabels = { food: "食物", housing: "住房", learning: "学习", entertainment: "娱乐", savings: "储蓄" };
  const careerHtml = career.enabled && budget.enabled ? `
    <div class="economic-facts">
      <div class="fact"><small>就业状态</small><strong>${career.employment_status === "employed" ? "在职" : "待业"}</strong></div>
      <div class="fact"><small>职业等级</small><strong>${escapeHtml(career.career_level_label)}</strong></div>
      <div class="fact"><small>可支配收入</small><strong>$${Number(budget.disposable_income).toFixed(2)}</strong></div>
      <div class="fact"><small>经济压力</small><strong>${Number(budget.economic_pressure).toFixed(1)}</strong></div>
    </div>
    <div class="budget-list">${Object.keys(budget.allocations).map(key => {
      const allocation = Number(budget.allocations[key]); const actual = Number(budget.actual[key]);
      return `<div class="budget-row"><span>${budgetLabels[key]}</span><span class="track"><span class="fill" style="width:${Math.min(100, actual / Math.max(1, allocation) * 100)}%"></span></span><strong>$${actual.toFixed(0)} / $${allocation.toFixed(0)}</strong></div>`;
    }).join("")}</div>
    <div class="pressure-reasons">${budget.pressure_reasons.map(reason => `<span>${escapeHtml(reason)}</span>`).join("")}</div>
    <div class="review-list">${career.reviews.length ? career.reviews.slice(0, 3).map(review => `<article><strong>评估 ${Number(review.score).toFixed(1)} · ${escapeHtml(review.outcome)}</strong><p>${review.reasons.map(escapeHtml).join("；")}</p></article>`).join("") : '<p class="empty memory-empty">首个完整周期后生成绩效评估</p>'}</div>
    <div class="report-list">${reports.length ? reports.map(report => `<article><strong>周收入 $${Number(report.income).toFixed(2)} · 结余 $${Number(report.saved).toFixed(2)}</strong><p>经济压力 ${Number(report.economic_pressure).toFixed(1)} · 可支配收入 $${Number(report.disposable_income).toFixed(2)}</p></article>`).join("") : '<p class="empty memory-empty">首个完整周后生成个人经济报告</p>'}</div>` : '<p class="empty memory-empty">职业与预算功能未启用；基础经济模拟继续运行</p>';
  const rhythmHtml = rhythm.enabled ? `
    <div class="economic-facts">
      <div class="fact"><small>排班</small><strong>${escapeHtml(rhythm.schedule.start)}–${escapeHtml(rhythm.schedule.end)}</strong></div>
      <div class="fact"><small>准时 / 迟到</small><strong>${rhythm.schedule.on_time_days} / ${rhythm.schedule.late_days}</strong></div>
      <div class="fact"><small>完成班次</small><strong>${rhythm.schedule.shifts_completed}</strong></div>
      <div class="fact"><small>今日节奏</small><strong>${rhythm.is_weekend ? "周末" : rhythm.today.on_workday ? "工作日" : "休息日"}</strong></div>
    </div>
    <div class="rhythm-chips"><span>${rhythm.today.store_open ? "商店营业中" : "商店已打烊"}</span><span>${rhythm.today.facility_available ? "设施有名额" : "设施不可用"}</span><span>${rhythm.today.training_available ? "可参加培训" : "培训不可用"}</span></div>
    <div class="housing-line"><strong>住房 ${escapeHtml(rhythm.housing.tier)}</strong><span>舒适度 ${Number(rhythm.housing.comfort).toFixed(0)} · 周租 $${Number(rhythm.housing.weekly_rent).toFixed(2)}${rhythm.housing.next_upgrade ? ` · 下一等级 ${escapeHtml(rhythm.housing.next_upgrade.tier)} $${Number(rhythm.housing.next_upgrade.cost).toFixed(0)}` : " · 已达最高等级"}</span></div>
    <div class="review-list">${rhythm.attendance.length ? rhythm.attendance.map(row => `<article><strong>第 ${row.world_day} 天 · ${row.status === "late" ? `迟到 ${row.minutes_late} 分钟` : "准时"}</strong><p>累计工作 ${row.worked_minutes} 分钟</p></article>`).join("") : '<p class="empty memory-empty">还没有出勤记录</p>'}</div>` : '<p class="empty memory-empty">社区生活节奏未启用；职业与预算功能继续运行</p>';
  const socialHtml = socialLife.enabled ? `
    <div class="economic-facts">
      <div class="fact"><small>归属感</small><strong>${Number(socialLife.belonging).toFixed(1)}</strong></div>
      <div class="fact"><small>信任指数</small><strong>${Number(socialLife.trust_index).toFixed(1)}</strong></div>
      <div class="fact"><small>待履行承诺</small><strong>${socialLife.commitments.filter(item => item.status === "planned").length}</strong></div>
      <div class="fact"><small>共同活动</small><strong>${socialLife.recent_activities.length}</strong></div>
    </div>
    <div class="social-chips">${socialLife.indicator_reasons.map(reason => `<span>${escapeHtml(reason)}</span>`).join("")}</div>
    <div class="review-list">${socialLife.bonds.map(bond => `<article><strong>${escapeHtml(bond.npc_low_id === Number(npcId) ? bond.npc_high_name : bond.npc_low_name)} · ${escapeHtml(stageLabels[bond.stage] || bond.stage)} · 信任 ${Number(bond.trust).toFixed(1)}</strong><p>双向 ${bond.low_to_high} / ${bond.high_to_low} · 差异 ${bond.asymmetry} · 衰减/修复 ${bond.decay_count}/${bond.repair_count}</p></article>`).join("")}</div>` : '<p class="empty memory-empty">群体关系功能未启用；社区生活节奏继续运行</p>';
  const timelineHtml = timeline.enabled && timeline.milestones.length ? timeline.milestones.map(item => `<article><strong>${escapeHtml(milestoneLabels[item.milestone_type] || item.milestone_type)} · ${escapeHtml(item.time_label)}</strong><p>${escapeHtml(item.title)}</p><small>事实摘要 ${escapeHtml(item.fact_digest.slice(0, 12))}</small></article>`).join("") : '<p class="empty memory-empty">还没有人生里程碑</p>';
  $("#npc-detail").innerHTML = `
    <div class="profile-head"><span class="avatar">${initials(npc.name)}</span><div><h2 id="npc-name">${escapeHtml(npc.name)}</h2><p>${escapeHtml(jobLabels[npc.job] || npc.job)} · ${npc.age} 岁</p></div></div>
    <div class="facts"><div class="fact"><small>金钱</small><strong>$${npc.money.toFixed(2)}</strong></div><div class="fact"><small>当前位置</small><strong>${escapeHtml(locationLabel(npc.current_location))}</strong></div><div class="fact"><small>当前行为</small><strong>${escapeHtml(actionLabel(npc.current_action))}</strong></div></div>
    <section class="panel-section"><h3>职业与生活经济</h3>${economyHtml}</section>
    <section class="panel-section"><h3>职业发展与个人预算</h3>${careerHtml}</section>
    <section class="panel-section"><h3>社区机构与生活节奏</h3>${rhythmHtml}</section>
    <section class="panel-section"><h3>群体关系与共同生活</h3>${socialHtml}</section>
    <section class="panel-section"><h3>人生事件时间线</h3><div class="review-list">${timelineHtml}</div></section>
    <section class="panel-section"><h3>长期目标</h3><div class="goal-list">${goalsHtml}</div></section>
    <section class="panel-section"><h3>当前状态</h3>${Object.entries(npc.states).map(([key, value]) => metric(fieldLabel(key), value)).join("")}</section>
    <section class="panel-section"><h3>性格特征</h3>${Object.entries(npc.personality).map(([key, value]) => metric(fieldLabel(key), value, true)).join("")}</section>
    <section class="panel-section"><h3>对他人的关系</h3>${npc.relationships.map(rel => `<div class="relation"><span>${escapeHtml(rel.name)}</span><span class="track"><span class="fill" style="width:${(rel.score + 100) / 2}%"></span></span><strong>${rel.score}</strong></div>`).join("")}</section>
    <section class="panel-section"><h3>多轮会话</h3><div class="dialogue-list">${conversationsHtml}</div></section>
    <section class="panel-section"><h3>每日反思、主观信念与计划</h3><div id="cognition-detail">${renderCognitionDetail(cognition)}</div></section>
    <section class="panel-section"><h3>基础叙事对话</h3><div class="dialogue-list">${dialoguesHtml}</div></section>
    <section class="panel-section"><h3>记忆总结</h3><div class="summary-list">${summariesHtml}</div></section>
    <section class="panel-section"><h3>记忆时间线</h3><div class="memory-list">${memoriesHtml}</div></section>
    <section class="panel-section"><h3>Agent Brain · 独立接管与审计</h3><div id="agent-control-detail">${renderAgentControl(agentControl)}</div></section>
    <section class="panel-section"><h3>Agent Brain · 影子决策兼容视图</h3><div id="agent-shadow-detail">${renderAgentShadow(agentShadow)}</div></section>
    <section class="panel-section"><h3>决策检查器</h3>${decisionHtml}</section>`;
  $("#modal").classList.remove("hidden");
}

async function legacyRefresh() {
  try {
    const [world, events, narrativeStatus, eventNarratives, economyStatus, stores, professions, careerStatus, reports, communityStatus, institutions, stock, socialStatus, bonds, circles, commitments, households, storyStatus, milestones, storySummaries, replay, productStatus, statistics, balance, onboarding, agentOverview, conversationStatus, conversations, cognitionStatus, cognitions, runtime] = await Promise.all([
      api("/api/world"), api("/api/events?limit=40"), api("/api/narrative/status"), api("/api/narratives/events?limit=80"),
      api("/api/economy"), api("/api/stores"), api("/api/professions"), api("/api/career-budget"), api("/api/economic-reports?limit=5"),
      api("/api/community-rhythm"), api("/api/institutions"), api("/api/store-stock"),
      api("/api/social-life"), api("/api/social-bonds"), api("/api/friend-circles"), api("/api/commitments"), api("/api/cohousing"),
      api("/api/life-story"), api("/api/milestones?limit=8"), api("/api/story-summaries?limit=3"), api("/api/story-replay"),
      api("/api/product"), api("/api/world-statistics"), api("/api/balance"), api("/api/onboarding"),
      api("/api/agents/takeover").catch(() => null),
      api("/api/agent-conversations/status").catch(() => null),
      api("/api/conversations?limit=6").catch(() => []),
      api("/api/agent-cognition/status").catch(() => null),
      Promise.all([1, 2, 3, 4, 5].map(id => api(`/api/agents/${id}/cognition`).catch(() => ({ npc_id: id, enabled: false, reflections: [], plans: [], subjective_beliefs: [] })))),
      api("/api/runtime").catch(() => null),
    ]);
    renderWorld(world); renderAgentOverview(agentOverview); renderRuntime(runtime); renderConversationOverview(conversationStatus, conversations); renderCognitionOverview(cognitionStatus, cognitions); renderProductOverview(productStatus, statistics, balance, onboarding); renderEvents(events, eventNarratives); renderEconomy(economyStatus, stores, professions); renderCareerOverview(careerStatus, reports); renderCommunityOverview(communityStatus, institutions, stock); renderSocialOverview(socialStatus, bonds, circles, commitments, households); renderStoryOverview(storyStatus, milestones, storySummaries, replay);
    const pending = narrativeStatus.jobs.pending + narrativeStatus.jobs.processing;
    $("#narrative-status").textContent = narrativeStatus.mode === "llm"
      ? `LLM 叙事层已启用 · ${narrativeStatus.model}${pending ? ` · ${pending} 项处理中` : ""}`
      : `无密钥安全模式 · 使用确定性叙事模板${pending ? ` · ${pending} 项处理中` : ""}`;
  } catch (error) { showToast(error.message); }
}

function showToast(message) {
  const toast = $("#toast"); toast.textContent = message; toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 3500);
}

const runtimeConfigurationErrors = {
  invalid_provider_key: "API Key 无效，请检查是否为空或包含不可见字符。",
  invalid_provider_model: "模型名称无效，请按服务商控制台中的模型 ID 填写。",
  invalid_provider_base_url: "接口地址无效，请填写完整的 HTTP 或 HTTPS 地址。",
  insecure_provider_base_url: "远程接口必须使用 HTTPS；HTTP 只允许本机地址。",
  provider_configuration_busy: "仍有模型任务未结束，请先紧急停止后再配置。",
  provider_configuration_requires_stopped_runtime: "请先停止在线自治，再更换模型配置。",
  local_configuration_only: "模型配置只允许从运行服务的本机打开。",
};

function openRuntimeConfig() {
  const modal = $("#runtime-config-modal");
  const form = $("#runtime-config-form");
  state.runtimeConfigLastFocus = document.activeElement;
  form.reset();
  $("#runtime-base-url").value = state.runtime?.provider?.base_url || "https://api.deepseek.com";
  $("#runtime-model").value = state.runtime?.provider?.model || "";
  $("#runtime-config-status").textContent = "";
  modal.classList.remove("hidden");
  document.body.classList.add("record-open");
  toggleBackgroundInert(true);
  $("#runtime-api-key").focus({ preventScroll: true });
}

function closeRuntimeConfig() {
  const modal = $("#runtime-config-modal");
  $("#runtime-api-key").value = "";
  $("#runtime-api-key").type = "password";
  $("#runtime-key-visibility").textContent = "显示";
  $("#runtime-key-visibility").setAttribute("aria-pressed", "false");
  modal.classList.add("hidden");
  if ($("#modal").classList.contains("hidden")) {
    document.body.classList.remove("record-open");
    toggleBackgroundInert(false);
  }
  state.runtimeConfigLastFocus?.focus?.({ preventScroll: true });
  state.runtimeConfigLastFocus = null;
}

async function legacyRefreshSelectedAgentShadow() {
  if (!state.selectedNpc || $("#modal").classList.contains("hidden")) return;
  const target = String(state.selectedNpc);
  try {
    const [shadow, control, cognition] = await Promise.all([
      api(`/api/npcs/${target}/agent-shadow`),
      fetchAgentControl(target),
      api(`/api/agents/${target}/cognition`).catch(() => null),
    ]);
    if (String(state.selectedNpc) === target && $("#agent-shadow-detail")) {
      $("#agent-shadow-detail").innerHTML = renderAgentShadow(shadow);
    }
    if (String(state.selectedNpc) === target && $("#agent-control-detail")) {
      $("#agent-control-detail").innerHTML = renderAgentControl(control);
    }
    if (String(state.selectedNpc) === target && $("#cognition-detail")) {
      $("#cognition-detail").innerHTML = renderCognitionDetail(cognition);
    }
  } catch (_) { /* V1.0 UI remains usable if the additive endpoint is unavailable. */ }
}

$("#pause-button").addEventListener("click", async () => { await api(state.world.paused ? "/api/world/resume" : "/api/world/pause", { method: "POST" }); refresh(); });
document.querySelectorAll(".speed").forEach(button => button.addEventListener("click", async () => { await api("/api/world/speed", { method: "POST", body: JSON.stringify({ speed: Number(button.dataset.speed) }) }); refresh(); }));
$("#reset-button").addEventListener("click", async () => { if (window.confirm("确定要重置当前存档吗？当前事件、记忆、关系、经济、人生故事、统计快照、平衡审计、新手引导和 NPC 状态都将恢复到该存档的安全起点；其他存档不会受影响。")) { await api("/api/world/reset", { method: "POST" }); closeNpcRecord({ historyMode: "replace" }); refresh(); } });
$("#locations").addEventListener("click", event => { const card = event.target.closest("[data-npc-id]"); if (card) openNpc(card.dataset.npcId).catch(error => showToast(error.message)); });
$("#modal").addEventListener("click", event => { if (event.target.dataset.close) closeNpcRecord(); });
document.addEventListener("click", event => {
  if (event.target.closest("[data-runtime-config-open]")) openRuntimeConfig();
});
$("#runtime-config-modal").addEventListener("click", event => {
  if (event.target.closest("[data-runtime-config-close]")) closeRuntimeConfig();
});
$("#runtime-key-visibility").addEventListener("click", event => {
  const input = $("#runtime-api-key");
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  event.currentTarget.textContent = visible ? "隐藏" : "显示";
  event.currentTarget.setAttribute("aria-pressed", String(visible));
  input.focus({ preventScroll: true });
});
$("#runtime-config-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("[type='submit']");
  const status = $("#runtime-config-status");
  submit.disabled = true;
  status.dataset.status = "pending";
  status.textContent = "正在保存到本次服务…";
  try {
    const runtime = await api("/api/runtime/provider", {
      method: "PUT",
      body: JSON.stringify({
        api_key: $("#runtime-api-key").value,
        base_url: $("#runtime-base-url").value,
        model: $("#runtime-model").value,
      }),
    }, "runtime-config");
    renderRuntime(runtime);
    closeRuntimeConfig();
    showToast("模型已配置，仅在本次服务运行期间有效");
  } catch (error) {
    status.dataset.status = "error";
    status.textContent = runtimeConfigurationErrors[error.code] || "配置失败，请检查接口地址、模型名称和 API Key。";
    $("#runtime-api-key").focus({ preventScroll: true });
  } finally {
    submit.disabled = false;
  }
});
$("#runtime-config-modal").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeRuntimeConfig();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...event.currentTarget.querySelectorAll('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])')];
  if (!focusable.length) return;
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-runtime-action]");
  if (!button) return;
  const action = button.dataset.runtimeAction;
  if (action === "emergency-stop" && !window.confirm("立即停止新的模型请求并作废在途结果？世界会继续使用安全 fallback。")) return;
  button.disabled = true;
  try {
    const body = action === "start" ? JSON.stringify({}) : action === "emergency-stop" ? JSON.stringify({ reason: "dashboard_emergency_stop" }) : undefined;
    await api(`/api/runtime/${action}`, { method: "POST", ...(body ? { body } : {}) });
    await refresh();
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-agent-toggle]");
  if (!button) return;
  button.disabled = true;
  try {
    const enabled = button.dataset.enabled !== "true";
    if (button.dataset.agentToggle === "global") {
      const overview = await api("/api/agents/takeover", { method: "PUT", body: JSON.stringify({ enabled }) });
      renderAgentOverview(overview);
    } else {
      const npcId = Number(button.dataset.npcId);
      const control = await api(`/api/agents/${npcId}/control`, { method: "PUT", body: JSON.stringify({ enabled }) });
      if (Number(state.selectedNpc) === npcId && $("#agent-control-detail")) {
        $("#agent-control-detail").innerHTML = renderAgentControl(control);
      }
      renderAgentOverview(await api("/api/agents/takeover"));
    }
    if (state.npcRecord?.activeTab === "decision") await loadNpcTab("decision", true);
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-cognition-toggle]");
  if (!button) return;
  button.disabled = true;
  try {
    const enabled = button.dataset.enabled !== "true";
    if (button.dataset.cognitionToggle === "global") {
      await api("/api/agent-cognition/status", { method: "PUT", body: JSON.stringify({ enabled }) });
    } else {
      await api(`/api/agents/${Number(button.dataset.npcId)}/cognition`, { method: "PUT", body: JSON.stringify({ enabled }) });
    }
    if (state.npcRecord?.activeTab === "memory") await loadNpcTab("memory", true);
    else await refresh();
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-conversation-toggle]");
  if (!button) return;
  button.disabled = true;
  try {
    const enabled = button.dataset.enabled !== "true";
    const status = await api("/api/agent-conversations/status", { method: "PUT", body: JSON.stringify({ enabled }) });
    renderConversationOverview(status, await api("/api/conversations?limit=6"));
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
document.addEventListener("keydown", event => { if (event.key === "Escape" && !$("#modal").classList.contains("hidden")) closeNpcRecord(); });

const groupRuns = new Map();
const groupUpdatedAt = new Map();
const NPC_TABS = [
  { key: "overview", label: "概览", interval: 10000 },
  { key: "decision", label: "决策与自治", interval: 6000 },
  { key: "life", label: "生活经济", interval: 30000 },
  { key: "social", label: "关系社交", interval: 10000 },
  { key: "memory", label: "记忆与人生", interval: 30000 },
];

function fulfilled(result, fallback = null) {
  return result?.status === "fulfilled" ? result.value : fallback;
}

function resultError(result) {
  return result?.status === "rejected" && result.reason?.name !== "AbortError" ? result.reason.message : null;
}

function runGroup(name, task) {
  if (groupRuns.has(name)) return groupRuns.get(name);
  const promise = Promise.resolve().then(task).finally(() => groupRuns.delete(name));
  groupRuns.set(name, promise);
  return promise;
}

function setDisclosureState(selector, status, message) {
  const disclosure = document.querySelector(selector);
  if (!disclosure) return;
  disclosure.dataset.status = status;
  const target = disclosure.querySelector(".disclosure-state");
  if (target) target.textContent = message;
}

function rejectedResult(results) {
  return Object.entries(results).find(([, result]) => result.status === "rejected" && result.reason?.name !== "AbortError");
}

function requireLegacyResults(results) {
  const rejected = Object.entries(results).find(([, result]) => result.status === "rejected");
  if (rejected) throw rejected[1].reason;
  return results;
}

const legacyDashboardLoaders = {
  runtime: () => api("/api/runtime", {}, "dashboard-fallback-runtime"),
  world: () => api("/api/world", {}, "dashboard-fallback-world"),
  npcs: async () => {
    const results = requireLegacyResults(await settleRequests([
      ["items", "/api/npcs"],
      ["agents", "/api/agents/takeover"],
    ], "dashboard-fallback-npcs"));
    return { items: results.items.value, agents: results.agents.value };
  },
  pulse: async () => {
    const results = requireLegacyResults(await settleRequests([
      ["events", "/api/events?limit=40"],
      ["narrativeStatus", "/api/narrative/status"],
      ["narratives", "/api/narratives/events?limit=80"],
    ], "dashboard-fallback-pulse"));
    return {
      events: results.events.value,
      narrative_status: results.narrativeStatus.value,
      narratives: results.narratives.value,
    };
  },
};

const dashboardResolver = new SnapshotResolver({
  fallbackLoaders: legacyDashboardLoaders,
  fallbackIntervals: { runtime: 3000, world: 3000, npcs: 3000, pulse: 5000 },
});
let dashboardSnapshotSupported = true;

function isMissingSnapshotEndpoint(error) {
  return error?.status === 404 && error?.code !== "npc_not_found";
}

function renderPulseModule(data) {
  renderEvents(data.events || [], data.narratives || []);
  const narrativeStatus = data.narrative_status;
  if (!narrativeStatus) return;
  const pending = Number(narrativeStatus.jobs?.pending || 0) + Number(narrativeStatus.jobs?.processing || 0);
  $("#narrative-status").textContent = narrativeStatus.mode === "llm"
    ? `LLM 叙事层已启用 · ${narrativeStatus.model}${pending ? ` · ${pending} 项处理中` : ""}`
    : `无密钥安全模式 · 使用确定性叙事模板${pending ? ` · ${pending} 项处理中` : ""}`;
}

function renderModuleUnavailable(name, entry) {
  const messages = {
    runtime: ["#runtime-overview", "运行时暂不可用", "世界与其他模块继续显示上次成功结果。"],
    world: ["#locations", "世界总览暂不可用", "运行安全与人物模块仍可独立更新。"],
    npcs: ["#npc-overview", "人物状态暂不可用", "世界总览与世界脉搏仍可独立更新。"],
    pulse: ["#events", "世界脉搏暂不可用", "其他成功模块不会被清空。"],
  };
  const [selector, title, detail] = messages[name];
  const target = $(selector);
  setHtml(target, `<div class="module-error"><strong>${title}</strong><p>${detail}</p><small>${escapeHtml(entry.error)}</small></div>`, `${name}-module-error`);
  target?.setAttribute("aria-busy", "false");
}

function applyDashboardEntry(name, entry) {
  const freshnessIds = { runtime: "runtime-freshness", world: "world-freshness", npcs: "npc-freshness", pulse: "pulse-freshness" };
  moduleFreshness(freshnessIds[name], entry);
  if (entry.data === undefined) {
    renderModuleUnavailable(name, entry);
    return;
  }
  if (name === "runtime") renderRuntime(entry.data);
  else if (name === "world") renderWorld(entry.data);
  else if (name === "npcs") {
    state.npcs = entry.data.items || [];
    renderAgentOverview(entry.data.agents);
    renderNpcOverview();
  } else if (name === "pulse") renderPulseModule(entry.data);
}

async function refreshDashboardSnapshot(force = false) {
  if (document.hidden || state.selectedNpc) return;
  return runGroup("dashboard-snapshot", async () => {
    let payload = null;
    let endpointError = dashboardSnapshotSupported ? null : new Error("聚合读取未启用");
    if (dashboardSnapshotSupported) {
      try {
        payload = await api("/api/dashboard/snapshot?groups=runtime,world,npcs,pulse", {}, "dashboard-snapshot");
      } catch (error) {
        if (error.name === "AbortError") return;
        endpointError = error;
        if (isMissingSnapshotEndpoint(error)) dashboardSnapshotSupported = false;
      }
    }
    let entries;
    try { entries = await dashboardResolver.resolve(payload, HOME_GROUPS, { endpointError, force }); }
    catch (error) { if (error.name === "AbortError") return; throw error; }
    HOME_GROUPS.forEach(name => applyDashboardEntry(name, entries[name]));
    const fallbackCount = HOME_GROUPS.filter(name => entries[name].source === "fallback").length;
    const unhealthyCount = HOME_GROUPS.filter(name => entries[name].status !== "ok").length;
    const message = fallbackCount
      ? unhealthyCount ? `${unhealthyCount} 个模块暂时无法更新` : "兼容读取正常"
      : "数据同步正常";
    freshness("overview-freshness", unhealthyCount ? "stale" : "ok", message);
    groupUpdatedAt.set("fast", Date.now());
    groupUpdatedAt.set("pulse", Date.now());
  });
}

async function refreshFast(force = false) { return refreshDashboardSnapshot(force); }
async function refreshPulse(force = false) { return refreshDashboardSnapshot(force); }

const trendLoaders = {
  product: async () => {
    const r = await settleRequests([["status", "/api/product"], ["statistics", "/api/world-statistics"], ["balance", "/api/balance"], ["onboarding", "/api/onboarding"]], "trend-product");
    if (Object.values(r).every(item => item.status === "fulfilled")) renderProductOverview(r.status.value, r.statistics.value, r.balance.value, r.onboarding.value);
    return r;
  },
  economy: async () => {
    const r = await settleRequests([["status", "/api/economy"], ["stores", "/api/stores"], ["professions", "/api/professions"]], "trend-economy");
    if (Object.values(r).every(item => item.status === "fulfilled")) renderEconomy(r.status.value, r.stores.value, r.professions.value);
    return r;
  },
  career: async () => {
    const r = await settleRequests([["status", "/api/career-budget"], ["reports", "/api/economic-reports?limit=5"]], "trend-career");
    if (Object.values(r).every(item => item.status === "fulfilled")) renderCareerOverview(r.status.value, r.reports.value);
    return r;
  },
  community: async () => {
    const r = await settleRequests([["status", "/api/community-rhythm"], ["institutions", "/api/institutions"], ["stock", "/api/store-stock"]], "trend-community");
    if (Object.values(r).every(item => item.status === "fulfilled")) renderCommunityOverview(r.status.value, r.institutions.value, r.stock.value);
    return r;
  },
  social: async () => {
    const r = await settleRequests([["status", "/api/social-life"], ["bonds", "/api/social-bonds"], ["circles", "/api/friend-circles"], ["commitments", "/api/commitments"], ["households", "/api/cohousing"]], "trend-social");
    if (Object.values(r).every(item => item.status === "fulfilled")) renderSocialOverview(r.status.value, r.bonds.value, r.circles.value, r.commitments.value, r.households.value);
    return r;
  },
  story: async () => {
    const r = await settleRequests([["status", "/api/life-story"], ["milestones", "/api/milestones?limit=8"], ["summaries", "/api/story-summaries?limit=3"], ["replay", "/api/story-replay"]], "trend-story");
    if (Object.values(r).every(item => item.status === "fulfilled")) renderStoryOverview(r.status.value, r.milestones.value, r.summaries.value, r.replay.value);
    return r;
  },
};

async function loadTrendGroup(group, force = false) {
  const disclosure = document.querySelector(`[data-trend-group="${group}"]`);
  if (!disclosure?.open || !trendLoaders[group] || document.hidden || state.selectedNpc) return;
  const last = groupUpdatedAt.get(`trend-${group}`) || 0;
  if (!force && Date.now() - last < 30000) return;
  return runGroup(`trend-${group}`, async () => {
    setDisclosureState(`[data-trend-group="${group}"]`, "loading", "正在载入");
    const results = await trendLoaders[group]();
    const errors = Object.entries(results).filter(([, value]) => value.status === "rejected" && value.reason?.name !== "AbortError");
    if (errors.length) setDisclosureState(`[data-trend-group="${group}"]`, "error", `部分失败 · ${errors.map(([name]) => name).join("、")}`);
    else {
      setDisclosureState(`[data-trend-group="${group}"]`, "ok", formatSyncTime());
      groupUpdatedAt.set(`trend-${group}`, Date.now());
    }
  });
}

async function loadAuditGroup(group, force = false) {
  const disclosure = document.querySelector(`[data-audit-group="${group}"]`);
  if (!disclosure?.open || document.hidden || state.selectedNpc) return;
  const last = groupUpdatedAt.get(`audit-${group}`) || 0;
  if (!force && Date.now() - last < (group === "agents" ? 2000 : 10000)) return;
  return runGroup(`audit-${group}`, async () => {
    setDisclosureState(`[data-audit-group="${group}"]`, "loading", "正在载入");
    let errors = [];
    if (group === "agents") {
      if (state.agentOverview) renderAgentOverview(state.agentOverview);
      else errors = ["Agent 总览尚未同步"];
    } else if (group === "conversations") {
      const r = await settleRequests([["status", "/api/agent-conversations/status"], ["items", "/api/conversations?limit=6"]], "audit-conversations");
      errors = Object.values(r).filter(item => item.status === "rejected").map(resultError);
      if (!errors.length) renderConversationOverview(r.status.value, r.items.value);
    } else if (group === "cognition") {
      const entries = [["status", "/api/agent-cognition/status"], ...[1, 2, 3, 4, 5].map(id => [`npc${id}`, `/api/agents/${id}/cognition`])];
      const r = await settleRequests(entries, "audit-cognition");
      errors = Object.values(r).filter(item => item.status === "rejected").map(resultError);
      const cognitions = [1, 2, 3, 4, 5].map(id => fulfilled(r[`npc${id}`], { npc_id: id, enabled: false, reflections: [], plans: [], subjective_beliefs: [] }));
      if (r.status.status === "fulfilled") renderCognitionOverview(r.status.value, cognitions);
    }
    if (errors.length) setDisclosureState(`[data-audit-group="${group}"]`, "error", `保留上次结果 · ${errors[0]}`);
    else {
      setDisclosureState(`[data-audit-group="${group}"]`, "ok", formatSyncTime());
      groupUpdatedAt.set(`audit-${group}`, Date.now());
    }
  });
}

async function refreshVisibleLowFrequency(force = false) {
  if (document.hidden || state.selectedNpc) return;
  const trends = [...document.querySelectorAll("[data-trend-group][open]")].map(item => loadTrendGroup(item.dataset.trendGroup, force));
  const audits = [...document.querySelectorAll("[data-audit-group][open]")].map(item => loadAuditGroup(item.dataset.auditGroup, force));
  await Promise.allSettled([...trends, ...audits]);
}

function renderRecordHeader(npc) {
  const statusMetrics = [["能量", npc.states.energy], ["饥饿", npc.states.hunger], ["心情", npc.states.mood], ["社交", npc.states.social_need]];
  setHtml($("#npc-record-header"), `<div class="record-identity"><span class="avatar">${initials(npc.name)}</span><div><h2 id="npc-name">${escapeHtml(npc.name)}</h2><p>${escapeHtml(jobLabels[npc.job] || npc.job)} · ${npc.age} 岁</p></div></div>
    <div class="record-primary-facts"><span><small>当前位置</small><strong>${escapeHtml(locationLabel(npc.current_location))}</strong></span><span><small>当前行为</small><strong>${escapeHtml(actionLabel(npc.current_action))}</strong></span><span><small>金钱</small><strong>$${Number(npc.money).toFixed(2)}</strong></span></div>
    <div class="record-vitals">${statusMetrics.map(([label, value]) => `<span><small>${label}</small><strong>${Math.round(Number(value))}</strong><i class="mini-track"><i style="width:${Math.max(0, Math.min(100, Number(value)))}%"></i></i></span>`).join("")}</div>`, "npc-record-header");
}

function renderRecordTabs(activeKey) {
  const html = NPC_TABS.map(tab => `<button id="npc-tab-${tab.key}" type="button" role="tab" data-npc-tab="${tab.key}" aria-selected="${tab.key === activeKey}" aria-controls="npc-detail" tabindex="${tab.key === activeKey ? "0" : "-1"}">${tab.label}</button>`).join("");
  setHtml($("#npc-tabs"), html, "npc-tabs");
  $("#npc-detail").setAttribute("aria-labelledby", `npc-tab-${activeKey}`);
}

function goalsMarkup(goals, goalNarratives) {
  const narrativeMap = new Map((goalNarratives || []).map(item => [item.goal_id, item]));
  return (goals || []).map(goal => {
    const format = goalValueLabels[goal.type] || (value => Number(value).toFixed(0));
    const narrative = narrativeMap.get(goal.id);
    return `<article class="goal ${escapeHtml(goal.status)}"><div class="goal-head"><strong>${escapeHtml(narrative?.content?.title || goal.label)}${goal.target_npc_name ? ` · ${escapeHtml(goal.target_npc_name)}` : ""}</strong><span>${goal.status === "completed" ? "已达成" : `优先级 ${Math.round(goal.priority * 100)}`}</span></div><div class="goal-values"><span>${format(goal.current_value)} / ${format(goal.target_value)}</span><span>驱动力 ${Math.round(goal.need_score)}</span></div><span class="track"><span class="fill" style="width:${goal.progress}%"></span></span>${narrative ? `<p class="goal-motivation">${escapeHtml(narrative.content.motivation)}</p>` : '<p class="goal-motivation pending">叙事生成中；数值已由 Engine 确定。</p>'}</article>`;
  }).join("") || '<p class="empty memory-empty">当前没有长期目标。</p>';
}

function renderNpcOverviewTab(data) {
  const npc = data.npc;
  const rhythm = data.rhythm;
  const social = data.social;
  const rhythmSummary = rhythm?.enabled ? `<div class="summary-strip"><span><small>今日节奏</small><strong>${rhythm.is_weekend ? "周末" : rhythm.today.on_workday ? "工作日" : "休息日"}</strong></span><span><small>排班</small><strong>${escapeHtml(rhythm.schedule.start)}–${escapeHtml(rhythm.schedule.end)}</strong></span><span><small>住房</small><strong>${escapeHtml(rhythm.housing.tier)}</strong></span></div>` : '<p class="empty memory-empty">社区节奏处于兼容模式。</p>';
  const socialSummary = social?.enabled ? `<div class="summary-strip"><span><small>归属感</small><strong>${Number(social.belonging).toFixed(1)}</strong></span><span><small>信任指数</small><strong>${Number(social.trust_index).toFixed(1)}</strong></span><span><small>待履行承诺</small><strong>${social.commitments.filter(item => item.status === "planned").length}</strong></span></div>` : '<p class="empty memory-empty">群体关系处于兼容模式。</p>';
  return `<section class="record-section"><h3>此刻状态</h3>${Object.entries(npc.states).map(([key, value]) => metric(fieldLabel(key), value)).join("")}</section>
    <section class="record-section"><h3>长期目标</h3><div class="goal-list">${goalsMarkup(data.goals, data.goalNarratives)}</div></section>
    <section class="record-section"><h3>今日生活</h3>${rhythmSummary}</section>
    <section class="record-section"><h3>关系摘要</h3>${socialSummary}</section>
    <section class="record-section"><h3>性格特征</h3>${Object.entries(npc.personality).map(([key, value]) => metric(fieldLabel(key), value, true)).join("")}</section>`;
}

function renderNpcDecisionTab(data) {
  const decision = data.decision;
  const candidates = decision.candidates || [];
  const maxScore = Math.max(1, ...candidates.filter(item => item.available).map(item => item.score));
  const decisionHtml = decision.chosen_action ? `<div class="decision-box"><small>选择时间：第 ${decision.world_day} 天 · ${escapeHtml(decision.world_time)}</small><div class="decision-choice">${escapeHtml(actionLabel(decision.chosen_action))}</div>${candidates.map(item => `<div class="candidate ${item.available ? "" : "unavailable"}"><span>${escapeHtml(actionLabel(item.action))}</span><span class="track"><span class="fill" style="width:${item.available ? Math.max(0, item.score / maxScore * 100) : 0}%"></span></span><strong>${item.available ? item.score.toFixed(1) : "不可用"}</strong></div>`).join("")}<ul class="reason-list"><li><strong>${escapeHtml(translateDecisionSummary(decision.reason.summary))}</strong><span></span></li>${Object.entries(decision.reason.top_contributions || {}).map(([key, value]) => `<li><span>${escapeHtml(contributionLabel(key))}</span><strong>${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(1)}</strong></li>`).join("")}</ul></div>` : `<div class="decision-box">${escapeHtml(translateDecisionSummary(decision.reason.summary))}</div>`;
  return `<section class="record-section"><h3>Engine 决策检查器</h3>${decisionHtml}</section>
    <section class="record-section"><h3>Utility → Agent → Engine 复核 → 最终行动</h3><div id="agent-control-detail">${renderAgentControl(data.control)}</div></section>
    <section class="record-section"><h3>Agent 影子建议 · 不执行</h3><div id="agent-shadow-detail">${renderAgentShadow(data.shadow)}</div></section>`;
}

function economyMarkup(economy) {
  if (!economy?.enabled) return '<p class="empty memory-empty">经济系统未启用；基础行动模拟继续运行。</p>';
  return `<div class="economic-facts"><div class="fact"><small>基础时薪</small><strong>$${Number(economy.employment.base_wage).toFixed(2)}</strong></div><div class="fact"><small>工作表现</small><strong>${Number(economy.employment.performance).toFixed(1)}</strong></div><div class="fact"><small>完成班次</small><strong>${economy.employment.shifts_completed}</strong></div><div class="fact"><small>累计工资</small><strong>$${Number(economy.employment.total_earnings).toFixed(2)}</strong></div></div>
    <div class="housing-line"><strong>${escapeHtml(economy.housing.name)}</strong><span>舒适度 ${Number(economy.housing.comfort).toFixed(0)} · 周租 $${Number(economy.housing.weekly_rent).toFixed(2)}${economy.housing.arrears ? ` · 欠费 $${Number(economy.housing.arrears).toFixed(2)}` : ""}</span></div>
    <div class="skill-list">${economy.skills.map(skill => `<div class="skill"><span>${escapeHtml(skillLabels[skill.skill_key] || skill.skill_key)} Lv.${skill.level}</span><span class="track"><span class="fill" style="width:${skill.next_level_experience ? Math.min(100, skill.experience / skill.next_level_experience * 100) : 100}%"></span></span><strong>${Number(skill.experience).toFixed(0)} XP</strong></div>`).join("")}</div>
    <div class="inventory-list">${economy.inventory.length ? economy.inventory.map(item => `<span>${escapeHtml(item.name)} ×${item.quantity}</span>`).join("") : '<span class="muted-chip">库存为空</span>'}</div>
    <div class="transaction-list">${economy.transactions.length ? economy.transactions.slice(0, 8).map(tx => `<div><span>${escapeHtml(transactionLabels[tx.kind] || tx.kind)} · ${escapeHtml(tx.description)}</span><strong class="${tx.amount >= 0 ? "income" : "expense"}">${tx.amount >= 0 ? "+" : ""}$${Number(tx.amount).toFixed(2)}</strong></div>`).join("") : '<p class="empty memory-empty">还没有经济流水。</p>'}</div>`;
}

function careerMarkup(career, budget, reports) {
  if (!career?.enabled || !budget?.enabled) return '<p class="empty memory-empty">职业与预算功能未启用；基础经济模拟继续运行。</p>';
  const labels = { food: "食物", housing: "住房", learning: "学习", entertainment: "娱乐", savings: "储蓄" };
  return `<div class="economic-facts"><div class="fact"><small>就业状态</small><strong>${career.employment_status === "employed" ? "在职" : "待业"}</strong></div><div class="fact"><small>职业等级</small><strong>${escapeHtml(career.career_level_label)}</strong></div><div class="fact"><small>可支配收入</small><strong>$${Number(budget.disposable_income).toFixed(2)}</strong></div><div class="fact"><small>经济压力</small><strong>${Number(budget.economic_pressure).toFixed(1)}</strong></div></div>
    <div class="budget-list">${Object.keys(budget.allocations).map(key => { const allocation = Number(budget.allocations[key]); const actual = Number(budget.actual[key]); return `<div class="budget-row"><span>${labels[key]}</span><span class="track"><span class="fill" style="width:${Math.min(100, actual / Math.max(1, allocation) * 100)}%"></span></span><strong>$${actual.toFixed(0)} / $${allocation.toFixed(0)}</strong></div>`; }).join("")}</div>
    <div class="pressure-reasons">${budget.pressure_reasons.map(reason => `<span>${escapeHtml(reason)}</span>`).join("")}</div>
    <div class="review-list">${career.reviews.length ? career.reviews.slice(0, 3).map(review => `<article><strong>评估 ${Number(review.score).toFixed(1)} · ${escapeHtml(review.outcome)}</strong><p>${review.reasons.map(escapeHtml).join("；")}</p></article>`).join("") : '<p class="empty memory-empty">首个完整周期后生成绩效评估。</p>'}</div>
    <div class="report-list">${reports.length ? reports.map(report => `<article><strong>周收入 $${Number(report.income).toFixed(2)} · 结余 $${Number(report.saved).toFixed(2)}</strong><p>经济压力 ${Number(report.economic_pressure).toFixed(1)} · 可支配收入 $${Number(report.disposable_income).toFixed(2)}</p></article>`).join("") : '<p class="empty memory-empty">首个完整周后生成个人经济报告。</p>'}</div>`;
}

function rhythmMarkup(rhythm) {
  if (!rhythm?.enabled) return '<p class="empty memory-empty">社区生活节奏未启用；职业与预算功能继续运行。</p>';
  return `<div class="economic-facts"><div class="fact"><small>排班</small><strong>${escapeHtml(rhythm.schedule.start)}–${escapeHtml(rhythm.schedule.end)}</strong></div><div class="fact"><small>准时 / 迟到</small><strong>${rhythm.schedule.on_time_days} / ${rhythm.schedule.late_days}</strong></div><div class="fact"><small>完成班次</small><strong>${rhythm.schedule.shifts_completed}</strong></div><div class="fact"><small>今日节奏</small><strong>${rhythm.is_weekend ? "周末" : rhythm.today.on_workday ? "工作日" : "休息日"}</strong></div></div>
    <div class="rhythm-chips"><span>${rhythm.today.store_open ? "商店营业中" : "商店已打烊"}</span><span>${rhythm.today.facility_available ? "设施有名额" : "设施不可用"}</span><span>${rhythm.today.training_available ? "可参加培训" : "培训不可用"}</span></div>
    <div class="housing-line"><strong>住房 ${escapeHtml(rhythm.housing.tier)}</strong><span>舒适度 ${Number(rhythm.housing.comfort).toFixed(0)} · 周租 $${Number(rhythm.housing.weekly_rent).toFixed(2)}${rhythm.housing.next_upgrade ? ` · 下一等级 ${escapeHtml(rhythm.housing.next_upgrade.tier)} $${Number(rhythm.housing.next_upgrade.cost).toFixed(0)}` : " · 已达最高等级"}</span></div>
    <div class="review-list">${rhythm.attendance.length ? rhythm.attendance.map(row => `<article><strong>第 ${row.world_day} 天 · ${row.status === "late" ? `迟到 ${row.minutes_late} 分钟` : "准时"}</strong><p>累计工作 ${row.worked_minutes} 分钟</p></article>`).join("") : '<p class="empty memory-empty">还没有出勤记录。</p>'}</div>`;
}

function renderNpcLifeTab(data) {
  return `<section class="record-section"><h3>职业与生活经济</h3>${economyMarkup(data.economy)}</section><section class="record-section"><h3>职业发展与个人预算</h3>${careerMarkup(data.career, data.budget, data.reports)}</section><section class="record-section"><h3>社区机构与生活节奏</h3>${rhythmMarkup(data.rhythm)}</section>`;
}

function renderNpcSocialTab(data) {
  const npc = data.npc;
  const social = data.social;
  const relationships = npc.relationships.map(rel => `<div class="relation"><span>${escapeHtml(rel.name)}</span><span class="track"><span class="fill" style="width:${(rel.score + 100) / 2}%"></span></span><strong>${rel.score}</strong></div>`).join("");
  const socialHtml = social?.enabled ? `<div class="economic-facts"><div class="fact"><small>归属感</small><strong>${Number(social.belonging).toFixed(1)}</strong></div><div class="fact"><small>信任指数</small><strong>${Number(social.trust_index).toFixed(1)}</strong></div><div class="fact"><small>待履行承诺</small><strong>${social.commitments.filter(item => item.status === "planned").length}</strong></div><div class="fact"><small>共同活动</small><strong>${social.recent_activities.length}</strong></div></div><div class="social-chips">${social.indicator_reasons.map(reason => `<span>${escapeHtml(reason)}</span>`).join("")}</div><div class="review-list">${social.bonds.map(bond => `<article><strong>${escapeHtml(bond.npc_low_id === Number(npc.id) ? bond.npc_high_name : bond.npc_low_name)} · ${escapeHtml(stageLabels[bond.stage] || bond.stage)} · 信任 ${Number(bond.trust).toFixed(1)}</strong><p>双向 ${bond.low_to_high} / ${bond.high_to_low} · 差异 ${bond.asymmetry} · 衰减/修复 ${bond.decay_count}/${bond.repair_count}</p></article>`).join("")}</div>` : '<p class="empty memory-empty">群体关系功能未启用；社区生活节奏继续运行。</p>';
  const conversations = data.conversations.length ? data.conversations.map(conversation => `<article class="dialogue"><div class="narrative-meta">会话 #${conversation.id} · ${escapeHtml(conversation.status)} · ${conversation.completed_turn_count}/${conversation.target_turn_count} 轮</div>${conversation.turns.map(turn => `<p><strong>${escapeHtml(turn.speaker.name)}</strong>${escapeHtml(turn.utterance)} <small>${escapeHtml(turn.provider)}${turn.fallback_used ? ` · 回退 ${escapeHtml(turn.failure_reason || "fallback")}` : ""}</small></p>`).join("")}${conversation.participant_results.map(item => `<small>${escapeHtml(item.npc_name)} 的主观记忆：${escapeHtml(item.subjective_summary)}</small>`).join("<br>")}<small>事实边界：文本只读；关系、金钱、承诺、地点和行动均由 Engine 决定。</small></article>`).join("") : '<p class="empty memory-empty">还没有多轮会话。</p>';
  const dialogues = data.dialogues.length ? data.dialogues.map(dialogue => `<article class="dialogue"><div class="narrative-meta">第 ${Math.floor(dialogue.created_minute / 1440) + 1} 天 · ${dialogue.fallback_used ? "确定性回退" : "LLM"}</div>${dialogue.content.lines.map(line => `<p><strong>${escapeHtml(line.speaker)}</strong>${escapeHtml(line.text)}</p>`).join("")}</article>`).join("") : '<p class="empty memory-empty">还没有兼容对话。</p>';
  return `<section class="record-section"><h3>对他人的有向关系</h3>${relationships}</section><section class="record-section"><h3>双向关系与共同生活</h3>${socialHtml}</section><section class="record-section"><h3>多轮会话</h3><div class="dialogue-list">${conversations}</div></section><section class="record-section"><h3>基础叙事对话</h3><div class="dialogue-list">${dialogues}</div></section>`;
}

function renderNpcMemoryTab(data) {
  const memories = data.memories.length ? data.memories.map(memory => `<article class="memory ${escapeHtml(memory.emotion)}"><div class="memory-meta"><time>第 ${memory.world_day} 天 · ${escapeHtml(memory.world_time)}</time><span>${escapeHtml(emotionLabels[memory.emotion] || memory.emotion)}</span><strong>重要度 ${memory.importance}</strong></div><p>${escapeHtml(memory.content)}</p>${memory.related_npc_name ? `<small>关联人物 · ${escapeHtml(memory.related_npc_name)}</small>` : ""}</article>`).join("") : '<p class="empty memory-empty">还没有形成记忆。</p>';
  const summaries = data.summaries.length ? data.summaries.map(summary => `<article class="summary"><div class="narrative-meta">记忆 ${summary.source_memory_start_id}–${summary.source_memory_end_id} · ${summary.fallback_used ? "确定性回退" : "LLM"}</div><p>${escapeHtml(summary.content.text)}</p></article>`).join("") : '<p class="empty memory-empty">积累 5 条新记忆后会生成总结。</p>';
  const timeline = data.timeline?.enabled && data.timeline.milestones.length ? data.timeline.milestones.map(item => `<article><strong>${escapeHtml(milestoneLabels[item.milestone_type] || item.milestone_type)} · ${escapeHtml(item.time_label)}</strong><p>${escapeHtml(item.title)}</p><small>事实摘要 ${escapeHtml(item.fact_digest.slice(0, 12))}</small></article>`).join("") : '<p class="empty memory-empty">还没有人生里程碑。</p>';
  return `<section class="record-section"><h3>人生事件时间线</h3><div class="review-list">${timeline}</div></section><section class="record-section"><h3>每日反思、主观信念与计划</h3><div id="cognition-detail">${renderCognitionDetail(data.cognition)}</div></section><section class="record-section"><h3>记忆总结</h3><div class="summary-list">${summaries}</div></section><section class="record-section"><h3>记忆时间线</h3><div class="memory-list">${memories}</div></section>`;
}

async function legacyNpcOverview(id) {
  const results = requireLegacyResults(await settleRequests([
    ["npc", `/api/npcs/${id}`],
    ["goals", `/api/npcs/${id}/goals`],
    ["goalNarratives", `/api/npcs/${id}/goal-narratives`],
    ["rhythm", `/api/npcs/${id}/rhythm`],
    ["social", `/api/npcs/${id}/social-life`],
  ], `npc-${id}-overview`));
  return {
    npc: results.npc.value,
    goals: results.goals.value,
    goal_narratives: results.goalNarratives.value,
    rhythm: results.rhythm.value,
    social: results.social.value,
  };
}

async function legacyNpcDecision(id) {
  const results = await settleRequests([
    ["decision", `/api/npcs/${id}/decision`],
    ["shadow", `/api/npcs/${id}/agent-shadow`],
  ], `npc-${id}-decision`);
  try { results.control = { status: "fulfilled", value: await fetchAgentControl(id, `npc-${id}-decision`) }; }
  catch (error) { results.control = { status: "rejected", reason: error }; }
  requireLegacyResults(results);
  return { decision: results.decision.value, shadow: results.shadow.value, control: results.control.value };
}

async function loadNpcSnapshotSection(id, section, force = false) {
  const record = state.npcRecord;
  if (!record || record.id !== Number(id)) throw new DOMException("NPC record changed", "AbortError");
  let payload = null;
  let endpointError = dashboardSnapshotSupported ? null : new Error("聚合读取未启用");
  if (dashboardSnapshotSupported) {
    try {
      payload = await api(`/api/dashboard/npcs/${id}/snapshot?sections=${section}`, {}, `npc-${id}-${section}`);
    } catch (error) {
      if (error.name === "AbortError") throw error;
      endpointError = error;
      if (isMissingSnapshotEndpoint(error)) dashboardSnapshotSupported = false;
    }
  }
  const entries = await record.snapshotResolver.resolve(payload, [section], { endpointError, force });
  const entry = entries[section];
  const data = section === "overview" && entry.data
    ? { ...entry.data, goalNarratives: entry.data.goal_narratives || entry.data.goalNarratives || [] }
    : entry.data;
  return { snapshotEntry: { ...entry, data }, data, results: {} };
}

const npcTabLoaders = {
  overview: (id, force = false) => loadNpcSnapshotSection(id, "overview", force),
  decision: (id, force = false) => loadNpcSnapshotSection(id, "decision", force),
  life: async id => {
    const r = await settleRequests([["economy", `/api/npcs/${id}/economy`], ["career", `/api/npcs/${id}/career`], ["budget", `/api/npcs/${id}/budget`], ["reports", `/api/npcs/${id}/economic-reports?limit=4`], ["rhythm", `/api/npcs/${id}/rhythm`]], `npc-${id}-life`);
    return { results: r, data: { economy: fulfilled(r.economy), career: fulfilled(r.career), budget: fulfilled(r.budget), reports: fulfilled(r.reports, []), rhythm: fulfilled(r.rhythm) } };
  },
  social: async id => {
    const r = await settleRequests([["npc", `/api/npcs/${id}`], ["social", `/api/npcs/${id}/social-life`], ["conversations", `/api/npcs/${id}/conversations?limit=10`], ["dialogues", `/api/npcs/${id}/dialogues?limit=10`]], `npc-${id}-social`);
    return { results: r, data: { npc: fulfilled(r.npc), social: fulfilled(r.social), conversations: fulfilled(r.conversations, []), dialogues: fulfilled(r.dialogues, []) } };
  },
  memory: async id => {
    const r = await settleRequests([["timeline", `/api/npcs/${id}/timeline?limit=30`], ["memories", `/api/npcs/${id}/memories?limit=20`], ["summaries", `/api/npcs/${id}/memory-summaries?limit=5`], ["cognition", `/api/agents/${id}/cognition`]], `npc-${id}-memory`);
    return { results: r, data: { timeline: fulfilled(r.timeline), memories: fulfilled(r.memories, []), summaries: fulfilled(r.summaries, []), cognition: fulfilled(r.cognition) } };
  },
};

const npcTabRenderers = { overview: renderNpcOverviewTab, decision: renderNpcDecisionTab, life: renderNpcLifeTab, social: renderNpcSocialTab, memory: renderNpcMemoryTab };
const npcTabRequiredData = { overview: ["npc"], decision: ["decision"], life: [], social: ["npc"], memory: [] };

function setRecordUrl(id, tab, mode = "push") {
  if (mode === "none") return;
  const url = new URL(window.location.href);
  if (id) { url.searchParams.set("npc", id); url.searchParams.set("tab", tab); }
  else { url.searchParams.delete("npc"); url.searchParams.delete("tab"); }
  history[mode === "replace" ? "replaceState" : "pushState"]({ npc: id || null, tab: id ? tab : null }, "", url);
}

function toggleBackgroundInert(enabled) {
  document.querySelectorAll("body > header, body > nav, body > main").forEach(element => { element.inert = enabled; });
}

async function openNpc(npcId, { tab = "overview", historyMode = "push" } = {}) {
  const id = Number(npcId);
  if (!Number.isInteger(id)) return;
  if (state.selectedNpc === id && !$("#modal").classList.contains("hidden")) return activateNpcTab(tab, { historyMode });
  if (state.npcRecord) {
    abortScope(`npc-${state.npcRecord.id}-core`);
    abortScope(`npc-${state.npcRecord.id}-${state.npcRecord.activeTab}`);
  }
  if (!state.selectedNpc) state.lastFocusedElement = document.activeElement;
  state.selectedNpc = id;
  const activeTab = NPC_TABS.some(item => item.key === tab) ? tab : "overview";
  const snapshotResolver = new SnapshotResolver({
    fallbackLoaders: { overview: () => legacyNpcOverview(id), decision: () => legacyNpcDecision(id) },
    fallbackIntervals: { overview: 10000, decision: 6000 },
  });
  state.npcRecord = { id, activeTab, cache: new Map(), lastWorldRefresh: 0, snapshotResolver };
  $("#modal").classList.remove("hidden");
  document.body.classList.add("record-open");
  toggleBackgroundInert(true);
  renderRecordTabs(state.npcRecord.activeTab);
  $("#npc-record-header").innerHTML = `<div class="record-identity"><span class="avatar">${id}</span><div><h2 id="npc-name">正在载入 NPC ${id}</h2><p>观察档案</p></div></div>`;
  $("#npc-detail").innerHTML = '<p class="empty memory-empty">正在打开观察档案…</p>';
  setRecordUrl(id, state.npcRecord.activeTab, historyMode);
  $(".npc-record").focus({ preventScroll: true });
  try {
    if (activeTab !== "overview") {
      const npc = await api(`/api/npcs/${id}`, {}, `npc-${id}-core`);
      if (state.selectedNpc !== id) return;
      state.npcRecord.npc = npc;
      renderRecordHeader(npc);
    }
    await activateNpcTab(state.npcRecord.activeTab, { historyMode: "replace", force: true });
  } catch (error) {
    if (error.name !== "AbortError") {
      $("#npc-detail").innerHTML = `<div class="module-error"><strong>无法打开人物档案</strong><p>${escapeHtml(error.message)}</p><button type="button" data-record-retry="true">重试</button></div>`;
    }
  }
}

async function activateNpcTab(tabKey, { historyMode = "push", force = false } = {}) {
  if (!state.npcRecord || !NPC_TABS.some(tab => tab.key === tabKey)) return;
  const record = state.npcRecord;
  const previous = record.activeTab;
  if (previous && previous !== tabKey) abortScope(`npc-${record.id}-${previous}`);
  record.activeTab = tabKey;
  renderRecordTabs(tabKey);
  setRecordUrl(record.id, tabKey, historyMode);
  const cached = record.cache.get(tabKey);
  const tab = NPC_TABS.find(item => item.key === tabKey);
  if (cached) {
    setHtml($("#npc-detail"), npcTabRenderers[tabKey](cached.data), "npc-detail");
    $("#npc-tab-status").textContent = `已显示上次成功数据 · ${new Date(cached.updatedAt).toLocaleTimeString("zh-CN", { hour12: false })}`;
  } else {
    $("#npc-detail").innerHTML = '<p class="empty memory-empty">正在载入当前标签…</p>';
  }
  if (!force && cached && Date.now() - cached.updatedAt < tab.interval) return;
  return loadNpcTab(tabKey, force);
}

function renderNpcTabContent(tabKey, data) {
  const target = $("#npc-detail");
  const scrollTop = target.scrollTop;
  const focusedKey = target.contains(document.activeElement) ? document.activeElement?.dataset?.focusKey : null;
  const changed = setHtml(target, npcTabRenderers[tabKey](data), "npc-detail");
  if (!changed) return;
  target.scrollTop = scrollTop;
  if (focusedKey) target.querySelector(`[data-focus-key="${CSS.escape(focusedKey)}"]`)?.focus({ preventScroll: true });
}

async function loadNpcTab(tabKey, force = false) {
  const record = state.npcRecord;
  if (!record || record.activeTab !== tabKey) return;
  const runKey = `npc-tab-${record.id}-${tabKey}`;
  return runGroup(runKey, async () => {
    $("#npc-tab-status").dataset.status = "loading";
    $("#npc-tab-status").textContent = "正在同步当前标签…";
    let payload;
    try { payload = await npcTabLoaders[tabKey](record.id, force); }
    catch (error) {
      if (error.name === "AbortError") return;
      payload = { results: { section: { status: "rejected", reason: error } }, data: null };
    }
    if (!state.npcRecord || state.npcRecord.id !== record.id || state.npcRecord.activeTab !== tabKey) return;
    if (payload.snapshotEntry) {
      const entry = payload.snapshotEntry;
      const previous = record.cache.get(tabKey);
      const data = entry.data || previous?.data;
      const requiredMissing = npcTabRequiredData[tabKey].some(name => data?.[name] == null);
      if (data && !requiredMissing) {
        const updatedAt = entry.updatedAt || previous?.updatedAt || Date.now();
        record.cache.set(tabKey, { data, updatedAt, source: entry.source, status: entry.status });
        if (tabKey === "overview" && data.npc) {
          record.npc = data.npc;
          renderRecordHeader(data.npc);
        }
        renderNpcTabContent(tabKey, data);
      }
      $("#npc-tab-status").dataset.status = entry.status;
      const time = entry.updatedAt ? new Date(entry.updatedAt).toLocaleTimeString("zh-CN", { hour12: false }) : "尚未成功";
      $("#npc-tab-status").textContent = entry.status === "ok"
        ? entry.source === "aggregate" ? "数据同步正常" : "兼容读取正常"
        : entry.status === "stale"
          ? `暂时无法更新 · 显示 ${time} 的数据`
          : "暂时无法获取数据";
      if (requiredMissing && !record.cache.has(tabKey)) {
        $("#npc-detail").innerHTML = `<div class="module-error"><strong>当前标签暂不可用</strong><p>${escapeHtml(entry.error)}。其他标签不受影响，可重试此标签。</p><button type="button" data-record-retry="true">重试</button></div>`;
      }
      return;
    }
    const aborted = Object.values(payload.results).some(result => result.status === "rejected" && result.reason?.name === "AbortError");
    if (aborted) return;
    const errors = Object.entries(payload.results).filter(([, result]) => result.status === "rejected" && result.reason?.name !== "AbortError");
    const previous = record.cache.get(tabKey);
    const mergedData = { ...payload.data };
    if (previous) errors.forEach(([name]) => {
      if (Object.prototype.hasOwnProperty.call(previous.data, name)) mergedData[name] = previous.data[name];
    });
    const requiredMissing = npcTabRequiredData[tabKey].some(name => mergedData[name] == null);
    if (!requiredMissing) {
      const updatedAt = Date.now();
      record.cache.set(tabKey, { data: mergedData, updatedAt });
      renderNpcTabContent(tabKey, mergedData);
    }
    $("#npc-tab-status").dataset.status = errors.length ? "error" : "ok";
    $("#npc-tab-status").textContent = errors.length ? `部分数据未更新，已保留上次成功结果 · ${errors.map(([name]) => name).join("、")}` : formatSyncTime();
    if (requiredMissing && !record.cache.has(tabKey)) {
      $("#npc-detail").innerHTML = `<div class="module-error"><strong>当前标签暂不可用</strong><p>其他标签不受影响，可重试此标签。</p><button type="button" data-record-retry="true">重试</button></div>`;
    }
  });
}

function closeNpcRecord({ historyMode = "push" } = {}) {
  if (!state.selectedNpc) return;
  abortScope(`npc-${state.npcRecord?.id}-core`);
  abortScope(`npc-${state.npcRecord?.id}-${state.npcRecord?.activeTab}`);
  $("#modal").classList.add("hidden");
  document.body.classList.remove("record-open");
  toggleBackgroundInert(false);
  state.selectedNpc = null;
  state.npcRecord = null;
  setRecordUrl(null, null, historyMode);
  state.lastFocusedElement?.focus?.({ preventScroll: true });
  state.lastFocusedElement = null;
  refreshFast(true);
  refreshPulse(true);
}

async function refreshNpcRecord() {
  if (document.hidden || !state.npcRecord) return;
  const record = state.npcRecord;
  const now = Date.now();
  if (now - record.lastWorldRefresh >= 6000) {
    record.lastWorldRefresh = now;
    try {
      const world = await api("/api/world", {}, `npc-${record.id}-world`);
      if (state.npcRecord?.id === record.id) {
        renderWorld(world);
        const summary = state.world?.npcs?.find(item => Number(item.id) === record.id);
        if (summary && record.npc) {
          record.npc = { ...record.npc, ...summary, states: { ...record.npc.states, ...summary.states } };
          renderRecordHeader(record.npc);
        }
      }
    } catch (_) { /* Header keeps the last successful world state. */ }
  }
  const tab = NPC_TABS.find(item => item.key === record.activeTab);
  const cached = record.cache.get(record.activeTab);
  if (!cached || now - cached.updatedAt >= tab.interval) await loadNpcTab(record.activeTab, true);
}

async function refresh(force = true) {
  if (state.selectedNpc) return refreshNpcRecord();
  await Promise.allSettled([refreshFast(force), refreshPulse(force), refreshVisibleLowFrequency(force)]);
}

document.querySelectorAll("[data-trend-group]").forEach(disclosure => disclosure.addEventListener("toggle", () => {
  if (disclosure.open) loadTrendGroup(disclosure.dataset.trendGroup, true);
  else abortScope(`trend-${disclosure.dataset.trendGroup}`);
}));

document.querySelectorAll("[data-audit-group]").forEach(disclosure => disclosure.addEventListener("toggle", () => {
  if (disclosure.open) loadAuditGroup(disclosure.dataset.auditGroup, true);
  else abortScope(`audit-${disclosure.dataset.auditGroup}`);
}));

$("#npc-overview").addEventListener("click", event => { const card = event.target.closest("[data-npc-id]"); if (card) openNpc(card.dataset.npcId).catch(error => showToast(error.message)); });
$("#event-filter").addEventListener("change", event => { state.eventFilter = event.target.value; renderEvents(state.events, state.eventNarratives); });
$("#npc-tabs").addEventListener("click", event => { const tab = event.target.closest("[data-npc-tab]"); if (tab) activateNpcTab(tab.dataset.npcTab); });
$("#npc-tabs").addEventListener("keydown", event => {
  const current = event.target.closest("[data-npc-tab]");
  if (!current || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = [...$("#npc-tabs").querySelectorAll("[data-npc-tab]")];
  let index = tabs.indexOf(current);
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = tabs.length - 1;
  else index = (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[index].focus();
  activateNpcTab(tabs[index].dataset.npcTab);
});
$("#modal").addEventListener("click", event => {
  const step = event.target.closest("[data-npc-step]");
  if (step && state.npcRecord) {
    const ids = (state.world?.npcs || []).map(item => Number(item.id)).sort((a, b) => a - b);
    const current = ids.indexOf(state.npcRecord.id);
    const offset = step.dataset.npcStep === "next" ? 1 : -1;
    const next = ids[(current + offset + ids.length) % ids.length];
    openNpc(next, { tab: state.npcRecord.activeTab });
  }
  if (event.target.closest("[data-record-retry]")) loadNpcTab(state.npcRecord?.activeTab, true);
});
$("#modal").addEventListener("keydown", event => {
  if (event.key !== "Tab") return;
  const focusable = [...$("#modal").querySelectorAll('button:not([disabled]), [href], select, [tabindex]:not([tabindex="-1"])')].filter(item => !item.closest("[hidden]"));
  if (!focusable.length) return;
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

window.addEventListener("popstate", () => {
  const params = new URLSearchParams(window.location.search);
  const npc = Number(params.get("npc"));
  const tab = params.get("tab") || "overview";
  if (npc) openNpc(npc, { tab, historyMode: "none" });
  else closeNpcRecord({ historyMode: "none" });
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    [...requestScopes.keys()].filter(scope => scope.startsWith("dashboard") || scope.startsWith("trend-") || scope.startsWith("audit-") || scope.startsWith("npc-")).forEach(abortScope);
  } else refresh(true);
});

const navLinks = [...document.querySelectorAll(".section-nav a")];
const navObserver = new IntersectionObserver(entries => {
  const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
  if (!visible) return;
  navLinks.forEach(link => link.toggleAttribute("aria-current", link.hash === `#${visible.target.id}`));
}, { rootMargin: "-20% 0px -65%", threshold: [0, 0.2, 0.6] });
document.querySelectorAll("main > section[id]").forEach(section => navObserver.observe(section));

refresh(true).then(() => {
  const params = new URLSearchParams(window.location.search);
  const npc = Number(params.get("npc"));
  if (npc) openNpc(npc, { tab: params.get("tab") || "overview", historyMode: "none" });
});
setInterval(() => refreshFast(), 2000);
setInterval(() => refreshVisibleLowFrequency(), 30000);
setInterval(() => refreshNpcRecord(), 2000);
