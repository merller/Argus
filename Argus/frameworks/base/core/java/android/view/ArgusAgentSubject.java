/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusAgentSubject.java.
 */

package android.view;

import android.annotation.Nullable;

import java.util.Arrays;

/**
 * Snapshot of the agent provenance marker attached to the current UI dispatch.
 *
 * @hide
 */
final class ArgusAgentSubject {
    final int source;
    @Nullable final byte[] domain;
    final boolean approved;

    ArgusAgentSubject(int source, @Nullable byte[] domain) {
        this(source, domain, false);
    }

    ArgusAgentSubject(int source, @Nullable byte[] domain, boolean approved) {
        this.source = source;
        this.domain = domain != null ? Arrays.copyOf(domain, domain.length) : null;
        this.approved = approved;
    }

    boolean isAgentInitiated() {
        return AccessibilityClickMarker.isAgentSource(source);
    }

    boolean isTampered() {
        return source == AccessibilityClickMarker.TAG_TAMPERED;
    }

    boolean isApproved() {
        return approved;
    }
}
