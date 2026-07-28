# In-App Media Viewer

## What it does

Local Assistant can present an approved image, audio file, or video in a
separate, resizable results window. The compact and full Assistant views show a
now-viewing card; opening it raises the results window without changing or
resizing the conversation window. This is separate from `windows.media`, which sends
global Windows media keys to another application.

Examples:

- `show C:\Users\Me\Pictures\photo.png`
- `show the vacation photo in Pictures`
- `pull up the second result`
- `play C:\Users\Me\Music\meeting.mp3`
- `show https://cdn.example.com/demo.mp4`
- `pause it`, `skip ahead 30 seconds`, `set playback speed to 1.5x`
- `mute this video`, `make it fullscreen`, `close the viewer`

Named searches are filename-only and stay beneath the approved folder. If more
than one file matches, the assistant returns numbered choices instead of
guessing.

## Permissions and network behavior

Local presentation uses `filesystem.read` scoped to the exact file or searched
folder. Remote presentation uses `network` scoped to the explicit, query-free
public HTTPS target. Viewer controls need no additional permission because they
can affect only the active in-app presentation.

Remote media is optional and disabled by the global offline lock. The assistant:

1. rejects HTTP, credentials in URLs, and private, loopback, link-local, or
   reserved destinations;
2. validates every production redirect and the final response target;
3. accepts only image, audio, or video response types;
4. enforces configured response-size and timeout limits;
5. downloads into the application cache instead of passing a URL to Qt;
6. verifies the file signature and fingerprint before display or playback.

URL credentials and query strings are not stored as provenance or written to
audit and terminal diagnostic logs. Sanitized provenance contains HTTPS host,
optional port, and path only.

## Viewer controls

Images support fit, actual size, wheel/button zoom, drag-to-pan, 90-degree
rotation, fullscreen, and close. Audio and video support play/pause, stop,
relative and absolute seek, ten-second skip, mute, volume, playback speed,
timestamps, fullscreen for video, and close.

Keyboard controls while the viewer has focus:

| Key | Action |
| --- | --- |
| `Space` | Play or pause audio/video |
| `Left` / `Right` | Seek backward/forward ten seconds |
| `M` | Mute or unmute |
| `F` | Enter or exit fullscreen |
| `Esc` | Exit fullscreen |
| `+` / `-` | Zoom an image |

Runtime codec support depends on the packaged Qt Multimedia backend and Windows.
The Diagnostics tab lists detected image and audio/video formats, output
devices, media-cache size, and the last sanitized decoder error code.

## Voice, TTS, and cancellation

Starting an explicit microphone interaction pauses playing viewer media.
Playback resumes only when the same presentation was paused solely for the
assistant interaction. A viewer command, a replacement presentation, a decoder
failure, or Emergency Stop cancels that pending resume.

TTS and viewer playback use named audio-focus ownership. Microphone capture
remains suppressed until every active speaker source has stopped, so one source
cannot incorrectly reopen capture while another is playing. Passive activation
is suppressed during speaker output. Emergency Stop and application shutdown
stop viewer playback and release the media backend.

## Privacy and cleanup

Remote files use a content-addressed cache under the application data directory.
Items older than the configured retention period are removed, and least-recent
files are pruned when the byte limit is exceeded. Open **Privacy** and select
**Clear Downloaded Media Cache** to close active remote media and delete cached
downloads. Local source files are never deleted by that action.

The assistant does not persist the active local path, playback position,
thumbnail, decoded frame, or file content. The in-memory presentation registry
is cleared by `forget this session` and on process exit.

## Troubleshooting

- `No supported media files matched`: include a supported filename or choose a
  narrower approved folder.
- `The media file changed`: request it again so authorization and fingerprinting
  use the current file.
- `Unsupported codec`: check **Diagnostics > In-app media** and try a format
  listed by the installed runtime.
- `Remote media is blocked`: disable offline lock only if network access is
  intended, then approve the exact `network` request.
- `Remote server did not return media`: use a direct media URL, not an HTML page,
  streaming manifest, authenticated player, or DRM service.

Generic web image/video search, YouTube pages, DRM, HLS/DASH, media editing,
OCR, and transcription are outside this viewer.

## Release acceptance matrix

Exercise this matrix against the packaged Windows executable after the archive
verification in `scripts/build.ps1` passes:

| Area | Required cases |
| --- | --- |
| Image | PNG, JPEG, GIF, BMP, WebP; fit/zoom/pan/rotate/fullscreen |
| Audio | WAV plus at least one runtime-listed compressed format; all controls |
| Video | MP4 plus one runtime-listed alternate; video surface and fullscreen |
| Selection | Exact path, named-folder single match, ambiguous numbered choice |
| Safety | Changed file, malformed signature, huge image, unsupported codec |
| Remote | HTTPS success, offline block, private host, redirect, wrong MIME, oversize, cancel |
| Coordination | Push-to-talk pause/resume, TTS priority, passive suppression |
| Lifecycle | Replacement, stale callback, close, Emergency Stop, shutdown |
| Privacy | Query redaction, cache retention, cache clear, no local-file deletion |

Record OS version, Qt/PySide version, Diagnostics format list, output device,
test filenames, expected/actual result, and any sanitized decoder error in
`docs/ACCEPTANCE_RUN_LOG.md`.
