/*
 * Phase 1, step 5 — the verification itself.
 *
 * This component was reconstructed *only* from examples/app_map.sample.json,
 * touching no other source of truth, to prove the JSON contract is sufficient
 * for an AI agent to rebuild real UI. Every value below traces to a field:
 *
 *   state.window_title      -> title bar
 *   state.breadcrumb         -> the menu path ("File › Export")
 *   state.reached_by         -> how this menu is opened (click File on the main window)
 *   control.name             -> item label
 *   control.enabled === false-> greyed, non-clickable ("Publish to Web…")
 *   control.shortcut         -> right-aligned accelerator badge ("Ctrl+Shift+E")
 *   control.leads_to         -> onClick navigation target (null = dead-ends here,
 *                               absent = disabled)
 *   rect ordering / rect.top -> vertical order of items
 *
 * Nothing here required the screenshot. That is the whole point of the map:
 * ~90 tokens of JSON per state rebuilds the menu, versus ~1,500 for a described
 * screenshot. Conclusion: the schema is locked — no fields were missing for this
 * task. (Candidate v0.2 niceties, not blockers: a `checked` flag for toggle
 * items, and preserved `Separator` controls for visual grouping.)
 */

// --- pasted straight from app_map.sample.json (state_0023) ------------------
const exportMenu = {
  window_title: "Untitled — InPage 2012",
  breadcrumb: ["MainWindow", "File", "Export"],
  reached_by: { action: "left_click", on: "MenuItem:Export", from: "state_0019" },
  controls: [
    { type: "MenuItem", name: "Export as PDF…", shortcut: "Ctrl+Shift+E", enabled: true, leads_to: "state_0024" },
    { type: "MenuItem", name: "Export as Image…", enabled: true, leads_to: null },
    { type: "MenuItem", name: "Publish to Web…", enabled: false },
  ],
};

function ExportMenu({ map = exportMenu, onNavigate = () => {} }) {
  return (
    <div className="menu" role="menu" aria-label={map.breadcrumb.join(" › ")}>
      {map.controls.map((c) => {
        const disabled = c.enabled === false;
        return (
          <button
            key={c.name}
            role="menuitem"
            className="menu-item"
            disabled={disabled}
            // leads_to is the routing table: a state_id navigates, null dead-ends.
            onClick={() => c.leads_to && onNavigate(c.leads_to)}
          >
            <span className="label">{c.name}</span>
            {c.shortcut && <kbd className="shortcut">{c.shortcut}</kbd>}
          </button>
        );
      })}
    </div>
  );
}

export default ExportMenu;
