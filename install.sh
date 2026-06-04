#!/usr/bin/env bash
# install.sh — Nautilus My Computer Extension Installer
#
# Default (dev branch):
#   curl -fsSL https://raw.githubusercontent.com/yannmasoch/nautilus-my-computer/main/install.sh | bash
#
# Pin to a specific branch or tag:
#   VERSION=main  curl -fsSL ... | bash
#   VERSION=v0.1  curl -fsSL ... | bash

main() {

set -euo pipefail

REF_OVERRIDE="${VERSION:-}"

# ─── Colors ───────────────────────────────────────────────────────────────────
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
DIM=$'\033[2m'
RESET=$'\033[0m'

# ─── Output helpers ───────────────────────────────────────────────────────────
COL=30
line()  { printf "  ${DIM}%-${COL}s${RESET}${CYAN}%s${RESET}\n" "$1" "$2"; }
ok()    { printf "  ${GREEN}✔${RESET}  %-$((COL-3))s${CYAN}%s${RESET}\n" "$1" "$2"; }
info()  { printf "  ${YELLOW}ℹ${RESET}  %s\n" "$*"; }
warn()  { printf "  ${YELLOW}⚠${RESET}  ${YELLOW}%s${RESET}\n" "$*" >&2; }
error() { printf "  ${RED}✖${RESET}  ${RED}%s${RESET}\n" "$*" >&2; }
die()   { error "$*"; exit 1; }
sep()   { printf "${DIM}"; printf '%0.s─' $(seq 1 44); printf "${RESET}\n"; }
bye()   { echo; printf "${BOLD}${CYAN}  👋 Bye!${RESET}\n"; echo; }

# ─── Temp dir + cleanup ───────────────────────────────────────────────────────
TEMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

# ─── Constants ────────────────────────────────────────────────────────────────
REPO="yannmasoch/nautilus-my-computer"
DEFAULT_BRANCH="dev"          # branch used when no release/tag found
EXT_DIR="$HOME/.local/share/nautilus-python/extensions"
EXT_FILE="nautilus-my-computer.py"
VERSION_FILE="$EXT_DIR/.nautilus-my-computer.version"
SCHEMA_FILE="io.github.yannmasoch.nautilus-my-computer.gschema.xml"
USER_SCHEMA_DIR="$HOME/.local/share/glib-2.0/schemas"

# ─── Source detection: local clone or remote ──────────────────────────────────
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "/dev/stdin" && \
      "${BASH_SOURCE[0]}" != "bash" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
fi

if [ -z "${INSTALL_SOURCE:-}" ]; then
    if [ -n "$SCRIPT_DIR" ] && \
       [ -f "$SCRIPT_DIR/$EXT_FILE" ] && \
       [ -f "$SCRIPT_DIR/$SCHEMA_FILE" ]; then
        INSTALL_SOURCE="$SCRIPT_DIR"
    else
        INSTALL_SOURCE="remote"
    fi
fi

# ─── Read from terminal even when piped via curl | bash ───────────────────────
ask() {
    local prompt="$1" var="$2" default="${3:-}"
    printf "%s" "$prompt" >/dev/tty
    read -r "$var" </dev/tty || true
    # Strip carriage return (Windows line endings)
    printf -v "$var" '%s' "${!var%$'\r'}"
    if [ -z "${!var}" ] && [ -n "$default" ]; then
        printf -v "$var" '%s' "$default"
        printf "\033[1A\033[%dC%s\n" "${#prompt}" "$default" >/dev/tty
    fi
}

# ─── System information ───────────────────────────────────────────────────────
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO_NAME="${PRETTY_NAME:-${NAME:-Unknown}}"
        DISTRO_ID="${ID:-unknown}"
        DISTRO_ID_LIKE="${ID_LIKE:-}"
    elif [ -f /etc/lsb-release ]; then
        . /etc/lsb-release
        DISTRO_NAME="${DISTRIB_DESCRIPTION:-${DISTRIB_ID:-Unknown}}"
        DISTRO_ID="${DISTRIB_ID:-unknown}"
        DISTRO_ID_LIKE=""
    else
        DISTRO_NAME="Unknown Linux"
        DISTRO_ID="unknown"
        DISTRO_ID_LIKE=""
    fi
}

get_gnome_version() {
    local ver=""
    if command -v gnome-shell >/dev/null 2>&1; then
        ver=$(gnome-shell --version 2>/dev/null | grep -oP '\d+\.\d+(\.\d+)?' | head -1 || true)
    fi
    echo "${ver:-not detected}"
}

