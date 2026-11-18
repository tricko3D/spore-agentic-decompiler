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
        cls.ops=(WEB/"views/Operations.tsx").read_text()
        cls.knowledge=(WEB/"views/Knowledge.tsx").read_text()

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
        self.assertNotIn("position:fixed",self.css)

    def test_fleet_is_dense_and_not_stretched_to_document_height(self):
        self.assertRegex(self.css,r"grid-auto-rows:\s*112px")
        self.assertRegex(self.css,r"\.fleet\s*\{\s*display:block;\s*align-self:start")
        self.assertIn(".agent-grid",self.css)
        self.assertIn(".agent.offline",self.base)

    def test_graph_is_bounded_and_not_scaled_svg(self):
        self.assertNotIn("<svg",self.graph)
        self.assertNotIn("viewBox",self.graph)
        self.assertIn("techniques.slice(0,7)",self.graph)
        self.assertIn("connections.slice(0,6)",self.graph)
        self.assertIn("graph.links.filter",self.graph)
        self.assertIn("onFinding(x.id)",self.graph)
        self.assertIn("onTechnique(selected.id)",self.graph)
        self.assertIn("aria-pressed",self.graph)

    def test_views_are_lazy_and_only_the_current_view_is_mounted(self):
        self.assertIn("lazy(()=>import('./views/Operations'))",self.app)
        self.assertIn("lazy(()=>import('./views/Knowledge'))",self.app)
        self.assertIn("view==='operations'?<Operations",self.app)
        self.assertNotIn("<div hidden=",self.app)
        self.assertIn("Suspense",self.app)

    def test_polling_is_serial_abortable_and_pauses_in_background(self):
        self.assertNotIn("setInterval",self.hooks)
        self.assertIn("setTimeout",self.hooks)
        self.assertIn("visibilitychange",self.hooks)
        self.assertIn("document.visibilityState",self.hooks)
        self.assertIn("inFlight.current?.abort()",self.hooks)
        self.assertIn("if(disposed||signal.signal.aborted||running)return",self.hooks)

    def test_controls_are_visible_above_data_explorer(self):
        self.assertLess(self.ops.index("<LaunchDock snapshot={d}"),
                        self.ops.index('<div className="operations-layout">'))
        self.assertLess(self.ops.index('<div className="operations-layout">'),
                        self.ops.index('title="Function collection"'))
        self.assertIn("Stop workers",self.ops)
        self.assertIn("Retry allowlist",self.ops)

    def test_knowledge_graph_memo_receives_stable_callbacks(self):
        self.assertIn("const openTechnique=useCallback",self.knowledge)
        self.assertIn("const openFinding=useCallback",self.knowledge)
        self.assertIn("<BrainGraph graph={graph} onTechnique={openTechnique}",self.knowledge)
        self.assertIn("export default memo(BrainGraph)",self.graph)

    def test_scroll_containers_are_bounded_without_zoom_hacks(self):
        self.assertRegex(self.css,r"\.table-scroll\s*\{[^}]*max-height:\s*350px")
        self.assertRegex(self.css,r"\.knowledge-results\s*\{[^}]*max-height:\s*410px")
        self.assertNotIn("scale(",self.css)
        self.assertNotIn("zoom:",self.css)


if __name__=="__main__":
    unittest.main()
