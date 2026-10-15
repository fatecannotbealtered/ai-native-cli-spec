# probe.sh: source it in bash (Git Bash on Windows) after setting
#   TOOL      the binary built from the working tree
#   OUT       an existing scratch directory for captures
#   CONTRACT  the tool's contract/ directory (its vendored contract.json)
# and the tool's <PREFIX>_STATE_DIR and <PREFIX>_SCOPE_FILE (reference/dynamic-review.md).
#
#   probe <name> <args...>     run "$TOOL" <args> with stdin closed and a 60 s limit, keep
#                              $OUT/<name>.json, .err and .exit, and check the envelope
#   rawprobe <name> <args...>  the same for output that is not an envelope (--format text,
#                              --help, --version), kept as $OUT/<name>.txt, no check
#   field <name> '<js>'        print a value from $OUT/<name>.json; `e` is the envelope,
#                              e.g. field dry 'e.data.confirm_token'

REVIEW_SCRIPTS=${REVIEW_SCRIPTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

_run() {
  local ext=$1 name=$2
  shift 2
  local limit=()
  command -v timeout >/dev/null 2>&1 && limit=(timeout 60)
  "${limit[@]}" "$TOOL" "$@" >"$OUT/$name.$ext" 2>"$OUT/$name.err" </dev/null
  local code=$?
  echo "$code" >"$OUT/$name.exit"
  if [ "$code" = 124 ] && [ ${#limit[@]} -gt 0 ]; then
    echo "TIMEOUT $name: no answer within 60 s with stdin closed (CLI-001 no prompts, CLI-049 bounded time)"
    return 124
  fi
}

probe() {
  local name=$1
  _run json "$@" || return
  node "$REVIEW_SCRIPTS/check-envelope.js" "$OUT/$name.json" "$(cat "$OUT/$name.exit")" "$CONTRACT"
  if [ -s "$OUT/$name.err" ]; then
    echo "  stderr: $(wc -c <"$OUT/$name.err") bytes (CLI-047: none expected when stderr is not a terminal): $(head -c 160 "$OUT/$name.err" | tr '\n' ' ')"
  fi
}

rawprobe() {
  local name=$1
  _run txt "$@" || return
  echo "RAW $name exit=$(cat "$OUT/$name.exit") stdout=$(wc -c <"$OUT/$name.txt")B stderr=$(wc -c <"$OUT/$name.err")B"
}

field() {
  node -e 'const e = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")); const v = ('"$2"'); console.log(typeof v === "string" ? v : JSON.stringify(v));' "$OUT/$1.json"
}
