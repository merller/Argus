/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusConfirmationDialog.java.
 */

package android.view;

import android.annotation.Nullable;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.view.Window;
import android.view.WindowManager;

/**
 * User confirmation surface that is hidden from agent XML and screenshots.
 *
 * @hide
 */
final class ArgusConfirmationDialog {
    interface Callback {
        void onDecision(boolean approved);
    }

    private static final String TITLE = "Confirm restricted action";
    private static final String MESSAGE = "An automated agent is trying to perform a restricted action.";

    private ArgusConfirmationDialog() {}

    static void show(Context context, @Nullable ArgusClickDci dci, Callback callback) {
        final AlertDialog dialog = new AlertDialog.Builder(context)
                .setTitle(TITLE)
                .setMessage(MESSAGE)
                .setPositiveButton("Allow", (unused, which) -> callback.onDecision(true))
                .setNegativeButton("Deny", (unused, which) -> callback.onDecision(false))
                .setOnCancelListener(unused -> callback.onDecision(false))
                .create();

        final Window window = dialog.getWindow();
        if (window != null) {
            window.addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        }
        dialog.setOnShowListener(new DialogInterface.OnShowListener() {
            @Override
            public void onShow(DialogInterface unused) {
                final Window shownWindow = dialog.getWindow();
                if (shownWindow != null) {
                    shownWindow.addFlags(WindowManager.LayoutParams.FLAG_SECURE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL);
                }
                hideFromAccessibility(dialog);
            }
        });
        dialog.show();
    }

    private static void hideFromAccessibility(AlertDialog dialog) {
        final Window window = dialog.getWindow();
        if (window == null || window.getDecorView() == null) {
            return;
        }
        final View decor = window.getDecorView();
        decor.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS);
        decor.setContentDescription(null);
    }
}
