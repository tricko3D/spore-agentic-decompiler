"""Regression checks for dashboard density, scroll performance and view separation.

These do not replace frontend typechecking or browser tests. The Dashboard V2
workflow also runs npm build and the HTTP integration tests.
"""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/"dashboard_web"/"src"


class CompactUIRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css=(WEB/"compact.css").read_text()
        cls.base=(WEB/"styles.css").read_text()
        cls.graph=(WEB/"components/BrainGraph.tsx").read_text()
        cls.app=(WEB/"App.tsx").read_text()
        cls.hooks=(WEB/"hooks.ts").read_text()
        cls.panels=(WEB/"components/Panel.tsx").read_text()
        cls.brain_search=(WEB/"components/BrainSearch.tsx").read_text()
        cls.ops=(WEB/"views/Operations.tsx").read_text()
        cls.knowledge=(WEB/"views/Knowledge.tsx").read_text()
        cls.infra=(WEB/"views/InfraDocs.tsx").read_text()
        cls.error_modal=(WEB/"components/ErrorModal.tsx").read_text()
        cls.agent_dock=(WEB/"components/AgentDock.tsx").read_text()
        cls.knowledge_dock=(WEB/"components/KnowledgeDock.tsx").read_text()

    def test_compact_sheet_is_last_and_uses_original_instrument_palette(self):
        main=(WEB/"main.tsx").read_text()
        self.assertLess(main.index("import './styles.css'"),
                        main.index("import './compact.css'"))
        self.assertIn("#6f91ac",self.css)
        self.assertIn("#daf9a9",self.css)
        self.assertIn("Trebuchet MS",self.css)
        self.assertIn("linear-gradient",self.css)

    def test_no_large_blurred_scroll_surfaces(self):
        self.assertRegex(self.css,r"backdrop-filter:\s*none")
        self.assertRegex(self.css,r"-webkit-backdrop-filter:\s*none")
        self.assertRegex(self.css,r"background-attachment:\s*scroll")
        self.assertRegex(self.css,r"\.game-shell:before\s*\{\s*display:none\s*\}")
        self.assertNotIn("background-attachment:fixed",self.css)

    def test_fleet_is_dense_and_not_stretched_to_document_height(self):
        self.assertRegex(self.css,r"grid-auto-rows:\s*112px")
        self.assertRegex(self.css,r"\.fleet\s*\{\s*display:block;\s*align-self:start")
        self.assertIn(".agent-grid",self.css)
        self.assertIn(".agent.offline",self.base)

    def test_restored_svg_graph_is_scoped_to_fixed_panel(self):
        self.assertIn("<svg",self.graph)
        self.assertIn("viewBox",self.graph)
        self.assertIn("width={980}",self.graph)
        self.assertIn("graph?.links",self.graph)
        self.assertIn("onFinding(x.id)",self.graph)
        self.assertIn("onTechnique(x.id)",self.graph)
        self.assertIn(".knowledge-view .graph-wrap",self.css)
        self.assertIn("display:block;flex:1 1 0;min-height:0;max-height:none;overflow:auto",self.css)

    def test_views_are_lazy_and_only_the_current_view_is_mounted(self):
        self.assertIn("lazy(()=>import('./views/Operations'))",self.app)
        self.assertIn("lazy(()=>import('./views/Knowledge'))",self.app)
        self.assertIn("view==='operations'",self.app)
        self.assertNotIn("<div hidden=",self.app)
        self.assertIn("Suspense",self.app)

    def test_polling_is_serial_abortable_and_pauses_in_background(self):
        self.assertNotIn("setInterval",self.hooks)
        self.assertIn("setTimeout",self.hooks)
        self.assertIn("visibilitychange",self.hooks)
        self.assertIn("document.visibilityState",self.hooks)
        self.assertIn("inFlight.current?.abort()",self.hooks)
        self.assertIn("if(disposed||signal.signal.aborted||running)return",self.hooks)

    def test_single_permanent_bottom_command_bar(self):
        self.assertIn('<footer className="bottom-hud"',self.app)
        self.assertIn('className="hud-brand"',self.app)
        self.assertIn('className="hud-nav"',self.app)
        self.assertIn("navigate('operations')",self.app)
        self.assertIn("navigate('knowledge')",self.app)
        self.assertIn('className="hud-spacer"',self.app)
        self.assertIn('<AgentDock snapshot={snapshot}',self.app)
        self.assertIn("'hud-connection '+connection",self.app)
        self.assertIn('CONNECTED',self.app)
        self.assertIn('DISCONNECTED',self.app)
        self.assertIn('className="control-dock"',self.agent_dock)
        self.assertIn("Stop workers",self.agent_dock)
        self.assertIn("Retry allowlist",self.agent_dock)
        self.assertNotIn('className="persistent-docks"',self.app)
        self.assertNotIn('<KnowledgeDock state=',self.app)
        self.assertNotIn('className="app-header"',self.app)
        self.assertNotIn('className="dock-knowledge"',self.agent_dock)
        self.assertIn('<KnowledgeDock state={overview?.consolidator',self.knowledge)
        self.assertIn('className="knowledge-service-body"',self.knowledge_dock)
        self.assertIn('.bottom-hud .control-dock',self.css)
        self.assertIn('flex:0 0 50px',self.css)

    def test_knowledge_view_fixed_and_cannot_expand_panels(self):
        self.assertNotIn('className="knowledge-intro"',self.knowledge)
        self.assertNotIn('How knowledge is formed',self.knowledge)
        self.assertIn('grid-template-rows:minmax(0,45fr) minmax(0,55fr)',self.css)
        self.assertIn('grid-auto-rows:111px',self.css)
        self.assertIn('max-height:111px',self.css)
        self.assertIn('overflow-y:auto;overflow-x:hidden',self.css)
        self.assertIn('content-visibility:visible',self.css)

    def test_search_results_and_evidence_do_not_resize_panels(self):
        self.assertIn('className="brain-search-panel"',self.brain_search)
        self.assertIn('<Drawer title="Collective Memory results"',self.brain_search)
        self.assertIn("createPortal",self.panels)
        self.assertIn("document.body",self.panels)
        self.assertIn("body>.overlay",self.css)
        self.assertIn("z-index:1000!important",self.css)

    def test_consolidation_errors_open_centered_copyable_modal(self):
        self.assertIn('<ErrorModal error={selectedError}',self.knowledge)
        self.assertIn('setSelectedError(x)',self.knowledge)
        self.assertIn("createPortal",self.error_modal)
        self.assertIn('className="error-modal-code"',self.error_modal)
        self.assertIn("navigator.clipboard.writeText",self.error_modal)
        self.assertIn('Copy error',self.error_modal)
        self.assertIn('.error-record-summary',self.css)

    def test_infra_docs_is_a_separate_full_height_view(self):
        self.assertIn("const InfraDocs=lazy",self.app)
        self.assertIn("navigate('infra')",self.app)
        self.assertIn("Infra Docs</button>",self.app)
        self.assertIn("view==='infra'",self.app)
        self.assertIn("export default function InfraDocs()",self.infra)
        self.assertIn('className="infra-branches"',self.infra)
        self.assertIn("spore_dashboard_control.py",self.infra)
        self.assertIn("dashboard/server.py",self.infra)
        self.assertIn("spore_technique_consolidator.py",self.infra)
        self.assertIn(".view-content>.infra-view",self.css)

    def test_raw_investigations_do_not_render_cached_technique_rows(self):
        self.assertIn('response.url===url',self.hooks)
        self.assertIn('current?response.value:null',self.hooks)
        self.assertIn('setResponse({url,value:next',self.hooks)
        self.assertIn("typeof value==='string'",self.panels)
        self.assertIn("label.toLowerCase()",self.panels)
        self.assertIn("export default memo(Knowledge)",self.knowledge)
        self.assertIn("export default memo(BrainSearch)",self.brain_search)

    def test_knowledge_skips_offscreen_rendering(self):
        self.assertIn("content-visibility:auto",self.css)
        self.assertIn("contain-intrinsic-size:auto 115px",self.css)
        self.assertIn("animation:none!important",self.css)

    def test_knowledge_graph_memo_receives_stable_callbacks(self):
        self.assertIn("const openTechnique=useCallback",self.knowledge)
        self.assertIn("const openFinding=useCallback",self.knowledge)
        self.assertIn("<BrainGraph graph={graph} onTechnique={openTechnique}",self.knowledge)
        self.assertIn("export default memo(BrainGraph)",self.graph)

    def test_desktop_shell_restores_fixed_viewport_when_height_allows(self):
        self.assertIn("@media(min-width:1600px) and (min-height:950px)",self.css)
        self.assertIn("height:100dvh",self.css)
        self.assertIn("scrollbar-gutter:stable",self.css)
        self.assertIn("overscroll-behavior:contain",self.css)

    def test_scroll_containers_are_bounded_without_zoom_hacks(self):
        self.assertRegex(self.css,r"\.table-scroll\s*\{[^}]*max-height:\s*350px")
        self.assertIn("grid-auto-rows:111px",self.css)
        self.assertNotIn("scale(",self.css)
        self.assertNotIn("zoom:",self.css)


if __name__=="__main__":
    unittest.main()
