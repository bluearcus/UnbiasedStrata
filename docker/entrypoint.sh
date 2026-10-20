#!/usr/bin/env bash
# Turns container environment into the strata-dual invocation.
#
# The shim owns the front-end/engine policy (native expert flags, peer defaults, PLE io mode, small-host fallback);
# this script only maps env -> its arguments and binds the listener inside the container.  Do not duplicate the
# shim's flag assembly here - that is what makes a rebase cheap.
set -euo pipefail

MODEL=${STRATA_MODEL:-}
PACK=${STRATA_PACK:-}
[ -n "$MODEL" ] || { echo "entrypoint: STRATA_MODEL is required" >&2; exit 2; }
[ -n "$PACK" ]  || { echo "entrypoint: STRATA_PACK is required" >&2; exit 2; }

MTP=${STRATA_MTP:-/models/mtp}
PORT=${STRATA_PORT:-8080}
CTX=${STRATA_CTX:-262144}
KV=${STRATA_KV:-q4_0}
ENGINE=${STRATA_ENGINE:-/opt/strata/strata}
PEER=${STRATA_PEER_DEVICE-1}          # empty string disables the peer tier; unset means 1
SHIM=${STRATA_SHIM:-/opt/strata/strata-dual}

for f in "$MODEL" "$PACK" "$MTP"; do
  [ -e "$f" ] || { echo "entrypoint: not found: $f (is /home/mike/models mounted at /models?)" >&2; exit 2; }
done

args=(--engine "$ENGINE" -m "$MODEL" --host 0.0.0.0 --port "$PORT"
      --pack "$PACK" --mtp "$MTP")
[ -n "$PEER" ] && args+=(--peer-device "$PEER")

engine_args=(--max-context "$CTX" --kv "$KV")
# Extra engine flags, space separated, for experiments (e.g. STRATA_EXTRA="--resident-experts").
if [ -n "${STRATA_EXTRA_ENGINE_ARGS:-}" ]; then
  read -r -a extra <<< "${STRATA_EXTRA_ENGINE_ARGS}"
  engine_args+=("${extra[@]}")
fi
args+=(-- "${engine_args[@]}")

echo "entrypoint: $(basename "$MODEL") ctx=$CTX kv=$KV peer=${PEER:-off} port=$PORT" >&2
exec "$SHIM" "${args[@]}"
