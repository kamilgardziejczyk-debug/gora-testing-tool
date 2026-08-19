# Analysis

2026-08-19 11:56:48 · claude-opus-5 · effort high


## Session gaps

Evidence: `results/tracker_20260819_092937.device.log`, `results/sd/session_117/tekpadz_adc.csv`, `results/sd/session_117/tekpadz_gnss.csv`, `results/sd/session_117/tekpadz_imu.csv`, `results/tracker_20260819_092937.combined.log`

# 1. Device run time

There are two separate power-on runs in this capture, so "device run time" has to be split.

**Run 1** — first boot at `[09:29:57.826] ESP-ROM:esp32s3-20210327` (device.log). The last uptime marker for this run is:

```
[09:30:28.446] I (09:30:28.115) POWER_SOURCE_TASK: Power diag: usb_pin=1 charger=1 fsm=USB uptime_ms=30228
```

with the last line of that run at `[09:30:29.842] ... Host ejected drive. Waiting for physical disconnect to shutdown USB storage.` The device is then cut at `[09:30:29.835] tool | INFO tools.relay_board.board: Relay 1 (BCM 5) de-energized` (combined.log, CMD 12/25). So run 1 lasted roughly **32 s** (≈30.2 s at the last diag line, ~09:29:57.8 → 09:30:29.8).

**Run 2** — second boot at `[09:30:37.004] ESP-ROM:esp32s3-20210327`. Its highest recorded uptime is:

```
[09:32:22.649] I (09:32:22.341) POWER_SOURCE_TASK: Power diag: usb_pin=1 charger=0 fsm=USB uptime_ms=105242
```

and the log continues to `[09:32:27.346] ... RTC and system time updated with new time.`, with power removed at `[09:32:27.438] --- CMD 25/25 START: Turn off the device (!RelayControl)`. So run 2 lasted **≈110 s** (105.2 s at the last diag, ~09:30:37.0 → 09:32:27.4).

Note the firmware's own uptime counter is not continuous with the log clock in run 2: it reports `uptime_ms=75242` at 09:31:52 and jumps in step with wall time thereafter, but restarts from 5228 ms after the reboot — consistent with two independent boots rather than one long run.

For context, the whole scenario (harness time, not device power-on time) was `[09:32:28.262] --- SCENARIO END: 25/25 passed in 170.92s ---`.

# 2. Did the device restart during the test?

Yes — exactly one restart, and it was deliberate, driven by the test harness rather than a fault.

Evidence in combined.log:

```
[09:30:29.833] --- CMD 12/25 START: Turn off the device (!RelayControl) [Tracker Mode] ---
[09:30:29.835] tool | INFO tools.relay_board.board: Relay 1 (BCM 5) de-energized
...
[09:30:36.772] --- CMD 14/25 START: Turn on the device (!RelayControl) [Tracker Mode] ---
[09:30:36.772] tool | INFO tools.relay_board.board: Relay 1 (BCM 5) energized
```

and the resulting second boot banner in device.log:

```
[09:30:37.008] rst:0x1 (POWERON),boot:0x2b (SPI_FAST_FLASH_BOOT)
[09:30:37.452] W (00:00:00.149) APP_MAIN: Boot reset reason: POWERON (1)
```

Both boots report `rst:0x1 (POWERON)` / `Boot reset reason: POWERON (1)` — no panic, watchdog or brownout reset appears anywhere in the log. The two boots differ only in power context, which changes the mode taken:

- Boot 1: `W (00:00:00.156) APP_MAIN: Boot power context: usb_connected=1, charger_status=1` → `APP_FSM: FSM: init: APP_USB_STORAGE`
- Boot 2: `W (00:00:00.155) APP_MAIN: Boot power context: usb_connected=0, charger_status=1` → `APP_FSM: FSM: init: APP_RECORDING`, `Session incremented: 116 -> 117`

