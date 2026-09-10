#!/usr/bin/env bash
# Control the modem through its HTTP configuration pages.

set -euo pipefail


# The first argument is the modem's base URL (for example, http://10.0.1.154).
# To change frequency and mod type and other parameters http://10.0.1.154/cT3?da=2&db=960000&dc=1000&de=1&df=0&dh=0&di=0&dk=1&tf=Apply
# db is the frequency in kHz, de is the number corresponding to some modtype (de = 1 is QPSK 1/4)
# To turn TX on and off and set power level http://10.0.1.154/cM3?da=2&dd=1&db=19&dc=0&tf=Apply
# dd=1 means TX is on, dd omitted means TX is off
# db is the whole number before the decimal
# dc is the fractional number
# TX level range (-dBm) 1.0-46.0
# All commands are GET as far as I can tell

MODEM_BASE_URL=""
MODEM_CURL_TIMEOUT="${MODEM_CURL_TIMEOUT:-10}"
MODEM_RESPONSE=""

modem_request() {
    local endpoint="${1:?usage: modem_request ENDPOINT [KEY=VALUE ...]}"
    shift

    MODEM_RESPONSE="$(
        curl --fail --show-error --silent \
            --connect-timeout "$MODEM_CURL_TIMEOUT" \
            --max-time "$MODEM_CURL_TIMEOUT" \
            --get \
            "${MODEM_BASE_URL%/}${endpoint}" \
            "$@"
    )"
}

report_modem_result() {
    local success_message="${1:?usage: report_modem_result SUCCESS_MESSAGE}"
    local response_text

    # The modem returns a complete HTML page for both reads and updates. Reduce
    # it to visible text so that an error page is not mistaken for success.
    response_text="$(
        printf '%s' "$MODEM_RESPONSE" |
            tr '\r\n' '  ' |
            sed -E \
                -e 's/<script[^>]*>.*<\/script>//Ig' \
                -e 's/<style[^>]*>.*<\/style>//Ig' \
                -e 's/<[^>]+>/ /g' \
                -e 's/&nbsp;/ /Ig' \
                -e 's/&amp;/\&/Ig' \
                -e 's/[[:space:]]+/ /g' \
                -e 's/^ //; s/ $//'
    )"

    if [[ -z "$response_text" ]]; then
        printf 'Modem returned an empty response.\n' >&2
        return 1
    fi

    if [[ "${response_text,,}" =~ (^|[^[:alnum:]_])(error|failed|failure|invalid)([^[:alnum:]_]|$) ]]; then
        printf 'Modem command failed: %s\n' "$response_text" >&2
        return 1
    fi

    printf '%s\n' "$success_message"
}

modem_status() {
    # The comments document no status API; fetch the modem's web UI to verify
    # that it is reachable.
    modem_request "/"
    report_modem_result "status OK"
}

modem_set_profile() {
    local frequency_hz="${1:?usage: modem_set_profile FREQUENCY_HZ MODULATION_TYPE}"
    local modulation_type="${2:?usage: modem_set_profile FREQUENCY_HZ MODULATION_TYPE}"
    local frequency_khz

    if [[ ! "$frequency_hz" =~ ^[0-9]+$ ]] || (( 10#$frequency_hz == 0 )); then
        printf 'Frequency must be a positive whole number in Hz.\n' >&2
        return 2
    fi
    if (( 10#$frequency_hz % 1000 != 0 )); then
        printf 'Frequency must be divisible by 1000 Hz because the modem uses whole kHz.\n' >&2
        return 2
    fi
    frequency_khz=$((10#$frequency_hz / 1000))
    if [[ ! "$modulation_type" =~ ^[0-9]+$ ]]; then
        printf 'Modulation type must be a non-negative whole number.\n' >&2
        return 2
    fi

    modem_request "/cT3" \
        --data-urlencode "da=2" \
        --data-urlencode "db=$frequency_khz" \
        --data-urlencode "dc=1000" \
        --data-urlencode "de=$modulation_type" \
        --data-urlencode "df=0" \
        --data-urlencode "dh=0" \
        --data-urlencode "di=0" \
        --data-urlencode "dk=1" \
        --data-urlencode "tf=Apply"
    report_modem_result "profile successful"
}

split_tx_level() {
    local level="${1:?usage: split_tx_level LEVEL}"

    # The web panel expresses power as signed dBm (for example, -30), while
    # the modem expects the positive magnitude of its negative-dBm value.
    # Continue accepting the legacy positive form as well.
    if [[ ! "$level" =~ ^-?([0-9]+)(\.([0-9]))?$ ]]; then
        printf 'TX level must be a number with at most one decimal place.\n' >&2
        return 2
    fi

    TX_LEVEL_WHOLE=$((10#${BASH_REMATCH[1]}))
    TX_LEVEL_FRACTION="${BASH_REMATCH[3]:-0}"
    if (( TX_LEVEL_WHOLE < 1 || TX_LEVEL_WHOLE > 46 ||
          (TX_LEVEL_WHOLE == 46 && TX_LEVEL_FRACTION != 0) )); then
        printf 'TX level must be between -46.0 and -1.0 dBm (or a positive magnitude from 1.0 to 46.0).\n' >&2
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
    report_modem_result "transmit successful"
}

modem_disable_transmit() {
    local level="${1:-19.0}"
    split_tx_level "$level"

    modem_request "/cM3" \
        --data-urlencode "da=2" \
        --data-urlencode "db=$TX_LEVEL_WHOLE" \
        --data-urlencode "dc=$TX_LEVEL_FRACTION" \
        --data-urlencode "tf=Apply"
    report_modem_result "transmit disabled successfully"
}

usage() {
    printf 'Usage: %s MODEM_BASE_URL {status|set-profile FREQUENCY_HZ MODULATION_TYPE|enable-transmit [LEVEL]|disable-transmit [LEVEL]}\n' "$0" >&2
}

main() {
    if (( $# < 2 )); then
        usage
        return 2
    fi

    MODEM_BASE_URL="${1:-}"
    local command="${2:-}"

    if [[ ! "$MODEM_BASE_URL" =~ ^https?://[^/[:space:]]+(/.*)?$ ]]; then
        printf 'MODEM_BASE_URL must be a valid HTTP or HTTPS URL.\n' >&2
        usage
        return 2
    fi

    shift 2

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
