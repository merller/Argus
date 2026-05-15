/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusClickContinuation.java.
 */

package android.view;

import java.lang.ref.WeakReference;

/**
 * Captures the target callback and UI-thread execution context without running it.
 *
 * @hide
 */
abstract class ArgusClickContinuation implements Runnable {
    private final WeakReference<View> mView;
    private final View.OnClickListener mListener;

    ArgusClickContinuation(View view, View.OnClickListener listener) {
        mView = new WeakReference<>(view);
        mListener = listener;
    }

    @Override
    public final void run() {
        if (mView.get() != null) {
            dispatch();
        }
    }

    View getViewOrNull() {
        return mView.get();
    }

    View.OnClickListener getListener() {
        return mListener;
    }

    public abstract void dispatch();
}
