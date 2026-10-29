"""Tests for the ScpiMessageProducer port (now a module of factories
in daqifi/_internal/scpi_commands.py). Upstream snapshot e8d84af."""

import pytest

from daqifi._internal import scpi_commands
from daqifi._internal.comm import ScpiMessage
from daqifi._internal.scpi import StreamInterface


pytestmark = pytest.mark.portomatic_generated


class TestSimpleCommands:
    @pytest.mark.parametrize(
        "factory, expected",
        [
            (scpi_commands.get_idn, "*IDN?"),
            (scpi_commands.reset_device, "*RST"),
            (scpi_commands.get_last_error, "SYSTem:ERRor?"),
            (scpi_commands.get_system_info, "SYSTem:INFO?"),
            (scpi_commands.reboot_device, "SYSTem:REboot"),
            (scpi_commands.get_device_info, "SYSTem:SYSInfoPB?"),
            (scpi_commands.force_bootloader, "SYSTem:FORceBoot"),
            (scpi_commands.get_power_state, "SYSTem:POWer:STATe?"),
            (scpi_commands.turn_device_on, "SYSTem:POWer:STATe 1"),
            (scpi_commands.disable_device_echo, "SYSTem:ECHO -1"),
            (scpi_commands.enable_device_echo, "SYSTem:ECHO 1"),
            (scpi_commands.enable_storage_sd, "SYSTem:STORage:SD:ENAble 1"),
            (scpi_commands.disable_storage_sd, "SYSTem:STORage:SD:ENAble 0"),
            (scpi_commands.get_sd_file_list, "SYSTem:STORage:SD:LIST?"),
            (scpi_commands.format_sd_card, "SYSTem:STORage:SD:FORmat"),
            (scpi_commands.get_sd_max_file_size, "SYSTem:STORage:SD:MAXSize?"),
            (scpi_commands.get_sd_benchmark_results, "SYSTem:STORage:SD:BENCHmark?"),
            (scpi_commands.get_stream_interface, "SYSTem:STReam:INTerface?"),
            (scpi_commands.stop_streaming, "SYSTem:STReam:STOP"),
            (scpi_commands.set_protobuf_stream_format, "SYSTem:STReam:FORmat 0"),
            (scpi_commands.set_json_stream_format, "SYSTem:STReam:FORmat 1"),
            (scpi_commands.set_csv_stream_format, "SYSTem:STReam:FORmat 2"),
            (scpi_commands.get_stream_format, "SYSTem:STReam:FORmat?"),
            (scpi_commands.enable_dio_ports, "DIO:PORt:ENAble 1"),
            (scpi_commands.disable_dio_ports, "DIO:PORt:ENAble 0"),
            (scpi_commands.set_network_wifi_mode_self_hosted, "SYSTem:COMMunicate:LAN:NETType 4"),
            (scpi_commands.set_network_wifi_mode_existing, "SYSTem:COMMunicate:LAN:NETType 1"),
            (scpi_commands.set_network_wifi_security_open, "SYSTem:COMMunicate:LAN:SECurity 0"),
            (scpi_commands.set_network_wifi_security_wpa, "SYSTem:COMMunicate:LAN:SECurity 3"),
            (scpi_commands.disable_network_lan, "SYSTem:COMMunicate:LAN:ENAbled 0"),
            (scpi_commands.enable_network_lan, "SYSTem:COMMunicate:LAN:ENAbled 1"),
            (scpi_commands.apply_network_lan, "SYSTem:COMMunicate:LAN:APPLY"),
            (scpi_commands.save_network_lan, "SYSTem:COMMunicate:LAN:SAVE"),
            (scpi_commands.set_lan_firmware_update_mode, "SYSTem:COMMUnicate:LAN:FWUpdate"),
            (scpi_commands.get_lan_chip_info, "SYSTem:COMMunicate:LAN:GETChipInfo?"),
        ],
    )
    def test_factory_emits_expected_command(self, factory, expected):
        msg = factory()
        assert isinstance(msg, ScpiMessage)
        assert msg.data == expected

    def test_factory_returns_fresh_instance(self):
        # Mirrors C# ``=> new()`` semantics: each call yields a new ScpiMessage.
        a = scpi_commands.reboot_device()
        b = scpi_commands.reboot_device()
        assert a is not b


