Output "demo/showcase.gif"

Set Shell "bash"
Set FontSize 18
Set Width 1800
Set Height 950
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

# Open the add-series screen and show directory suggestions plus MAL link entry.
Type "a"
Sleep 1s
Type "Jujutsu Kaisen S3"
Sleep 600ms
Tab
Sleep 300ms
Type "/mnt/ssd/anime"
Sleep 800ms
Type "/Jujutsu Kaisen"
Sleep 1200ms
Down
Sleep 600ms
Right
Sleep 1000ms
Tab
Sleep 300ms
Tab
Sleep 300ms
Tab
Sleep 300ms
Type "https://myanimelist.net/anime/57658/Jujutsu_Kaisen__Shimetsu_Kaiyuu_-_Zenpen"
Sleep 800ms
Ctrl+S
Sleep 2s

# Show the MAL login / account screen.
Type "m"
Sleep 3s
Tab
Sleep 400ms
Right
Sleep 400ms
Right
Sleep 400ms
Left
Sleep 800ms
Escape
Sleep 1s

# Move to Dragon Ball and open the detail view.
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
