#!/bin/sh
# Install the optional PaddleOCR backend into an isolated, versioned venv.

main() {
set -eu

RED="$(printf '\033[0;31m')"
CYAN="$(printf '\033[0;36m')"
RESET="$(printf '\033[0m')"
line()  { printf "%-20s%s%s%s\n" "$1" "$CYAN" "$2" "$RESET"; }
error() { printf '%s[ERROR]%s %s\n' "$RED" "$RESET" "$*" >&2; }
die()   { error "$*"; exit 1; }

SCRIPT_DIR=""
if [ -f "$0" ]; then
    SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")"
fi
MANIFEST="${MC_PADDLEOCR_MANIFEST:-${SCRIPT_DIR:+$SCRIPT_DIR/}paddleocr-runtime.json}"
HELPER="${MC_PADDLEOCR_HELPER:-${SCRIPT_DIR:+$SCRIPT_DIR/}nautilus_my_computer/paddle_ocr_helper.py}"
MODE="install"

usage() {
    cat <<'EOF'
Usage: install-paddleocr.sh [OPTION]

Install PaddleOCR without modifying system Python or Nautilus's Python.

Options:
  --manifest=PATH       Compatibility manifest (normally auto-detected).
  --helper=PATH         Paddle OCR helper source (normally auto-detected).
  --remove              Remove all private PaddleOCR runtimes and models.
  -h, --help            Show this help message.

MC_PADDLE_PYTHON may name a specific compatible Python interpreter. Without
it, the newest installed interpreter accepted by the manifest is selected.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --manifest=*) MANIFEST="${arg#--manifest=}" ;;
        --helper=*) HELPER="${arg#--helper=}" ;;
        --remove) MODE="remove" ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
PADDLE_HOME="${MC_PADDLEOCR_HOME:-$DATA_HOME/nautilus-my-computer/paddleocr}"

if [ "$MODE" = remove ]; then
    if [ -d "$PADDLE_HOME" ]; then
        rm -rf -- "$PADDLE_HOME"
        line "PaddleOCR" "removed ($PADDLE_HOME)"
    else
        line "PaddleOCR" "not installed"
    fi
    exit 0
fi

[ -f "$MANIFEST" ] || die "Compatibility manifest not found: $MANIFEST"
[ -f "$HELPER" ] || die "PaddleOCR helper not found: $HELPER"
command -v python3 >/dev/null 2>&1 || die "Python 3 is required to read the manifest."

manifest_value() {
    python3 -c 'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8"));
for key in sys.argv[2].split("."): value=value[key]
print(value)' "$MANIFEST" "$1"
}

SCHEMA_VERSION=$(manifest_value schema_version)
[ "$SCHEMA_VERSION" = 2 ] || die "Unsupported PaddleOCR manifest schema: $SCHEMA_VERSION"
RUNTIME_REVISION=$(manifest_value runtime_revision)
PYTHON_MIN=$(manifest_value python.minimum)
PYTHON_MAX=$(manifest_value python.maximum_exclusive)
PADDLE_VERSION=$(manifest_value packages.paddlepaddle)
OCR_VERSION=$(manifest_value packages.paddleocr)
LAYOUT_MODEL=$(manifest_value models.layout)

python_compatible() {
    "$1" -c 'import platform,ssl,sys,venv
def pair(value): return tuple(int(part) for part in value.split(".")[:2])
current=sys.version_info[:2]
ok=(pair(sys.argv[1]) <= current < pair(sys.argv[2]) and
    sys.maxsize > 2**32 and platform.machine().lower() in {"x86_64", "amd64"})
raise SystemExit(0 if ok else 1)' "$PYTHON_MIN" "$PYTHON_MAX" >/dev/null 2>&1 \
        && "$1" -m pip --version >/dev/null 2>&1
}

PYTHON=""
if [ -n "${MC_PADDLE_PYTHON:-}" ]; then
    command -v "$MC_PADDLE_PYTHON" >/dev/null 2>&1 \
        || die "MC_PADDLE_PYTHON is not executable: $MC_PADDLE_PYTHON"
    python_compatible "$MC_PADDLE_PYTHON" \
        || die "$MC_PADDLE_PYTHON is not a compatible 64-bit Python ($PYTHON_MIN <= version < $PYTHON_MAX) with venv and pip."
    PYTHON=$(command -v "$MC_PADDLE_PYTHON")
else
    # Highest compatible minor first. The manifest remains authoritative: a
    # future Paddle release can extend this range without rewriting the scan.
    PYTHON_CANDIDATES=$(python3 -c 'import sys
low=tuple(map(int, sys.argv[1].split(".")[:2]))
high=tuple(map(int, sys.argv[2].split(".")[:2]))
if low[0] == high[0]:
    print(" ".join(f"python{low[0]}.{minor}" for minor in range(high[1]-1, low[1]-1, -1)))' "$PYTHON_MIN" "$PYTHON_MAX")
    for candidate in $PYTHON_CANDIDATES python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if python_compatible "$candidate"; then
            PYTHON=$(command -v "$candidate")
            break
        fi
    done
fi
[ -n "$PYTHON" ] \
    || die "No compatible Python found. PaddleOCR requires $PYTHON_MIN <= Python < $PYTHON_MAX with venv and pip. Nothing was created."

PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
PYTHON_MINOR=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
line "Python" "$PYTHON_VERSION ($PYTHON)"
line "Compatibility" "$PYTHON_MIN <= Python < $PYTHON_MAX"
line "PaddlePaddle" "$PADDLE_VERSION"
line "PaddleOCR" "$OCR_VERSION"
line "Layout model" "$LAYOUT_MODEL"

RUNTIME_ID="py$PYTHON_MINOR-paddle$PADDLE_VERSION-ocr$OCR_VERSION"
RUNTIMES="$PADDLE_HOME/runtimes"
RUNTIME="$RUNTIMES/$RUNTIME_ID"

validate_runtime() {
    [ -x "$1/.venv/bin/python" ] && [ -f "$1/runtime.json" ] || return 1
    "$1/.venv/bin/python" -c 'import json,sys
metadata=json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if metadata.get("schema_version") == 2 and metadata.get("runtime_revision") == int(sys.argv[2]) else 1)' \
        "$1/runtime.json" "$RUNTIME_REVISION" >/dev/null 2>&1 || return 1
    runtime_health=$(
        "$1/.venv/bin/python" "$HELPER" --serve --runtime-root "$1" \
            </dev/null 2>/dev/null
    ) || return 1
    printf '%s\n' "$runtime_health" | grep -q '"event":"ready"'
}

