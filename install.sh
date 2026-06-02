#!/usr/bin/env bash
# install.sh — Nautilus My Computer Extension Installer
# curl -fsSL https://raw.githubusercontent.com/yannmasoch/nautilus-my-computer/main/install.sh | bash

main() {

set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

line()      { printf "%-26s" "$1"; echo -e "${CYAN}$2${RESET}"; }
print_bye() { echo ""; echo -e "${BOLD}${CYAN}👋 Bye${RESET}"; echo ""; }
error()     { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()       { error "$*"; exit 1; }

# ─── Temp dir + cleanup ───────────────────────────────────────────────────────
TEMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

# ─── Constants ────────────────────────────────────────────────────────────────
REPO="yannmasoch/nautilus-my-computer"
EXT_DIR="$HOME/.local/share/nautilus-python/extensions"
EXT_FILE="nautilus-my-computer.py"
SCHEMA_FILE="io.github.yannmasoch.nautilus-my-computer.gschema.xml"
USER_SCHEMA_DIR="$HOME/.local/share/glib-2.0/schemas"

# ─── Source detection: local clone or remote ──────────────────────────────────
# INSTALL_SOURCE can be set externally to override auto-detection.
# If unset: use local files when run from inside a git clone, else download.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
if [ -z "${INSTALL_SOURCE:-}" ]; then
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/$EXT_FILE" ] && [ -f "$SCRIPT_DIR/$SCHEMA_FILE" ]; then
        INSTALL_SOURCE="$SCRIPT_DIR"
    else
        INSTALL_SOURCE="remote"
    fi
fi

# ─── Read from terminal even when piped via curl | bash ───────────────────────
ask() {
    local prompt="$1" var="$2" default="${3:-}"
    printf "%s" "$prompt" >/dev/tty
    read -r "$var" </dev/tty
    printf -v "$var" '%s' "${!var%$'\r'}"
    if [ -z "${!var}" ] && [ -n "$default" ]; then
        printf -v "$var" '%s' "$default"
        printf "\033[1A\033[%dC%s\n" "${#prompt}" "$default" >/dev/tty
    fi
}

# ─── Package manager detection ────────────────────────────────────────────────
detect_pm() {
    if   command -v pacman  >/dev/null 2>&1; then PM=pacman;  NP_PKG="python-nautilus"
    elif command -v apt-get >/dev/null 2>&1; then PM=apt;     NP_PKG="python3-nautilus"
    elif command -v dnf     >/dev/null 2>&1; then PM=dnf;     NP_PKG="nautilus-python"
    elif command -v zypper  >/dev/null 2>&1; then PM=zypper;  NP_PKG="python3-nautilus"
    else die "Cannot detect package manager. Install the nautilus-python package manually and re-run."
    fi
    line "Package Manager" "$PM detected"
}

nautilus_python_installed() {
    case "$PM" in
        pacman) pacman -Q "$NP_PKG" >/dev/null 2>&1 ;;
        apt)    dpkg -l "$NP_PKG"   >/dev/null 2>&1 ;;
        dnf)    rpm -q  "$NP_PKG"   >/dev/null 2>&1 ;;
        zypper) rpm -q  "$NP_PKG"   >/dev/null 2>&1 ;;
    esac
}

ensure_nautilus_python() {
    if nautilus_python_installed; then
        line "$NP_PKG" "detected"
        return
    fi
    line "$NP_PKG" "not detected — installing..."
    case "$PM" in
        pacman) sudo pacman -S --noconfirm "$NP_PKG" ;;
        apt)    sudo apt-get install -y "$NP_PKG" python3-gi ;;
        dnf)    sudo dnf install -y "$NP_PKG" ;;
        zypper) sudo zypper install -y "$NP_PKG" ;;
    esac
    nautilus_python_installed || die "$NP_PKG installation failed."
    line "$NP_PKG" "installed"
}

# ─── Dependency check ─────────────────────────────────────────────────────────
check_dependencies() {
    local missing=""
    local tools="python3 glib-compile-schemas gsettings"
    if [ "$INSTALL_SOURCE" = "remote" ]; then tools="curl $tools"; fi
    for tool in $tools; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    [ -z "$missing" ] || die "Required tools missing:$missing"
}

# ─── Fetch latest version via GitHub API (Option A) ──────────────────────────
# Falls back to the main branch when the repo has no published releases yet.
fetch_latest_version() {
    local response
    response=$(curl -s "https://api.github.com/repos/$REPO/releases/latest") \
        || die "Failed to reach GitHub API."

    LATEST=$(echo "$response" | grep '"tag_name"' | sed 's/.*"tag_name": *"\(.*\)".*/\1/' || true)
    if [ -z "$LATEST" ]; then
        LATEST="main"
        line "Latest release" "none — using main branch"
    fi
}

