Output "demo/showcase.gif"

Set Shell "bash"
Set FontSize 18
Set Width 1800
Set Height 950
Set Padding 30
Set TypingSpeed 0ms
Set Framerate 30

# Assumes the current library already contains:
# - Baki Hanma 2021
# - Dragon Ball
# - Jujutsu Kaisen S3
# and that Dragon Ball is MAL-linked and has AnimeFillerList configured.

Type "mpv-tracker"
Enter
Sleep 2s

# The initial focus is in the search bar. Search for Dragon Ball first.
Type "Dragon"
Sleep 1200ms
Down
Sleep 600ms
Enter
Sleep 2s

# Open MAL info, then return.
Type "i"
Sleep 2s
Type "r"
Sleep 2s
Escape
Sleep 1s

# Open preferences and show audio/subtitle selection plus AnimeFillerList support.
Type "o"
Sleep 2s
Tab
Sleep 400ms
Tab
Sleep 400ms
Tab
Sleep 400ms
Tab
Sleep 400ms
Tab
Sleep 400ms
Tab
Sleep 400ms
Space
Sleep 1s

# Scroll within preferences so filler details are visible.
PageDown
Sleep 800ms
PageDown
Sleep 800ms

# Toggle skip filler radio option.
Down
Sleep 400ms
Space
Sleep 1200ms

Escape
Sleep 1s

# Jump back through the episode table until the filler range is visible.
PageUp
Sleep 800ms
PageUp
Sleep 800ms
Down
Sleep 150ms
Down
Sleep 150ms
Down
Sleep 800ms

# Return to library.
Escape
Sleep 1s

# Clear the existing Dragon search before starting the Jujutsu flow.
Type "/"
Sleep 300ms
Backspace
Sleep 10ms
Backspace
Sleep 10ms
Backspace
Sleep 10ms
Backspace
Sleep 10ms
Backspace
Sleep 10ms
Backspace
Sleep 300ms
Escape
Sleep 300ms

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

# Search for Jujutsu and delete it to showcase removal.
Type "/"
Sleep 300ms
Backspace
Sleep 100ms
Backspace
Sleep 100ms
Backspace
Sleep 100ms
Backspace
Sleep 100ms
Backspace
Sleep 100ms
Backspace
Sleep 100ms
Backspace
Sleep 100ms
Backspace
Sleep 300ms
Type "Jujutsu"
Sleep 1000ms
Down
Sleep 600ms
Type "d"
Sleep 1s
Enter
Sleep 2s

Ctrl+C
