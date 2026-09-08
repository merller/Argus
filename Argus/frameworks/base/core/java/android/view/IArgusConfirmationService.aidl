/* Copyright (C) 2026 The Android Open Source Project */
package android.view;

import android.view.IArgusConfirmationCallback;

/** @hide */
interface IArgusConfirmationService {
    void requestConfirmation(long restrictedToken, String packageName,
            String operation, IArgusConfirmationCallback callback);
}
