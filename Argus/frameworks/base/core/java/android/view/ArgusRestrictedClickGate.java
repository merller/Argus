package android.view;

import android.annotation.Nullable;
import android.app.AlertDialog;
import android.content.Context;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.SystemProperties;
import android.util.Log;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Process-wide capture-freeze-resume state machine for Restricted click operations.
 *
 * @hide
 */
final class ArgusRestrictedClickGate {
    enum GateResult {
        DISPATCH_NOW,
        CONSUMED_RESTRICTED,
        CONSUMED_FROZEN,
        CONSUMED_DENIED
    }

    private static final String TAG = "ArgusRestricted";
    private static final long DEFAULT_TIMEOUT_MS = 30_000L;
    private static final long MIN_DEBUG_TIMEOUT_MS = 250L;
    private static final String TIMEOUT_PROPERTY = "debug.argus.restricted_timeout_ms";
    private static final String SUBJECT_ATTACK_PROPERTY = "debug.argus.subject_attack";
    private static final int SUBJECT_PURPOSE_CLICK = 1;
    private static final ArgusRestrictedClickGate sInstance =
            new ArgusRestrictedClickGate();
    private static final AtomicLong sNextOperationId = new AtomicLong(1L);
    @Nullable private static SubjectMaterial sReplayMaterial;

    private final Handler mUiHandler = new Handler(Looper.getMainLooper());

    @Nullable
    private PendingOperation mPending;

    static ArgusRestrictedClickGate getInstance() {
        return sInstance;
    }

    private ArgusRestrictedClickGate() {
    }

    GateResult beforeClick(View target, Object callback, String methodName, String descriptor,
            String operation, int inputEventId, int inputSource, int subjectEventId,
            long subjectCounter, long subjectMac, String arguments,
            ArgusCapturedOperation continuation) {
        if (isSubjectVerificationEnabled(target)) {
            final SubjectMaterial material = applyDebugAttack(new SubjectMaterial(
                    inputSource, subjectEventId, subjectCounter, subjectMac));
            final int verifiedSource;
            try {
                verifiedSource = InputEventReceiver.verifyArgusSubject(
                        material.source, material.eventId, material.counter, material.mac,
                        SUBJECT_PURPOSE_CLICK);
            } catch (RuntimeException error) {
                Log.w(TAG, "phase=subject_verify result=service_error eventId=" + inputEventId,
                        error);
                InputEventReceiver.clearArgusSubjectContext();
                return GateResult.CONSUMED_DENIED;
            }
            if (verifiedSource != material.source) {
                Log.i(TAG, "phase=subject_verify result=tampered eventId=" + inputEventId
                        + " subjectEventId=" + material.eventId
                        + " counter=" + Long.toUnsignedString(material.counter));
                InputEventReceiver.clearArgusSubjectContext();
                return GateResult.CONSUMED_DENIED;
            }
            inputSource = verifiedSource;
            Log.i(TAG, "phase=subject_verify result=valid eventId=" + inputEventId
                    + " subjectEventId=" + material.eventId
                    + " source=" + inputSource
                    + " counter=" + Long.toUnsignedString(material.counter));
        }
        // sigma=false is an unconditional allow, including while an agent continuation is pending.
        if (!ArgusDci.isAgentInputSource(inputSource)) {
            return GateResult.DISPATCH_NOW;
        }

        synchronized (this) {
            if (mPending != null) {
                mPending.droppedAgentOperations++;
                Log.i(TAG, "phase=freeze operationId=" + mPending.operationId
                        + " droppedEventId=" + inputEventId
                        + " droppedOperation=" + operation
                        + " droppedCount=" + mPending.droppedAgentOperations);
                return GateResult.CONSUMED_FROZEN;
            }
        }

        final int verdict = ArgusDci.resolvePolicy(target, callback, methodName, descriptor,
                operation, "pre_effect", inputEventId, inputSource, arguments);
        if (verdict == ArgusDci.VERDICT_ALLOWED) {
            return GateResult.DISPATCH_NOW;
        }
        if (verdict != ArgusDci.VERDICT_RESTRICTED) {
            Log.i(TAG, "phase=deny eventId=" + inputEventId + " operation=" + operation);
            return GateResult.CONSUMED_DENIED;
        }

        final PendingOperation pending = new PendingOperation(
                sNextOperationId.getAndIncrement(), continuation, timeoutMillis());
        synchronized (this) {
            // The second check makes the gate safe if a future operation reaches it off-main-thread.
            if (mPending != null) {
                mPending.droppedAgentOperations++;
                return GateResult.CONSUMED_FROZEN;
            }
            mPending = pending;
        }

        mUiHandler.post(() -> showConfirmation(pending));
        mUiHandler.postDelayed(pending.timeoutRunnable, pending.timeoutMs);
        Log.i(TAG, "phase=capture operationId=" + pending.operationId
                + " eventId=" + inputEventId
                + " operation=" + operation
                + " timeoutMs=" + pending.timeoutMs);
        return GateResult.CONSUMED_RESTRICTED;
    }

