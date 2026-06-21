#!/bin/sh
# install.sh - Nautilus My Computer Extension Installer
#
# Wrap everything in main() so a truncated curl | sh never executes a half-downloaded script.
#
# Latest release:
#   curl -fsSL https://raw.githubusercontent.com/yannmasoch/nautilus-my-computer/main/install.sh | sh
#
# Specific version:
#   curl -fsSL https://.../install.sh | sh -s -- --version=v0.1.1
#
# Dev branch:
#   curl -fsSL https://.../install.sh | sh -s -- --branch=dev
#
# Uninstall:
#   curl -fsSL https://.../install.sh | sh -s -- --uninstall

main() {

set -eu

# --- Colors -------------------------------------------------------------------
RED="$(printf '\033[0;31m')"
CYAN="$(printf '\033[0;36m')"
BOLD="$(printf '\033[1m')"
RESET="$(printf '\033[0m')"

line()  { printf "%-20s" "$1"; printf '%s%s%s\n' "$CYAN" "$2" "$RESET"; }
error() { printf '%s[ERROR]%s %s\n' "$RED" "$RESET" "$*" >&2; }
die()   { error "$*"; exit 1; }

# --- Temp dir + cleanup --------------------------------------------------------
TEMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

# --- Constants ---
REPO="yannmasoch/nautilus-my-computer"
EXT_DIR="$HOME/.local/share/nautilus-python/extensions"
EXT_FILE="nautilus-my-computer.py"
SCHEMA_ID="io.github.yannmasoch.nautilus-my-computer"
SCHEMA_FILE="$SCHEMA_ID.gschema.xml"
USER_SCHEMA_DIR="$HOME/.local/share/glib-2.0/schemas"
GETTEXT_DOMAIN="${EXT_FILE%.py}"
PYCACHE_GLOB="${EXT_FILE%.py}.cpython-"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

# --- Argument parsing ---
MODE="install"
VERSION="${VERSION:-}"
BRANCH="${BRANCH:-}"
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE="uninstall" ;;
        --version=*) VERSION="${arg#--version=}" ;;
        --branch=*) BRANCH="${arg#--branch=}" ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

