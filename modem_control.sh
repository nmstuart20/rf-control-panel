#!/usr/bin/env bash
# Control the modem through its HTTP configuration pages.

set -euo pipefail


# Base URL is the static IP of the modem (for example, http://10.0.1.154).
# To change frequency and mod type and other parameters http://10.0.1.154/cT3?da=2&db=960000&dc=1000&de=1&df=0&dh=0&di=0&dk=1&tf=Apply
# db is the frequency, de is the number corresponding to some modtype (de = 1 is QPSK 1/4)
# To turn TX on and off and set power level http://10.0.1.154/cM3?da=2&dd=1&db=19&dc=0&tf=Apply
# dd=1 means TX is on, dd omitted means TX is off
# db is the whole number before the decimal
# dc is the fractional number
# TX level range (-dBm) 1.0-46.0
# All commands are GET as far as I can tell

MODEM_BASE_URL="${MODEM_BASE_URL:-http://10.0.1.154}"
MODEM_CURL_TIMEOUT="${MODEM_CURL_TIMEOUT:-10}"

modem_request() {
    local endpoint="${1:?usage: modem_request ENDPOINT [KEY=VALUE ...]}"
    shift

    curl --fail --show-error --silent \
        --connect-timeout "$MODEM_CURL_TIMEOUT" \
        --max-time "$MODEM_CURL_TIMEOUT" \
        --get \
        "${MODEM_BASE_URL%/}${endpoint}" \
        "$@"
    printf '\n'
}

modem_status() {
    # The comments document no status API; fetch the modem's web UI to verify
    # that it is reachable.
    modem_request "/"
}

modem_set_profile() {
    local frequency="${1:?usage: modem_set_profile FREQUENCY MODULATION_TYPE}"
    local modulation_type="${2:?usage: modem_set_profile FREQUENCY MODULATION_TYPE}"

    if [[ ! "$frequency" =~ ^[0-9]+$ ]] || (( 10#$frequency == 0 )); then
        printf 'Frequency must be a positive whole number.\n' >&2
        return 2
    fi
    if [[ ! "$modulation_type" =~ ^[0-9]+$ ]]; then
        printf 'Modulation type must be a non-negative whole number.\n' >&2
        return 2
    fi

    modem_request "/cT3" \
        --data-urlencode "da=2" \
        --data-urlencode "db=$frequency" \
        --data-urlencode "dc=1000" \
        --data-urlencode "de=$modulation_type" \
        --data-urlencode "df=0" \
        --data-urlencode "dh=0" \
        --data-urlencode "di=0" \
        --data-urlencode "dk=1" \
        --data-urlencode "tf=Apply"
}

split_tx_level() {
    local level="${1:?usage: split_tx_level LEVEL}"

    if [[ ! "$level" =~ ^([0-9]+)(\.([0-9]))?$ ]]; then
        printf 'TX level must have at most one decimal place.\n' >&2
        return 2
    fi

    TX_LEVEL_WHOLE=$((10#${BASH_REMATCH[1]}))
    TX_LEVEL_FRACTION="${BASH_REMATCH[3]:-0}"
    if (( TX_LEVEL_WHOLE < 1 || TX_LEVEL_WHOLE > 46 ||
          (TX_LEVEL_WHOLE == 46 && TX_LEVEL_FRACTION != 0) )); then
        printf 'TX level must be between 1.0 and 46.0 -dBm.\n' >&2
        return 2
    fi
}

modem_enable_transmit() {
    local level="${1:-19.0}"
    split_tx_level "$level"

    modem_request "/cM3" \
        --data-urlencode "da=2" \
        --data-urlencode "dd=1" \
        --data-urlencode "db=$TX_LEVEL_WHOLE" \
        --data-urlencode "dc=$TX_LEVEL_FRACTION" \
        --data-urlencode "tf=Apply"
}

modem_disable_transmit() {
    local level="${1:-19.0}"
    split_tx_level "$level"

    modem_request "/cM3" \
        --data-urlencode "da=2" \
        --data-urlencode "db=$TX_LEVEL_WHOLE" \
        --data-urlencode "dc=$TX_LEVEL_FRACTION" \
        --data-urlencode "tf=Apply"
}

usage() {
    printf 'Usage: %s {status|set-profile FREQUENCY MODULATION_TYPE|enable-transmit [LEVEL]|disable-transmit [LEVEL]}\n' "$0" >&2
}

main() {
    local command="${1:-}"
    shift || true

    case "$command" in
        status) modem_status "$@" ;;
        set-profile) modem_set_profile "$@" ;;
        enable-transmit) modem_enable_transmit "$@" ;;
        disable-transmit) modem_disable_transmit "$@" ;;
        *) usage; return 2 ;;
    esac
}

# Do not dispatch when this file is sourced by another shell script.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
