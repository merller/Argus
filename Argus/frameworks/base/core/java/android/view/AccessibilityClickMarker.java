package android.view;

import android.annotation.Nullable;

/**
 * @hide
 */
public final class AccessibilityClickMarker {

    private AccessibilityClickMarker() {}

    static final int SOURCE_USER = 0;
    static final int SOURCE_ACCESSIBILITY_ACTION = 1;
    static final int SOURCE_INJECTED_EVENT = 2;
    static final int SOURCE_ACCESSIBILITY_GESTURE = 3;

    static final int TAG_TAMPERED = -1;
    static final int TAG_USER = SOURCE_USER;
    static final int TAG_AGENT_ACCESSIBILITY_ACTION = SOURCE_ACCESSIBILITY_ACTION;
    static final int TAG_AGENT_INJECTED_EVENT = SOURCE_INJECTED_EVENT;
    static final int TAG_AGENT_ACCESSIBILITY_GESTURE = SOURCE_ACCESSIBILITY_GESTURE;

    private static final ThreadLocal<ArgusAgentSubject> sAgentSubject =
            new ThreadLocal<>();
    private static final ThreadLocal<Boolean> sSubjectVerified =
            ThreadLocal.withInitial(() -> Boolean.FALSE);

    public static void setAccessibilityTrigger(boolean isAgent, byte[] domain) {
        if (isAgent) {
            writeSubjectTag(SOURCE_ACCESSIBILITY_ACTION, domain);
        }
    }

    public static void setInjectedEventTrigger(byte[] domain) {
        writeSubjectTag(SOURCE_INJECTED_EVENT, domain);
    }

    static void writeUserEventTag() {
        writeSubjectTag(SOURCE_USER, null);
    }

    static void writeInjectedEventTag() {
        writeSubjectTag(SOURCE_INJECTED_EVENT, null);
    }

    static void writeAccessibilityGestureTag() {
        writeSubjectTag(SOURCE_ACCESSIBILITY_GESTURE, null);
    }

    static void writeAccessibilityActionTag(byte[] domain) {
        writeSubjectTag(SOURCE_ACCESSIBILITY_ACTION, domain);
    }

    static void writeApprovedAgentTag(int source) {
        sAgentSubject.set(new ArgusAgentSubject(source, null, true));
        sSubjectVerified.set(Boolean.TRUE);
    }

    private static void writeSubjectTag(int source, byte[] domain) {
        sAgentSubject.set(new ArgusAgentSubject(source, domain));
        sSubjectVerified.set(Boolean.FALSE);
        nativeWriteSubjectTag(source);
    }

    static int verifySubjectTag() {
        final ArgusAgentSubject cached = sAgentSubject.get();
        if (cached != null && Boolean.TRUE.equals(sSubjectVerified.get())) {
            return cached.source;
        }
        final int verifiedSource = nativeVerifySubjectTag();
        if (verifiedSource == TAG_TAMPERED) {
            sAgentSubject.remove();
            sSubjectVerified.set(Boolean.FALSE);
            return TAG_TAMPERED;
        }
        final ArgusAgentSubject current = sAgentSubject.get();
        if (current == null || current.source != verifiedSource) {
            sAgentSubject.set(new ArgusAgentSubject(verifiedSource, null));
        }
        sSubjectVerified.set(Boolean.TRUE);
        return verifiedSource;
    }

    public static void clearMarker() {
        sAgentSubject.remove();
        sSubjectVerified.remove();
        nativeClearSubjectTag();
    }

    @Nullable
    static ArgusAgentSubject getAgentSubjectSnapshot() {
        final int verifiedSource = verifySubjectTag();
        if (verifiedSource == TAG_TAMPERED) {
            return new ArgusAgentSubject(TAG_TAMPERED, null);
        }
        return sAgentSubject.get();
    }

    static boolean isAgentSource(int source) {
        return source == SOURCE_ACCESSIBILITY_ACTION
                || source == SOURCE_INJECTED_EVENT
                || source == SOURCE_ACCESSIBILITY_GESTURE;
    }

    private static native void nativeWriteSubjectTag(int source);
    private static native int nativeVerifySubjectTag();
    private static native void nativeClearSubjectTag();

    static native String nativeGetDciCodebase(View.OnClickListener listener);
    static native int nativeGetDciIndex(View.OnClickListener listener);

    // Added for XML android:onClick. View.DeclaredOnClickListener resolves the real
    // application Method and calls these overloads so DCI does not identify the
    // framework wrapper's onClick(View).
    static native String nativeGetDciCodebase(java.lang.reflect.Method method);
    static native int nativeGetDciIndex(java.lang.reflect.Method method);
}
