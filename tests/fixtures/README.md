# Test fixtures

## `sd_json_firmware_sample.json`

A real SD-card JSON log, captured 2026-10-27 by the nq-a bench lane: 216 samples, 4 channels, `tick_hz` 42000000.
- **Firmware:** 3.8.0, image FB1B909F @ daqifi-nyquist-firmware commit 374510776.
- **Format equivalence with firmware `main`:** `JSON_Encoder.c`, `JSON_Encoder.h` and `StreamingRuntimeConfig.h` are byte-identical blobs between that commit and origin/main 4bdcfc798, so the format is representative of `main`.
- **Size:** 22752 bytes, equal to the size `SD:LISt?` reported.

**One edit:** the board serial in the `meta` header is replaced by the firmware's own documented example value, `0123456789ABCDEF` (JSON_Encoder.c:99), with the same length. No board serial goes into this repository. The original file's md5, `6dff453c7f991904c87b07d5bc848898`, is therefore not this file's md5.

Used by `tests/test_sd_json_firmware_format.py` (#163).