There is no third boot banner; the later session change to 118 is a mode transition, not a reboot (`APP_FSM: FSM: transition: APP_WAIT_USB_UNMOUNT -> APP_RECORDING`, `Session incremented: 117 -> 118` at 09:32:25).

# 3. Gaps in ADC, IMU and GNSS readings

The three CSVs pulled off the card are from `session_117` (`[09:31:55.134] tool | INFO tools.mass_storage.files: Copied /session_117 off the card (1/1)`), i.e. the recording run of boot 2 only.

**ADC (`tekpadz_adc.csv`)** — no gaps. Samples run from `0,2279,...` to `1787131909,71901,...` with a Systick step of consistently 10–11 ms throughout; I found no interval materially larger than that. The last row is the only one with a real timestamp; every earlier row has `Timestamp=0`.

**IMU (`tekpadz_imu.csv`)** — nominal spacing is ~79–80 ms, but there are several rows where the step is roughly double, i.e. one sample appears to have been dropped:

| previous Systick | next Systick | step (ms) |
|---|---|---|
| 3386 | 3513 | 127 |
| 29216 | 29329 | 113 |
| 41102 | 41195 | 93 |
| 48550 | 48634 | 84 (then 48634→48708 = 74) |
| 53541 | 53657 | 116 |
| 57186 | 57275 | 89 |
| 63605 | 63709 | 104 |
| 66219 | 66320 | 101 |

The clearest are `0,3386,...` → `0,3513,...` (127 ms) and `0,53541,...` → `0,53657,...` (116 ms); note that in both cases the following step is unusually short (3513→3544 = 31 ms; 53657→53700 = 43 ms), which looks like a delayed write followed by a catch-up sample rather than lost time. Nothing in the device log names an IMU write error, so I can only report the timing irregularity, not its cause.

**GNSS (`tekpadz_gnss.csv`)** — effectively empty of content: a header plus a single row.

```
Timestamp,Systick,Latitude,Longitude
0,71856,+54.3502693,+18.4980806
```

That one row matches the single fix logged for session 117:

```
[09:31:49.266] I (09:31:48.745) GNSS_TASK: GNSS Sample: Timestamp=0 s, Systick=71856, Latitude=+54.3502693, Longitude=+18.4980806
[09:31:49.281] I (09:31:48.746) GNSS_TASK: GNSS FIX ACQUIRED with 5 satellites
```

This is not a data-loss gap so much as a consequence of test sequencing: the GNSS file was created at the moment of first fix (`I (09:31:48.764) SD_CARD_TASK: Created log file: /sdcard/session_117/tekpadz_gnss.csv`) and ~0.3 s later the harness connected storage USB, which pushed the app out of recording (`I (09:31:49.041) APP_FSM: FSM: transition: APP_RECORDING -> APP_WAIT_SD_UNMOUNT`, `GNSS: GNSS_ACTIVE -> GNSS_INACTIVE`). So GNSS logging had only a single sample's worth of life. The device spent ~71 s in `GNSS_WAITING_FIX` before that (fix at uptime 71.9 s), which explains why the ADC/IMU files are long and the GNSS file is one line.

Two further observations worth flagging, both timestamp-related rather than gaps:

- Almost every row in all three files carries `Timestamp=0`; only the final row of the ADC and IMU files has a real epoch value (`1787131909`). Time was only marked valid at `I (09:31:49.000) RTC_TASK: System time marked valid after GNSS sync.`, i.e. at the very end of the session, so the bulk of the data has no absolute time reference.
- The ADC and IMU final rows both show `1787131909`, while the later session-118 fix in the log shows `Timestamp=1787131946`. These are self-consistent; I have no data to check session 118's CSVs because they were never copied off the card.

Caveat: session 118's files (`/sdcard/session_118/tekpadz_imu.csv`, `_adc.csv`, `_gnss.csv`, created 09:32:25–09:32:27) exist on the device per the log but are not among the attached artefacts, so nothing can be said about gaps there.
