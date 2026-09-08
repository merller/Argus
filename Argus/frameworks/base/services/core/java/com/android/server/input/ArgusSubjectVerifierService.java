/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
package com.android.server.input;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Binder;
import android.os.ServiceManager;
import android.view.IArgusSubjectVerifier;

import java.security.MessageDigest;
import java.util.Arrays;

/** system_server endpoint for InputDispatcher's private subject-tag state. */
public final class ArgusSubjectVerifierService extends IArgusSubjectVerifier.Stub {
    private static final char[] HEX = "0123456789abcdef".toCharArray();
    private final Context mContext;

    public static ArgusSubjectVerifierService publish(Context context) {
        final ArgusSubjectVerifierService service = new ArgusSubjectVerifierService(context);
        ServiceManager.addService("argus_subject_verifier", service, false,
                ServiceManager.DUMP_FLAG_PRIORITY_HIGH);
        return service;
    }

    private ArgusSubjectVerifierService(Context context) {
        mContext = context;
    }

    @Override
    public int verifySubjectTag(int eventId, long counter, long authenticator) {
        return nativeVerifySubjectTag(eventId, counter, authenticator,
                Binder.getCallingPid(), Binder.getCallingUid());
    }

    /** Called directly by trusted accessibility code in system_server, never over Binder. */
    long[] mintAccessibilityTagForDispatch(int eventId, int targetPid, int targetUid) {
        return nativeMintAccessibilityTag(eventId, targetPid, targetUid);
    }

    @Override
    public String getVerifiedDomain(String packageName) {
        if (packageName == null) return null;
        final int callingUid = Binder.getCallingUid();
        final PackageManager packageManager = mContext.getPackageManager();
        final String[] packages = packageManager.getPackagesForUid(callingUid);
        if (packages == null || !Arrays.asList(packages).contains(packageName)) return null;
        try {
            final PackageInfo info = packageManager.getPackageInfo(
                    packageName, PackageManager.GET_SIGNING_CERTIFICATES);
            if (info.signingInfo == null) return null;
            final Signature[] signers = info.signingInfo.getApkContentsSigners();
            if (signers == null || signers.length != 1) return null;
            return packageName + ":" + hex(MessageDigest.getInstance("SHA-256")
                    .digest(signers[0].toByteArray()));
        } catch (PackageManager.NameNotFoundException | java.security.NoSuchAlgorithmException e) {
            return null;
        }
    }

    private static String hex(byte[] value) {
        final char[] result = new char[value.length * 2];
        for (int i = 0; i < value.length; ++i) {
            final int current = value[i] & 0xff;
            result[i * 2] = HEX[current >>> 4];
            result[i * 2 + 1] = HEX[current & 0xf];
        }
        return new String(result);
    }

    private static native int nativeVerifySubjectTag(int eventId, long counter,
            long authenticator, int callingPid, int callingUid);
    private static native long[] nativeMintAccessibilityTag(int eventId, int targetPid,
            int targetUid);
}
