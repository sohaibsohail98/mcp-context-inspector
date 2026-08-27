#!/bin/sh
# Encodes one raw Playwright capture into the two shipped demo formats.
# Split out of the Makefile because both `make demo` (one take) and
# `make demo-candidates` (one per scripts/demo_capture.py EXPAND_VARIANTS
# entry) need the identical ffmpeg pass, and a shell function is far
# easier to get right here than a Make multi-line define expanded inside
# a for loop (tried that first, hit an unescaped semicolon).
#
# Usage: encode_demo_take.sh <raw_webm> <trim_start_seconds> <out_mp4> <out_gif> <palette_png>
set -eu

RAW_VIDEO="$1"
TRIM_START="$2"
OUT_MP4="$3"
OUT_GIF="$4"
PALETTE="$5"

mkdir -p "$(dirname "$OUT_MP4")" "$(dirname "$PALETTE")"

# Square, letterboxed onto the dashboard's own background colour rather
# than black bars, since LinkedIn favours square in the feed (see the brief).
ffmpeg -y -ss "$TRIM_START" -t 8 -i "$RAW_VIDEO" \
	-vf "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=0x17150f" \
	-c:v libx264 -pix_fmt yuv420p -movflags +faststart "$OUT_MP4"

# Two pass palette method (palettegen with stats_mode=diff, then
# paletteuse with dither=bayer:bayer_scale=3): a single pass produces a
# dithered mess (see the brief). -update 1 is needed for a modern
# ffmpeg's image2 muxer to accept a single-frame PNG output.
ffmpeg -y -ss "$TRIM_START" -t 8 -i "$RAW_VIDEO" \
	-vf "fps=15,scale=900:-1:flags=lanczos,palettegen=stats_mode=diff" -update 1 "$PALETTE"
ffmpeg -y -ss "$TRIM_START" -t 8 -i "$RAW_VIDEO" -i "$PALETTE" \
	-lavfi "fps=15,scale=900:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3" \
	-t 8 "$OUT_GIF"
