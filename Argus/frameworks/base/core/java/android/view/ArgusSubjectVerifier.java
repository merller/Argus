/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
package android.view;

import android.content.Context;
import android.os.IBinder;
import android.os.RemoteException;
import android.os.ServiceManager;

/** Client for the system_server-only subject verifier. @hide */
final class ArgusSubjectVerifier {
    static final String SERVICE_NAME = "argus_subject_verifier";

    private ArgusSubjectVerifier() {}

    private static IArgusSubjectVerifier service() {
        final IBinder binder = ServiceManager.getService(SERVICE_NAME);
        if (binder == null) {
            throw new IllegalStateException("Argus subject verifier is unavailable");
        }
        return IArgusSubjectVerifier.Stub.asInterface(binder);
    }

    static int verify(ArgusAgentSubject subject) {
        try {
            return service().verifySubjectTag(
                    subject.eventId, subject.counter, subject.authenticator);
        } catch (RemoteException e) {
            return AccessibilityClickMarker.TAG_TAMPERED;
        } catch (RuntimeException e) {
            return AccessibilityClickMarker.TAG_TAMPERED;
        }
    }

    static String getVerifiedDomain(Context context) {
        if (context == null) return null;
        try {
            return service().getVerifiedDomain(context.getPackageName());
        } catch (RemoteException | RuntimeException e) {
            return null;
        }
    }
}
