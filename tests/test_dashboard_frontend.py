from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SNAPSHOTS_JS = (ROOT / "static" / "js" / "dashboard-snapshots.js").read_text(encoding="utf-8")


class DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.sections: list[str] = []
        self.attributes: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.append(element_id)
            self.attributes[element_id] = values
            if tag == "section" and values.get("class", "").find("page-section") >= 0:
                self.sections.append(element_id)


def parsed_dashboard() -> DashboardParser:
    parser = DashboardParser()
    parser.feed(HTML)
    return parser


def test_homepage_information_order_matches_product_context() -> None:
    parser = parsed_dashboard()
    assert parser.sections == ["overview", "npcs", "pulse", "trends", "audit"]
    for required_id in (
        "runtime-overview",
        "runtime-freshness",
        "world-freshness",
        "locations",
        "npc-overview",
        "events",
        "product-overview",
        "economy-overview",
        "career-overview",
        "community-overview",
        "social-overview",
        "story-overview",
        "agent-overview",
        "conversation-overview",
        "cognition-overview",
    ):
        assert required_id in parser.ids


def test_npc_record_has_accessible_dialog_and_tab_structure() -> None:
    parser = parsed_dashboard()
    modal = parser.attributes["modal"]
    assert modal["role"] == "dialog"
    assert modal["aria-modal"] == "true"
    assert modal["aria-labelledby"] == "npc-name"
    assert parser.attributes["npc-tabs"]["role"] == "tablist"
    assert parser.attributes["npc-detail"]["role"] == "tabpanel"
    assert parser.attributes["npc-detail"]["tabindex"] == "0"
    assert 'data-close="true"' in HTML
    assert "toggleBackgroundInert(true)" in JS
    assert "state.lastFocusedElement?.focus" in JS
    assert "ArrowLeft" in JS and "ArrowRight" in JS
    assert 'setAttribute("aria-labelledby", `npc-tab-${activeKey}`)' in JS


def test_request_scheduler_has_required_safety_mechanisms() -> None:
    assert "const requestPool = new Map()" in JS
    assert "const controller = new AbortController()" in JS
    assert 'document.addEventListener("visibilitychange"' in JS
    assert "Promise.allSettled" in JS
    assert "setInterval(() => refreshFast(), 2000)" in JS
    assert "setInterval(() => refreshPulse(), 5000)" not in JS
    assert "setInterval(() => refreshVisibleLowFrequency(), 30000)" in JS
    assert "renderSignatures" in JS
    assert "requestPool.get(key)" in JS


def test_npc_tabs_lazy_load_every_existing_detail_domain() -> None:
    for tab in ("life", "social", "memory"):
        assert f"{tab}: async id =>" in JS
    assert 'overview: (id, force = false) =>' in JS
    assert 'decision: (id, force = false) =>' in JS
    for endpoint in (
        "/api/npcs/${id}",
        "/api/npcs/${id}/decision",
        "/api/npcs/${id}/memories?limit=20",
        "/api/npcs/${id}/goals",
        "/api/npcs/${id}/dialogues?limit=10",
        "/api/npcs/${id}/conversations?limit=10",
        "/api/npcs/${id}/goal-narratives",
        "/api/npcs/${id}/memory-summaries?limit=5",
        "/api/npcs/${id}/economy",
        "/api/npcs/${id}/career",
        "/api/npcs/${id}/budget",
        "/api/npcs/${id}/economic-reports?limit=4",
        "/api/npcs/${id}/rhythm",
        "/api/npcs/${id}/social-life",
        "/api/npcs/${id}/timeline?limit=30",
        "/api/npcs/${id}/agent-shadow",
        "/api/agents/${id}/cognition",
    ):
        assert endpoint in JS
    assert "/api/dashboard/snapshot?groups=runtime,world,npcs,pulse" in JS
    assert "/api/dashboard/npcs/${id}/snapshot?sections=${section}" in JS
    assert "overview: (id, force = false) => loadNpcSnapshotSection" in JS
    assert "decision: (id, force = false) => loadNpcSnapshotSection" in JS
    assert 'life: async id =>' in JS and 'social: async id =>' in JS and 'memory: async id =>' in JS


def test_phase_three_keeps_module_fallbacks_and_stale_data_contract() -> None:
    for group in ("runtime", "world", "npcs", "pulse"):
        assert f"{group}:" in JS
    for endpoint in (
        '"/api/runtime"',
        '"/api/world"',
        '"/api/npcs"',
        '"/api/agents/takeover"',
        '"/api/events?limit=40"',
        '"/api/narrative/status"',
        '"/api/narratives/events?limit=80"',
    ):
        assert endpoint in JS
    assert "class SnapshotResolver" in SNAPSHOTS_JS
    assert 'status: "stale"' in SNAPSHOTS_JS
    assert "fallbackIntervals" in SNAPSHOTS_JS
    assert "moduleFreshness" in JS
    assert "renderModuleUnavailable" in JS


def test_refresh_preserves_focus_scroll_and_visibility_cancels_requests() -> None:
    assert "const scrollTop = target.scrollTop" in JS
    assert "target.scrollTop = scrollTop" in JS
    assert 'data-focus-key="npc-control-toggle"' in JS
    assert "state.lastFocusedElement?.focus" in JS
    assert 'scope.startsWith("dashboard")' in JS
    assert 'if (document.hidden || state.selectedNpc) return' in JS


def test_visual_system_and_responsive_contract_are_present() -> None:
    for token in (
        "--green: #245847",
        "--paper: #f4f1e9",
        "--card: #fffdf7",
        "--blue: #2f6f8f",
        "--amber: #9a5b13",
        "--red: #b4332d",
    ):
        assert token in CSS
    assert "@media (max-width: 1200px)" in CSS
    assert "@media (max-width: 980px)" in CSS
    assert "@media (max-width: 700px)" in CSS
    assert "@media (max-width: 480px)" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert ".npc-record { width: 100vw; height: 100dvh" in CSS
    assert "outline: 3px solid var(--blue)" in CSS


def test_user_facing_copy_uses_capabilities_instead_of_release_versions() -> None:
    script_without_block_comments = re.sub(r"/\*.*?\*/", "", JS, flags=re.DOTALL)
    assert re.search(r"V[01]\.\d+", HTML) is None
    assert re.search(r"V[01]\.\d+", script_without_block_comments) is None


def test_runtime_controls_explain_state_and_progressively_disclose_diagnostics() -> None:
    assert "runtime-guidance" in JS
    assert "data-runtime-config-open" in JS
    assert "配置模型" in JS
    assert "canEmergencyStop" in JS
    assert "runtime-diagnostics" in JS
    assert "用量与诊断" in JS


def test_runtime_provider_form_keeps_secrets_ephemeral() -> None:
    assert 'id="runtime-config-form"' in HTML
    assert 'type="password"' in HTML
    assert 'autocomplete="new-password"' in HTML
    assert "不写入数据库、日志或浏览器存储" in HTML
    assert "localStorage" not in JS
    assert "sessionStorage" not in JS
    assert 'api("/api/runtime/provider"' in JS


def test_phase_three_node_behavior_suite() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available for the frontend behavior suite")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "dashboard_phase3_frontend.test.mjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
