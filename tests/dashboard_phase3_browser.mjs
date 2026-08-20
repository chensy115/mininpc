import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire("C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/__miniworld_phase3__.js");
const { chromium } = require("playwright");

const baseUrl = "http://127.0.0.1:8765/";
const artifacts = process.env.PHASE3_ARTIFACT_DIR || "D:/mininpc/artifacts/dashboard_phase3_browser";
fs.mkdirSync(artifacts, { recursive: true });

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
  args: [
    "--disable-gpu",
    "--disable-gpu-sandbox",
    "--disable-features=Vulkan,UseSkiaRenderer,CanvasOopRasterization",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
  ],
});

const report = {
  browser: "Chrome headless via local Playwright",
  baseUrl,
  viewports: {},
  interactions: {},
  fallbackScenarios: {},
  requestMeasurements: {},
  consoleErrors: [],
  resourceErrors: [],
};

async function newPage(viewport) {
  const context = await browser.newContext({ viewport, locale: "zh-CN" });
  const page = await context.newPage();
  page.on("console", message => {
    if (message.type() !== "error") return;
    if (message.text().startsWith("Failed to load resource:")) report.resourceErrors.push(message.text());
    else report.consoleErrors.push(message.text());
  });
  page.on("pageerror", error => report.consoleErrors.push(error.message));
  return { context, page };
}

async function waitForDashboard(page) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#runtime-freshness")?.dataset.status === "ok");
  await page.waitForFunction(() => document.querySelectorAll("#npc-overview [data-npc-id]").length === 5);
}

async function layoutFacts(page) {
  return page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    dangerOpen: document.querySelector(".danger-menu").open,
    runtimeTitle: document.querySelector("#runtime-overview h3")?.textContent,
    runtimeMode: document.querySelector("#runtime-overview .runtime-state")?.textContent,
    runtimeConfigText: document.querySelector("#runtime-overview [data-runtime-config-open]")?.textContent,
    runtimeEmergencyCount: document.querySelectorAll("#runtime-overview [data-runtime-action='emergency-stop']").length,
    runtimeDiagnosticsOpen: document.querySelector("#runtime-overview .runtime-diagnostics")?.open,
    pausedText: document.querySelector("#status-text")?.textContent,
  }));
}

