import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
vm.runInThisContext(fs.readFileSync(path.join(root, "static", "js", "dashboard-snapshots.js"), "utf8"));

const { HOME_GROUPS, SnapshotResolver, projectStableRequests, validateEnvelope } = globalThis.MiniWorldDashboardSnapshots;

function envelope(overrides = {}) {
  const snapshotId = "120:7";
  const modules = Object.fromEntries(HOME_GROUPS.map(name => [name, {
    status: "ok",
    version: snapshotId,
    snapshot_id: snapshotId,
    world_minute: 120,
    data: { name },
  }]));
  return {
    schema_version: "1.0",
    snapshot_id: snapshotId,
    captured_at: "2026-08-20T08:00:00+00:00",
    world_minute: 120,
    modules,
    ...overrides,
  };
}

test("normal aggregate uses every successful module without legacy GETs", async () => {
  let fallbackCalls = 0;
  const loaders = Object.fromEntries(HOME_GROUPS.map(name => [name, async () => { fallbackCalls += 1; return { legacy: name }; }]));
  const resolver = new SnapshotResolver({ fallbackLoaders: loaders });
  const result = await resolver.resolve(envelope(), HOME_GROUPS);
  assert.equal(fallbackCalls, 0);
  assert.deepEqual(Object.values(result).map(entry => entry.source), ["aggregate", "aggregate", "aggregate", "aggregate"]);
  assert.deepEqual(result.world.data, { name: "world" });
});

test("invalid envelope and unavailable endpoint fall back module by module", async () => {
  const calls = [];
  const loaders = Object.fromEntries(HOME_GROUPS.map(name => [name, async () => { calls.push(name); return { legacy: name }; }]));
  const resolver = new SnapshotResolver({ fallbackLoaders: loaders });
  const invalid = envelope({ schema_version: "9.9" });
  const invalidResult = await resolver.resolve(invalid, HOME_GROUPS);
  assert.deepEqual(calls.sort(), [...HOME_GROUPS].sort());
  assert.ok(Object.values(invalidResult).every(entry => entry.source === "fallback"));

  calls.length = 0;
  const second = new SnapshotResolver({ fallbackLoaders: loaders });
  await second.resolve(null, HOME_GROUPS, { endpointError: new Error("503 unavailable") });
  assert.deepEqual(calls.sort(), [...HOME_GROUPS].sort());
});

test("one aggregate module error falls back only that module", async () => {
  const payload = envelope();
  payload.modules.pulse = {
    status: "error",
    version: payload.snapshot_id,
    snapshot_id: payload.snapshot_id,
    world_minute: payload.world_minute,
    error: { code: "pulse_snapshot_unavailable", message: "pulse unavailable", retryable: true },
  };
  const calls = [];
  const loaders = Object.fromEntries(HOME_GROUPS.map(name => [name, async () => { calls.push(name); return { legacy: name }; }]));
  const result = await new SnapshotResolver({ fallbackLoaders: loaders }).resolve(payload, HOME_GROUPS);
  assert.deepEqual(calls, ["pulse"]);
  assert.equal(result.pulse.source, "fallback");
  assert.equal(result.world.source, "aggregate");
  assert.equal(result.npcs.source, "aggregate");
});

test("valid fallback cache stays healthy until a real refresh fails", async () => {
  let now = 0;
  let calls = 0;
  let fail = false;
  const resolver = new SnapshotResolver({
    now: () => now,
    fallbackIntervals: { overview: 5000 },
    fallbackLoaders: { overview: async () => { calls += 1; if (fail) throw new Error("legacy down"); return { npc: { id: 1 } }; } },
  });
  const first = await resolver.resolve(null, ["overview"], { endpointError: new Error("snapshot down"), force: true });
  assert.equal(first.overview.status, "ok");
  assert.equal(calls, 1);

  fail = true;
  now = 1000;
  const throttled = await resolver.resolve(null, ["overview"], { endpointError: new Error("snapshot down") });
  assert.equal(throttled.overview.status, "ok");
  assert.equal(calls, 1);
  assert.deepEqual(throttled.overview.data, { npc: { id: 1 } });

  now = 6000;
  const failed = await resolver.resolve(null, ["overview"], { endpointError: new Error("snapshot down") });
  assert.equal(failed.overview.status, "stale");
  assert.equal(failed.overview.error, "legacy down");
  assert.equal(calls, 2);
});

test("envelope boundary validation rejects mismatched module snapshots", () => {
  const payload = envelope();
  payload.modules.world.snapshot_id = "120:6";
  assert.equal(validateEnvelope(payload, HOME_GROUPS).valid, false);
});

test("repeatable scheduler count stays within the phase-three GET budgets", () => {
  assert.equal(projectStableRequests({ mode: "home" }), 30);
  assert.equal(projectStableRequests({ mode: "npc", tab: "overview" }), 16);
  assert.equal(projectStableRequests({ mode: "npc", tab: "decision" }), 20);
  assert.ok(projectStableRequests({ mode: "home" }) <= 40);
  assert.ok(projectStableRequests({ mode: "npc", tab: "decision" }) <= 45);
});
