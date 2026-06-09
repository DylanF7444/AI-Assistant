# Offline Operation and Hardware

The tested target is Windows 11, Ryzen 5 5600G, GTX 1660 Super, and 16 GB RAM.
Recommended starting models are Whisper `small.en`, Ollama `qwen2.5:3b`, and a
Piper medium voice. On the target GPU, `qwen2.5:3b` achieved 100% accuracy on
the initial structured-call benchmark at 1.7 seconds median and roughly 68-84
generated tokens/second. Providers load lazily and should be unloaded under
VRAM pressure.

## Latency and resource budgets

| Stage | Budget |
| --- | --- |
| Listening indication | under 100 ms |
| First partial transcript | under 1.5 s after enough speech |
| Final transcript | under 1.0 s after end of utterance |
| Deterministic intent | under 100 ms |
| Safe local execution | under 500 ms, excluding the requested operation |
| First TTS audio | under 750 ms for a warmed voice |
| Total resident memory | below 12 GB on the 16 GB target |

The application loads providers lazily and does not require STT, LLM, and TTS
to occupy the GPU simultaneously. Model/device placement is configurable.
Under pressure, use a smaller Whisper/LLM model, move STT to CPU, disable
speech output, or restart a provider to release its process memory.

Core routing, policy, storage, skills, and UI require no cloud account.
Model files may be installed from local media. Ollama uses loopback only.
Optional network integrations are labeled and remain blocked while the global
offline lock is enabled. Telemetry and cloud crash reporting are absent.

Release builds publish checksums for installers and require signed model
bundles. Deterministic and local-LLM figures are recorded in `benchmarks/`.
Microphone, STT, and TTS release figures still require the controlled audio
corpus and configured local voice described in `IMPLEMENTATION_STATUS.md`.
