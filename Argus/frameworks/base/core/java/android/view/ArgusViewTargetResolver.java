/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusViewTargetResolver.java.
 */

package android.view;

import android.annotation.Nullable;

/**
 * Resolves the clickable view that will receive a pointer event.
 *
 * @hide
 */
final class ArgusViewTargetResolver {
    private ArgusViewTargetResolver() {}

    @Nullable
    static View findClickableTarget(View root, float x, float y) {
        if (root == null || root.getVisibility() != View.VISIBLE) {
            return null;
        }
        if (root instanceof ViewGroup) {
            final ViewGroup group = (ViewGroup) root;
            final int count = group.getChildCount();
            for (int i = count - 1; i >= 0; i--) {
                final View child = group.getChildAt(i);
                if (!isPointInsideChild(group, child, x, y)) {
                    continue;
                }
                final View target = findClickableTarget(
                        child, x - child.getLeft() + child.getScrollX(),
                        y - child.getTop() + child.getScrollY());
                if (target != null) {
                    return target;
                }
            }
        }
        if (root.hasOnClickListeners() && root.pointInView(x, y)) {
            return root;
        }
        return null;
    }

    private static boolean isPointInsideChild(ViewGroup parent, View child, float x, float y) {
        if (child.getVisibility() != View.VISIBLE) {
            return false;
        }
        final float localX = x - child.getLeft() + parent.getScrollX();
        final float localY = y - child.getTop() + parent.getScrollY();
        return child.pointInView(localX, localY);
    }
}