# ─── Fetch or copy source files ───────────────────────────────────────────────
download_files() {
    if [ "$INSTALL_SOURCE" = "remote" ]; then
        local base="https://raw.githubusercontent.com/$REPO/$LATEST"
        curl -fsSL "$base/$EXT_FILE" -o "$TEMP_DIR/$EXT_FILE" \
            || die "Failed to download $EXT_FILE"
        curl -fsSL "$base/$SCHEMA_FILE" -o "$TEMP_DIR/$SCHEMA_FILE" \
            || die "Failed to download $SCHEMA_FILE"
    else
        cp "$INSTALL_SOURCE/$EXT_FILE"    "$TEMP_DIR/$EXT_FILE"    || die "Local $EXT_FILE not found"
        cp "$INSTALL_SOURCE/$SCHEMA_FILE" "$TEMP_DIR/$SCHEMA_FILE" || die "Local $SCHEMA_FILE not found"
    fi

    python3 -m py_compile "$TEMP_DIR/$EXT_FILE" \
        || die "Extension file failed syntax check — aborting."
}

# ─── Install extension + schema ───────────────────────────────────────────────
install_files() {
    mkdir -p "$EXT_DIR"
    cp "$TEMP_DIR/$EXT_FILE" "$EXT_DIR/$EXT_FILE"
    rm -f "$EXT_DIR/__pycache__/nautilus-my-computer.cpython-"*.pyc 2>/dev/null || true
    line "Extension installed" "$EXT_DIR/$EXT_FILE"

    mkdir -p "$USER_SCHEMA_DIR"
    cp "$TEMP_DIR/$SCHEMA_FILE" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
    line "Preferences installed" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
    glib-compile-schemas "$USER_SCHEMA_DIR"
}

# ─── Restart Nautilus ─────────────────────────────────────────────────────────
offer_restart() {
    echo "" >/dev/tty
    local answer
    ask "Restart Nautilus now? [Y/n]: " answer "Y"
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        nautilus -q >/dev/null 2>&1 || true
        sleep 1
        (exec >/dev/null 2>&1 </dev/null; exec nautilus) &
        disown $!
    fi
}

# ─── INSTALL ──────────────────────────────────────────────────────────────────
do_install() {
    echo ""
    check_dependencies

    if [ "$INSTALL_SOURCE" = "remote" ]; then
        fetch_latest_version
        line "Installation type" "GitHub ($LATEST)"
    else
        line "Installation type" "local"
    fi

    if [ -f "$EXT_DIR/$EXT_FILE" ]; then
        line "Previous installation" "detected"
        local confirm
        ask "Update to latest version or reinstall? [Y/n]: " confirm "Y"
        [[ "$confirm" =~ ^[Yy]$ ]] || { print_bye; return; }
    else
        line "Previous installation" "not detected"
    fi

    echo ""
    detect_pm
    ensure_nautilus_python
    download_files
    install_files

    echo ""
    echo -e "${BOLD}${CYAN}🚀 Installation completed!${RESET}"
    offer_restart
}

# ─── UNINSTALL ────────────────────────────────────────────────────────────────
do_uninstall() {
    echo ""

    local found=false

    if [ -f "$EXT_DIR/$EXT_FILE" ]; then
        rm -f "$EXT_DIR/$EXT_FILE"
        rm -f "$EXT_DIR/__pycache__/nautilus-my-computer.cpython-"*.pyc 2>/dev/null || true
        line "Extension removed" "$EXT_DIR/$EXT_FILE"
        found=true
    fi

    if [ -f "$USER_SCHEMA_DIR/$SCHEMA_FILE" ]; then
        gsettings reset-recursively io.github.yannmasoch.nautilus-my-computer 2>/dev/null || true
        rm -f "$USER_SCHEMA_DIR/$SCHEMA_FILE"
        glib-compile-schemas "$USER_SCHEMA_DIR"
        line "Preferences removed" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
        found=true
    fi

    if [ "$found" = false ]; then
        line "Nothing to uninstall" "extension was not found"
        print_bye
        return
    fi

    echo ""
    echo -e "${BOLD}${CYAN}🗑️  Uninstall completed!${RESET}"
    offer_restart
}

# ─── MAIN MENU ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Nautilus My Computer Extension Installer${RESET}"
printf '%0.s-' {1..40}; echo
echo ""
echo    "1) Install / Update"
echo    "2) Uninstall"
echo    "3) Exit"
echo ""

choice=""
ask "Choose an option [1-3]: " choice ""

case "$choice" in
    1) do_install ;;
    2) do_uninstall ;;
    3) print_bye; exit 0 ;;
    *) die "Invalid choice: '$choice'" ;;
esac

} # end main

main