get_nautilus_version() {
    local ver=""
    if command -v nautilus >/dev/null 2>&1; then
        ver=$(nautilus --version 2>/dev/null | grep -oP '\d+\.\d+(\.\d+)?' | head -1 || true)
    fi
    echo "${ver:-not detected}"
}

get_installed_version() {
    if [ -f "$VERSION_FILE" ]; then
        cat "$VERSION_FILE"
    elif [ -f "$EXT_DIR/$EXT_FILE" ]; then
        echo "installed (version unknown)"
    else
        echo "not installed"
    fi
}

show_sysinfo() {
    detect_os
    local kernel gnome_ver nautilus_ver installed_ver
    kernel=$(uname -r)
    gnome_ver=$(get_gnome_version)
    nautilus_ver=$(get_nautilus_version)
    installed_ver=$(get_installed_version)

    echo ""
    printf "${BOLD}${CYAN}  Nautilus My Computer — Extension Installer${RESET}\n"
    sep
    echo ""
    line "Distro"                 "$DISTRO_NAME"
    line "Kernel"                 "$kernel"
    line "GNOME Shell"            "$gnome_ver"
    line "Nautilus"               "$nautilus_ver"
    echo ""
    if [ "$installed_ver" = "not installed" ]; then
        line "Extension"          "${RED}not installed${RESET}"
    else
        line "Extension"          "${GREEN}${installed_ver}${RESET}"
    fi
    echo ""

    if command -v flatpak >/dev/null 2>&1; then
        if flatpak list 2>/dev/null | grep -qi "org.gnome.Nautilus"; then
            warn "Flatpak Nautilus detected. This installer targets the system/native Nautilus."
            warn "If you use the Flatpak version, extensions may not load automatically."
            echo ""
        fi
    fi

    case "${DISTRO_ID}${DISTRO_ID_LIKE}" in
        *silverblue*|*kinoite*|*bazzite*|*aurora*|*ucore*|*microos*|*aeon*|*kalpa*)
            warn "Atomic/immutable distro detected."
            warn "System packages installed with rpm-ostree/transactional-update may be"
            warn "needed. This installer uses user-level paths (~/.local) which should work."
            echo "" ;;
    esac

    if [ "$gnome_ver" != "not detected" ]; then
        local major
        major=$(echo "$gnome_ver" | cut -d. -f1)
        if [ "$major" -lt 40 ] 2>/dev/null; then
            warn "GNOME ${gnome_ver} detected. This extension targets GNOME 40+."
            warn "It may not work correctly on older versions."
            echo ""
        fi
    fi
}

# ─── Package manager detection ────────────────────────────────────────────────
PM=""
NP_PKG=""

detect_pm() {
    if   command -v pacman  >/dev/null 2>&1; then PM=pacman;  NP_PKG="python-nautilus"
    elif command -v apt-get >/dev/null 2>&1; then PM=apt;     NP_PKG="python3-nautilus"
    elif command -v dnf     >/dev/null 2>&1; then PM=dnf;     NP_PKG="nautilus-python"
    elif command -v zypper  >/dev/null 2>&1; then PM=zypper;  NP_PKG="python3-nautilus"
    elif command -v apk     >/dev/null 2>&1; then PM=apk;     NP_PKG="py3-nautilus"
    elif command -v xbps-install >/dev/null 2>&1; then PM=xbps; NP_PKG="python3-nautilus"
    else
        die "Cannot detect package manager. Install the nautilus-python package manually and re-run."
    fi
    ok "Package manager" "$PM"
}

nautilus_python_installed() {
    case "$PM" in
        pacman) pacman -Q "$NP_PKG"     >/dev/null 2>&1 ;;
        apt)    dpkg -l "$NP_PKG" 2>/dev/null | grep -q '^ii' ;;
        dnf)    rpm -q  "$NP_PKG"       >/dev/null 2>&1 ;;
        zypper) rpm -q  "$NP_PKG"       >/dev/null 2>&1 ;;
        apk)    apk info "$NP_PKG"      >/dev/null 2>&1 ;;
        xbps)   xbps-query "$NP_PKG"   >/dev/null 2>&1 ;;
    esac
}