activate_runtime() {
    mkdir -p "$PADDLE_HOME"
    active_tmp="$PADDLE_HOME/.active-runtime.json.$$"
    printf '{"schema_version":1,"runtime_id":"%s"}\n' "$RUNTIME_ID" >"$active_tmp"
    mv "$active_tmp" "$PADDLE_HOME/active-runtime.json"
    line "Active runtime" "$RUNTIME_ID"
}

# Updating the extension must not download 300+ MB again when the exact tested
# runtime already exists. Loading both local models is the compatibility and
# integrity check; only a failed/missing runtime proceeds to wheel resolution.
if [ -d "$RUNTIME" ] && validate_runtime "$RUNTIME"; then
    line "Runtime" "existing runtime passed health check"
    activate_runtime
    line "System Python" "unchanged"
    exit 0
fi

# A manifest-only model revision does not require duplicating the large
# Python environment. Prepare downloads into the runtime's private model
# cache and writes metadata only after every model passes native inference.
if [ -x "$RUNTIME/.venv/bin/python" ]; then
    line "Runtime" "upgrading models in the existing private environment"
    if PADDLE_PDX_MODEL_SOURCE=BOS "$RUNTIME/.venv/bin/python" "$HELPER" \
        --prepare --runtime-root "$RUNTIME" --manifest "$MANIFEST" \
        && validate_runtime "$RUNTIME"; then
        line "Runtime" "$RUNTIME_ID passed upgraded health check"
        activate_runtime
        line "System Python" "unchanged"
        exit 0
    fi
    line "Runtime" "in-place upgrade failed; creating a clean replacement"
fi

if [ -r /proc/cpuinfo ] && ! grep -qi '\<avx\>' /proc/cpuinfo; then
    die "This CPU does not advertise AVX, which the PaddlePaddle CPU wheel requires. Nothing was created."
fi

TEMP_DIR=$(mktemp -d)
STAGING=""
cleanup() {
    rm -rf -- "$TEMP_DIR"
    if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then
        rm -rf -- "$STAGING"
    fi
}
trap cleanup EXIT HUP INT TERM
WHEELHOUSE="$TEMP_DIR/wheels"
mkdir -p "$WHEELHOUSE"

# Resolve and download every wheel before creating a venv. This proves that
# the selected interpreter has a compatible binary distribution and avoids a
# half-created runtime when a Python/Paddle release pair is unsupported.
line "Preflight" "resolving compatible wheels"
PIP_CACHE_DIR="$TEMP_DIR/pip-cache" "$PYTHON" -m pip download \
    --disable-pip-version-check \
    --only-binary=:all: \
    --dest "$WHEELHOUSE" \
    --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
    "paddlepaddle==$PADDLE_VERSION" "paddleocr==$OCR_VERSION" \
    || die "No compatible PaddleOCR/PaddlePaddle wheel set is available for Python $PYTHON_MINOR. Nothing was created."
line "Preflight" "passed"

STAGING="$RUNTIMES/.staging-$RUNTIME_ID-$$"
mkdir -p "$RUNTIMES"

# A killed or failed older setup must not leave a hundreds-of-megabytes
# temporary environment behind. Runtime IDs contain only manifest versions.
for stale_runtime in "$RUNTIMES"/.staging-"$RUNTIME_ID"-*; do
    [ -d "$stale_runtime" ] || continue
    rm -rf -- "$stale_runtime"
done

if [ -d "$RUNTIME" ]; then
    BROKEN="$RUNTIMES/.replaced-$RUNTIME_ID-$(date +%Y%m%d%H%M%S)"
    mv "$RUNTIME" "$BROKEN"
    line "Runtime" "preserved invalid runtime as $(basename "$BROKEN")"
fi
rm -rf -- "$STAGING"
mkdir -p "$STAGING"
line "Runtime" "creating isolated virtual environment"
"$PYTHON" -m venv "$STAGING/.venv" \
    || die "Could not create the isolated Python environment."
"$STAGING/.venv/bin/python" -m pip install \
    --disable-pip-version-check --no-index --find-links "$WHEELHOUSE" \
    "paddlepaddle==$PADDLE_VERSION" "paddleocr==$OCR_VERSION" \
    || die "Could not install the resolved PaddleOCR wheels."
line "Models" "downloading OCR and balanced document-layout models"
PADDLE_PDX_MODEL_SOURCE=BOS "$STAGING/.venv/bin/python" "$HELPER" \
    --prepare --runtime-root "$STAGING" --manifest "$MANIFEST" \
    || die "PaddleOCR model download or native inference health check failed."
validate_runtime "$STAGING" \
    || die "The new PaddleOCR runtime did not pass its isolated startup check."
mv "$STAGING" "$RUNTIME"
line "Runtime" "$RUNTIME_ID passed health check"

activate_runtime
line "System Python" "unchanged"
}

main "$@"
