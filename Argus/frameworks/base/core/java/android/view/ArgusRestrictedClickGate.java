/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusRestrictedClickGate.java.
 */

package android.view;

import android.annotation.Nullable;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.util.ArrayDeque;

/**
 * Three-phase gate for Restricted verdicts.
 *
 * @hide
 */
final class ArgusRestrictedClickGate {
    enum GateResult {
        DISPATCH_NOW,
        HELD_FOR_USER,
        DROPPED_WHILE_FROZEN,
        BLOCKED
    }

    private static final String TAG = "ARGUS_RESTRICTED";
    private static final long CONFIRMATION_TIMEOUT_MS = 30_000L;
    private static final int MAX_DROPPED_AGENT_EVENTS = 128;
    private static final ArgusRestrictedClickGate sInstance = new ArgusRestrictedClickGate();

    private final Handler mUiHandler = new Handler(Looper.getMainLooper());
    private final ArrayDeque<ArgusClickDci> mDroppedAgentEvents = new ArrayDeque<>();

    private PendingRestrictedClick mPending;

    static ArgusRestrictedClickGate getInstance() {
        return sInstance;
    }

    private ArgusRestrictedClickGate() {}

    synchronized GateResult beforePerformClick(View view, @Nullable ArgusClickDci dci,
            @Nullable ArgusAgentSubject subject, ArgusClickContinuation continuation) {
        final boolean isAgent = subject != null && subject.isAgentInitiated();
        if (subject != null && subject.isTampered()) {
            return GateResult.BLOCKED;
        }
        if (subject != null && subject.isApproved()) {
            return GateResult.DISPATCH_NOW;
        }
        if (!isAgent) {
            return GateResult.DISPATCH_NOW;
        }

        if (mPending != null) {
            rememberDroppedEvent(dci);
            return GateResult.DROPPED_WHILE_FROZEN;
        }

        final int verdict = getVerdict(dci, subject);
        if (verdict == ArgusRestrictedPolicy.VERDICT_BLOCK) {
            return GateResult.BLOCKED;
        }
        if (verdict != ArgusRestrictedPolicy.VERDICT_RESTRICTED) {
            return GateResult.DISPATCH_NOW;
        }

        mPending = new PendingRestrictedClick(dci, continuation);
        final Context context = view.getContext();
        mUiHandler.post(() -> ArgusConfirmationDialog.show(
                context,
                dci,
                approved -> finishPending(continuation, approved, "user")));
        mUiHandler.postDelayed(
                () -> finishPending(continuation, false, "timeout"),
                CONFIRMATION_TIMEOUT_MS);
        Log.i(TAG, "Restricted callback captured and awaiting user approval");
        return GateResult.HELD_FOR_USER;
    }

    private synchronized void finishPending(ArgusClickContinuation continuation, boolean approved,
            String reason) {
        if (mPending == null || mPending.continuation != continuation) {
            return;
        }
        final PendingRestrictedClick pending = mPending;
        mPending = null;
        mDroppedAgentEvents.clear();

        if (!approved) {
            Log.i(TAG, "Restricted callback aborted: " + reason);
            return;
        }
        final View view = pending.continuation.getViewOrNull();
        if (view == null) {
            Log.i(TAG, "Restricted callback discarded because the view is gone");
            return;
        }
        view.post(pending.continuation);
        Log.i(TAG, "Restricted callback resumed after user approval");
    }

    GateResult beforeInputDispatch(@Nullable ArgusClickDci dci,
            @Nullable ArgusAgentSubject subject) {
        if (subject != null && subject.isTampered()) {
            return GateResult.BLOCKED;
        }
        final boolean isAgent = subject != null && subject.isAgentInitiated();
        if (!isAgent) {
            return GateResult.DISPATCH_NOW;
        }
        if (mPending != null) {
            rememberDroppedEvent(dci);
            return GateResult.DROPPED_WHILE_FROZEN;
        }
        final int verdict = getVerdict(dci, subject);
        if (verdict == ArgusRestrictedPolicy.VERDICT_BLOCK) {
            return GateResult.BLOCKED;
        }
        if (verdict == ArgusRestrictedPolicy.VERDICT_RESTRICTED) {
            return GateResult.HELD_FOR_USER;
        }
        return GateResult.DISPATCH_NOW;
    }

    private int getVerdict(@Nullable ArgusClickDci dci, ArgusAgentSubject subject) {
        if (dci == null) {
            return ArgusRestrictedPolicy.VERDICT_ALLOW;
        }
        return ArgusRestrictedPolicy.getVerdict(dci, subject);
    }

    private void rememberDroppedEvent(@Nullable ArgusClickDci dci) {
        if (dci == null) {
            return;
        }
        if (mDroppedAgentEvents.size() == MAX_DROPPED_AGENT_EVENTS) {
            mDroppedAgentEvents.removeFirst();
        }
        mDroppedAgentEvents.addLast(dci);
    }

    private static final class PendingRestrictedClick {
        final ArgusClickDci dci;
        final ArgusClickContinuation continuation;

        PendingRestrictedClick(@Nullable ArgusClickDci dci, ArgusClickContinuation continuation) {
            this.dci = dci;
            this.continuation = continuation;
        }
    }
}

final class ArgusRestrictedPolicy {
    private ArgusRestrictedPolicy() {}

    static boolean isRestricted(ArgusClickDci dci, ArgusAgentSubject subject) {
        return getVerdict(dci, subject) == VERDICT_RESTRICTED;
    }

    static int getVerdict(ArgusClickDci dci, ArgusAgentSubject subject) {
        return nativeGetVerdict(dci.domain, dci.codebase, dci.index, subject.source);
    }

    static final int VERDICT_ALLOW = 0;
    static final int VERDICT_BLOCK = 1;
    static final int VERDICT_RESTRICTED = 2;

    private static native int nativeGetVerdict(String domain, String codebase, int index,
            int agentSource);
}
