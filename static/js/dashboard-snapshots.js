(function dashboardSnapshotsModule(global) {
  "use strict";

  const SCHEMA_VERSION = "1.0";
  const HOME_GROUPS = Object.freeze(["runtime", "world", "npcs", "pulse"]);

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function validateEnvelope(payload, requestedModules) {
    if (!isObject(payload)) return { valid: false, error: "快照信封不是对象" };
    if (payload.schema_version !== SCHEMA_VERSION) return { valid: false, error: "快照版本不受支持" };
    if (typeof payload.snapshot_id !== "string" || !payload.snapshot_id) return { valid: false, error: "快照标识缺失" };
    if (typeof payload.captured_at !== "string" || !Number.isFinite(Date.parse(payload.captured_at))) return { valid: false, error: "快照时间无效" };
    if (!Number.isFinite(payload.world_minute)) return { valid: false, error: "世界分钟无效" };
    if (!isObject(payload.modules)) return { valid: false, error: "快照模块缺失" };

    for (const name of requestedModules) {
      const module = payload.modules[name];
      if (!isObject(module)) return { valid: false, error: `模块 ${name} 缺失` };
      if (!(["ok", "error"].includes(module.status))) return { valid: false, error: `模块 ${name} 状态无效` };
      if (module.snapshot_id !== payload.snapshot_id || module.world_minute !== payload.world_minute) {
        return { valid: false, error: `模块 ${name} 不在统一快照边界` };
      }
      if (module.status === "ok" && !Object.prototype.hasOwnProperty.call(module, "data")) {
        return { valid: false, error: `模块 ${name} 数据缺失` };
      }
      if (module.status === "error" && !isObject(module.error)) {
        return { valid: false, error: `模块 ${name} 错误信息缺失` };
      }
    }
    return { valid: true, capturedAt: Date.parse(payload.captured_at) };
  }

  function errorMessage(error, fallback) {
    if (error?.name === "AbortError") return "请求已取消";
    return String(error?.message || fallback || "模块暂不可用");
  }

  class SnapshotResolver {
    constructor({ fallbackLoaders, fallbackIntervals = {}, now = () => Date.now() }) {
      this.fallbackLoaders = fallbackLoaders;
      this.fallbackIntervals = fallbackIntervals;
      this.now = now;
      this.cache = new Map();
    }

    async resolve(payload, requestedModules, { endpointError = null, force = false } = {}) {
      if (endpointError?.name === "AbortError") throw endpointError;
      const validation = validateEnvelope(payload, requestedModules);
      const envelopeError = validation.valid ? null : (endpointError || new Error(validation.error));
      const entries = await Promise.all(requestedModules.map(async name => {
        const module = validation.valid ? payload.modules[name] : null;
        if (module?.status === "ok") {
          const entry = {
            name,
            status: "ok",
            source: "aggregate",
            data: module.data,
            updatedAt: validation.capturedAt,
            snapshotId: payload.snapshot_id,
            error: null,
          };
          this.cache.set(name, entry);
          return entry;
        }

        const reason = module?.error?.message || errorMessage(envelopeError, `${name} 聚合模块暂不可用`);
        const previous = this.cache.get(name);
        const interval = Number(this.fallbackIntervals[name] || 0);
        const now = this.now();
        if (!force && previous?.source === "fallback" && now - previous.updatedAt < interval) {
          return previous;
        }

        try {
          const data = await this.fallbackLoaders[name]();
          const entry = {
            name,
            status: "ok",
            source: "fallback",
            data,
            updatedAt: now,
            snapshotId: null,
            error: reason,
          };
          this.cache.set(name, entry);
          return entry;
        } catch (fallbackError) {
          if (fallbackError?.name === "AbortError") throw fallbackError;
          const detail = errorMessage(fallbackError, reason);
          if (previous?.data !== undefined) {
            const stale = { ...previous, status: "stale", error: detail };
            this.cache.set(name, stale);
            return stale;
          }
          return {
            name,
            status: "error",
            source: "fallback",
            data: undefined,
            updatedAt: null,
            snapshotId: null,
            error: detail,
          };
        }
      }));
      return Object.fromEntries(entries.map(entry => [entry.name, entry]));
    }
  }

  function projectStableRequests({ durationMs = 60000, mode = "home", tab = "overview" } = {}) {
    const ticks = interval => Math.floor(durationMs / interval);
    if (mode === "home") return ticks(2000);
    const tabIntervals = { overview: 10000, decision: 6000, life: 30000, social: 10000, memory: 30000 };
    return ticks(6000) + ticks(tabIntervals[tab] || 30000);
  }

  global.MiniWorldDashboardSnapshots = Object.freeze({
    HOME_GROUPS,
    SnapshotResolver,
    projectStableRequests,
    validateEnvelope,
  });
})(globalThis);