for (const viewport of [
  { width: 360, height: 800, name: "360x800" },
  { width: 596, height: 760, name: "596x760" },
  { width: 768, height: 1024, name: "768x1024" },
  { width: 1440, height: 900, name: "1440x900" },
]) {
  const { context, page } = await newPage(viewport);
  const methods = [];
  page.on("request", request => { if (request.url().startsWith("http://127.0.0.1:8765/api/")) methods.push(request.method()); });
  await waitForDashboard(page);
  const facts = await layoutFacts(page);
  assert.ok(facts.scrollWidth <= facts.clientWidth, `${viewport.name} has document horizontal overflow`);
  assert.ok(facts.bodyScrollWidth <= facts.clientWidth, `${viewport.name} has body horizontal overflow`);
  assert.equal(facts.dangerOpen, false);
  assert.equal(facts.runtimeTitle, "在线自治");
  assert.match(facts.runtimeMode, /紧急停止/);
  assert.equal(facts.runtimeConfigText, "配置模型");
  assert.equal(facts.runtimeEmergencyCount, 0);
  assert.equal(facts.runtimeDiagnosticsOpen, false);
  assert.match(facts.pausedText, /暂停/);
  assert.ok(!methods.includes("POST"), `${viewport.name} unexpectedly triggered a write request`);
  await page.locator(".danger-menu > summary").click();
  const dangerVisibility = await page.evaluate(() => {
    const button = document.querySelector("#reset-button");
    const rect = button.getBoundingClientRect();
    const point = document.elementFromPoint(rect.left + rect.width / 2, rect.bottom - 2);
    return {
      bottomEdgeOwner: point?.id || point?.className || point?.tagName,
      fullyInViewport: rect.left >= 0 && rect.right <= window.innerWidth && rect.top >= 0 && rect.bottom <= window.innerHeight,
      unobscured: point === button || button.contains(point),
    };
  });
  assert.ok(dangerVisibility.fullyInViewport, `${viewport.name} danger action extends outside the viewport`);
  assert.ok(dangerVisibility.unobscured, `${viewport.name} danger action is obscured by ${dangerVisibility.bottomEdgeOwner}`);
  await page.locator(".danger-menu > summary").click();
  await page.locator("#runtime-overview .runtime-diagnostics > summary").click();
  assert.equal(await page.locator("#runtime-overview .runtime-diagnostics").evaluate(element => element.open), true);
  await page.waitForTimeout(2200);
  assert.equal(await page.locator("#runtime-overview .runtime-diagnostics").evaluate(element => element.open), true);
  await page.locator("#runtime-overview .runtime-diagnostics > summary").click();
  await page.locator("#runtime-overview [data-runtime-config-open]").click();
  await page.locator("#runtime-config-modal:not(.hidden)").waitFor();
  const configFacts = await page.evaluate(() => {
    const panel = document.querySelector(".runtime-config").getBoundingClientRect();
    const key = document.querySelector("#runtime-api-key");
    return {
      keyType: key.type,
      keyValue: key.value,
      keyFocused: document.activeElement === key,
      fullyInViewport: panel.left >= 0 && panel.right <= window.innerWidth && panel.top >= 0 && panel.bottom <= window.innerHeight,
      backgroundInert: document.querySelector("main").inert,
    };
  });
  assert.deepEqual(configFacts, { keyType: "password", keyValue: "", keyFocused: true, fullyInViewport: true, backgroundInert: true });
  await page.screenshot({ path: path.join(artifacts, `runtime-config-${viewport.name}.png`) });
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("#runtime-config-modal").evaluate(element => element.classList.contains("hidden")), true);
  const screenshot = path.join(artifacts, `dashboard-${viewport.name}.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  report.viewports[viewport.name] = { ...facts, screenshot };
  await context.close();
}

if (process.env.PHASE3_LAYOUT_ONLY === "1") {
  assert.equal(report.consoleErrors.length, 0, `browser console errors: ${report.consoleErrors.join(" | ")}`);
  await browser.close();
  console.log(JSON.stringify({ viewports: report.viewports, consoleErrors: report.consoleErrors }, null, 2));
  process.exit(0);
}

{
  const { context, page } = await newPage({ width: 1440, height: 900 });
  const requests = [];
  page.on("request", request => {
    if (request.url().startsWith("http://127.0.0.1:8765/api/")) requests.push({ method: request.method(), url: new URL(request.url()).pathname + new URL(request.url()).search, at: Date.now() });
  });
  await waitForDashboard(page);

  const npcCard = page.locator("#npc-overview [data-npc-id]").first();
  await npcCard.focus();
  await page.keyboard.press("Enter");
  await page.locator("#modal:not(.hidden)").waitFor();
  await page.waitForFunction(() => document.querySelector("#npc-tab-status")?.textContent.includes("聚合快照"));
  assert.equal(new URL(page.url()).searchParams.get("tab"), "overview");
  assert.equal(await page.locator("#npc-tabs [aria-selected='true']").getAttribute("data-npc-tab"), "overview");

  const tabsVisited = ["overview"];
  for (const tab of ["decision", "life", "social", "memory"]) {
    const active = page.locator("#npc-tabs [aria-selected='true']");
    await active.focus();
    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(expected => document.querySelector("#npc-tabs [aria-selected='true']")?.dataset.npcTab === expected, tab);
    await page.waitForFunction(() => !document.querySelector("#npc-tab-status")?.textContent.includes("正在同步"));
    tabsVisited.push(tab);
  }

  await page.locator("#npc-tabs [data-npc-tab='decision']").click();
  await page.waitForFunction(() => document.querySelector("#npc-tab-status")?.textContent.includes("聚合快照"));
  const focusTarget = page.locator("#npc-detail [data-focus-key='npc-control-toggle']");
  if (await focusTarget.count()) await focusTarget.focus();
  const beforeRefresh = await page.evaluate(() => {
    const target = document.querySelector("#npc-detail");
    target.scrollTop = Math.min(180, Math.max(0, target.scrollHeight - target.clientHeight));
    return { scrollTop: target.scrollTop, focusKey: document.activeElement?.dataset?.focusKey || null };
  });
  await page.waitForTimeout(6500);
  const afterRefresh = await page.evaluate(() => ({
    scrollTop: document.querySelector("#npc-detail").scrollTop,
    focusKey: document.activeElement?.dataset?.focusKey || null,
  }));
  assert.equal(afterRefresh.focusKey, beforeRefresh.focusKey);
  assert.ok(Math.abs(afterRefresh.scrollTop - beforeRefresh.scrollTop) <= 2);

  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelector("#modal").classList.contains("hidden"));
  assert.equal(await page.evaluate(() => document.activeElement?.dataset?.focusKey), "overview-npc-1");

  await npcCard.focus();
  await page.keyboard.press("Enter");
  await page.locator("#modal:not(.hidden)").waitFor();
  await page.goBack();
  await page.waitForFunction(() => document.querySelector("#modal").classList.contains("hidden"));

  const disclosure = page.locator("[data-trend-group='product']");
  await disclosure.locator("summary").click();
  await disclosure.locator("summary").focus();
  const beforeHomeRefresh = await page.evaluate(() => ({ scrollY: window.scrollY, activeTag: document.activeElement?.tagName }));
  await page.waitForTimeout(2300);
  const afterHomeRefresh = await page.evaluate(() => ({
    scrollY: window.scrollY,
    activeTag: document.activeElement?.tagName,
    disclosureOpen: document.querySelector("[data-trend-group='product']").open,
  }));
  assert.equal(afterHomeRefresh.disclosureOpen, true);
  assert.equal(afterHomeRefresh.activeTag, beforeHomeRefresh.activeTag);
  assert.ok(Math.abs(afterHomeRefresh.scrollY - beforeHomeRefresh.scrollY) <= 2);
  assert.ok(!requests.some(item => item.method === "POST"));
  report.interactions = { tabsVisited, beforeRefresh, afterRefresh, afterHomeRefresh, writeRequests: 0 };
  await context.close();
}

{
  const { context, page } = await newPage({ width: 1440, height: 900 });
  let releaseOverview;
  let releaseDecision;
  let markOverviewStarted;
  let markDecisionStarted;
  const overviewGate = new Promise(resolve => { releaseOverview = resolve; });
  const decisionGate = new Promise(resolve => { releaseDecision = resolve; });
  const overviewStarted = new Promise(resolve => { markOverviewStarted = resolve; });
  const decisionStarted = new Promise(resolve => { markDecisionStarted = resolve; });
  await page.route("**/api/dashboard/npcs/1/snapshot?sections=overview", async route => {
    markOverviewStarted();
    await overviewGate;
    try { await route.continue(); } catch (_) { /* The application canceled this request. */ }
  });
  await page.route("**/api/dashboard/npcs/1/snapshot?sections=decision", async route => {
    markDecisionStarted();
    await decisionGate;
    try { await route.continue(); } catch (_) { /* The application canceled this request. */ }
  });
  await waitForDashboard(page);
  await page.locator("#npc-overview [data-npc-id]").first().click();
  await overviewStarted;
  await page.locator("#npc-tabs [data-npc-tab='decision']").click();
  await decisionStarted;
  const afterSwitch = await page.evaluate(() => window.__MINIWORLD_DIAGNOSTICS__.aborted);
  assert.ok(afterSwitch >= 1);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.querySelector("#modal").classList.contains("hidden"));
  const afterClose = await page.evaluate(() => window.__MINIWORLD_DIAGNOSTICS__.aborted);
  assert.ok(afterClose >= 2);
  releaseOverview();
  releaseDecision();
  report.interactions.cancellation = { afterSwitch, afterClose };
  await context.close();
}

async function fallbackPage(name, routeHandler) {
  const { context, page } = await newPage({ width: 1440, height: 900 });
  const gets = [];
  page.on("request", request => {
    if (request.method() === "GET" && request.url().startsWith("http://127.0.0.1:8765/api/")) gets.push(new URL(request.url()).pathname + new URL(request.url()).search);
  });
  await page.route("**/api/dashboard/snapshot?groups=runtime,world,npcs,pulse", routeHandler);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#overview-freshness")?.textContent.includes("兼容读取正常"));
  report.fallbackScenarios[name] = { gets, moduleStatuses: await page.evaluate(() => Object.fromEntries([
    ["runtime", document.querySelector("#runtime-freshness")?.dataset.status],
    ["world", document.querySelector("#world-freshness")?.dataset.status],
    ["npcs", document.querySelector("#npc-freshness")?.dataset.status],
    ["pulse", document.querySelector("#pulse-freshness")?.dataset.status],
  ])) };
  await context.close();
  return gets;
}

const invalidGets = await fallbackPage("invalidEnvelope", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ schema_version: "invalid" }) }));
assert.ok(invalidGets.includes("/api/runtime") && invalidGets.includes("/api/world") && invalidGets.includes("/api/npcs"));

const unavailableGets = await fallbackPage("endpointUnavailable", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "unavailable" }) }));
assert.ok(unavailableGets.includes("/api/runtime") && unavailableGets.includes("/api/events?limit=40"));

{
  const { context, page } = await newPage({ width: 1440, height: 900 });
  const gets = [];
  page.on("request", request => { if (request.method() === "GET" && request.url().includes("/api/")) gets.push(new URL(request.url()).pathname + new URL(request.url()).search); });
  await page.route("**/api/dashboard/snapshot?groups=runtime,world,npcs,pulse", async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.modules.pulse = {
      status: "error", version: body.snapshot_id, snapshot_id: body.snapshot_id, world_minute: body.world_minute,
      error: { code: "pulse_snapshot_unavailable", message: "pulse unavailable", retryable: true },
    };
    await route.fulfill({ response, json: body });
  });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#pulse-freshness")?.textContent.includes("兼容读取正常"));
  assert.ok(gets.includes("/api/events?limit=40"));
  assert.ok(!gets.includes("/api/runtime") && !gets.includes("/api/world") && !gets.includes("/api/npcs"));
  report.fallbackScenarios.singleModuleError = { gets };
  await context.close();
}

{
  const { context, page } = await newPage({ width: 1440, height: 900 });
  await waitForDashboard(page);
  const runtimeText = await page.locator("#runtime-overview").innerText();
  await page.route("**/api/dashboard/snapshot?groups=runtime,world,npcs,pulse", route => route.fulfill({ status: 503, contentType: "application/json", body: "{}" }));
  await page.route("**/api/runtime", route => route.fulfill({ status: 503, contentType: "application/json", body: "{}" }));
  await page.evaluate(() => window.refresh(true));
  await page.waitForFunction(() => document.querySelector("#runtime-freshness")?.dataset.status === "stale");
  assert.equal(await page.locator("#runtime-overview").innerText(), runtimeText);
  report.fallbackScenarios.staleRetention = { status: await page.locator("#runtime-freshness").getAttribute("data-status"), retained: true };
  await context.close();
}

{
  const { context, page } = await newPage({ width: 1440, height: 900 });
  const gets = [];
  page.on("request", request => { if (request.method() === "GET" && request.url().includes("/api/")) gets.push({ path: new URL(request.url()).pathname + new URL(request.url()).search, at: Date.now() }); });
  await waitForDashboard(page);
  await page.waitForTimeout(2100);
  let start = Date.now();
  let index = gets.length;
  await page.waitForTimeout(12000);
  const homeElapsed = Date.now() - start;
  const homeGets = gets.slice(index);
  const homePerMinute = homeGets.length / homeElapsed * 60000;
  assert.ok(homePerMinute <= 40, `home measured ${homePerMinute.toFixed(1)} GET/min`);

  await page.locator("#npc-overview [data-npc-id]").first().click();
  await page.locator("#npc-tabs [data-npc-tab='decision']").click();
  await page.waitForFunction(() => document.querySelector("#npc-tab-status")?.textContent.includes("聚合快照"));
  await page.waitForTimeout(2100);
  start = Date.now();
  index = gets.length;
  await page.waitForTimeout(13000);
  const npcElapsed = Date.now() - start;
  const npcGets = gets.slice(index);
  const npcPerMinute = npcGets.length / npcElapsed * 60000;
  assert.ok(npcPerMinute <= 45, `NPC measured ${npcPerMinute.toFixed(1)} GET/min`);

  const hiddenBefore = gets.length;
  await page.evaluate(() => { Object.defineProperty(document, "hidden", { configurable: true, get: () => true }); document.dispatchEvent(new Event("visibilitychange")); });
  await page.waitForTimeout(2300);
  const hiddenAfter = gets.length;
  assert.equal(hiddenAfter, hiddenBefore);
  await page.evaluate(() => { Object.defineProperty(document, "hidden", { configurable: true, get: () => false }); document.dispatchEvent(new Event("visibilitychange")); });
  await page.waitForTimeout(500);
  assert.ok(gets.length > hiddenAfter);

  const diagnostics = await page.evaluate(() => window.__MINIWORLD_DIAGNOSTICS__.snapshot());
  assert.ok(Object.values(diagnostics.maxActiveByPath).every(value => value <= 1));
  report.requestMeasurements = {
    home: { observedMs: homeElapsed, count: homeGets.length, perMinute: Number(homePerMinute.toFixed(2)), paths: homeGets.map(item => item.path) },
    npc: { observedMs: npcElapsed, count: npcGets.length, perMinute: Number(npcPerMinute.toFixed(2)), paths: npcGets.map(item => item.path) },
    hidden: { before: hiddenBefore, afterCycle: hiddenAfter, resumed: gets.length },
    diagnostics,
  };
  await context.close();
}

assert.equal(report.consoleErrors.length, 0, `browser console errors: ${report.consoleErrors.join(" | ")}`);
fs.writeFileSync(path.join(artifacts, "browser-report.json"), JSON.stringify(report, null, 2));
await browser.close();
console.log(JSON.stringify(report, null, 2));
