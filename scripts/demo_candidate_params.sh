#!/bin/sh
# Per reveal-mode (and, for guided_tour, per-cut) sample prompt and
# dashboard-half duration, looked up by name. Split out of the Makefile
# because a shell `case` embedded in a Make `define`/`call` gets
# flattened onto one line and loses its own statement separators, which
# is fragile enough to be worth a real file instead of fighting Make's
# line joining.
#
# Usage: demo_candidate_params.sh prompt <mode> [cut]   -> prints the sample prompt
#        demo_candidate_params.sh seconds <mode> [cut]  -> prints the dashboard-half seconds to keep
#
# [cut] only matters for guided_tour (full/short, default full); the
# other modes ignore it since they never grew a second cut.
set -eu

KIND="$1"
MODE="$2"
CUT="${3:-full}"

case "$KIND" in
	prompt)
		case "$MODE" in
			guided_tour) echo "do a full multi-signal investigation of the checkout-api incident" ;;
			cost_reveal) echo "why did checkout-api page me last night?" ;;
			surprise) echo "do a full multi-signal investigation of the checkout-api incident" ;;
			multi_turn) echo "investigate the checkout-api incident, follow up on whatever you find" ;;
			*) echo "unknown reveal mode: $MODE" >&2; exit 1 ;;
		esac
		;;
	seconds)
		# Matches scripts/demo_capture.py's REVEAL_MODE_HOLD_MS (converted
		# to seconds, the same figure), since that constant already is the
		# full length of that mode's scripted sequence from tab switch to
		# final held frame; kept as a second source of truth here rather
		# than importing the Python module into a shell script.
		case "$MODE" in
			guided_tour)
				case "$CUT" in
					full) echo 12.8 ;;
					short) echo 5.9 ;;
					*) echo "unknown cut: $CUT (expected full or short)" >&2; exit 1 ;;
				esac
				;;
			cost_reveal) echo 8.6 ;;
			surprise) echo 7.75 ;;
			multi_turn) echo 7.2 ;;
			*) echo "unknown reveal mode: $MODE" >&2; exit 1 ;;
		esac
		;;
	*)
		echo "unknown kind: $KIND (expected prompt or seconds)" >&2
		exit 1
		;;
esac
