/*
 * Copyright (C) 2026 The Android Open Source Project
 * SPDX-License-Identifier: Apache-2.0
 */
package com.android.server.input;

import android.app.AlertDialog;
import android.content.Context;
import android.os.Binder;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.RemoteException;
import android.os.ServiceManager;
import android.view.IArgusConfirmationCallback;
import android.view.IArgusConfirmationService;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;

import com.android.internal.R;

import java.util.Arrays;
import java.util.NoSuchElementException;

/**
 * Trusted system_server UI for a kernel-armed Restricted operation.
 *
 * <p>The requesting application supplies presentation text only.  Authority
 * comes exclusively from the calling UID and the unguessable token returned
 * by the kernel policy query.  InputDispatcher drops all agent input while
 * this surface is active, so only physical human input can choose a result.
 */
public final class ArgusConfirmationService extends IArgusConfirmationService.Stub {
    private static final String SERVICE_NAME = "argus_confirmation";
    private static final long TIMEOUT_MS = 30_000L;
    private static final int MAX_DISPLAY_TEXT = 160;

    private final Context mContext;
    private final Handler mMainHandler = new Handler(Looper.getMainLooper());
    private PendingConfirmation mPending;

    public static void publish(Context context) {
        ServiceManager.addService(SERVICE_NAME, new ArgusConfirmationService(context), false,
                ServiceManager.DUMP_FLAG_PRIORITY_HIGH);
    }

    private ArgusConfirmationService(Context context) {
        mContext = context;
    }

    @Override
    public void requestConfirmation(long restrictedToken, String packageName, String operation,
            IArgusConfirmationCallback callback) {
        final int callingUid = Binder.getCallingUid();
        if (restrictedToken == 0 || callback == null || !"click".equals(operation)
                || !uidOwnsPackage(callingUid, packageName)) {
            sendDecision(callback, false);
            return;
        }
        final String safePackage = truncate(packageName);
        mMainHandler.post(() -> show(callingUid, restrictedToken, safePackage, callback));
    }

    private boolean uidOwnsPackage(int uid, String packageName) {
        if (packageName == null) return false;
        final String[] packages = mContext.getPackageManager().getPackagesForUid(uid);
        return packages != null && Arrays.asList(packages).contains(packageName);
    }

    private void show(int uid, long token, String packageName,
            IArgusConfirmationCallback callback) {
        if (mPending != null) {
            sendDecision(callback, false);
            return;
        }

        final PendingConfirmation pending =
                new PendingConfirmation(uid, token, callback, callback.asBinder());
        try {
            pending.callbackBinder.linkToDeath(pending, 0);
        } catch (RemoteException deadCaller) {
            resolveWithoutCallback(pending, false);
            return;
        }

        final AlertDialog dialog = new AlertDialog.Builder(
                mContext, R.style.Theme_DeviceDefault_Dialog_Alert)
                .setTitle("Confirm agent action")
                .setMessage("Allow one agent-initiated click in " + packageName
                        + "? Confirm using the device screen.")
                .setNegativeButton("Deny", (ignored, which) -> finish(pending, false))
                .setPositiveButton("Allow once", (ignored, which) -> finish(pending, true))
                .create();
        dialog.setCancelable(false);
        dialog.setCanceledOnTouchOutside(false);

        final Window window = dialog.getWindow();
        if (window == null) {
            unlinkDeathRecipient(pending);
            resolveWithoutCallback(pending, false);
            sendDecision(callback, false);
            return;
        }
        window.setType(WindowManager.LayoutParams.TYPE_SYSTEM_ERROR);
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        final WindowManager.LayoutParams attributes = window.getAttributes();
        attributes.privateFlags |= WindowManager.LayoutParams.PRIVATE_FLAG_TRUSTED_OVERLAY;
        window.setAttributes(attributes);
        dialog.setOnShowListener(ignored -> {
            final View decor = window.getDecorView();
            decor.setFilterTouchesWhenObscured(true);
            decor.setImportantForAccessibility(
                    View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS);
        });

        pending.dialog = dialog;
        mPending = pending;
        mMainHandler.postDelayed(pending.timeout, TIMEOUT_MS);
        try {
            dialog.show();
        } catch (RuntimeException cannotShow) {
            finish(pending, false);
        }
    }

    private void finish(PendingConfirmation pending, boolean approved) {
        if (mPending != pending) return;
        mPending = null;
        mMainHandler.removeCallbacks(pending.timeout);
        unlinkDeathRecipient(pending);
        if (pending.dialog != null && pending.dialog.isShowing()) pending.dialog.dismiss();

        // Resolve the kernel freeze before notifying the app.  A failed ioctl
        // can never turn into approval; the kernel failsafe will later unfreeze.
        final boolean resolved = nativeResolveRestricted(
                pending.token, pending.uid, approved);
        sendDecision(pending.callback, approved && resolved);
    }

    private void resolveWithoutCallback(PendingConfirmation pending, boolean approved) {
        nativeResolveRestricted(pending.token, pending.uid, approved);
    }

    private static void unlinkDeathRecipient(PendingConfirmation pending) {
        try {
            pending.callbackBinder.unlinkToDeath(pending, 0);
        } catch (NoSuchElementException ignored) {
        }
    }

    private static void sendDecision(IArgusConfirmationCallback callback, boolean approved) {
        if (callback == null) return;
        try {
            callback.onDecision(approved);
        } catch (RemoteException ignored) {
        }
    }

    private static String truncate(String value) {
        if (value == null || value.isEmpty()) return "an unresolved target";
        return value.length() <= MAX_DISPLAY_TEXT ? value : value.substring(0, MAX_DISPLAY_TEXT);
    }

    private final class PendingConfirmation implements IBinder.DeathRecipient {
        final int uid;
        final long token;
        final IArgusConfirmationCallback callback;
        final IBinder callbackBinder;
        final Runnable timeout = () -> finish(this, false);
        AlertDialog dialog;

        PendingConfirmation(int uid, long token, IArgusConfirmationCallback callback,
                IBinder callbackBinder) {
            this.uid = uid;
            this.token = token;
            this.callback = callback;
            this.callbackBinder = callbackBinder;
        }

        @Override
        public void binderDied() {
            mMainHandler.post(() -> finish(this, false));
        }
    }

    private static native boolean nativeResolveRestricted(long restrictedToken,
            int targetUid, boolean approved);
}
