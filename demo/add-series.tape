Output "demo/add-series.gif"

Set Shell "bash"
Set FontSize 18
Set Width 2200
Set Height 1400
Set Padding 30
Set TypingSpeed 0ms
Set Framerate 30

# Show the add-series flow for a new MAL-linked anime entry.

Type "mpv-tracker"
Enter
Sleep 2s

# Open the add-series screen.
Type "a"
Sleep 1s

# Fill the new entry.
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

# Save and return to the library with the new item visible.
Ctrl+S
Sleep 2s

# Open the newly added third entry to show linked MAL metadata.
Down
Sleep 500ms
Down
Sleep 500ms
Enter
Sleep 3s

Ctrl+C
