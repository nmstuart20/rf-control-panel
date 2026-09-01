#!/usr/bin/env bash
# Placeholder modem controls for use by scenarios/scenarios.json.

set -euo pipefail

MODEM_BASE_URL="${MODEM_BASE_URL:-http://192.0.2.10/api}"
MODEM_CURL_TIMEOUT="${MODEM_CURL_TIMEOUT:-10}"

modem_request() {
    local method="$1"
    local endpoint="$2"
    shift 2

    curl --fail --show-error --silent \
        --connect-timeout "$MODEM_CURL_TIMEOUT" \
        --max-time "$MODEM_CURL_TIMEOUT" \
        --request "$method" \
        "${MODEM_BASE_URL}${endpoint}" \
        "$@"
    printf '\n'
}

modem_status() {
    modem_request GET "/status"
}

modem_set_profile() {
    local profile="${1:?usage: modem_set_profile PROFILE}"
    if [[ ! "$profile" =~ ^[A-Za-z0-9._-]+$ ]]; then
        printf 'Profile names may contain only letters, numbers, dots, underscores, and hyphens.\n' >&2
        return 2
    fi
    modem_request PUT "/configuration/profile" \
        --header "Content-Type: application/json" \
        --data "{\"profile\":\"${profile}\"}"
}

modem_enable_transmit() {
    modem_request POST "/transmitter/enable"
}

modem_disable_transmit() {
    modem_request POST "/transmitter/disable"
}

modem_reboot() {
    modem_request POST "/system/reboot"
}

usage() {
    printf 'Usage: %s {status|set-profile PROFILE|enable-transmit|disable-transmit|reboot}\n' "$0" >&2
}

main() {
    local command="${1:-}"
    shift || true

    case "$command" in
        status) modem_status "$@" ;;
        set-profile) modem_set_profile "$@" ;;
        enable-transmit) modem_enable_transmit "$@" ;;
        disable-transmit) modem_disable_transmit "$@" ;;
        reboot) modem_reboot "$@" ;;
        *) usage; return 2 ;;
    esac
}

# Do not dispatch when this file is sourced by another shell script.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
