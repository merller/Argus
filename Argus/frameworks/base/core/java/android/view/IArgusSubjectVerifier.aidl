/* Copyright (C) 2026 The Android Open Source Project */
package android.view;

/** @hide */
interface IArgusSubjectVerifier {
    int verifySubjectTag(int eventId, long counter, long authenticator);

    /* Returns packageName:signingCertSHA256 after binding packageName to caller UID. */
    String getVerifiedDomain(String packageName);
}
