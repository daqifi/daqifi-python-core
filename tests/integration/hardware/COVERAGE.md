# Hardware-test coverage matrix

One row per public symbol in `daqifi.*` that needs hardware exercise.
Every box is unchecked at Phase A landing time; slices in Phase B
check them off as they ship.

`scripts/check_hardware_coverage.py` walks `daqifi.*` and fails CI
if any public symbol is missing from this file (so adding a new
public API forces a test or a documented skip).

**Legend:**

- `[ ]` — not yet tested with hardware
- `[x]` — tested in a `tests/integration/hardware/test_*.py` file
- `[~]` — pure logic; covered by unit tests, no hardware needed
- `[d]` — destructive; gated behind `DAQIFI_DESTRUCTIVE_OK=1`
- `[s]` — skipped intentionally (cite reason)

---

## daqifi.transport

- [x] `SerialStreamTransport.__init__` — open/close idempotence (B1)
- [x] `SerialStreamTransport.connect` — happy path + retry options (B1)
- [x] `SerialStreamTransport.disconnect` — clean shutdown (B1)
- [x] `SerialStreamTransport.close` — disposal idempotence (B1)
- [x] `SerialStreamTransport.is_connected` — property reflects actual state (B1)
- [x] `SerialStreamTransport.connection_info` — string format (B1)
- [x] `SerialStreamTransport.add_status_changed` / `remove_status_changed` — events fire on connect/disconnect (B1)
- [x] `SerialStreamTransport.stream` — exposes underlying serial.Serial (B1)
- [x] `SerialStreamTransport.get_available_port_names` — enumerates ports (B1)
- [ ] `TcpStreamTransport.connect` (s — needs TCP-bridged Nyquist; defer)
- [x] `UdpTransport.send_broadcast` — used by WiFiDeviceFinder (B3)
- [x] `UdpTransport.receive` — UDP discovery datagram (B3)
- [ ] `HidLibraryTransport` (s — needs HID/bootloader; covered by B13 firmware update)

## daqifi.communication

- [x] `StreamMessageConsumer.start` — background reader spins up against real bytes (B4)
- [x] `StreamMessageConsumer.stop` — clean halt + thread join (B4)
- [x] `StreamMessageConsumer.stop_safely` — bounded-timeout stop (B4)
- [x] `StreamMessageConsumer.add_message_received` — events fire per-frame (B4)
- [x] `StreamMessageConsumer.queued_message_count` — accurate during/after stream (B4)
- [x] `StreamMessageConsumer.clear_buffer` — drops in-flight bytes (B6)
- [~] `LineBasedMessageParser.parse_messages` — pure parser; unit tests
- [~] `ProtobufMessageParser.parse_messages` — pure parser; unit tests
- [~] `CompositeMessageParser.parse_messages` — pure parser; unit tests

## daqifi.device (NyquistDevice — high-level facade)

