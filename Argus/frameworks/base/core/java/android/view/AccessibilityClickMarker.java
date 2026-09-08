//frameworks/base/core/java/android/view/AccessibilityClickMarker.java
package android.view;

/**
 * @hide
 */
public final class AccessibilityClickMarker {

    private AccessibilityClickMarker() {}

    /**
     * @hide
     */
    public static void setAccessibilityTrigger(boolean isAgent, byte[] domain) {
        if (isAgent) {
            nativeSetAgentSubject(SOURCE_ACCESSIBILITY_ACTION, domain);
        }
    }

    /**
     * @hide
     */
    public static void setInjectedEventTrigger(byte[] domain) {
        nativeSetAgentSubject(SOURCE_INJECTED_EVENT, domain);
    }

    /**
     * @hide
     */
    public static void clearMarker() {
        nativeClearAgentSubject();
    }

    static final int SOURCE_ACCESSIBILITY_ACTION = 1;
    static final int SOURCE_INJECTED_EVENT        = 2;

    private static native void nativeSetAgentSubject(int source, byte[] domain);
    private static native void nativeClearAgentSubject();
    static native String nativeGetDciCodebase(View.OnClickListener listener);
    static native int nativeGetDciIndex(View.OnClickListener listener);


}

