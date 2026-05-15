/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusInputEventGate.java.
 */

package android.view;

import android.annotation.Nullable;
import android.content.Context;
import android.util.Log;

/**
 * Enforcement gate for adb injection and dispatchGesture input events.
 *
 * @hide
 */
final class ArgusInputEventGate {
    private static final String TAG = "ARGUS_INPUT_GATE";
    private static final ArgusInputEventGate sInstance = new ArgusInputEventGate();

    private PendingInputEvent mPending;

    static ArgusInputEventGate getInstance() {
        return sInstance;
    }

    private ArgusInputEventGate() {}

    boolean shouldDropWindowInputEvent(ViewRootImpl viewRoot, InputEvent event,
            InputEventReceiver receiver) {
        final ArgusAgentSubject subject = AccessibilityClickMarker.getAgentSubjectSnapshot();
        if (subject == null) {
            return false;
        }
        if (subject.isTampered()) {
            Log.w(TAG, "Dropping input event with tampered ARGUS TLS tag");
            return true;
        }
        if (!subject.isAgentInitiated()) {
            return false;
        }

        final View target = viewRoot.findArgusTargetForInputEvent(event);
        final ArgusClickDci dci = viewRoot.buildArgusClickDciForTarget(target);
        if (dci != null && dci.isBlockedByOverride()) {
            Log.w(TAG, "Dropping input event because target class bypasses framework hooks");
            return true;
        }

        final int verdict = dci != null
                ? ArgusRestrictedPolicy.getVerdict(dci, subject)
                : ArgusRestrictedPolicy.VERDICT_ALLOW;
        if (verdict == ArgusRestrictedPolicy.VERDICT_ALLOW) {
            return false;
        }
        if (verdict == ArgusRestrictedPolicy.VERDICT_BLOCK) {
            Log.i(TAG, "Dropping blocked agent input event");
            return true;
        }

        captureRestrictedInputEvent(target, dci, subject);
        return true;
    }

    private synchronized void captureRestrictedInputEvent(@Nullable View target,
            @Nullable ArgusClickDci dci, ArgusAgentSubject subject) {
        if (mPending != null) {
            Log.i(TAG, "Dropping agent input event while restricted input is pending");
            return;
        }
        if (target == null) {
            return;
        }
        mPending = new PendingInputEvent(target, subject.source);
        final Context context = target.getContext();
        if (context == null) {
            mPending = null;
            return;
        }
        ArgusConfirmationDialog.show(context, dci, approved -> finishPending(approved));
        Log.i(TAG, "Restricted input event captured and awaiting user approval");
    }

    private synchronized void finishPending(boolean approved) {
        if (mPending == null) {
            return;
        }
        final PendingInputEvent pending = mPending;
        mPending = null;
        if (!approved) {
            Log.i(TAG, "Restricted input event aborted");
            return;
        }
        final View target = pending.target;
        if (target == null) {
            return;
        }
        target.post(() -> {
            AccessibilityClickMarker.writeApprovedAgentTag(pending.agentSource);
            target.performClick();
        });
        Log.i(TAG, "Restricted input click resumed after user approval");
    }

    private static final class PendingInputEvent {
        final View target;
        final int agentSource;

        PendingInputEvent(View target, int agentSource) {
            this.target = target;
            this.agentSource = agentSource;
        }
    }
}
