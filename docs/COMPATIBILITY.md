# Compatibility Matrix

| Component | Supported |
| --- | --- |
| Windows | Windows 10 22H2 and Windows 11 |
| Python source | 3.12 |
| Skill API | 1.0 |
| PySide6 | 6.8+ |
| Images | Runtime formats reported by Qt `QImageReader`; packaged PNG/JPEG/GIF/BMP/WebP matrix |
| Image understanding | Explicit local images supported by Pillow; OCR requires local Tesseract on `PATH` |
| Documents | TXT/Markdown/CSV built in; PDF via pypdf; DOCX/XLSX/PPTX via python-docx/openpyxl/python-pptx |
| Audio/video | Runtime formats reported in Diagnostics by Qt Multimedia; packaged FFmpeg and Windows media plugins |
| faster-whisper | 1.1+ |
| Ollama | local `/api/generate` with JSON Schema format |
| Piper | maintained CLI accepting ONNX voice models |
| ElevenLabs | HTTPS text-to-speech API with selectable voice and model IDs |
| Database | SQLite schema version 15 |

Codec availability can differ by Windows edition and Qt release. The application
does not promise playback based only on a filename extension; Diagnostics is
the authoritative runtime list, and packaged release acceptance follows
`docs/MEDIA_VIEWER.md`.

Built-in event polling covers local files, exact process names, exact local
device instance IDs, and sanitized public HTTPS fingerprints while the desktop
process is running. Calendar event observations require a configured connector.
The per-user app does not run while powered off and does not request OS wake
timers. Speech latency/WER claims, microphone/echo behavior, media codecs,
sleep/restart/toast behavior, and clean install/upgrade/uninstall remain
hardware or clean-machine acceptance gates documented in
`RELEASE_AND_SIGNING_RUNBOOK.md`.