    private void showConfirmation(PendingOperation pending) {
        synchronized (this) {
            if (mPending != pending || pending.state != PendingOperation.STATE_WAITING) {
                return;
            }
        }
        final ArgusCapturedOperation continuation = pending.continuation;
        final View target = continuation.getTarget();
        if (target == null || !continuation.isStillValid()) {
            abort(pending, "target_invalid_before_dialog");
            return;
        }
        final Context context = target.getContext();
        if (context == null) {
            abort(pending, "missing_context");
            return;
        }
        try {
            final AlertDialog dialog = ArgusConfirmationDialog.show(
                    context, context.getPackageName(), continuation.getOperation(),
                    targetName(target), approved -> decide(pending, approved,
                            approved ? "user_allow" : "user_deny"));
            synchronized (this) {
                if (mPending == pending && pending.state == PendingOperation.STATE_WAITING) {
                    pending.dialog = dialog;
                } else {
                    dialog.dismiss();
                }
            }
            Log.i(TAG, "phase=prompt operationId=" + pending.operationId);
        } catch (RuntimeException error) {
            Log.w(TAG, "Unable to present Restricted confirmation", error);
            abort(pending, "dialog_failure");
        }
    }

    private void decide(PendingOperation pending, boolean approved, String reason) {
        if (!approved) {
            abort(pending, reason);
            return;
        }
        synchronized (this) {
            if (mPending != pending || pending.state != PendingOperation.STATE_WAITING) {
                return;
            }
            pending.state = PendingOperation.STATE_APPROVED;
            mUiHandler.removeCallbacks(pending.timeoutRunnable);
        }
        Log.i(TAG, "phase=decision operationId=" + pending.operationId
                + " decision=approve latencyMs="
                + (SystemClock.uptimeMillis() - pending.createdUptimeMs));
        // Resume only after the dialog button callback has returned and normal UI scheduling wins.
        mUiHandler.post(() -> resume(pending));
    }

    private void resume(PendingOperation pending) {
        final ArgusCapturedOperation continuation;
        synchronized (this) {
            if (mPending != pending || pending.state != PendingOperation.STATE_APPROVED) {
                return;
            }
            continuation = pending.continuation;
            if (!continuation.isStillValid()) {
                clearPendingLocked(pending);
                Log.i(TAG, "phase=abort operationId=" + pending.operationId
                        + " reason=continuation_invalid droppedCount="
                        + pending.droppedAgentOperations);
                return;
            }
            // The permit is one-shot. Clearing immediately before run on the same UI turn leaves
            // no scheduling point at which a different agent operation can consume the approval.
            pending.state = PendingOperation.STATE_RESUMED;
            clearPendingLocked(pending);
        }
        continuation.run();
        Log.i(TAG, "phase=resume operationId=" + pending.operationId
                + " eventId=" + continuation.getInputEventId()
                + " droppedCount=" + pending.droppedAgentOperations);
    }