# --- Source detection: local clone or remote -----------------------------------
# Only treat this as a local-clone run when the script was invoked as a real file
# (e.g. ./install.sh). When piped via `curl | sh`, $0 is "sh" or "-" (no slash),
# so the case below leaves SCRIPT_DIR empty and we fall through to remote install,
# even if the cwd happens to contain files with matching names.
SCRIPT_DIR=""
case "$0" in
    */*)
        if [ -f "$0" ]; then
            SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")"
        fi
        ;;
esac
if [ -z "${INSTALL_SOURCE:-}" ]; then
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/$EXT_FILE" ] && [ -f "$SCRIPT_DIR/$SCHEMA_FILE" ]; then
        INSTALL_SOURCE="$SCRIPT_DIR"
    else
        INSTALL_SOURCE="remote"
    fi
fi

# --- Package manager detection ---
PM=""
NP_PKG=""

detect_pm() {
    if   command -v pacman  >/dev/null 2>&1; then PM=pacman;  NP_PKG="python-nautilus"
    elif command -v apt-get >/dev/null 2>&1; then PM=apt;     NP_PKG="python3-nautilus"
    elif command -v dnf     >/dev/null 2>&1; then PM=dnf;     NP_PKG="nautilus-python"
    elif command -v zypper  >/dev/null 2>&1; then PM=zypper;  NP_PKG="python313-nautilus"
    else die "Cannot detect package manager. Install nautilus-python manually and re-run."
    fi
    line "Package manager" "$PM"
}

nautilus_python_installed() {
    case "$PM" in
        pacman) pacman -Q "$NP_PKG" >/dev/null 2>&1 ;;
        apt)    dpkg-query -W -f='${Status}' "$NP_PKG" 2>/dev/null | grep -q 'install ok installed' ;;
        dnf)    rpm -q  "$NP_PKG"   >/dev/null 2>&1 ;;
        zypper) rpm -q  "$NP_PKG"   >/dev/null 2>&1 ;;
    esac
}

ensure_nautilus_python() {
    if nautilus_python_installed; then
        line "$NP_PKG" "detected"; return
    fi
    line "$NP_PKG" "not found, installing..."
    case "$PM" in
        pacman) $SUDO pacman -S --noconfirm "$NP_PKG" ;;
        apt)    $SUDO apt-get install -y "$NP_PKG" python3-gi ;;
        dnf)    $SUDO dnf install -y "$NP_PKG" ;;
        zypper) $SUDO zypper install -y "$NP_PKG" ;;
    esac
    nautilus_python_installed || die "$NP_PKG installation failed."
    line "$NP_PKG" "installed"
}

ensure_gettext() {
    if command -v msgfmt >/dev/null 2>&1; then
        line "gettext" "detected"; return
    fi
    line "gettext" "not found, installing..."
    case "$PM" in
        pacman) $SUDO pacman -S --noconfirm gettext ;;
        apt)    $SUDO apt-get install -y gettext ;;
        dnf)    $SUDO dnf install -y gettext ;;
        zypper) $SUDO zypper install -y gettext-tools ;;
    esac
    if command -v msgfmt >/dev/null 2>&1; then
        line "gettext" "installed"
    else
        line "gettext" "install failed, translations will be skipped"
    fi
}

# --- Dependency check ---
check_dependencies() {
    missing="" tools="python3 glib-compile-schemas gsettings"
    [ "$INSTALL_SOURCE" = "remote" ] && tools="curl $tools"
    for tool in $tools; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    [ -z "$missing" ] || die "Required tools missing:$missing"
}

# --- Resolve ref ---
LATEST=""
REF_BRANCH=""
REF_VERSION=""

probe_ref() {
    curl -s -o /dev/null -w "%{http_code}" \
        "https://raw.githubusercontent.com/$REPO/$1/$EXT_FILE"
}

resolve_ref() {
    # Resolve the two display axes independently, then pick a single git ref.
    #
    #   REF_BRANCH  - branch label shown to the user (default "main")
    #   REF_VERSION - version label shown to the user (latest tag if not pinned)
    #   LATEST      - the one git ref actually fetched (a tag pins a commit, so a
    #                 valid --version always wins as the download ref)

    # Branch axis: keep the arg only if it resolves, else fall back to main.
    REF_BRANCH="main"
    if [ -n "$BRANCH" ] && [ "$(probe_ref "$BRANCH")" = "200" ]; then
        REF_BRANCH="$BRANCH"
    fi

    # Version axis: latest git tag, overridden by a valid --version.
    # /tags lists tags newest-first, so the first name is the latest version.
    response=$(curl -s "https://api.github.com/repos/$REPO/tags") \
        || die "Failed to reach GitHub API."
    latest_tag=$(echo "$response" | grep '"name"' \
        | sed 's/.*"name": *"\(.*\)".*/\1/' | head -n 1)

    pinned=""
    [ -n "$VERSION" ] && [ "$(probe_ref "$VERSION")" = "200" ] && pinned="$VERSION"

    if [ -n "$pinned" ]; then
        REF_VERSION="$pinned"
        LATEST="$pinned"            # a tag pins a commit, so it is the ref
    else
        [ -n "$latest_tag" ] && REF_VERSION="$latest_tag (latest)"
        LATEST="$REF_BRANCH"
    fi
}

# --- Fetch or copy source files ---
download_files() {
    if [ "$INSTALL_SOURCE" = "remote" ]; then
        base="https://raw.githubusercontent.com/$REPO/$LATEST"
        curl -fsSL "$base/$EXT_FILE"    -o "$TEMP_DIR/$EXT_FILE"    || die "Failed to download $EXT_FILE"
        curl -fsSL "$base/$SCHEMA_FILE" -o "$TEMP_DIR/$SCHEMA_FILE" || die "Failed to download $SCHEMA_FILE"

        mkdir -p "$TEMP_DIR/po"
        langs=$(curl -fsSL "https://api.github.com/repos/$REPO/contents/po?ref=$LATEST" \
            | sed -n 's/.*"name": "\(.*\)\.po".*/\1/p') || true
        for lang in $langs; do
            curl -fsSL "$base/po/$lang.po" -o "$TEMP_DIR/po/$lang.po" || true
        done
    else
        cp "$INSTALL_SOURCE/$EXT_FILE"    "$TEMP_DIR/$EXT_FILE"    || die "Local $EXT_FILE not found"
        cp "$INSTALL_SOURCE/$SCHEMA_FILE" "$TEMP_DIR/$SCHEMA_FILE" || die "Local $SCHEMA_FILE not found"
        [ -d "$INSTALL_SOURCE/po" ] && cp -r "$INSTALL_SOURCE/po" "$TEMP_DIR/"
    fi

    python3 -m py_compile "$TEMP_DIR/$EXT_FILE" \
        || die "Extension file failed syntax check, aborting."
}

