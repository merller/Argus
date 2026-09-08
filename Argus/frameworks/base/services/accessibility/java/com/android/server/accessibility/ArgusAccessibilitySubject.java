/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
package com.android.server.accessibility;

import android.os.Bundle;
import android.view.AccessibilityClickMarker;

import java.util.Objects;

/** system_server-local bridge from accessibility delivery to InputDispatcher. */
public final class ArgusAccessibilitySubject {
    /** Implemented by the InputDispatcher-backed issuer in services/core. */
    public interface Issuer {
        long[] mint(int eventId, int targetPid, int targetUid);
    }

    private static volatile Issuer sIssuer;

    private ArgusAccessibilitySubject() {}

    /** Called once during SystemServer bootstrap; this is not a Binder endpoint. */
    public static void registerIssuer(Issuer issuer) {
        if (sIssuer != null) {
            throw new IllegalStateException("Argus accessibility issuer already registered");
        }
        sIssuer = Objects.requireNonNull(issuer);
    }

    /** Attaches one destination-bound tuple immediately before Binder delivery. */
    static boolean attach(Bundle arguments, int eventId, int targetPid, int targetUid) {
        final Issuer issuer = sIssuer;
        if (issuer == null || arguments == null || eventId < 0
                || targetPid <= 0 || targetUid < 0) {
            return false;
        }
        final long[] tag = issuer.mint(eventId, targetPid, targetUid);
        if (tag == null || tag.length != 3) return false;
        arguments.putInt(AccessibilityClickMarker.ARGUS_A11Y_EVENT_ID, (int) tag[0]);
        arguments.putLong(AccessibilityClickMarker.ARGUS_A11Y_COUNTER, tag[1]);
        arguments.putLong(AccessibilityClickMarker.ARGUS_A11Y_AUTHENTICATOR, tag[2]);
        return true;
    }
}