class TestParameterizedCommands:
    def test_get_sd_file(self):
        assert scpi_commands.get_sd_file("data.bin").data == 'SYSTem:STORage:SD:GET "data.bin"'

    def test_set_sd_logging_file_name(self):
        # Firmware #311 rename: command updated to SD:FILE (was SD:LOGging).
        assert (
            scpi_commands.set_sd_logging_file_name("foo.bin").data
            == 'SYSTem:STORage:SD:FILE "foo.bin"'
        )

    def test_delete_sd_file(self):
        assert scpi_commands.delete_sd_file("x.bin").data == 'SYSTem:STORage:SD:DELete "x.bin"'

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_delete_sd_file_rejects_empty(self, bad):
        with pytest.raises(ValueError, match="null or empty"):
            scpi_commands.delete_sd_file(bad)

    def test_set_sd_max_file_size(self):
        assert scpi_commands.set_sd_max_file_size(0).data == "SYSTem:STORage:SD:MAXSize 0"
        assert scpi_commands.set_sd_max_file_size(2048).data == "SYSTem:STORage:SD:MAXSize 2048"

    def test_set_sd_max_file_size_rejects_negative(self):
        with pytest.raises(ValueError, match="negative"):
            scpi_commands.set_sd_max_file_size(-1)

    def test_run_sd_benchmark(self):
        assert scpi_commands.run_sd_benchmark(1024).data == "SYSTem:STORage:SD:BENCHmark 1024"

    def test_set_power_state(self):
        assert scpi_commands.set_power_state(0).data == "SYSTem:POWer:STATe 0"
        assert scpi_commands.set_power_state(1).data == "SYSTem:POWer:STATe 1"
        assert scpi_commands.set_power_state(2).data == "SYSTem:POWer:STATe 2"

    @pytest.mark.parametrize("bad", [True, False, 1.0, "1", None])
    def test_set_power_state_rejects_non_int(self, bad):
        with pytest.raises(TypeError, match="state must be an int"):
            scpi_commands.set_power_state(bad)  # type: ignore[arg-type]

    def test_set_power_state_rejects_negative(self):
        with pytest.raises(ValueError, match="non-negative"):
            scpi_commands.set_power_state(-1)

    def test_get_sd_file_list_for_directory(self):
        assert (
            scpi_commands.get_sd_file_list_for_directory("logs").data
            == 'SYSTem:STORage:SD:LIST? "logs"'
        )

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_get_sd_file_list_for_directory_rejects_empty(self, bad):
        with pytest.raises(ValueError, match="null or empty"):
            scpi_commands.get_sd_file_list_for_directory(bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_run_sd_benchmark_rejects_non_positive(self, bad):
        with pytest.raises(ValueError, match="positive"):
            scpi_commands.run_sd_benchmark(bad)

    def test_set_stream_interface_emits_int(self):
        assert (
            scpi_commands.set_stream_interface(StreamInterface.SdCard).data
            == "SYSTem:STReam:INTerface 2"
        )

    def test_set_stream_interface_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown stream interface"):
            scpi_commands.set_stream_interface(99)  # type: ignore[arg-type]

    def test_start_streaming(self):
        assert scpi_commands.start_streaming(100).data == "SYSTem:STReam:START 100"

    def test_enable_adc_channels(self):
        assert scpi_commands.enable_adc_channels("84").data == "ENAble:VOLTage:DC 84"

    def test_set_dio_port_direction(self):
        assert scpi_commands.set_dio_port_direction(1, 1).data == "DIO:PORt:DIRection 1,1"

    def test_set_dio_port_state(self):
        # Qodo PR 17 pass 7 #1: value must be int or bool, 0 or 1.
        # Floats and out-of-range ints are rejected so the firmware
        # never sees a non-binary level.
        assert scpi_commands.set_dio_port_state(2, 1).data == "DIO:PORt:STATe 2,1"
        assert scpi_commands.set_dio_port_state(3, 0).data == "DIO:PORt:STATe 3,0"
        assert scpi_commands.set_dio_port_state(4, True).data == "DIO:PORt:STATe 4,1"
        assert scpi_commands.set_dio_port_state(5, False).data == "DIO:PORt:STATe 5,0"
        with pytest.raises(ValueError):
            scpi_commands.set_dio_port_state(2, 1.0)
        with pytest.raises(ValueError):
            scpi_commands.set_dio_port_state(2, 2)

    def test_set_network_wifi_ssid(self):
        assert (
            scpi_commands.set_network_wifi_ssid("MyNet").data
            == 'SYSTem:COMMunicate:LAN:SSID "MyNet"'
        )

    def test_set_network_wifi_password(self):
        assert (
            scpi_commands.set_network_wifi_password("p@ss").data
            == 'SYSTem:COMMunicate:LAN:PASs "p@ss"'
        )

    def test_set_usb_transparency_mode(self):
        assert (
            scpi_commands.set_usb_transparency_mode(1).data
            == "SYSTem:USB:SetTransparentMode 1"
        )