    private void abort(PendingOperation pending, String reason) {
        final AlertDialog dialog;
        synchronized (this) {
            if (mPending != pending || pending.state == PendingOperation.STATE_ABORTED
                    || pending.state == PendingOperation.STATE_RESUMED) {
                return;
            }
            pending.state = PendingOperation.STATE_ABORTED;
            mUiHandler.removeCallbacks(pending.timeoutRunnable);
            dialog = pending.dialog;
            clearPendingLocked(pending);
        }
        if (dialog != null && dialog.isShowing()) {
            dialog.dismiss();
        }
        Log.i(TAG, "phase=abort operationId=" + pending.operationId
                + " reason=" + reason
                + " droppedCount=" + pending.droppedAgentOperations
                + " latencyMs=" + (SystemClock.uptimeMillis() - pending.createdUptimeMs));
    }

    private void clearPendingLocked(PendingOperation pending) {
        if (mPending == pending) {
            mPending = null;
        }
        pending.dialog = null;
    }

    private static String targetName(View target) {
        final int id = target.getId();
        if (id == View.NO_ID) {
            return target.getClass().getName() + " (no resource id)";
        }
        try {
            return target.getResources().getResourceName(id);
        } catch (RuntimeException ignored) {
            return target.getClass().getName() + " (id=" + id + ')';
        }
    }

    private static long timeoutMillis() {
        if (!Build.IS_DEBUGGABLE) {
            return DEFAULT_TIMEOUT_MS;
        }
        final long configured = SystemProperties.getLong(TIMEOUT_PROPERTY, DEFAULT_TIMEOUT_MS);
        return Math.max(MIN_DEBUG_TIMEOUT_MS, Math.min(DEFAULT_TIMEOUT_MS, configured));
    }

    private static boolean isSubjectVerificationEnabled(View target) {
        return InputEventReceiver.isArgusSubjectVerificationEnabled(target);
    }

    private static synchronized SubjectMaterial applyDebugAttack(SubjectMaterial material) {
        if (!Build.IS_DEBUGGABLE) {
            return material;
        }
        final String attack = SystemProperties.get(SUBJECT_ATTACK_PROPERTY, "");
        switch (attack) {
            case "capture":
                sReplayMaterial = material;
                Log.i(TAG, "phase=subject_attack type=capture counter="
                        + Long.toUnsignedString(material.counter));
                return material;
            case "replay":
                if (sReplayMaterial != null) {
                    Log.i(TAG, "phase=subject_attack type=replay counter="
                            + Long.toUnsignedString(sReplayMaterial.counter));
                    return sReplayMaterial;
                }
                return material;
            case "flip_mac":
                Log.i(TAG, "phase=subject_attack type=flip_mac");
                return new SubjectMaterial(material.source, material.eventId, material.counter,
                        material.mac ^ 1L);
            case "force_physical":
                Log.i(TAG, "phase=subject_attack type=force_physical");
                return new SubjectMaterial(InputEventReceiver.INPUT_EVENT_SOURCE_PHYSICAL,
                        material.eventId, material.counter, material.mac);
            case "zero":
                Log.i(TAG, "phase=subject_attack type=zero");
                return new SubjectMaterial(material.source, material.eventId, 0, 0);
            default:
                return material;
        }
    }

    private static final class SubjectMaterial {
        final int source;
        final int eventId;
        final long counter;
        final long mac;

        SubjectMaterial(int source, int eventId, long counter, long mac) {
            this.source = source;
            this.eventId = eventId;
            this.counter = counter;
            this.mac = mac;
        }
    }

    private final class PendingOperation {
        static final int STATE_WAITING = 0;
        static final int STATE_APPROVED = 1;
        static final int STATE_ABORTED = 2;
        static final int STATE_RESUMED = 3;

        final long operationId;
        final ArgusCapturedOperation continuation;
        final long timeoutMs;
        final long createdUptimeMs = SystemClock.uptimeMillis();
        final Runnable timeoutRunnable;

        int state = STATE_WAITING;
        int droppedAgentOperations;
        @Nullable AlertDialog dialog;

        PendingOperation(long operationId, ArgusCapturedOperation continuation, long timeoutMs) {
            this.operationId = operationId;
            this.continuation = continuation;
            this.timeoutMs = timeoutMs;
            this.timeoutRunnable = () -> abort(this, "timeout");
        }
    }
}
