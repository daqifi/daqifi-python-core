# Backing up and restoring ADC calibration across a firmware update

On a device whose bootloader predates
[daqifi/daqifi-nyquist-firmware#1141](https://github.com/daqifi/daqifi-nyquist-firmware/pull/1141),
a firmware update erases **all** of program flash. That includes the pages
holding the factory and user ADC calibration, and the WiFi settings (see
Known issues in the CHANGELOG). Back the calibration up before updating and
restore it afterwards.

Everything below is plain SCPI, sent from a serial terminal to the device's
USB serial port. python-core has no calibration API.

## What you need to know first

**There is only ONE live set of coefficients.** `CHANCALM`/`CHANCALB` edit it,
`LOADcal`/`LOADFcal` overwrite it from the user/factory bank, and
`SAVEcal`/`SAVEFcal` copy it to the user/factory bank. So each bank must be
written and then saved **before** the other bank is touched. Setting values
once and then calling both saves writes the *same* numbers into both banks.

**The channel IDs are not contiguous, and the device does not list them.**
Both variants carry eight internal monitoring channels (3.3 V, 5 V, VREF and
so on) at IDs **248–255**, in addition to their user inputs:

| variant | user inputs | monitoring | entries |
|---|---|---|---|
| NQ1 | 0–15 | 248–255 | 24 |
| NQ3 | 0–7 | 248–255 | 16 |

`CONF:CAP:JSON?` reports only the user inputs, so the monitoring IDs cannot be
discovered from the device. A loop over 0–23 is wrong twice over: it misses
every monitoring channel and asks for eight IDs that do not exist.

**Check the error queue after every load and save.** A load that fails leaves
the live coefficients *unchanged*, so the reads that follow would silently
report the bank you did not ask for.

**One state needs a reboot instead of `USECal`.** If the selection you record
is **0** (factory) and the device has **no factory bank**, `CONF:ADC:USECal 0`
is refused: there is no factory bank to load. Such a device runs on the
coefficients it starts with when no factory bank is stored, which are
identity. In that case, wherever a step below says
`CONF:ADC:USECal <the value you recorded>` and then `SYST:ERR?`, send
`SYST:REboot` instead. On restart the device loads its selected bank, finds
none, and is back on those coefficients. Without the reboot, the last bank a
step loaded stays in use.

**Nothing may be streaming.** These commands are refused during acquisition.
`SYST:STR:STOP` is safe to send when nothing is running; it leaves the error
queue clean.

**No power-state change is needed.** After an update the device starts in
standby, and every command below works there.

**`SYST:REboot` drops the serial port.** The device restarts and its USB
serial port goes away and comes back a few seconds later. A terminal or
script that held the port open will report an error at that moment; reopen
the port.

## Back up

    SYST:STR:STOP
    SYST:ERR?                     -> 0,"No error"

    CONF:ADC:USECal?

If that answers **2**, the device is in raw-code output mode, which is a
runtime-only setting and tells you nothing about the stored selection:

    SYST:REboot
    CONF:ADC:USECal?              -> now 0 (factory) or 1 (user); record it

Record the factory bank:

    CONF:ADC:LOADFcal
    SYST:ERR?
      0,"No error"  -> a factory bank is stored; read it below.
      an error      -> no factory bank is stored. That is a valid state on an
                       uncalibrated device. Record "no factory bank" and skip
                       the reads. Do NOT record the numbers you can still
                       read: the failed load left the previous coefficients
                       live, so they belong to some other bank.
    CONF:ADC:CHANCALM? 0
    CONF:ADC:CHANCALB? 0
    ... repeat for every ID of your variant, e.g. on NQ1:
        0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 248 249 250 251 252 253 254 255

Record the user bank the same way:

    CONF:ADC:LOADcal
    SYST:ERR?
      0,"No error"  -> a user bank is stored; read it below.
      an error      -> no user bank is stored, which is normal if none was
                       ever saved. Record "no user bank" and skip the reads,
                       for the same reason as above.
    CONF:ADC:CHANCALM? 0
    CONF:ADC:CHANCALB? 0
    ... repeat for the same IDs

Put the selection back; this reloads that bank into the live coefficients
(selection 0 with no factory bank: `SYST:REboot` instead, see above):

    CONF:ADC:USECal <the value you recorded>
    SYST:ERR?                     -> 0,"No error"

Keep the numbers exactly as printed. Do not round them.

## Restore

    SYST:STR:STOP
    SYST:ERR?                     -> 0,"No error"

Factory bank first, if the backup recorded one, and saved before any user
value is written. If the backup recorded "no factory bank", skip this whole
block; writing values into a bank the device never had is not a restore:

    CONF:ADC:CHANCALM 0,<factory M for 0>
    CONF:ADC:CHANCALB 0,<factory B for 0>
    ... every ID
    CONF:ADC:SAVEFcal
    SYST:ERR?                     -> 0,"No error"

Then the user bank, if the backup recorded one:

    CONF:ADC:CHANCALM 0,<user M for 0>
    CONF:ADC:CHANCALB 0,<user B for 0>
    ... every ID
    CONF:ADC:SAVEcal
    SYST:ERR?                     -> 0,"No error"

Only now choose the bank. `USECal` refuses a bank it cannot load, so it has to
come after the banks are populated (selection 0 with no factory bank:
`SYST:REboot` instead, see above):

    CONF:ADC:USECal <the value you recorded>
    SYST:ERR?                     -> 0,"No error"

## Check it

If the backup recorded a factory bank:

    CONF:CAP:JSON?                -> identity.cal.factory_present is true

(On a device that never had one, this stays false and that is correct.)

Compare each bank the backup recorded. Skip the block for a bank it did not:

    CONF:ADC:LOADFcal
    SYST:ERR?                     -> 0,"No error"
    CONF:ADC:CHANCALM? 0
    CONF:ADC:CHANCALB? 0
    ... every ID; compare BOTH values against the factory set

    CONF:ADC:LOADcal
    SYST:ERR?                     -> 0,"No error"
    CONF:ADC:CHANCALM? 0
    CONF:ADC:CHANCALB? 0
    ... every ID; compare BOTH values against the user set

    CONF:ADC:USECal <recorded>
    SYST:ERR?                     -> 0,"No error"

    SYST:REboot

Then repeat the whole block above (both loads, both error checks, both
comparisons on every ID) **and the closing two lines**:

    CONF:ADC:USECal <recorded>
    SYST:ERR?                     -> 0,"No error"

Do not leave those off. Each comparison LOADS a bank into the live
coefficients, so a check that ends on `LOADcal` leaves the user bank live no
matter which bank is selected, until something reboots the device. The
closing `USECal` puts the selected bank back in use. For selection 0 with no
factory bank, both `USECal` pairs in this section are a `SYST:REboot`
instead, as described at the top.

Compare values as numbers, not as text. The device prints the shortest form
that reads back to the same double, which need not be the digits you typed:
a value written with 17 significant digits can come back with a different
last digit and still be exactly the same double.

**`CONF:ADC:USECal?` on its own does not prove the restore worked.** It reports
the stored selection, which can be right while the bank behind it is not; the
comparisons above are what settle it.

## A note on older firmware

Firmware older than 3.9.0 printed doubles with 15 significant digits. A
double needs up to 17 to be reproduced exactly, so a backup taken from such a
device can be low in the 16th digit: a relative error near 1e-15, far below
ADC resolution and not worth correcting. 3.9.0 and later print the shortest
form that reads back exactly, so a backup taken from those is lossless.

## How this procedure was tested

On an NQ1 with a bootloader reporting version 1.4, distinct non-identity
values were written to all 24 entries of both banks, with the user bank
selected. The backup above was taken on firmware 3.9.0, and the device was
then updated to 3.9.1 with `update_firmware`. After the update neither bank
could be loaded, every coefficient read as identity, and the selection was
back at its default. The restore above then brought back every value in both
banks exactly, including a 17-significant-digit value, and the result
survived a reboot. The NQ3 channel list comes from the firmware source; the
procedure has not been run on an NQ3. Neither has the reboot path for
selection 0 without a factory bank: it follows from how the firmware loads
calibration at startup.
