package android.view;

import android.annotation.Nullable;

/**
 * One-shot operation captured before an application callback is invoked.
 *
 * @hide
 */
interface ArgusCapturedOperation extends Runnable {
    @Nullable View getTarget();
    boolean isStillValid();
    String getOperation();
    int getInputEventId();
}
