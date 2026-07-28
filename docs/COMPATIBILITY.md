# Compatibility Matrix

| Component | Supported |
| --- | --- |
| Windows | Windows 10 22H2 and Windows 11 |
| Python source | 3.12 |
| Skill API | 1.0 |
| PySide6 | 6.8+ |
| Images | Runtime formats reported by Qt `QImageReader`; packaged PNG/JPEG/GIF/BMP/WebP matrix |
| Audio/video | Runtime formats reported in Diagnostics by Qt Multimedia; packaged FFmpeg and Windows media plugins |
| faster-whisper | 1.1+ |
| Ollama | local `/api/generate` with JSON Schema format |
| Piper | maintained CLI accepting ONNX voice models |
| ElevenLabs | HTTPS text-to-speech API with selectable voice and model IDs |
| Database | SQLite schema version 9 |

Codec availability can differ by Windows edition and Qt release. The application
does not promise playback based only on a filename extension; Diagnostics is
the authoritative runtime list, and packaged release acceptance follows
`docs/MEDIA_VIEWER.md`.
