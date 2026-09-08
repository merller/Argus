/* Copyright (C) 2026 The Android Open Source Project */
package android.view;

/** @hide */
oneway interface IArgusConfirmationCallback {
    void onDecision(boolean approved);
}
