# Security policy

## Supported versions

The project is pre-1.0. Only the latest release and `main` receive fixes.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Email **support@daqifi.com** with "SECURITY" in the subject line. Include
the affected version or commit, the steps to reproduce, and the impact you
expect. If GitHub private vulnerability reporting is enabled on this
repository, you may use that instead.

We aim to acknowledge reports within five business days, and we will keep
you informed until a fix ships.

## Scope

In scope: this library's handling of device traffic (USB CDC, TCP, UDP
discovery, USB HID firmware update) and of credentials it passes to devices.
Firmware vulnerabilities belong to
[daqifi-nyquist-firmware](https://github.com/daqifi/daqifi-nyquist-firmware);
report them through the same address.