# --- Install extension + schema ---
install_files() {
    mkdir -p "$EXT_DIR"
    cp "$TEMP_DIR/$EXT_FILE" "$EXT_DIR/$EXT_FILE"
    rm -f "$EXT_DIR/__pycache__/$PYCACHE_GLOB"*.pyc 2>/dev/null || true
    line "Extension" "$EXT_DIR/$EXT_FILE"

    mkdir -p "$USER_SCHEMA_DIR"
    cp "$TEMP_DIR/$SCHEMA_FILE" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
    glib-compile-schemas "$USER_SCHEMA_DIR"
    line "Preferences" "$USER_SCHEMA_DIR/$SCHEMA_FILE"

    [ -d "$TEMP_DIR/po" ] || return
    command -v msgfmt >/dev/null 2>&1 || return
    lang_list=""
    for po_file in "$TEMP_DIR"/po/*.po; do
        [ -f "$po_file" ] || continue
        lang=$(basename "$po_file" .po)
        loc_dir="$HOME/.local/share/locale/$lang/LC_MESSAGES"
        mkdir -p "$loc_dir"
        msgfmt "$po_file" -o "$loc_dir/$GETTEXT_DOMAIN.mo"
        lang_list="$lang_list $lang"
    done
    [ -n "$lang_list" ] && line "Languages" "$(format_lang_list "$lang_list")"
}

# --- Format language list: EN (default) first, then alpha-sorted uppercase ---
format_lang_list() {
    langs="$1" result="EN (default)" rest=""
    rest=$(echo "$langs" | tr ' ' '\n' | grep -v "^en$" | sort | tr '[:lower:]' '[:upper:]' | tr '\n' ' ')
    for lang in $rest; do
        result="$result, $lang"
    done
    echo "$result"
}

# --- Restart Nautilus ---
restart_nautilus() {
    nautilus -q >/dev/null 2>&1 || true
    sleep 1
    if command -v gtk-launch >/dev/null 2>&1; then
        nohup gtk-launch org.gnome.Nautilus >/dev/null 2>&1 &
    else
        nohup nautilus >/dev/null 2>&1 &
    fi
}

# --- INSTALL ---
do_install() {
    echo ""
    check_dependencies

    printf '%s\n' "${BOLD}Install type${RESET}"
    if [ "$INSTALL_SOURCE" = "remote" ]; then
        resolve_ref
        line "Source" "GitHub"
        line "Branch" "$REF_BRANCH"
        [ -n "$REF_VERSION" ] && line "Version" "$REF_VERSION"
    else
        REF_BRANCH=$(git -C "$INSTALL_SOURCE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        REF_VERSION=$(sed -n 's/^EXT_VERSION = "\(.*\)"/\1/p' "$INSTALL_SOURCE/$EXT_FILE")
        line "Source" "local"
        line "Branch" "$REF_BRANCH"
        line "Version" "v${REF_VERSION:-?} (latest)"
    fi


    echo ""
    printf '%s\n' "${BOLD}System${RESET}"
    detect_pm
    ensure_nautilus_python
    ensure_gettext

    echo ""
    printf '%s\n' "${BOLD}Install${RESET}"
    download_files
    install_files

    echo ""
    printf '%s\n' "${BOLD}${CYAN}🚀 Installation complete!${RESET}"
    echo ""
    restart_nautilus
}

# --- UNINSTALL ---
do_uninstall() {
    echo ""
    printf '%s\n' "${BOLD}Uninstall${RESET}"
    found=false

    if [ -f "$EXT_DIR/$EXT_FILE" ]; then
        rm -f "$EXT_DIR/$EXT_FILE"
        rm -f "$EXT_DIR/__pycache__/$PYCACHE_GLOB"*.pyc 2>/dev/null || true
        line "Extension" "$EXT_DIR/$EXT_FILE"
        found=true
    fi

    if [ -f "$USER_SCHEMA_DIR/$SCHEMA_FILE" ]; then
        gsettings reset-recursively "$SCHEMA_ID" 2>/dev/null || true
        rm -f "$USER_SCHEMA_DIR/$SCHEMA_FILE"
        glib-compile-schemas "$USER_SCHEMA_DIR"
        line "Preferences" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
        found=true
    fi

    loc_prefix="$HOME/.local/share/locale"
    lang_list=""
    for mo_file in "$loc_prefix"/*/LC_MESSAGES/"$GETTEXT_DOMAIN".mo; do
        [ -f "$mo_file" ] || continue
        lang=$(echo "$mo_file" | sed "s|$loc_prefix/\(.*\)/LC_MESSAGES.*|\1|")
        rm -f "$mo_file"
        lang_list="$lang_list $lang"
        found=true
    done
    [ -n "$lang_list" ] && line "Languages" "$(format_lang_list "$lang_list")"

    if [ "$found" = false ]; then
        printf '%s\n' "${BOLD}${CYAN}Nothing to uninstall!${RESET}"
        echo ""
        return
    fi

    echo ""
    printf '%s\n' "${BOLD}${CYAN}🗑️  Uninstall complete!${RESET}"
    echo ""
    restart_nautilus
}

# --- Entry point ---
echo ""
printf '%s\n' "${BOLD}Nautilus My Computer Installer${RESET}"
printf '──────────────────────────────\n'

case "$MODE" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
esac

} # end main

main "$@"