install_pkg() {
    local pkg="$1"
    case "$PM" in
        pacman) sudo pacman -S --noconfirm "$pkg" ;;
        apt)    sudo apt-get install -y "$pkg" ;;
        dnf)    sudo dnf install -y "$pkg" ;;
        zypper) sudo zypper install -y "$pkg" ;;
        apk)    sudo apk add "$pkg" ;;
        xbps)   sudo xbps-install -y "$pkg" ;;
    esac
}

ensure_nautilus_python() {
    if nautilus_python_installed; then
        ok "$NP_PKG" "already installed"
        return
    fi
    info "$NP_PKG not found — installing…"
    if [ "$PM" = "apt" ]; then
        sudo apt-get install -y "$NP_PKG" python3-gi
    else
        install_pkg "$NP_PKG"
    fi
    nautilus_python_installed || die "$NP_PKG installation failed."
    ok "$NP_PKG" "installed"
}

ensure_gettext() {
    if command -v msgfmt >/dev/null 2>&1; then
        ok "gettext (msgfmt)" "already installed"
        return
    fi
    info "gettext not found — installing…"
    case "$PM" in
        zypper) install_pkg "gettext-tools" ;;
        *)      install_pkg "gettext" ;;
    esac
    command -v msgfmt >/dev/null 2>&1 \
        || die "gettext installation failed. Install gettext manually and re-run."
    ok "gettext (msgfmt)" "installed"
}

# ─── Dependency check ─────────────────────────────────────────────────────────
check_dependencies() {
    local missing="" tools
    tools="python3 glib-compile-schemas gsettings"
    [ "$INSTALL_SOURCE" = "remote" ] && tools="curl $tools"
    for tool in $tools; do
        command -v "$tool" >/dev/null 2>&1 || missing="${missing:+$missing, }$tool"
    done
    [ -z "$missing" ] || die "Required tools not found: $missing"
}

# ─── Resolve version ──────────────────────────────────────────────────────────
LATEST=""
LATEST_RELEASE=""
REF_FALLBACK=false

_curl_api() {
    curl -s --max-time 10 \
        -w "\n__HTTP_CODE__:%{http_code}" \
        "$1" 2>/dev/null || true
}
_api_code() { printf '%s' "$1" | grep -o '__HTTP_CODE__:[0-9]*' | cut -d: -f2 || true; }
_api_body() { printf '%s' "$1" | sed '/__HTTP_CODE__:/d'; }

fetch_latest_version() {
    # Evitar llamada a la API si ya pedimos una rama o versión específica
    if [ -n "$REF_OVERRIDE" ]; then
        LATEST="$REF_OVERRIDE"
        return
    fi

    local raw code body

    raw=$(_curl_api "https://api.github.com/repos/$REPO/releases/latest")
    code=$(_api_code "$raw")
    body=$(_api_body "$raw")

    case "$code" in
        200)
            LATEST_RELEASE=$(printf '%s' "$body" \
                | grep '"tag_name"' \
                | sed 's/.*"tag_name": *"\(.*\)".*/\1/' \
                | head -1 || true)
            ;;
        404)
            raw=$(_curl_api "https://api.github.com/repos/$REPO/tags")
            code=$(_api_code "$raw")
            body=$(_api_body "$raw")
            if [ "$code" = "200" ]; then
                LATEST_RELEASE=$(printf '%s' "$body" \
                    | grep '"name"' \
                    | sed 's/.*"name": *"\(.*\)".*/\1/' \
                    | head -1 || true)
            fi
            ;;
        403|429)
            warn "GitHub API rate limit (HTTP $code) — using '$DEFAULT_BRANCH' branch."
            ;;
        "")
            warn "No response from GitHub — check your connection. Using '$DEFAULT_BRANCH' branch."
            ;;
        *)
            warn "GitHub API returned HTTP $code — using '$DEFAULT_BRANCH' branch."
            ;;
    esac

    [ -z "$LATEST_RELEASE" ] && LATEST_RELEASE="$DEFAULT_BRANCH"
    LATEST="$LATEST_RELEASE"
}

validate_ref() {
    [ -n "$REF_OVERRIDE" ] || return
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" \
        "https://raw.githubusercontent.com/$REPO/$LATEST/$EXT_FILE")
    if [ "$status" != "200" ]; then
        warn "Version '$REF_OVERRIDE' not found on GitHub — using latest ($LATEST_RELEASE)."
        REF_FALLBACK=true
        LATEST="$LATEST_RELEASE"
    fi
}

