# Graphics

**Every image in this folder is AI-generated.** This is a one-person hobby
project and there was no budget for an artist. If you are an artist and want to
replace any of it, open an issue — the real thing would be better and it would
be credited properly.

| File | Used for |
| --- | --- |
| `logo.png` | Wordmark on its own, transparent |
| `icon.png` | Wordmark + character, square, transparent — source of the app icon |
| `library.png` | 600×900 portrait, for Steam's library / grid art |
| `banner.jpg` | 1024×512 wide header, top of the README |
| `girl.jpg` | The character on her own, opaque background |

Nothing here is load-bearing — the firmware and the client run the same without
it.

## App icon

`..\ounce.ico` (exe icon) and `..\ounce_icon.png` (pygame window icon) are both
derived from `icon.png`, cropped to head-and-shoulders because the wordmark is
unreadable below 48 px. From `Ounce-Client\`:

```python
from PIL import Image
crop = Image.open("Graphics/icon.png").convert("RGBA").crop((285, 110, 835, 660))
crop.resize((256, 256), Image.LANCZOS).save("ounce_icon.png")
crop.save("ounce.ico",
          sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
                 (48, 48), (64, 64), (128, 128), (256, 256)])
```

Re-run that after changing `icon.png`, then `build_exe.bat`.
