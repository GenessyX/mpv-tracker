Output "demo/showcase.gif"

Set Shell "bash"
Set FontSize 18
Set Width 2200
Set Height 1400
Set Padding 30
Set TypingSpeed 0ms
Set Framerate 30

# Assumes the current library already contains:
# - Baki Hanma
# - Dragon Ball
# and that Dragon Ball is MAL-linked.

Type "mpv-tracker"
Enter
Sleep 2s

# Library view, move to Dragon Ball.
Down
Sleep 800ms
Enter
Sleep 2s

# Dragon Ball detail with watched progress and MAL summary.
Type "i"
Sleep 2s
Type "r"
Sleep 2s
Escape
Sleep 1s

# Move across the action row to the series preferences screen.
Tab
Sleep 300ms
Right
Sleep 300ms
Right
Sleep 300ms
Right
Sleep 300ms
Right
Sleep 300ms
Enter
Sleep 2s

# Return to detail and move to the MAL score controls.
Escape
Sleep 1s
Tab
Sleep 300ms
Tab
Sleep 300ms
Right
Sleep 300ms
Right
Sleep 300ms
Right
Sleep 300ms
Right
Sleep 300ms

# Return to library and briefly show the built-in help.
Escape
Sleep 1s
Type "h"
Sleep 3s
Escape
Sleep 1s

Ctrl+C
