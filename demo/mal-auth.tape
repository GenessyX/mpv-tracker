Output "demo/mal-auth.gif"

Set Shell "bash"
Set FontSize 22
Set Width 1600
Set Height 1000
Set Padding 20
Set TypingSpeed 0ms
Set Framerate 30

# Assumes mpv-tracker is configured with the default MAL client ID.
# This tape focuses on the TUI MAL screen. The external browser page itself
# won't be visible in the GIF because VHS records the terminal only.

Type "mpv-tracker"
Enter
Sleep 2s

Type "m"
Sleep 3s

# Show the MAL settings/authentication screen and move focus through the
# relevant actions, including Authenticate, without opening the browser.
Tab
Sleep 400ms
Tab
Sleep 400ms
Tab
Sleep 400ms
Shift+Tab
Sleep 400ms
Shift+Tab
Sleep 400ms
Right
Sleep 400ms
Right
Sleep 400ms
Left
Sleep 800ms

Escape
Sleep 1s
Ctrl+C