# ─── Fetch or copy source files ───────────────────────────────────────────────
download_files() {
    if [ "$INSTALL_SOURCE" = "remote" ]; then
        local base="https://raw.githubusercontent.com/$REPO/$LATEST"

        curl -fsSL "$base/$EXT_FILE"    -o "$TEMP_DIR/$EXT_FILE"    \
            || die "Failed to download $EXT_FILE from $base"
        curl -fsSL "$base/$SCHEMA_FILE" -o "$TEMP_DIR/$SCHEMA_FILE" \
            || die "Failed to download $SCHEMA_FILE from $base"

        mkdir -p "$TEMP_DIR/po"
        local langs po_json
        po_json=$(curl -sL --max-time 10 \
            "https://api.github.com/repos/$REPO/contents/po?ref=$LATEST" \
            2>/dev/null || true)
        langs=$(printf '%s' "$po_json" \
            | grep '"name"' \
            | sed 's/.*"name": "\(.*\)\.po".*/\1/' \
            | grep -v '"' \
            || true)
        for lang in $langs; do
            curl -sL --max-time 10 "$base/po/$lang.po" -o "$TEMP_DIR/po/$lang.po" 2>/dev/null || true
        done
    else
        cp "$INSTALL_SOURCE/$EXT_FILE"    "$TEMP_DIR/$EXT_FILE"    \
            || die "Local file not found: $INSTALL_SOURCE/$EXT_FILE"
        cp "$INSTALL_SOURCE/$SCHEMA_FILE" "$TEMP_DIR/$SCHEMA_FILE" \
            || die "Local file not found: $INSTALL_SOURCE/$SCHEMA_FILE"
        [ -d "$INSTALL_SOURCE/po" ] && cp -r "$INSTALL_SOURCE/po" "$TEMP_DIR/"
    fi

    python3 -m py_compile "$TEMP_DIR/$EXT_FILE" \
        || die "Extension file failed Python syntax check — aborting."
}

