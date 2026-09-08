#!/system/bin/sh
# Usage: run_restricted_case.sh allowed|denied|user_bypass|approve|reject|timeout
set -eu

CASE=${1:?missing experiment case}
PKG=org.argus.continuationtest
ACTIVITY="$PKG/.MainActivity"
DCIPLUS=bd2d9b6fdb4de2c31d81b3acd5fcd4697d44fec8385d3b9a2cc504604cc21213
TMP=/data/local/tmp/argus_restricted

setprop debug.argus.experimental_enforce 1
setprop debug.argus.experimental_package "$PKG"
setprop debug.argus.experimental_policy_dciplus "$DCIPLUS"

case "$CASE" in
    allowed|denied|user_bypass)
        if [ "$CASE" = user_bypass ]; then
            setprop debug.argus.experimental_policy_verdict denied
        else
            setprop debug.argus.experimental_policy_verdict "$CASE"
        fi
        setprop debug.argus.restricted_timeout_ms 30000
        ;;
    approve|reject)
        setprop debug.argus.experimental_policy_verdict restricted
        setprop debug.argus.restricted_timeout_ms 30000
        ;;
    timeout)
        setprop debug.argus.experimental_policy_verdict restricted
        setprop debug.argus.restricted_timeout_ms 1000
        ;;
    *)
        echo "unknown case: $CASE" >&2
        exit 2
        ;;
esac

logcat -c
am force-stop "$PKG"
am start -W -n "$ACTIVITY" >/dev/null
sleep 1

# Agent cases use an InputManager-injected MotionEvent. The user-bypass case
# reaches the same dangerous button using qwerty2: SAFE is the first focus
# target, DANGER the second.
if [ "$CASE" = user_bypass ]; then
    sh "$TMP/send_physical_keys.sh" 15 15 28
else
    input tap 540 315
fi

case "$CASE" in
    approve)
        sleep 1
        # Focus a dialog button and try to activate it through injected input.
        # The pending gate must freeze this click rather than accept a fake user decision.
        input keyevent KEYCODE_TAB KEYCODE_ENTER
        sleep 1
        # The first focus target is Deny. Move once to Allow and activate it through
        # qwerty2 (physical/user).
        sh "$TMP/send_physical_keys.sh" 15 28
        sleep 2
        ;;
    reject)
        sleep 1
        # KEY_BACK from qwerty2 cancels the dialog and maps to explicit rejection.
        sh "$TMP/send_physical_keys.sh" 158
        sleep 2
        ;;
    timeout)
        sleep 2
        ;;
    *)
        sleep 1
        ;;
esac

uiautomator dump "$TMP/${CASE}-window.xml" >/dev/null
logcat -d -s art:I ArgusDci:I ArgusRestricted:I ArgusInput:I '*:S' \
    >"$TMP/${CASE}.log"

status=$(grep -o 'text="[^"]*"' "$TMP/${CASE}-window.xml" \
    | grep -v 'text=""' | head -n 1 | cut -d'"' -f2)
echo "CASE=$CASE STATUS=$status"
cat "$TMP/${CASE}.log"

require_status() {
    expected="$1"
    if [ "$status" != "$expected" ]; then
        echo "RESULT=FAIL expected_status=$expected actual_status=$status" >&2
        exit 10
    fi
}

require_log() {
    needle="$1"
    if ! grep -Fq "$needle" "$TMP/${CASE}.log"; then
        echo "RESULT=FAIL missing_log=$needle" >&2
        exit 11
    fi
}

case "$CASE" in
    allowed)
        require_status DANGER:1
        require_log 'decision=allowed decisionReason=exact_dciplus_rule'
        ;;
    denied)
        require_status READY
        require_log 'decision=denied decisionReason=exact_dciplus_rule'
        require_log 'phase=deny'
        ;;
    user_bypass)
        require_status DANGER:1
        if grep -Fq 'Argus DCIPlus' "$TMP/${CASE}.log"; then
            echo 'RESULT=FAIL physical_user_reached_agent_policy' >&2
            exit 12
        fi
        ;;
    approve)
        require_status DANGER:1
        require_log 'phase=capture'
        require_log 'phase=freeze'
        require_log 'decision=approve'
        require_log 'phase=resume'
        require_log 'droppedCount=1'
        ;;
    reject)
        require_status READY
        require_log 'phase=capture'
        require_log 'reason=user_deny'
        ;;
    timeout)
        require_status READY
        require_log 'phase=capture'
        require_log 'reason=timeout'
        ;;
esac

echo RESULT=PASS