- [ ] `NyquistDevice.connect` (B9)
- [ ] `NyquistDevice.disconnect` (B9)
- [ ] `NyquistDevice.power_up` / `power_down` (B9)
- [ ] `NyquistDevice.reset` (B9)
- [ ] `NyquistDevice.get_system_info` (B9)
- [ ] `NyquistDevice.get_last_error` (B9)
- [ ] `NyquistDevice.clear_errors` (B9 — currently capped at 10; see python-core#22)
- [ ] `NyquistDevice.is_connected` (B9)
- [ ] `NyquistDevice.variant` / `manufacturer` / `model` / `serial_number` / `firmware_version` (B9)
- [ ] `NyquistDevice.create_stream` (B14 async)
- [ ] `NyquistDevice.sd_enable` / `sd_list_files` / `sd_get_file` / `sd_get_file_binary` / `sd_set_logging` / `sd_benchmark` (B10)
- [d] `NyquistDevice.sd_format` / `sd_delete_file` (B11)
- [ ] `NyquistDevice.analog_outputs` / `analog_inputs` / `digital_io` (B7, B8)
- [ ] `NyquistDevice.power_state` (B9)
- [ ] `NyquistDevice.__enter__` / `__exit__` — context manager (B9)

## daqifi.devices

- [x] `DaqifiDevice.connect` (B2)
- [x] `DaqifiDevice.disconnect` (B2)
- [x] `DaqifiDevice.send` (B2 — implicit; used by every test)
- [x] `DaqifiDevice.initialize` (B2)
- [x] `DaqifiDevice.metadata` populated post-init (B2)
- [x] `DaqifiDevice.channels` populated via `populate_channels_from_status` (B2)
- [x] `DaqifiDevice.timestamp_frequency` (B2)
- [x] `DaqifiDevice.add_channels_populated` event fires once on init (B2)
- [x] `DaqifiStreamingDevice.start_streaming` / `stop_streaming` (B6)
- [x] `DaqifiStreamingDevice.is_streaming` / `streaming_frequency` (B6)
- [x] `DaqifiStreamingDevice.is_usb_connection` (B6)
- [ ] `DaqifiStreamingDevice.enable_sd_card_logging` / `disable_sd_card_logging` / `set_sd_logging_file_name` / `request_sd_card_file_list` (B10)
- [d] `DaqifiStreamingDevice.delete_sd_file` / `format_sd_card` (B11)
- [ ] `DaqifiStreamingDevice.set_wifi_ssid` / `set_wifi_password` / `set_wifi_mode_self_hosted` / `set_wifi_mode_existing` (B12)
- [d] `DaqifiStreamingDevice.apply_network_configuration` / `save_network_configuration` (B12)
- [ ] `DaqifiStreamingDevice.request_lan_chip_info` (B12)
- [x] `DaqifiDeviceFactory.connect_serial` (B2)
- [ ] `DaqifiDeviceFactory.connect_tcp` (s — needs TCP Nyquist; defer)

## daqifi.device_finders

- [x] `SerialDeviceFinder.discover` (B3)
- [x] `SerialDeviceFinder.add_device_discovered` / `add_discovery_completed` (B3)
- [x] `WiFiDeviceFinder.discover` (B3)
- [ ] `HidDeviceFinder.discover` (s — needs bootloader-mode device; B13)

## daqifi.discovery

- [x] `DeviceFinder.discover` (B3)
- [x] `DeviceFinder.discover_first` (B3 — already used by `device_info` fixture)
- [x] `DeviceFinder.get_serial_ports` (B3)

## daqifi.channels

- [x] `AnalogInputs.__getitem__` / `__len__` / `__iter__` (B7)
- [x] `AnalogInputs.configure_channels` (B7)
- [x] `AnalogInputs.enable_all` / `disable_all` (B7)
- [x] `AnalogInputChannel.enabled` (get + set) (B7)
- [x] `AnalogInputChannel.range` (get + set) (B7)
- [ ] `AnalogOutputs.__getitem__` / `__len__` / `__iter__` (s — Nq3 only)
- [ ] `AnalogOutputs.set_voltages` (s — Nq3 only)
- [ ] `AnalogOutputs.enable_all` / `disable_all` (s — Nq3 only)
- [ ] `AnalogOutputChannel.voltage` / `enabled` (s — Nq3 only)
- [x] `DigitalIO.__getitem__` / `__len__` / `__iter__` (B8)
- [x] `DigitalIO.configure_outputs` / `configure_inputs` (B8)
- [x] `DigitalIO.set_states` / `read_all` (B8)
- [x] `DigitalIOChannel.direction` / `state` (B8)

## daqifi.streaming

- [ ] `StreamManager.start` / `stop` (B4)
- [ ] `StreamManager.read` (blocking) (B4)
- [ ] `StreamManager.read_all` — collects to SampleBuffer (B4)
- [ ] `StreamManager.state` / `channels` / `sample_rate` properties (B4)
- [ ] `StreamManager.__enter__` / `__exit__` context manager (B4)
- [ ] `AsyncStreamManager.start` / `stop` / `read` / async-iter (B14)
- [ ] `SampleBuffer.add_sample` / `clear` (B15)
- [ ] `SampleBuffer.get_channel_data` / `get_timestamps` (B15)
- [ ] `SampleBuffer.to_csv` / `to_json` / `to_dataframe` (B15)

## daqifi.sdcard

- [~] All dataclasses (`SdCardFileInfo`, `SdCardParseProgress`, `SdCardDeviceConfiguration`, `SdCardParseOptions`, `SdCardTransferProgress`, `SdCardDownloadResult`) — pure data
- [ ] `SdCardOperations` impl on `DaqifiStreamingDevice` (B10)
- [ ] `SdCardOperations.download_sd_card_file` / `download_sd_card_file_to_temp` (B10)

## daqifi.network

- [~] `format_ipv4` / `format_mac` — pure formatters; unit-tested
- [~] `get_ip_address_string` / `get_mac_address_string` / `get_subnet_mask_string` / `get_gateway_string` / `get_primary_dns_string` / `get_secondary_dns_string` — pure; unit-tested
- [ ] `NetworkConfiguration.clone` (B12)
- [ ] `NetworkConfigurable.update_network_configuration` (B12 — non-applying round-trip)
- [ ] `NetworkConfigurable.prepare_sd_interface` / `prepare_lan_interface` (B10)

## daqifi.protocol

- [~] `ProtobufProtocolHandler.detect_message_type` — pure logic
- [ ] `ProtobufProtocolHandler.handle` integration via `DaqifiDevice.send` (B2; happens implicitly)

## daqifi.timestamp

- [~] `TimestampProcessor` — pure; unit-tested in tests/portomatic-generated/

## daqifi.firmware

- [~] `IntelHexParser.parse` — pure
- [~] `Crc16` — pure
- [~] `FirmwareVersion.parse` — pure
- [~] `BootloaderProtocol.create_*_message` / `decode_*_response` — pure
- [d] `Pic32BootloaderProtocol` against real bootloader (B13)
- [d] `FirmwareDownloadService.download_latest` — needs network + GitHub API
- [~] `LanChipInfo` — dataclass

## daqifi.exceptions / daqifi.enums / daqifi.version

- [~] `DAQiFiError` / `ConnectionError` / `DeviceNotFoundError` / `CommandError` / `ConfigurationError` / `FeatureNotSupportedError` / `PowerStateError` — exception types, no behaviour to test on hardware
- [~] `BoardVariant` / `WifiMode` / `WifiSecurityType` / `FirmwareUpdateState` / `ProtobufMessageType` / `SdCardLogFormat` / `ConnectionType` / `DeviceType` / `StreamState` — enums; pure
- [~] `FirmwareVersion.compare` / `FirmwareVersion.try_parse` / `FirmwareVersion.try_parse_lines` — pure parser; unit-tested
- [~] `compare` / `try_parse` / `try_parse_lines` — module-level convenience aliases of the above
- [~] `Sample` / `TimestampResult` / `InboundMessage` / `ObjectInboundMessage` / `DeviceInfo` (defined in both discovery.py and device_finders.py) / `DeviceDiscoveredEventArgs` / `ChannelsPopulatedEventArgs` / `DigitalChannel` / `AnalogChannel` — frozen dataclasses; pure
- [~] `IProtocolHandler.can_handle` / `IProtocolHandler.handle_async` — Protocol base; tested via `ProtobufProtocolHandler`
- [~] `ChannelCollection` / `ChannelCollection.channels` — generic base; concrete subclasses (AnalogInputs/Outputs/DigitalIO) are exercised under `daqifi.channels`
- [~] `FirmwareUpdateException` / `FirmwareUpdateProgress` / `FirmwareUpdateStateChangedEventArgs` / `FirmwareUpdateServiceOptions` / `FirmwareUpdateServiceOptions.get_state_timeout` / `FirmwareUpdateServiceOptions.validate` — pure config + events; unit-tested
- [~] `FirmwareReleaseInfo` / `FirmwareUpdateCheckResult` — release-info dataclasses; pure
- [~] `HexFileRecord` — single Intel HEX record; pure
- [~] `ExternalProcessRequest` / `ExternalProcessResult` / `ExternalProcessRunner.run` / `SubprocessRunner.run` — process-invocation abstraction; pure (mocked in unit tests)
- [d] `GitHubFirmwareDownloadService.check_for_update` / `download_firmware_by_tag` / `download_latest_firmware` / `download_wifi_firmware` / `get_latest_release` / `get_latest_wifi_release` / `invalidate_cache` — needs network + GitHub; gated, B13 firmware-update slice
- [~] `BootloaderProtocol.create_request_version_message` / `BootloaderProtocol.create_erase_flash_message` / `BootloaderProtocol.create_program_flash_message` / `BootloaderProtocol.create_jump_to_application_message` / `BootloaderProtocol.decode_version_response` / `BootloaderProtocol.decode_program_flash_response` / `BootloaderProtocol.decode_erase_flash_response` / `BootloaderProtocol.parse_hex_file` — abstract Protocol; concrete impl tested via `Pic32BootloaderProtocol`
- [~] `create_request_version_message` / `create_erase_flash_message` / `create_program_flash_message` / `create_jump_to_application_message` / `decode_version_response` / `decode_program_flash_response` / `decode_erase_flash_response` — module-level convenience functions in `daqifi.firmware.bootloader`; same logic as the Protocol methods above

---

## Slice ownership matrix

| Slice | Module focus | Approx symbols |
|---|---|---|
| **B1** | `transport.py` (SerialStreamTransport) | 9 |
| **B2** | `devices.py` (DaqifiDevice, Factory) | 8 |
| **B3** | `device_finders.py`, `discovery.py` | 6 |
| **B4** | `communication.py`, `streaming.py` (StreamManager) | 11 |
| **B5** | multi-channel decode (parser) — popcount invariant + decode-rate guard | 1 (typed factory) |
| **B6** | streaming lifecycle | 4 |
| **B7** | `channels.py` (AI) | 6 |
| **B8** | `channels.py` (DIO) | 4 |
| **B9** | `device.py` (NyquistDevice) | 14 |
| **B10** | SD read-only + network prep | 6 |
| **B11** | SD destructive | 2 (gated) |
| **B12** | network config | 5 |
| **B13** | firmware update | 1 (gated) |
| **B14** | async streaming | 4 |
| **B15** | SampleBuffer I/O | 5 |

---

## Per-symbol explicit listings

The earlier sections use the doc shorthand
``\`Foo.method_a\` / \`method_b\` / \`method_c\``. The coverage script
still enforces every symbol individually, so this section spells
out the remaining members the shorthand glossed over (added pass
9 after the script's class-name fallback was removed).

### Pure logic (no hardware needed)

- [~] `Crc16.crc` / `Crc16.high` / `Crc16.low` — CRC16 accumulator state; pure
- [~] `IntelHexParser.parse_hex_records` / `IntelHexParser.parse_records` — pure parser
- [~] `FirmwareVersion.is_pre_release` — pure property derived from the parsed version
- [~] `Pic32BootloaderProtocol.create_request_version_message` / `Pic32BootloaderProtocol.create_erase_flash_message` / `Pic32BootloaderProtocol.create_program_flash_message` / `Pic32BootloaderProtocol.create_jump_to_application_message` / `Pic32BootloaderProtocol.decode_version_response` / `Pic32BootloaderProtocol.decode_program_flash_response` / `Pic32BootloaderProtocol.decode_erase_flash_response` / `Pic32BootloaderProtocol.parse_hex_file` — pure framing helpers; tested as part of the bootloader unit tests
- [~] `ProtobufProtocolHandler.can_handle` / `ProtobufProtocolHandler.handle_async` — concrete impl of `IProtocolHandler`; routed via `DaqifiDevice.send` and exercised implicitly by every B2 hardware test
- [~] `SampleBuffer.to_dict` — pure data transform; covered alongside `to_csv` / `to_json` / `to_dataframe` in B15
- [~] `TimestampProcessor.process_timestamp` / `TimestampProcessor.reset` / `TimestampProcessor.reset_all` / `TimestampProcessor.tick_period` — pure rollover handler; unit-tested
- [~] `AnalogInputChannel.index` / `AnalogOutputChannel.index` / `DigitalIOChannel.index` — index property; pure
- [~] `NetworkConfigurable.network_configuration` — Protocol attribute; tested via concrete `DaqifiStreamingDevice` in B12

### Gated (destructive or external network)

- [d] `FirmwareDownloadService.check_for_update` / `FirmwareDownloadService.download_firmware_by_tag` / `FirmwareDownloadService.download_latest_firmware` / `FirmwareDownloadService.download_wifi_firmware` / `FirmwareDownloadService.get_latest_release` / `FirmwareDownloadService.get_latest_wifi_release` / `FirmwareDownloadService.invalidate_cache` — needs network + GitHub API; B13 firmware-update slice

### B2 — DaqifiDevice (round out the existing slice)

- [x] `DaqifiDevice.add_message_received` / `DaqifiDevice.remove_message_received` / `DaqifiDevice.add_status_changed` / `DaqifiDevice.remove_status_changed` / `DaqifiDevice.remove_channels_populated`
- [x] `DaqifiDevice.close` / `DaqifiDevice.is_connected` / `DaqifiDevice.name` / `DaqifiDevice.ip_address` / `DaqifiDevice.state` / `DaqifiDevice.status` / `DaqifiDevice.transport`

### B3 — discovery (round out)

- [x] `SerialDeviceFinder.close` / `SerialDeviceFinder.remove_device_discovered` / `SerialDeviceFinder.remove_discovery_completed`

### B4 — streaming (round out)

- [ ] `StreamManager.buffer` — exposed property; covered alongside the StreamManager block
- [x] `StreamMessageConsumer.is_running` / `StreamMessageConsumer.add_error_occurred` / `StreamMessageConsumer.remove_error_occurred` (B6)
- [x] `StreamMessageConsumer.close` / `StreamMessageConsumer.remove_message_received` (B4)

### B6 — streaming lifecycle (round out)

- [x] `DaqifiStreamingDevice.is_logging_to_sd_card` / `DaqifiStreamingDevice.sd_card_files` — properties readable via the streaming-device facade (B6)

### B10 — SD card (round out)

- [ ] `NyquistDevice.sd_get_file_size`
- [ ] `SdCardOperations.is_logging_to_sd_card` / `SdCardOperations.sd_card_files` / `SdCardOperations.get_sd_card_files` / `SdCardOperations.start_sd_card_logging` / `SdCardOperations.stop_sd_card_logging`

### B11 — SD card destructive (round out, gated)

- [d] `SdCardOperations.delete_sd_card_file` / `SdCardOperations.format_sd_card`

### Skipped (no test rig)

- [s] `TcpStreamTransport.add_status_changed` / `TcpStreamTransport.remove_status_changed` / `TcpStreamTransport.close` / `TcpStreamTransport.connection_info` / `TcpStreamTransport.disconnect` / `TcpStreamTransport.is_connected` / `TcpStreamTransport.stream` — no TCP-bridged Nyquist test rig
- [s] `HidLibraryTransport.connect` / `HidLibraryTransport.disconnect` / `HidLibraryTransport.close` / `HidLibraryTransport.read` / `HidLibraryTransport.write` / `HidLibraryTransport.is_connected` / `HidLibraryTransport.vendor_id` / `HidLibraryTransport.product_id` / `HidLibraryTransport.serial_number` / `HidLibraryTransport.device_path` / `HidLibraryTransport.read_timeout` / `HidLibraryTransport.write_timeout` — needs bootloader-mode HID device; B13 firmware-update only
- [s] `HidDeviceFinder.close` / `HidDeviceFinder.add_device_discovered` / `HidDeviceFinder.remove_device_discovered` / `HidDeviceFinder.add_discovery_completed` / `HidDeviceFinder.remove_discovery_completed` — paired with HidLibraryTransport above
- [x] `WiFiDeviceFinder.close` / `WiFiDeviceFinder.add_device_discovered` / `WiFiDeviceFinder.remove_device_discovered` / `WiFiDeviceFinder.add_discovery_completed` / `WiFiDeviceFinder.remove_discovery_completed` (B3 — WiFi-attached Nyquists confirmed on local LAN)
- [ ] `UdpTransport.open` / `UdpTransport.close` / `UdpTransport.is_open` / `UdpTransport.local_port` / `UdpTransport.connection_info` / `UdpTransport.send_broadcast_to` / `UdpTransport.send_unicast` / `UdpTransport.add_status_changed` / `UdpTransport.remove_status_changed` — exercised indirectly by `WiFiDeviceFinder.discover()` in B3 (when WiFi rig becomes available)

---

_Last updated: B7 — analog-input channels slice. Checked off
`AnalogInputs.__getitem__` / `__len__` / `__iter__` /
`configure_channels` / `enable_all` / `disable_all`, plus
`AnalogInputChannel.enabled` / `range` (get + set). Cross-checks
every per-channel ``enabled`` GETTER against the firmware-side
bitmask returned by ``ENABle:VOLTage:DC?`` after raw-SCPI
bitmask writes. NQ1 range setter raises
``FeatureNotSupportedError``._

_**Production bugs surfaced (logged for a follow-up production
PR; tests-only-PR rule keeps fixes out of B7):**_

_1. ``AnalogInputChannel.enabled`` SETTER sends malformed SCPI —_
   _``CONFigure:ADC:CHANnel <ch> <val>`` (space-separated)_
   _instead of the comma-separated form the firmware accepts._
   _The malformed command is silently dropped (no error queued)_
   _and ``SerialInterface.send_set`` independently returns False_
   _due to a SYST:ERR? response-buffering race, so the setter_
   _raises ``CommandError``. xfailed:_
   _``test_per_channel_enabled_setter_reflects_in_firmware_bitmask``,_
   _``test_enable_all_sets_full_bitmask``,_
   _``test_disable_all_clears_bitmask``,_
   _``test_configure_channels_enables_listed_channels_on_nq3``._

_2. ``AnalogInputs.configure_channels`` writes ``range`` BEFORE_
   _``enabled``, so on NQ1 it raises ``FeatureNotSupportedError``_
   _and never reaches the enabled write — fully unusable on NQ1._
   _Documented + asserted by_
   _``test_configure_channels_raises_on_nq1_due_to_range_setter``._
