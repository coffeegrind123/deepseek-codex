// codex-workspace: Ovis's New workspace / New tab picker remembers its agent
// per browser (localStorage "ovis:new-session-agent"; "" is Shell, its
// default). Until a choice has been made in this browser it is Codex, so a new
// workspace or tab opens on a Codex chat. A choice made in the picker, Shell
// included, is kept as Ovis keeps it. Loaded from dist/index.html
// (scripts/ovis_patch.py); a file of its own because Ovis's CSP is
// script-src 'self'.
try {
  if (window.localStorage.getItem("ovis:new-session-agent") === null) {
    window.localStorage.setItem("ovis:new-session-agent", "codex");
  }
} catch {
  // storage refused: the picker shows its own default
}
