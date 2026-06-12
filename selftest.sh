#!/usr/bin/env bash
# Self-test: regenerate an explorer from the bundled sample artifacts and
# assert that the tool's own trace-vs-waveform cross-check passes.
# Requirements: python3, fst2vcd (apt install gtkwave). node is optional.
set -euo pipefail
cd "$(dirname "$0")"

command -v fst2vcd >/dev/null || { echo "fst2vcd not found (apt install gtkwave)"; exit 1; }

out=$(python3 pipeline_explorer.py --trace sample/trace.log --fst sample/sim.fst \
  --vcd /tmp/ipe_selftest.vcd --dis sample/disasm.txt --out /tmp/ipe_selftest.html)
echo "$out"

echo "$out" | grep -q -- "cross-check: 170/170 register writes match the waveform -- PASS" \
  || { echo "SELFTEST FAIL: cross-check did not pass"; exit 1; }

python3 - << 'EOF'
import re
html = open('/tmp/ipe_selftest.html').read()
assert not re.search(r'__[A-Z0-9]+__', html), 'unreplaced template token'
open('/tmp/ipe_selftest.js', 'w').write(html.split('<script>')[1].split('</script>')[0])
print("template tokens OK")
EOF

if command -v node >/dev/null; then
  node --check /tmp/ipe_selftest.js && echo "JS syntax OK"
fi

echo "SELFTEST PASS"
