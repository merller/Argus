/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
package com.android.server.input;

import android.content.Context;

import com.android.server.accessibility.ArgusAccessibilitySubject;

/** Starts the two Argus Binder endpoints during SystemServer bootstrap. */
public final class ArgusServices {
    private ArgusServices() {}

    public static void publish(Context systemContext) {
        final ArgusSubjectVerifierService subjectVerifier =
                ArgusSubjectVerifierService.publish(systemContext);
        ArgusAccessibilitySubject.registerIssuer(
                subjectVerifier::mintAccessibilityTagForDispatch);
        ArgusConfirmationService.publish(systemContext);
    }
}
