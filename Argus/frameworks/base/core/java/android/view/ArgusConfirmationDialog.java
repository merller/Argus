package android.view;

import android.app.AlertDialog;
import android.content.Context;
import android.view.Window;
import android.view.WindowManager;

/**
 * Framework-owned confirmation surface for an Argus Restricted operation.
 *
 * @hide
 */
final class ArgusConfirmationDialog {
    interface Callback {
        void onDecision(boolean approved);
    }

    private ArgusConfirmationDialog() {
    }

    static AlertDialog show(Context context, String packageName, String operation,
            String targetName, Callback callback) {
        final String message = "Application: " + packageName
                + "\nAction: " + operation
                + "\nTarget: " + targetName
                + "\n\nAllow this automated action once?";
        final AlertDialog dialog = new AlertDialog.Builder(context)
                .setTitle("Confirm restricted action")
                .setMessage(message)
                .setPositiveButton("Allow once", (unused, which) -> callback.onDecision(true))
                .setNegativeButton("Deny", (unused, which) -> callback.onDecision(false))
                .setOnCancelListener(unused -> callback.onDecision(false))
                .create();
        dialog.setCanceledOnTouchOutside(false);
        dialog.setOnShowListener(unused -> {
            final Window window = dialog.getWindow();
            if (window == null) {
                return;
            }
            window.addFlags(WindowManager.LayoutParams.FLAG_SECURE);
            final View decor = window.getDecorView();
            if (decor != null) {
                decor.setImportantForAccessibility(
                        View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS);
                decor.setContentDescription(null);
            }
        });
        dialog.show();
        final Window window = dialog.getWindow();
        if (window != null) {
            window.addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        }
        return dialog;
    }
}
