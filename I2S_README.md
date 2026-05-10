## Hardware required

- A Tildagon badge with WiFi configured.
- A custom firmware build that includes the `mp3` user C module
  (see `drivers/mp3/` in the firmware tree).
- A **PCM5102A** I²S DAC board (e.g. the cheap purple Aliexpress board, or the
  Adafruit "I2S Stereo Decoder" #3678).
- Headphones / amplified speakers.
- A breakout for the Tildagon hexpansion port. Either:
  - Jake Walker's [Protoboard Hexpansion][proto] (recommended), or
  - any breakout exposing the 4 high-speed pins of an SFP-style hexpansion
    connector.

[proto]: https://www.tindie.com/products/jakew/protoboard-hexpansion/

## Which hexpansion port?

Use **port 2** (or any port whose HS pins are not already in use). On the
Tildagon, port 2's four high-speed pins map to:

| Hexpansion HS | ESP32-S3 GPIO |
| ---           | ---           |
| HS1           | 35            |
| HS2           | 36            |
| HS3           | 37            |
| HS4           | 38            |

If you use a different port, edit `PIN_BCK` / `PIN_LCK` / `PIN_DIN` at the top
of `app.py` to match — the GPIO numbers per port are listed in the
[Tildagon hardware docs](https://tildagon.badge.emfcamp.org/tildagon-apps/reference/badge-hardware/).

## Wiring

| PCM5102A pin | Connect to             | SFP pin | GPIO |
| ---          | ---                    | ---     | ---  |
| **VIN**      | hexpansion **+3V3**    | 15 / 16 | —    |
| **GND**      | hexpansion **GND**     | any GND | —    |
| **BCK**      | hexpansion **HS1**     | 12      | 35   |
| **LCK** (LRCK / WS) | hexpansion **HS2**| 13     | 36   |
| **DIN**      | hexpansion **HS3**     | 18      | 37   |
| **SCK**      | **GND**                | —       | —    |
| **FMT**      | leave / **GND**        | —       | —    |
| **XSMT**     | tie to **+3V3**        | —       | —    |

**SCK to GND** is the critical one. The PCM5102A has an internal PLL that
will recover the master clock from BCK *only* if SCK is grounded. If left
floating you get silence or noise. (Some boards have a solder jumper labelled
`SCK` on the back instead of a pin — short it to GND.)

`XSMT` (soft-mute) must be high for the DAC to output. Many breakout boards
already pull it up; check yours.

If your board has `H1L`, `H2L`, `H3L`, `H4L` jumpers, leave them in their
default positions for 16-bit / I²S format.

### Hexpansion SFP pinout (reference)

Numbering matches the [Tildagon hexpansion spec][spec]:

```
Bot side                    Top side
 1  GND                     11  GND
 2  Low speed 1             12  High speed 1   <-- BCK
 3  Low speed 2             13  High speed 2   <-- LCK
 4  I2C SDA                 14  GND
 5  I2C SCL                 15  +3V3           <-- VIN
 6  Detect                  16  +3V3
 7  Low speed 3             17  GND            <-- GND, SCK
 8  Low speed 4             18  High speed 3   <-- DIN
 9  Low speed 5             19  High speed 4
10  GND                     20  GND
```

`Detect` (pin 6) must be tied to `GND` on the hexpansion side or the badge
will leave the +3V3 rail unpowered. The Protoboard Hexpansion handles this
automatically.

[spec]: https://tildagon.badge.emfcamp.org/hexpansions/creating-hexpansions/

## Quick verification

After wiring, before running the radio, sanity-check I²S with a sine wave:

```python
import math
from machine import I2S, Pin

SR = 44100; F = 441
n = SR // F
buf = bytearray(n * 4)  # 16-bit stereo
for i in range(n):
    s = int(math.sin(2 * math.pi * i / n) * 16000)
    buf[i*4:i*4+2] = s.to_bytes(2, 'little', True)
    buf[i*4+2:i*4+4] = s.to_bytes(2, 'little', True)

i2s = I2S(0, sck=Pin(35), ws=Pin(36), sd=Pin(37),
          mode=I2S.TX, bits=16, format=I2S.STEREO,
          rate=SR, ibuf=20000)
for _ in range(200):
    i2s.write(buf)
i2s.deinit()
```

Should produce a clean 441 Hz tone in both channels.