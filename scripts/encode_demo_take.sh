#!/bin/sh
# Encodes one raw Playwright capture (terminal act + dashboard act) into
# the two shipped demo formats. Split out of the Makefile because both
# `make demo` (one take) and `make demo-candidates` (one per
# scripts/demo_capture.py REVEAL_MODE_HOLD_MS entry) need the identical
# ffmpeg pass, and a shell function is far easier to get right here than
# a Make multi-line define expanded inside a for loop (tried that first,
# hit an unescaped semicolon).
#
# The two acts are genuinely two different Playwright pages now (a
# terminal mockup, then a real tab switch onto the dashboard), so this
# concatenates two clips rather than transforming one: the terminal clip
# played whole (it's already short and scripted to finish cleanly), the
# dashboard clip trimmed from TRIM_START and capped so the total lands
# in the ~10-15s target instead of running to whatever the reveal mode's
# hold time happens to add up to.
#
# Usage: encode_demo_take.sh <terminal_webm> <dashboard_webm> <trim_start_seconds> <dashboard_seconds> <out_mp4> <out_gif> <workdir>
set -eu

TERMINAL_VIDEO="$1"
DASHBOARD_VIDEO="$2"
TRIM_START="$3"
DASHBOARD_SECONDS="$4"
OUT_MP4="$5"
OUT_GIF="$6"
WORKDIR="$7"

mkdir -p "$(dirname "$OUT_MP4")" "$WORKDIR"

TERMINAL_NORM="$WORKDIR/terminal_norm.mp4"
DASHBOARD_NORM="$WORKDIR/dashboard_norm.mp4"
CONCAT_LIST="$WORKDIR/concat_list.txt"
PALETTE="$WORKDIR/palette.png"
JOINED="$WORKDIR/joined.mp4"

# Square, letterboxed onto the dashboard's own background colour rather
# than black bars, since LinkedIn favours square in the feed (see the
# brief). Both acts are normalised to the same square canvas/framerate
# before concatenation, otherwise ffmpeg's concat demuxer will refuse to
# join streams with mismatched parameters.
ffmpeg -y -i "$TERMINAL_VIDEO" \
	-vf "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=0x17150f,fps=30" \
	-c:v libx264 -pix_fmt yuv420p "$TERMINAL_NORM"

ffmpeg -y -ss "$TRIM_START" -t "$DASHBOARD_SECONDS" -i "$DASHBOARD_VIDEO" \
	-vf "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=0x17150f,fps=30" \
	-c:v libx264 -pix_fmt yuv420p "$DASHBOARD_NORM"

printf "file '%s'\nfile '%s'\n" "$(cd "$(dirname "$TERMINAL_NORM")" && pwd)/$(basename "$TERMINAL_NORM")" \
	"$(cd "$(dirname "$DASHBOARD_NORM")" && pwd)/$(basename "$DASHBOARD_NORM")" > "$CONCAT_LIST"

ffmpeg -y -f concat -safe 0 -i "$CONCAT_LIST" -c copy "$JOINED"
cp "$JOINED" "$OUT_MP4"

# Two pass palette method (palettegen with stats_mode=diff, then
# paletteuse with dither=bayer:bayer_scale=3): a single pass produces a
# dithered mess (see the brief). -update 1 is needed for a modern
# ffmpeg's image2 muxer to accept a single-frame PNG output.
ffmpeg -y -i "$JOINED" -vf "fps=15,scale=900:-1:flags=lanczos,palettegen=stats_mode=diff" -update 1 "$PALETTE"
ffmpeg -y -i "$JOINED" -i "$PALETTE" \
	-lavfi "fps=15,scale=900:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3" \
	"$OUT_GIF"
