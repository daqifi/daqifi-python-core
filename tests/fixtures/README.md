# Test fixtures

## `sd_json_firmware_sample.json`

A real SD-card JSON log, captured 2026-10-27 on the bench: 216 samples, 4 channels, `tick_hz` 42000000.
- **Firmware:** 3.8.0, image FB1B909F @ daqifi-nyquist-firmware commit 374510776.
- **Format equivalence with firmware `main`:** `JSON_Encoder.c`, `JSON_Encoder.h` and `StreamingRuntimeConfig.h` are byte-identical blobs between that commit and origin/main 4bdcfc798, so the format is representative of `main`.
- **Size:** 22752 bytes, equal to the size `SD:LISt?` reported.

**One edit:** the board serial in the `meta` header is replaced by the firmware's own documented example value, `0123456789ABCDEF` (JSON_Encoder.c:99), with the same length. No board serial goes into this repository. The original file's md5, `6dff453c7f991904c87b07d5bc848898`, is therefore not this file's md5.

Used by `tests/test_sd_json_firmware_format.py` (#163).

## `sd_csv_pattern_raw.csv` and `sd_csv_pattern_mv.csv`

A real discriminating pair of SD-card CSV logs, captured 2026-10-27 on the bench.
- **Firmware:** 3.8.0, image FB1B909F.
- **Settings:** `SYSTem:STReam:TEST:PATtern 1` (counter), `SYST:STR:FORmat 2`, precision 0 in both, 4 channels at 200 Hz for 2 s.
- **The one difference:** `CONF:ADC:USECal` is 2 for the raw file (raw ADC codes, csv_encoder.c:438) and 0 for the mV file (integer millivolts, csv_encoder.c:449).
- **Why the pair compares positionally:** the pattern is injected into the sample values upstream of calibration and of the encoder's raw/mV branch (streaming.c:1204-1212), and its counter resets on start (streaming.c:2067). So row i carries code i + 1 + channel in both files, and they differ only by the conversion: 433 of 435 compared samples differ, all but codes 1 and 2.
- **Sizes and original md5s:** raw 25837 bytes (`cdc18dfe270fdb16af2ffa84c8679ffd`); mV 27117 bytes (`9569f7145c5e774cc588364b3f924044`).

**One edit:** the board serial in each header is replaced by `0123456789ABCDEF`, with the same length, so the md5s above are the originals', not these files'.

Used by `tests/test_sd_integer_millivolts.py` (#163 item 20).
