# Fonts for non-Latin PDF output

Nandaka's PDF writer is **stdlib-only**: it embeds at most one TrueType font
for non-Latin runs (e.g. Telugu) and uses built-in Helvetica for Latin text
(WinAnsiEncoding). No font is bundled in this repository.

## Search order (`NANDAKA_FONT` → bundled → system)

1. `$NANDAKA_FONT` — an explicit `.ttf` path (highest priority).
2. `nandaka/assets/fonts/*.ttf` — drop a font here to ship it with your
   install. Files here are git-ignored (see `.gitignore`); only this README
   is tracked.
3. `/usr/share/fonts/**/*.ttf` (Linux)
4. `/system/fonts/**/*.ttf` (Android/Termux — where `NotoSansTelugu*.ttf` lives)
5. `$PREFIX/share/fonts/**/*.ttf` (Termux package fonts)
6. `~/fonts/**/*.ttf`

The first font whose `cmap` covers **every** non-Latin character in the
document wins.

## Telugu

On Termux the system already provides
`/system/fonts/NotoSansTelugu-VF.ttf`, which covers Telugu but **not** Latin —
that is expected: Latin always uses Helvetica, Telugu uses the embedded TTF.

On a headless Linux box with no fonts installed, run:

```bash
# Debian/Ubuntu
sudo apt-get install fonts-noto-core fonts-telu
# or pin a single font explicitly
export NANDAKA_FONT=/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf
```

## Disclosure behaviour

If no covering font is found, Nandaka **does not fail and does not silently
drop text**: the PDF is still produced, and a warning is added to the output
manifest, e.g.

```
non-Latin text present but no covering TTF found; those glyphs are replaced
by a placeholder — install a font or set NANDAKA_FONT
```

`nandaka doctor` reports the current state under `fonts`:

```json
"fonts": {
  "candidates": 12,
  "selected": "/system/fonts/NotoSansTelugu-VF.ttf",
  "telugu_capable": true,
  "latin_fallback": "Helvetica (built in; always available)"
}
```
