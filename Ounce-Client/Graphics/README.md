# Graphics

**Every image in this folder is AI-generated.** This is a one-person hobby
project and there was no budget for an artist. If you are an artist and want to
replace any of it, open an issue — the real thing would be better and it would
be credited properly.

| File | Used for |
| --- | --- |
| `logo.png` | Wordmark on its own, transparent |
| `icon.png` | Wordmark + character, square, transparent — source of the app icon |
| `library.png` | 600×900 portrait for Steam's library / grid art — opaque, since Steam renders a transparent tile black |
| `banner.jpg` | 1024×512 wide header, top of the README |
| `girl.jpg` | The character on her own, opaque background |

Nothing here is load-bearing — the firmware and the client run the same without
it.

## App icon

`..\ounce.ico` (exe icon) and `..\ounce_icon.png` (pygame window icon) are both
derived from `icon.png`. The `.ico` carries **two different crops**, because
"Ounce" is a smear below 48 px but the mark is worth having where it fits:

| Frame | Art | Where Windows uses it |
| --- | --- | --- |
| 16–40 px | Character only | Taskbar, title bar, list and detail views |
| 48–256 px | Full mark, wordmark included | Large/extra-large icons, the properties dialog, Steam's tile |

`ounce_icon.png` is character-only at every size: pygame hands SDL one surface
for both the title bar and the taskbar, and both are small.

Run this from `Ounce-Client\` after changing `icon.png`, then `build_exe.bat`:

```python
from PIL import Image

full = Image.open("Graphics/icon.png").convert("RGBA")
char = full.crop((285, 110, 835, 660))
SMALL, LARGE = [16, 20, 24, 32, 40], [48, 64, 128, 256]

char.resize((256, 256), Image.LANCZOS).save("ounce_icon.png")

frames = {s: char.resize((s, s), Image.LANCZOS) for s in SMALL}
frames.update({s: full.resize((s, s), Image.LANCZOS) for s in LARGE})
frames[256].save("ounce.ico", sizes=[(s, s) for s in SMALL + LARGE],
                 append_images=[f for s, f in sorted(frames.items()) if s != 256])
```

Pillow uses an exact size match from `append_images` when it finds one and
resizes the base image otherwise, which is what lets one `.ico` hold both crops.
