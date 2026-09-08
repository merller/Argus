/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusAgentSubject.java.
 */

package android.view;

/**
 * Immutable, source-opaque snapshot of (eid,ctr,auth) for the current UI dispatch.
 *
 * @hide
 */
final class ArgusAgentSubject {
    final int eventId;
    final long counter;
    final long authenticator;

    ArgusAgentSubject(int eventId, long counter, long authenticator) {
        this.eventId = eventId;
        this.counter = counter;
        this.authenticator = authenticator;
    }

    boolean isPresent() {
        return eventId >= 0 && counter != 0 && authenticator != 0;
    }
}
