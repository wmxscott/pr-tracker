# The README screenshots. Run `scripts/demo --vhs` rather than this tape
# directly: it seeds a made-up ledger and builds the isolated environment the
# tape expects.

Require prs
Require fzf

Set Shell "bash"
Set FontFamily "FiraCode Nerd Font"
Set FontSize 16
Set LineHeight 1.25
Set Width 1600
Set Height 720
Set Padding 24
Set Theme "Catppuccin Macchiato"
Set TypingSpeed 0

Hide
Type "clear && prs"
Enter
Wait+Screen@10s /Document the invoices API/
# Down to #413, the stack's failing middle PR, so the preview has something to say.
Type "jj"
Sleep 1.5s
Show
Screenshot docs/screenshots/prs.png
Sleep 500ms

Hide
# Every session's PRs, in every state.
Type "s"
Sleep 500ms
Type "aaa"
Wait+Screen@10s /Rate-limit[\s\S]*Log queries/
Sleep 500ms
# To the top, then down to #420: approved, but now conflicting.
Up 20
Down 5
Sleep 1.5s
Show
Screenshot docs/screenshots/prs-all.png
Sleep 500ms