# ─── Install extension + schema + translations ────────────────────────────────
install_files() {
    mkdir -p "$EXT_DIR"
    cp "$TEMP_DIR/$EXT_FILE" "$EXT_DIR/$EXT_FILE"
    find "$EXT_DIR/__pycache__/" -name "nautilus-my-computer.cpython-*.pyc" \
        -delete 2>/dev/null || true
    ok "Extension" "$EXT_DIR/$EXT_FILE"

    printf '%s\n' "$LATEST" > "$VERSION_FILE"

    mkdir -p "$USER_SCHEMA_DIR"
    cp "$TEMP_DIR/$SCHEMA_FILE" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
    glib-compile-schemas "$USER_SCHEMA_DIR"
    ok "Preferences (schema)" "$USER_SCHEMA_DIR/$SCHEMA_FILE"

    [ -d "$TEMP_DIR/po" ] || return 0
    command -v msgfmt >/dev/null 2>&1 || return 0
    local langs_installed=""
    for po_file in "$TEMP_DIR"/po/*.po; do
        [ -f "$po_file" ] || continue
        local lang loc_dir
        lang=$(basename "$po_file" .po)
        loc_dir="$HOME/.local/share/locale/$lang/LC_MESSAGES"
        mkdir -p "$loc_dir"
        msgfmt "$po_file" -o "$loc_dir/nautilus-my-computer.mo" \
            && langs_installed="${langs_installed:+$langs_installed, }$lang" || true
    done
    [ -n "$langs_installed" ] && ok "Translations" "$langs_installed"
    return 0
}

# ─── Restart Nautilus ─────────────────────────────────────────────────────────
offer_restart() {
    echo ""
    local answer
    ask "  Restart Nautilus now? [Y/n]: " answer "Y"
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        nautilus -q >/dev/null 2>&1 || true
        sleep 1
        if command -v gtk-launch >/dev/null 2>&1; then
            gtk-launch org.gnome.Nautilus >/dev/null 2>&1 &
        elif command -v nohup >/dev/null 2>&1; then
            nohup nautilus >/dev/null 2>&1 &
        else
            (exec nautilus >/dev/null 2>&1 </dev/null) &
        fi
        disown $! 2>/dev/null || true
        ok "Nautilus" "restarted"
    fi
}

# ─── INSTALL ──────────────────────────────────────────────────────────────────
do_install() {
    echo ""
    check_dependencies

    if [ "$INSTALL_SOURCE" = "remote" ]; then
        fetch_latest_version
        validate_ref
        ok "Install source" "GitHub  ($LATEST)"
        if [ -n "${VERSION:-}" ] || [ "$LATEST" = "$DEFAULT_BRANCH" ] || [ "$LATEST" = "main" ]; then
            if [ "$REF_FALLBACK" = "true" ]; then
                info "Requested '$REF_OVERRIDE' not found — installing $LATEST_RELEASE instead."
            else
                ok "Requested version" "$LATEST"
            fi
        fi
    else
        ok "Install source" "local clone"
        [ -n "$REF_OVERRIDE" ] && info "VERSION ignored for local installs."
    fi

    if [ -f "$EXT_DIR/$EXT_FILE" ]; then
        local prev_ver
        prev_ver=$(get_installed_version)
        info "Previous installation detected (${prev_ver}) — upgrading."
    fi

    echo ""
    detect_pm
    ensure_nautilus_python
    ensure_gettext
    echo ""
    info "Downloading files…"
    download_files
    install_files

    echo ""
    printf "${BOLD}${CYAN}  🚀 Installation complete!${RESET}\n"
    offer_restart
    echo ""
}

# ─── UNINSTALL ────────────────────────────────────────────────────────────────
do_uninstall() {
    echo ""
    local found=false

    if [ -f "$EXT_DIR/$EXT_FILE" ]; then
        rm -f "$EXT_DIR/$EXT_FILE" "$VERSION_FILE"
        find "$EXT_DIR/__pycache__/" -name "nautilus-my-computer.cpython-*.pyc" \
            -delete 2>/dev/null || true
        ok "Extension removed" "$EXT_DIR/$EXT_FILE"
        found=true
    fi

    if [ -f "$USER_SCHEMA_DIR/$SCHEMA_FILE" ]; then
        gsettings reset-recursively io.github.yannmasoch.nautilus-my-computer \
            2>/dev/null || true
        rm -f "$USER_SCHEMA_DIR/$SCHEMA_FILE"
        glib-compile-schemas "$USER_SCHEMA_DIR"
        ok "Schema removed" "$USER_SCHEMA_DIR/$SCHEMA_FILE"
        found=true
    fi

    local loc_dir_prefix="$HOME/.local/share/locale"
    local langs_removed=""
    if [ -d "$loc_dir_prefix" ]; then
        while IFS= read -r -d '' mo_file; do
            local lang
            lang=$(printf '%s' "$mo_file" \
                | sed "s|${loc_dir_prefix}/\(.*\)/LC_MESSAGES.*|\1|")
            rm -f "$mo_file"
            langs_removed="${langs_removed:+$langs_removed, }$lang"
            found=true
        done < <(find "$loc_dir_prefix" \
            -path "*/LC_MESSAGES/nautilus-my-computer.mo" -print0 2>/dev/null)
    fi
    [ -n "$langs_removed" ] && ok "Translations removed" "$langs_removed"

    if [ "$found" = false ]; then
        info "Nothing to uninstall — extension was not found."
        bye
        return
    fi

    echo ""
    printf "${BOLD}${CYAN}  🗑️  Uninstall complete!${RESET}\n"
    offer_restart
    echo ""
}

# ─── MAIN MENU ────────────────────────────────────────────────────────────────
show_sysinfo

if [ -n "$REF_OVERRIDE" ]; then
    BRANCH_LABEL="${REF_OVERRIDE}"
else
    BRANCH_LABEL="${DEFAULT_BRANCH}"
fi

printf "  ${BOLD}1)${RESET} Install / Update  ${CYAN}[${BRANCH_LABEL}]${RESET}\n"
printf "  ${BOLD}2)${RESET} Install / Update  ${DIM}[main — stable]${RESET}\n"
printf "  ${BOLD}3)${RESET} Uninstall\n"
printf "  ${BOLD}4)${RESET} Exit\n"
echo ""

choice=""
ask "  Choose an option [1-4]: " choice ""
echo ""

case "$choice" in
    1) 
        # Forzar la rama por defecto si el usuario no especificó una
        [ -z "$REF_OVERRIDE" ] && REF_OVERRIDE="$DEFAULT_BRANCH"
        do_install 
        ;;
    2) 
        REF_OVERRIDE="main"
        BRANCH_LABEL="main"
        do_install 
        ;;
    3) do_uninstall ;;
    4) bye; exit 0 ;;
    *) die "Invalid option: '$choice'" ;;
esac

} # end main

main
